"""Bridge lifecycle tools: install and verify the mcp-bridge WebDev project."""

from __future__ import annotations

import ast
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from ..app import AppContext, tool_result
from ..bridge import MIN_BRIDGE_VERSION
from ..errors import (
    AuthError,
    BridgeUnavailableError,
    GatewayError,
    IgnitionMcpError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from ..guards import require_writes

SECRET_PLACEHOLDER = "__BRIDGE_SECRET__"
LIB_ARCNAME = "ignition/script-python/mcp_bridge_lib/code.py"
PROJECT = "mcp-bridge"
BRIDGE_PROJECT_LIBRARY = f"{PROJECT}/{LIB_ARCNAME}"
LIB_MANIFEST = LIB_ARCNAME.rsplit("/", 1)[0] + "/resource.json"
WEBDEV = "com.inductiveautomation.webdev/resources/api"
BRIDGE_FILES = {
    "project.json",
    LIB_ARCNAME,
    LIB_MANIFEST,
    f"{WEBDEV}/config.json",
    f"{WEBDEV}/doPost",
    f"{WEBDEV}/resource.json",
}
LOCAL_LIBRARY = Path(__file__).resolve().parents[3] / "bridge" / "src" / "mcp_bridge_lib.py"


def _bridge_version(tree: ast.Module) -> str:
    version, = (ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "BRIDGE_VERSION"
                        for target in node.targets))
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]{1,5}(?:\.[0-9]{1,5}){2}", version):
        raise ValueError("Invalid Bridge version")
    return version


