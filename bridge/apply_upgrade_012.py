"""Update only the mcp-bridge script library on the Cupper test Gateway."""

from __future__ import annotations

import argparse
import ast
import fcntl
import hashlib
import hmac
import json
import sys
import time
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from diagnose_scoped_tags import NAMES, PATHS, TEST_GATEWAY
from upgrade_011 import (
    CANDIDATE, LIBRARY, PLACEHOLDER, SCOPED, UpgradeError, _check, _constant,
    _functions, _handlers, _library, _one, _project, _save_private,
)

OLD = "0.1.1"
NEW = "0.1.2"
PROJECT = "mcp-bridge"
GATEWAY_LIBRARY = f"{PROJECT}/{LIBRARY}"
BACKUPS = Path(__file__).resolve().parents[3] / "backups" / "mcp-bridge"
LEGACY_CONFIG = """    configs = system.tag.getConfiguration(path, False)
    if len(configs) != 1:
        raise ValueError('Could not read exactly one tag configuration')
    config = system.util.jsonDecode(system.util.jsonEncode(configs[0]))
    if config.get('name') != name or config.get('tagType', 'AtomicTag') != 'AtomicTag' or 'tags' in config:
        raise ValueError('Unexpected tag configuration')"""
EXPORT_CALL = "    config = _scoped_export_config(path, name)"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read_library(bridge):
    result = bridge.call("files/read", {"path": GATEWAY_LIBRARY})
    _check(isinstance(result, dict) and isinstance(result.get("content"), str),
           "Signed read did not return library content")
    return result["content"].encode("utf-8")


def excerpt(code, node):
    return "\n".join(code.splitlines()[node.lineno - 1:node.end_lineno])


def same_ast(code, node):
    return ast.dump(ast.parse(code).body[0]) == ast.dump(node)


def prepare(live, candidate):
    code, tree, assignments, secret = _library(live)
    new_code, new_tree, new_assignments, _ = _library(candidate)
    _check(_constant(assignments, "BRIDGE_VERSION") == OLD, "Expected Bridge 0.1.1 on disk")
    _check(_constant(new_assignments, "BRIDGE_VERSION") == NEW, "Expected a Bridge 0.1.2 candidate")
    scope = "TAG_CONFIG_ROOTS = frozenset(('[default]Line_1/Cupper/Ignition',))"
    _check(same_ast(scope, _one(assignments, "TAG_CONFIG_ROOTS")), "Unexpected tag write scope")
    old_functions, new_functions = _functions(tree), _functions(new_tree)
    helper = "_scoped_export_config"
    snapshot = "_scoped_tag_snapshot"
    _check(helper not in old_functions, "Bridge already contains a native export helper")
    read_properties = "TAG_READ_PROPERTIES"
    old_properties = _one(assignments, read_properties)
    new_properties = excerpt(new_code, _one(new_assignments, read_properties))
    _check(same_ast(new_properties.replace("'defaultValue', ", ""), old_properties),
           "Unexpected effective tag properties")
    old_snapshot = old_functions[snapshot]
    new_snapshot = excerpt(new_code, new_functions[snapshot])
    _check(new_snapshot.count(EXPORT_CALL) == 1
           and same_ast(new_snapshot.replace(EXPORT_CALL, LEGACY_CONFIG), old_snapshot),
           "Existing snapshot function differs from the reviewed 0.1.1")
    version = _one(assignments, "BRIDGE_VERSION")
    has_json = any(alias.name == "json" and alias.asname is None
                   for node in tree.body if isinstance(node, ast.Import) for alias in node.names)
    lines = code.splitlines(keepends=True)
    eol = "\r\n" if lines[0].endswith("\r\n") else "\n"
    edits = (
        (version, ("" if has_json else "import json\n") + f"BRIDGE_VERSION = {NEW!r}"),
        (old_properties, new_properties),
        (old_snapshot, excerpt(new_code, new_functions[helper]) + "\n\n\n" + new_snapshot),
    )
    for node, text in sorted(edits, key=lambda edit: edit[0].lineno, reverse=True):
        lines[node.lineno - 1:node.end_lineno] = [text.replace("\n", eol) + eol]
    updated = "".join(lines).encode("utf-8")
    _, _, actual, new_secret = _library(updated)
    _check(secret == new_secret and ast.dump(_handlers(assignments)[1]) == ast.dump(_handlers(actual)[1]),
           "Prepared update changed the secret or routing")
    return updated


