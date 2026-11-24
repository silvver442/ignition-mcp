"""Apply the reviewed single-file bridge upgrade using the configured clients."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import json
import sys
import time
from pathlib import Path

from upgrade_011 import (
    CANDIDATE, LIBRARY, SCOPED, WEBDEV, UpgradeError, _check, _delta, _handlers,
    _library, _minimal_update, _project, _unseal,
)

from ignition_mcp.bridge import BridgeClient
from ignition_mcp.client import GatewayClient
from ignition_mcp.config import load_settings

PROJECT = "mcp-bridge"
TEST_GATEWAY = "https://192.168.50.20:8043"
REPO = Path(__file__).resolve().parents[3]
BACKUP = REPO / "backups/mcp-bridge/mcp-bridge-0.1.0-1795467508565.zip.enc"
REVIEW_PATCH = REPO / "backups/mcp-bridge/mcp-bridge-0.1.1-1795467508565.patch"
BACKUP_HASH = "27a1710735e6f811b88ad9279156c403aa6848f40f990487caaa2e83d4104190"
OLD = "0.1.0"
NEW = "0.1.1"


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _read_library(bridge, path):
    result = bridge.call("files/read", {"path": path})
    _check(isinstance(result, dict) and isinstance(result.get("content"), str),
           "Signed read did not return library content")
    return result["content"].encode("utf-8")


def _runtime(bridge):
    result = bridge.ping()
    caps = result.get("capabilities")
    version = result.get("version")
    _check(isinstance(caps, list) and all(isinstance(item, str) for item in caps),
           "Signed ping did not return a capability list")
    return version if version in (OLD, NEW) else "other", set(caps)


def _locate(exported):
    candidates = [name for name in exported if name.endswith("/mcp_bridge_lib/code.py")]
    _check(candidates == [LIBRARY], "Project export did not identify the expected single library")
    return f"{PROJECT}/{candidates[0]}"


def _verify_runtime(bridge, old_routes):
    deadline = time.monotonic() + 20
    while True:
        try:
            version, caps = _runtime(bridge)
            if version == NEW and old_routes | set(SCOPED) <= caps:
                return
        except Exception:
            # During scan the WebDev project may reload. Only signed reads are retried.
            pass
        if time.monotonic() >= deadline:
            raise UpgradeError("Signed ping did not verify 0.1.1 and all required capabilities")
        time.sleep(0.5)


def _state(bridge, path, before, desired):
    state = {}
    try:
        actual = _read_library(bridge, path)
        state["library"] = "0.1.1_prepared" if actual == desired else (
            "0.1.0_backup" if actual == before else "different"
        )
        state["library_sha256"] = _sha(actual)
    except Exception:
        state["library"] = "unavailable"
    try:
        version, caps = _runtime(bridge)
        state.update(runtime_version=version, scoped_capabilities=sorted(set(SCOPED) & caps))
    except Exception:
        state["runtime_version"] = "unavailable"
    return state


def upgrade(gateway, bridge, secret, sealed, reviewed_patch, candidate, check_only=False):
    status = {"status": "error", "phase": "verify_backup", "write_attempted": False,
              "scan_attempted": False, "restore_backup": BACKUP.name}
    path = f"{PROJECT}/{LIBRARY}"
    before = desired = None
    try:
        _check(hmac.compare_digest(_sha(sealed), BACKUP_HASH), "Encrypted backup SHA-256 differs")
        backup = _project(_unseal(sealed, secret))
        before = backup[LIBRARY]
        old_routes = set(_handlers(_library(before)[2])[0])
        desired = _minimal_update(before, candidate)
        _check(_delta(before, desired) == reviewed_patch, "Generated update differs from the reviewed patch")
        status["backup_verified"] = True
        status["expected_sha256"] = _sha(desired)

        status["phase"] = "read_gateway"
        info = gateway.get_json("gateway-info")
        version = info.get("ignitionVersion", "").split()
        _check(version[:1] == ["8.3.9"], "Gateway version is not 8.3.9")
        exported = _project(gateway.get_bytes(f"projects/export/{PROJECT}"))
        path = _locate(exported)
        live = _read_library(bridge, path)
        running, capabilities = _runtime(bridge)
        _check(old_routes <= capabilities,
               "Running bridge is missing capabilities from the reviewed backup")
        api = {name: content for name, content in backup.items() if name.startswith(f"{WEBDEV}/")}
        _check(api == {name: content for name, content in exported.items()
                       if name.startswith(f"{WEBDEV}/")}, "WebDev api has changed since backup")
        _check(live in (before, desired), "Live library differs from the backup and prepared update")
        _check(exported[LIBRARY] in (before, desired), "Gateway export has an unexpected library")
        if live == before:
            _check(running == OLD, "Running bridge and on-disk versions disagree")
            _check(exported == backup, "Gateway project has changed since the reviewed backup")
        if check_only:
            status.update(status="checked", phase="complete",
                          observed=_state(bridge, path, before, desired))
            return status

        if live == desired and running == NEW and set(SCOPED) <= capabilities:
            status.update(status="already_upgraded", phase="complete",
                          observed=_state(bridge, path, before, desired))
            return status

        status["phase"] = "pre_write_read"
        _check(_read_library(bridge, path) == live, "The library changed during preflight")
        if live == before:
            status["phase"] = "write_library"
            status["write_attempted"] = True
            result = bridge.call("files/write", {"path": path, "content": desired.decode("utf-8")})
            _check(isinstance(result, dict) and result.get("written") == path,
                   "Signed write did not acknowledge the single library path")

        status["phase"] = "readback"
        readback = _read_library(bridge, path)
        _check(readback == desired, "SHA-256 readback does not match prepared library")
        status["written_sha256"] = _sha(readback)
        status["phase"] = "scan_projects"
        status["scan_attempted"] = True
        gateway.scan_projects()

        status["phase"] = "verify_runtime"
        _verify_runtime(bridge, old_routes)
        observed = _state(bridge, path, before, desired)
        status["observed"] = observed
        _check(observed.get("library") == "0.1.1_prepared"
               and observed.get("runtime_version") == NEW
               and set(observed.get("scoped_capabilities", ())) == set(SCOPED),
               "Final signed read and ping disagree with the prepared 0.1.1")
        status.update(status="success", phase="complete")
    except Exception as exc:
        if status["write_attempted"] or status["scan_attempted"]:
            status["status"] = "uncertain"
        if before is not None and desired is not None:
            status["observed"] = _state(bridge, path, before, desired)
        status["error"] = str(exc) if isinstance(exc, UpgradeError) else type(exc).__name__
    return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="only verify the current state")
    args = parser.parse_args()
    try:
        settings = load_settings()
        _check(settings.gateway_url == TEST_GATEWAY, "Configured Gateway is outside this update")
        _check(bool(settings.bridge_secret), "The configured bridge credential is unavailable")
        _check(args.check or settings.allow_writes, "Writes are disabled in the launcher profile")
        gateway = GatewayClient(settings)
        bridge = BridgeClient(settings)
        try:
            with BACKUP.open("rb") as backup:
                fcntl.flock(backup, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = upgrade(gateway, bridge, settings.bridge_secret, backup.read(),
                                 REVIEW_PATCH.read_bytes(), CANDIDATE.read_bytes(), args.check)
        finally:
            bridge.close()
            gateway.close()
    except Exception as exc:
        result = {"status": "error", "phase": "local_setup", "write_attempted": False,
                  "error": str(exc) if isinstance(exc, UpgradeError) else type(exc).__name__}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] in {"success", "already_upgraded", "checked"} else 1


if __name__ == "__main__":
    sys.exit(main())