def _scoped_result_fingerprint(source: str) -> tuple[str, str]:
    tree = ast.parse(source)
    result, = (node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_scoped_result")
    return _bridge_version(tree), hashlib.sha256(ast.dump(result).encode()).hexdigest()

# Matches only the `BRIDGE_SECRET = '__BRIDGE_SECRET__'` assignment line, not the
# gateway-side "is this configured?" guard that also references the placeholder
# (`if BRIDGE_SECRET == '__BRIDGE_SECRET__'`). A blind text-wide replace would
# substitute the guard too, making it compare the secret against itself and
# always report "not configured" even on a correct install.
_ASSIGNMENT_RE = re.compile(
    r"^BRIDGE_SECRET = '" + re.escape(SECRET_PLACEHOLDER) + r"'$", re.MULTILINE
)


def _find_bridge_zip() -> Path:
    """Locate mcp-bridge.zip in the installed package or the source checkout."""
    candidates = [
        Path(__file__).parent.parent / "_bridge" / "mcp-bridge.zip",  # packaged
        Path(__file__).parent.parent.parent.parent / "bridge" / "mcp-bridge.zip",  # repo
    ]
    for c in candidates:
        if c.is_file():
            return c
    return candidates[0]


BRIDGE_ZIP = _find_bridge_zip()


def _zip_with_secret(secret: str) -> tuple[bytes, str, str]:
    """Prepare the in-memory install ZIP, its version and its exact library digest."""
    try:
        with zipfile.ZipFile(BRIDGE_ZIP) as src:
            names = src.namelist()
            if len(names) != len(BRIDGE_FILES) or set(names) != BRIDGE_FILES:
                raise ValueError("Unexpected Bridge archive entries")
            files = {name: src.read(name) for name in names}
        project = json.loads(files["project.json"])
        if (
            project.get("name", PROJECT) != PROJECT
            or project.get("enabled") is not True
            or project.get("parent") not in ("", None)
            or project.get("inheritable") is not False
        ):
            raise ValueError("Invalid Bridge project metadata")
        for path, expected in (
            (LIB_MANIFEST, ["code.py"]),
            (f"{WEBDEV}/resource.json", ["config.json", "doPost"]),
        ):
            manifest = json.loads(files[path])
            if manifest.get("scope") != "G" or manifest.get("files") != expected:
                raise ValueError("Invalid Bridge resource manifest")
        webdev = json.loads(files[f"{WEBDEV}/config.json"])
        if (
            webdev.get("resource-type") != "python-resource"
            or webdev.get("doPost", {}).get("enabled") is not True
        ):
            raise ValueError("The WebDev endpoint is inactive")
        if not any(
            isinstance(node, ast.FunctionDef) and node.name == "doPost"
            for node in ast.parse(files[f"{WEBDEV}/doPost"]).body
        ):
            raise ValueError("The WebDev handler is absent")
        text = files[LIB_ARCNAME].decode("utf-8")
        match, = _ASSIGNMENT_RE.finditer(text)
        version = _bridge_version(ast.parse(text))
    except (OSError, ValueError, AttributeError, SyntaxError, RuntimeError, zipfile.BadZipFile):
        raise BridgeUnavailableError(
            "Bridge archive is missing or invalid. Run `python bridge/build_zip.py` first."
        ) from None

    start, end = match.span()
    literal = ascii(secret) if secret.isascii() else "u" + ascii(secret)
    library = (text[:start] + f"BRIDGE_SECRET = {literal}" + text[end:]).encode("utf-8")
    files[LIB_ARCNAME] = library
    out_buf = io.BytesIO()
    with zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as dst:
        for name, data in files.items():
            dst.writestr(name, data)
    return out_buf.getvalue(), version, hashlib.sha256(library).hexdigest()


def register(mcp: FastMCP, ctx: AppContext) -> None:
    @mcp.tool()
    @tool_result(ctx)
    def bridge_scoped_result_verify() -> dict[str, Any]:
        """Compare only _scoped_result with the local fork via a signed, fixed-path Bridge read."""
        try:
            runtime = ctx.bridge.ping().get("version")
            reply = ctx.bridge.call("files/read", {"path": BRIDGE_PROJECT_LIBRARY})
            live_version, live_hash = _scoped_result_fingerprint(reply["content"])
            _, reference_hash = _scoped_result_fingerprint(LOCAL_LIBRARY.read_text(encoding="utf-8"))
        except (IgnitionMcpError, OSError, SyntaxError, ValueError, TypeError, KeyError, RecursionError):
            raise GatewayError("Unable to compare the signed Bridge library read with the local fork") from None
        return {"library_version": live_version, "runtime_matches_library": runtime == live_version,
                "scoped_result_matches_fork": live_hash == reference_hash,
                "scoped_result_sha256": live_hash, "fork_sha256": reference_hash}

    @mcp.tool()
    @tool_result(ctx)
    def bridge_install(confirm: bool = False) -> dict[str, Any]:
        """Create the mcp-bridge WebDev project only if it is absent. Mutating, REST plane.

        Uses IGNITION_BRIDGE_SECRET as the shared HMAC secret. After import, verifies
        the running version and library digest through signed Bridge calls."""
        require_writes(ctx.settings, confirm, "bridge_install")
        secret = ctx.settings.bridge_secret
        if not secret or secret == SECRET_PLACEHOLDER:
            raise ValidationError(
                "IGNITION_BRIDGE_SECRET must be set before installing the bridge.",
                remediation="Generate a random secret and set IGNITION_BRIDGE_SECRET, then retry.",
            )
        try:
            ctx.client.get_json(f"projects/find/{PROJECT}")
        except NotFoundError:
            pass
        else:
            raise ValidationError(
                "The mcp-bridge project already exists.",
                remediation="Use bridge_status to verify it or follow the version-specific upgrade.",
            )

        payload, version, digest = _zip_with_secret(secret)
        recovery = (
            "Check project_get for mcp-bridge and bridge_status before retrying. See docs/bridge.md."
        )
        try:
            ctx.client.post_bytes(
                f"projects/import/{PROJECT}",
                payload,
                "application/zip",
                params={"overwrite": "false"},
            )
        except (AuthError, PermissionDeniedError):
            raise
        except (IgnitionMcpError, httpx.TransportError):
            return {
                "status": "uncertain",
                "phase": "import",
                "installed": None,
                "verified": False,
                "bridge_version": version,
                "note": recovery,
            }

        try:
            if ctx.bridge.ping().get("version") != version:
                raise GatewayError("Running Bridge version differs from the archive")
            readback = ctx.bridge.call(
                "files/read", {"path": BRIDGE_PROJECT_LIBRARY, "format": "sha256"},
            )
            if readback != {"sha256": digest}:
                raise GatewayError("Bridge library digest differs from the imported library")
        except (IgnitionMcpError, httpx.TransportError):
            return {
                "status": "uncertain",
                "phase": "verification",
                "installed": True,
                "verified": False,
                "bridge_version": version,
                "note": recovery,
            }
        return {"status": "success", "installed": True, "verified": True, "bridge_version": version}

    @mcp.tool()
    @tool_result(ctx)
    def bridge_status() -> dict[str, Any]:
        """Report bridge availability and version. Read-only, bridge plane."""
        if not ctx.settings.bridge_enabled:
            return {"enabled": False, "note": "IGNITION_BRIDGE_SECRET not set."}
        pong = ctx.bridge.ping()
        version = pong.get("version", "unknown")
        return {
            "enabled": True,
            "version": version,
            "min_supported": MIN_BRIDGE_VERSION,
            "capabilities": pong.get("capabilities", []),
        }


# expose helper for tests
__all__ = ["register", "_zip_with_secret"]