def rebind_secret(source, replacement):
    _, _, assignments, current = _library(source)
    lines = source.splitlines(keepends=True)
    index = _one(assignments, "BRIDGE_SECRET").lineno - 1
    current_bytes = current.encode("utf-8")
    _check(lines[index].count(current_bytes) == 1, "Unsupported bridge secret literal")
    lines[index] = lines[index].replace(current_bytes, replacement.encode("utf-8"), 1)
    result = b"".join(lines)
    _check(_library(result)[3] == replacement, "Could not substitute the bridge secret")
    return result


def backup_library(folder, live, secret, *, version=OLD):
    _, _, _, live_secret = _library(live)
    encoded = secret.encode("utf-8")
    _check(len(encoded) >= 32 and secret != PLACEHOLDER and hmac.compare_digest(encoded, live_secret.encode())
           and live.count(encoded) == 1,
           "Cannot create a byte-exact backup without including the actual bridge credential")
    redacted = rebind_secret(live, PLACEHOLDER)
    _check(encoded not in redacted, "Backup did not remove the configured secret")
    folder.mkdir(mode=0o700)
    backup = folder / "code.py.redacted"
    _save_private(backup, redacted)
    _check(rebind_secret(backup.read_bytes(), secret) == live, "Backup reconstruction differs")
    manifest = {"resource": GATEWAY_LIBRARY, "version": version, "secret_placeholder": PLACEHOLDER,
                "sha256_redacted": sha(redacted),
                "sha256_reconstructed": sha(live)}
    _save_private(folder / "manifest.json", json.dumps(manifest, indent=2).encode())
    return {"path": str(backup), **manifest}


def verify_tags(bridge):
    tags = {}
    for name, path in zip(NAMES, PATHS):
        snap = bridge.call("tags/scoped-state", {"path": path})
        config = snap.get("config")
        effective = snap.get("effective")
        _check(snap.get("path") == path and isinstance(config, dict) and config.get("name") == name
               and isinstance(effective, dict), f"Incomplete scoped read for {name}")
        resolved = {**config, **effective}
        tags[name] = {key: resolved.get(key) for key in (
            "dataType", "valueSource", "historyEnabled", "historyProvider",
        )}
        if name == "Outfeed_Counter":
            initial = resolved.get("defaultValue")
            if initial is None:
                initial = config.get("value")
            tags[name].update(initialValue=initial, liveValue=snap.get("value"), valueGood=snap.get("valueGood"))
    return tags


def counter_matches(tags):
    counter = tags["Outfeed_Counter"]
    return (str(counter["valueSource"]).lower() == "memory" and counter["dataType"] == "Int4"
            and counter["historyEnabled"] is False and counter["valueGood"] is True
            and all(type(counter[key]) is int and counter[key] == 0 for key in ("initialValue", "liveValue")))


def upgrade(gateway, bridge, secret, backups, candidate, *, old=OLD, new=NEW,
            prepare_update=prepare, added_routes=frozenset(), verify_after=None, gateway_name=None,
            verify_existing_tags=True, verify_library_after_scan=False):
    status = {"status": "error", "phase": "preflight", "write_attempted": False}
    try:
        info = gateway.get_json("gateway-info")
        _check(info.get("ignitionVersion", "").split()[:1] == ["8.3.9"], "Expected Ignition 8.3.9")
        _check(gateway_name is None or info.get("name") == gateway_name, "Unexpected Gateway name")
        runtime = bridge.ping()
        caps = set(runtime.get("capabilities", []))
        _check(runtime.get("version") == old and set(SCOPED) | {"files/read", "files/write"} <= caps,
               f"Expected the running Bridge {old} and its update capabilities")
        exported = _project(gateway.get_bytes(f"projects/export/{PROJECT}"))
        live = read_library(bridge)
        _check(live == exported[LIBRARY], "Signed file read and project export disagree")
        desired = prepare_update(live, candidate)
        _check(set(_handlers(_library(live)[2])[0]) == caps, "Running and saved routes disagree")

        status["phase"] = "backup"
        status["backup"] = backup_library(backups / f"bridge-{old}-{uuid4().hex}", live, secret, version=old)
        _check(read_library(bridge) == live, "Library changed after backup")
        status["phase"] = "write_library"
        status["write_attempted"] = True
        result = bridge.call("files/write", {"path": GATEWAY_LIBRARY, "content": desired.decode("utf-8")})
        _check(result.get("written") == GATEWAY_LIBRARY, "Bridge did not acknowledge the exact library")
        readback = read_library(bridge)
        _check(readback == desired, "Signed readback differs from the prepared library")
        status["library_sha256"] = sha(readback)
        status["phase"] = "scan"
        gateway.scan_projects()
        status["phase"] = "verify_runtime"
        deadline = time.monotonic() + 20
        while True:
            try:
                runtime = bridge.ping()
                if runtime.get("version") == new and set(runtime.get("capabilities", [])) == caps | added_routes:
                    break
            except Exception:
                pass  # The project may be briefly unavailable during its scan.
            _check(time.monotonic() < deadline, f"Signed ping did not confirm Bridge {new} and its expected routes")
            time.sleep(0.5)
        status["runtime_version"] = new
        if verify_library_after_scan:
            status["phase"] = "verify_active_library"
            readback = read_library(bridge)
            exported = _project(gateway.get_bytes(f"projects/export/{PROJECT}"))
            _check(readback == exported[LIBRARY] == desired,
                   "Active library readback and independent project export must match the candidate")
            status["active_library_verified"] = True
        if verify_existing_tags:
            status["phase"] = "verify_tags"
            status["tags"] = verify_tags(bridge)
            if verify_after is None:
                _check(counter_matches(status["tags"]), "Outfeed_Counter does not match the requested configuration and Good value")
        if verify_after is not None:
            if not verify_existing_tags:
                status["phase"] = "verify_capabilities"
            status.update(verify_after(bridge))
        status.update(status="success", phase="complete")
    except Exception as exc:
        status["status"] = "uncertain" if status["write_attempted"] else "error"
        status["error"] = str(exc) if isinstance(exc, UpgradeError) else type(exc).__name__
    return status


def main(*, candidate=CANDIDATE, lock_name=".upgrade-012.lock", **upgrade_options):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", required=True, help="perform the one-time bridge update")
    parser.parse_args()
    try:
        from ignition_mcp.bridge import BridgeClient
        from ignition_mcp.client import GatewayClient
        from ignition_mcp.config import load_settings

        settings = load_settings()
        _check(settings.gateway_url == TEST_GATEWAY and settings.bridge_secret and settings.allow_writes,
               "Run with the existing authenticated test Gateway launcher and writes enabled")
        BACKUPS.mkdir(parents=True, mode=0o700, exist_ok=True)
        with (BACKUPS / lock_name).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with closing(GatewayClient(settings)) as gateway, closing(BridgeClient(settings)) as bridge:
                result = upgrade(gateway, bridge, settings.bridge_secret, BACKUPS, candidate.read_bytes(),
                                 **upgrade_options)
    except Exception as exc:
        result = {"status": "error", "phase": "local_setup", "write_attempted": False,
                  "error": str(exc) if isinstance(exc, UpgradeError) else type(exc).__name__}
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
