"""Verified, project-scoped Perspective deployment and explicit restoration."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import time
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4
from zipfile import BadZipFile, ZipFile
from zlib import error as CompressionError

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel

from ..app import AppContext, tool_result
from ..errors import GatewayError, IgnitionMcpError, NotFoundError, ValidationError
from ..config import PROJECT_NAME
from ..guards import require_deployment_write
from ..perspective.schema import validate_view
from .perspective import PAGE_CONFIG, PERSPECTIVE, _read_project_file, _resource_manifest, _set_page

FILES = dict[str, bytes]
PATCH = dict[str, bytes | None]
_LOCK = Lock()
_VERIFY_ATTEMPTS = 4
_VERIFY_DELAY = 0.5
_PAGE_MANIFEST = f"{PAGE_CONFIG}/resource.json"
_SESSION_PROPS = f"{PERSPECTIVE}/session-props"
_SESSION_PATH = f"{_SESSION_PROPS}/props.json"
_SESSION_MANIFEST = f"{_SESSION_PROPS}/resource.json"
_SIDENAV_VIEWS = ("SideNav", "Shared/SideNavArea")
_SELECTION_KEY = "scadaSelection"
_SELECTION_DEFAULT = {"line": "Line_1", "tagPath": "Line_1/Line/Ignition"}
_SIDENAV_DOCK = {
    "anchor": "fixed", "autoBreakpoint": 480, "content": "push", "handle": "hide",
    "iconUrl": "", "id": "SideNav", "modal": False, "resizable": False,
    "show": "visible", "size": 232, "viewParams": {}, "viewPath": "SideNav",
}


class ViewChange(BaseModel):
    view_path: str
    view_json: dict[str, Any]


class PageChange(BaseModel):
    page_url: str
    view_path: str
    title: str = ""


class Conflict(GatewayError):
    code = "deployment_conflict"


class _ManifestConflict(Conflict):
    def __init__(self, path: str, exported: bytes | None, live: bytes | None) -> None:
        super().__init__(f"Unexpected live content at {path}")
        self.comparison = _manifest_comparison(path, exported, live)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fingerprint(files: FILES) -> str:
    return _sha(json.dumps({p: _sha(v) for p, v in files.items()}, sort_keys=True).encode())


def _encode(value: Any) -> bytes:
    try:
        return json.dumps(value, indent=2, allow_nan=False).encode()
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"Value cannot be encoded as JSON: {exc}") from exc


def _object(data: bytes, path: str) -> dict[str, Any]:
    def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("duplicate object key")
        return result

    try:
        value = json.loads(data, object_pairs_hook=unique_keys)
        if not isinstance(value, dict):
            raise ValueError("expected a JSON object")
        json.dumps(value, allow_nan=False)
    except (ValueError, RecursionError) as exc:
        raise GatewayError(f"Invalid JSON object in {path}") from exc
    return value


def _without_scan_metadata(content: bytes, path: str, *, drop_actor: bool = False) -> str:
    manifest = _object(content, path)
    # Ignition 8.3 can refresh these fields during a project scan.
    attributes = manifest.get("attributes")
    if isinstance(attributes, dict):
        attributes.pop("lastModificationSignature", None)
        modification = attributes.get("lastModification")
        if isinstance(modification, dict):
            modification.pop("timestamp", None)
            if drop_actor:
                modification.pop("actor", None)
    return json.dumps(manifest, sort_keys=True)


def _manifest_comparison(path: str, exported: bytes | None, live: bytes | None) -> dict[str, Any]:
    report: dict[str, Any] = {
        "path": path,
        "export_sha256": _sha(exported) if exported is not None else None,
        "bridge_sha256": _sha(live) if live is not None else None,
    }
    if exported is None or live is None:
        return {**report, "classification": "missing_manifest"}

    try:
        left, right = (_object(data, path) for data in (exported, live))
        _declared_files(left, path)
        _declared_files(right, path)
    except GatewayError:
        return {**report, "classification": "invalid_manifest"}

    def fields(manifest: dict[str, Any]) -> dict[str, str]:
        known = {key: manifest[key] for key in ("scope", "version", "restricted", "overridable", "files")
                 if key in manifest}
        attributes = manifest.get("attributes")
        if isinstance(attributes, dict):
            if "lastModificationSignature" in attributes:
                known["attributes.lastModificationSignature"] = attributes["lastModificationSignature"]
            modification = attributes.get("lastModification")
            if isinstance(modification, dict):
                for key in ("actor", "timestamp"):
                    if key in modification:
                        known[f"attributes.lastModification.{key}"] = modification[key]
        return {key: json.dumps(value, sort_keys=True) for key, value in known.items()}

    if json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True):
        classification = "formatting_only"
    elif _without_scan_metadata(exported, path) == _without_scan_metadata(live, path):
        classification = "scan_metadata_only"
    else:
        classification = "substantive_difference"
    left_fields, right_fields = fields(left), fields(right)
    return {**report, "classification": classification, "differing_known_fields": [
        key for key in sorted(left_fields.keys() | right_fields.keys())
        if left_fields.get(key) != right_fields.get(key)
    ]}


def _matches_after_scan(
    path: str, expected: bytes | None, actual: bytes | None, *, session_edit: bool = False,
) -> bool:
    if expected == actual:
        return True
    if expected is None or actual is None:
        return False
    if path == _SESSION_MANIFEST and session_edit:
        attributes = _object(actual, path).get("attributes")
        modification = attributes.get("lastModification") if isinstance(attributes, dict) else None
        external = isinstance(modification, dict) and modification.get("actor") == "external"
        return _without_scan_metadata(expected, path, drop_actor=external) == (
            _without_scan_metadata(actual, path, drop_actor=external)
        )
    if _is_perspective_manifest(path):
        return _without_scan_metadata(expected, path) == _without_scan_metadata(actual, path)
    return False


def _is_perspective_manifest(path: str) -> bool:
    return path in {_PAGE_MANIFEST, _SESSION_MANIFEST} or re.fullmatch(
        rf"{re.escape(PERSPECTIVE)}/views/.+/resource\.json", path
    ) is not None


def _relative(path: str) -> bool:
    return (
        not any(c in path for c in ("\\", ":", "\x00"))
        and all(part not in ("", ".", "..") for part in path.split("/"))
    )


def _declared_files(manifest: dict[str, Any], path: str) -> list[str]:
    names = manifest.get("files")
    if not isinstance(names, list) or any(
        not isinstance(name, str) or not _relative(name) for name in names
    ):
        raise GatewayError(f"Invalid resource manifest files list in {path}")
    if _is_perspective_manifest(path):
        attributes = manifest.get("attributes", {})
        modification = attributes.get("lastModification", {}) if isinstance(attributes, dict) else None
        if (
            "resource.json" in names
            or len(names) != len(set(names))
            or manifest.get("scope", "G") not in ("A", "C", "G")
            or type(manifest.get("version", 1)) is not int
            or not isinstance(manifest.get("restricted", False), bool)
            or not isinstance(manifest.get("overridable", True), bool)
            or not isinstance(modification, dict)
            or not isinstance(attributes.get("lastModificationSignature", ""), str)
            or not isinstance(modification.get("timestamp", ""), str)
            or not isinstance(modification.get("actor", ""), str)
        ):
            raise GatewayError(f"Invalid Perspective resource manifest in {path}")
    return names


def _check_write_scope(changes: PATCH) -> None:
    for path in changes:
        # Reject names that Windows can normalize to a different filesystem path.
        for part in path.split("/"):
            if (
                part.endswith((".", " "))
                or re.search(r'[<>"|?*\x00-\x1f]', part)
                or re.fullmatch(
                    r"CON|PRN|AUX|NUL|COM[0-9¹²³]|LPT[0-9¹²³]",
                    part.partition(".")[0].rstrip(),
                    re.IGNORECASE,
                )
            ):
                raise ValidationError("Deployment contains an unsafe filesystem path")


def _archive(data: bytes) -> FILES:
    files: FILES = {}
    seen: set[str] = set()
    try:
        with ZipFile(io.BytesIO(data)) as archive:
            for entry in archive.infolist():
                name = entry.filename.removesuffix("/") if entry.is_dir() else entry.filename
                if not _relative(name) or name in seen or stat.S_ISLNK(entry.external_attr >> 16):
                    raise GatewayError(f"Invalid or repeated ZIP entry: {name}")
                seen.add(name)
                if not entry.is_dir():
                    files[name] = archive.read(entry)  # also checks CRC
    except (BadZipFile, CompressionError, OSError, RuntimeError, ValueError) as exc:
        raise GatewayError("Could not read or validate project ZIP") from exc

    if "project.json" not in files:
        raise GatewayError("Project ZIP is missing project.json")
    _object(files["project.json"], "project.json")
    for path, content in files.items():
        if not path.endswith("/resource.json"):
            continue
        parent = path.rsplit("/", 1)[0]
        for name in _declared_files(_object(content, path), path):
            if f"{parent}/{name}" not in files:
                raise GatewayError(f"Project ZIP is missing a file declared in {path}")
    return files


def _export(ctx: AppContext, project: str) -> tuple[bytes, FILES]:
    data = ctx.client.get_bytes(f"projects/export/{project}")
    return data, _archive(data)


def _check_inventory(ctx: AppContext, project: str, files: FILES) -> None:
    result = ctx.bridge.call("files/list", {"path": project, "recursive": True})
    prefix = project + "/"
    try:
        entries = result["entries"]
        if not isinstance(entries, list):
            raise TypeError("entries must be a list")
        paths = set()
        for entry in entries:
            path = entry["path"]
            if not path.startswith(prefix) or not isinstance(entry["isDir"], bool):
                raise ValueError("invalid inventory entry")
            if not entry["isDir"]:
                paths.add(path[len(prefix):])
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise GatewayError("Invalid bridge project inventory response") from exc
    if paths != files.keys():
        raise Conflict("Gateway project inventory differs from the export")


def _plan(
    before: FILES, views: list[ViewChange], pages: list[PageChange],
    configure_side_nav: bool = False,
) -> PATCH:
    changes: PATCH = {}
    view_paths: set[str] = set()
    page_urls: set[str] = set()
    for view in views:
        if not _relative(view.view_path) or view.view_path in view_paths:
            raise ValidationError("View paths must be unique, relative paths")
        view_paths.add(view.view_path)
        problems = validate_view(view.view_json)
        if problems:
            raise ValidationError(f"Invalid view {view.view_path}: {'; '.join(problems)}")
        folder = f"{PERSPECTIVE}/views/{view.view_path}"
        _stage_resource(before, changes, folder, "view.json", view.view_json)

    if pages or configure_side_nav:
        path = f"{PAGE_CONFIG}/config.json"
        if configure_side_nav and path not in before:
            raise Conflict("SideNav requires existing local Perspective page configuration")
        if path not in before and _object(before["project.json"], "project.json").get("parent"):
            raise Conflict("Cannot initialize local page configuration with a parent project")
        config = _object(before[path], path) if path in before else {"pages": {}}
        for page in pages:
            if page.page_url in page_urls or not _relative(page.view_path):
                raise ValidationError("Page URLs must be unique and target a relative view path")
            page_urls.add(page.page_url)
            _set_page(config, path, page.page_url, page.view_path, page.title)
        if configure_side_nav:
            for view_path in _SIDENAV_VIEWS:
                folder = f"{PERSPECTIVE}/views/{view_path}"
                if any(f"{folder}/{name}" not in (before.keys() | changes.keys())
                       for name in ("view.json", "resource.json")):
                    raise Conflict(f"SideNav view is missing: {view_path}")
            _add_side_nav_dock(config)
        _stage_resource(before, changes, PAGE_CONFIG, "config.json", config)
    if configure_side_nav:
        _stage_scada_selection(before, changes)
    return changes


def _add_side_nav_dock(config: dict[str, Any]) -> None:
    shared = config.get("sharedDocks")
    if not isinstance(config.get("pages"), dict) or not isinstance(shared, dict):
        raise Conflict("Unsupported Perspective page configuration")
    matches = []
    for side in ("top", "bottom", "left", "right"):
        docks = shared.get(side, [])
        if not isinstance(docks, list) or any(not isinstance(dock, dict) for dock in docks):
            raise Conflict(f"Invalid shared dock list: {side}")
        matches.extend(
            (side, dock) for dock in docks
            if dock.get("id") == "SideNav" or dock.get("viewPath") == "SideNav"
        )
    if matches:
        if matches != [("left", _SIDENAV_DOCK)]:
            raise Conflict("Existing shared SideNav dock conflicts with the requested dock")
        return
    shared.setdefault("left", []).append(_SIDENAV_DOCK.copy())


def _stage_scada_selection(before: FILES, changes: PATCH) -> None:
    if _SESSION_PATH not in before or _SESSION_MANIFEST not in before:
        raise Conflict("SideNav requires an existing local Perspective session-props resource")
    manifest = _object(before[_SESSION_MANIFEST], _SESSION_MANIFEST)
    if manifest.get("scope") != "G" or manifest.get("version") != 1 or (
        manifest.get("files") != ["props.json"]
    ):
        raise Conflict("Unsupported Perspective session-props manifest")
    props = _object(before[_SESSION_PATH], _SESSION_PATH)
    custom = props.get("custom")
    config = props.get("propConfig")
    if not all(isinstance(value, dict) for value in (custom, config, props.get("props"))):
        raise Conflict("Unsupported Perspective session properties format")
    path = f"custom.{_SELECTION_KEY}"
    if any(key == "custom" or key == path or key.startswith(path + ".") for key in config):
        raise Conflict("Existing property configuration controls custom.scadaSelection")
    if _SELECTION_KEY in custom and custom[_SELECTION_KEY] != _SELECTION_DEFAULT:
        raise Conflict("Existing custom.scadaSelection conflicts with the requested default")
    custom[_SELECTION_KEY] = _SELECTION_DEFAULT.copy()
    _stage_resource(before, changes, _SESSION_PROPS, "props.json", props)


def _stage_resource(
    before: FILES, changes: PATCH, folder: str, name: str, value: dict[str, Any]
) -> None:
    path, manifest = f"{folder}/{name}", f"{folder}/resource.json"
    data = _encode(value)
    if path in before and manifest in before:
        if name not in _object(before[manifest], manifest)["files"]:
            raise Conflict(f"Manifest does not declare {path}")
        if _object(before[path], path) != value:
            changes[path] = data
    elif path in before or manifest in before:
        raise Conflict(f"Incomplete resource: {folder}")
    else:
        if any(p.startswith(folder + "/") and "/" not in p[len(folder) + 1:] for p in before):
            raise Conflict(f"Unmanaged files already exist in {folder}")
        changes[path] = data
        changes[manifest] = _encode(_resource_manifest(name))


def _requires_binary_read(content: bytes | None) -> bool:
    if content is None:
        return False
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def _check_paths(
    ctx: AppContext, project: str, expected: PATCH, *, after_scan: bool = False,
    session_edit: bool = False, export_manifests: frozenset[str] = frozenset(),
    diagnose_manifests: bool = False,
) -> PATCH:
    observed: PATCH = {}
    for path, content in expected.items():
        target = f"{project}/{path}"
        try:
            if content is not None and _requires_binary_read(content):
                result = ctx.bridge.call("files/read", {"path": target, "format": "sha256"})
                digest = result.get("sha256") if isinstance(result, dict) else None
                if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                    raise GatewayError("Bridge did not return a valid raw SHA-256")
                # Only retain the reference bytes when the digest of the live file matches.
                actual = content if digest == _sha(content) else None
            else:
                actual = _read_project_file(ctx, target).encode("utf-8")
        except NotFoundError:
            actual = None
        except (GatewayError, UnicodeError) as exc:
            raise GatewayError(f"Could not verify live bytes at {path}: {exc}") from exc
        matches = actual == content
        if after_scan:
            matches = _matches_after_scan(path, content, actual, session_edit=session_edit)
        elif (
            not matches and path in export_manifests
            and content is not None and actual is not None
        ):
            try:
                _declared_files(_object(actual, path), path)
                matches = _without_scan_metadata(content, path) == _without_scan_metadata(actual, path)
            except GatewayError:
                matches = False
        if not matches:
            if diagnose_manifests and path in export_manifests:
                raise _ManifestConflict(path, content, actual)
            raise Conflict(f"Unexpected live content at {path}")
        observed[path] = actual
    return observed


def _check_export_paths(
    ctx: AppContext, project: str, before: FILES, changes: PATCH, *,
    diagnose_manifests: bool = False,
) -> PATCH:
    manifests = frozenset(f"{path.rsplit('/', 1)[0]}/resource.json" for path in changes)
    paths = changes.keys() | manifests
    for path in manifests:
        if path in before:
            folder = path.rsplit("/", 1)[0]
            paths.update(f"{folder}/{name}" for name in _declared_files(_object(before[path], path), path))
    return _check_paths(
        ctx, project, {path: before.get(path) for path in sorted(paths)},
        export_manifests=manifests, diagnose_manifests=diagnose_manifests,
    )


def _write_private(path: Path, content: bytes) -> None:
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _save_backup(root: Path, project: str, data: bytes) -> dict[str, str]:
    backup_id = uuid4().hex
    folder = root / project / backup_id
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    path = folder / "before.zip"
    _write_private(path, data)
    saved = path.read_bytes()
    if saved != data:
        raise GatewayError("Stored backup differs from the verified export")
    return {"id": backup_id, "path": str(path), "sha256": _sha(saved)}


def _apply_patch(before: FILES, changes: PATCH) -> FILES:
    expected = before.copy()
    for path, content in changes.items():
        if content is None:
            del expected[path]
        else:
            expected[path] = content
    return expected


def _verify(
    ctx: AppContext, project: str, expected: FILES, changes: PATCH, *, session_edit: bool = False,
) -> FILES:
    _check_paths(ctx, project, changes, after_scan=True, session_edit=session_edit)
    for attempt in range(_VERIFY_ATTEMPTS):
        _, actual = _export(ctx, project)
        if actual.keys() == expected.keys() and all(
            _matches_after_scan(path, content, actual[path], session_edit=session_edit)
            for path, content in expected.items()
        ):
            _check_inventory(ctx, project, actual)
            _check_paths(ctx, project, {path: actual.get(path) for path in changes})
            return actual
        if attempt + 1 < _VERIFY_ATTEMPTS:
            time.sleep(_VERIFY_DELAY)
    raise Conflict("Post-scan export did not converge to the expected project state")


def _execute(
    ctx: AppContext, root: Path, project: str, data: bytes, before: FILES, changes: PATCH,
    report: dict[str, Any],
) -> None:
    report["phase"] = "backup"
    backup = _save_backup(root, project, data)
    report["backup"] = backup
    report["phase"] = "preflight"
    _, current = _export(ctx, project)
    if current != before:
        raise Conflict("Project changed since the backup export")
    _check_inventory(ctx, project, before)

    # Capture the original Bridge bytes for strict checks before every write.
    live = _check_export_paths(ctx, project, before, changes)
    for path, content in changes.items():
        report["phase"] = "preflight"
        _check_paths(ctx, project, live)
        report["phase"] = "write"
        report["attempted"].append(path)
        target = f"{project}/{path}"
        if content is None:
            reply = ctx.bridge.call("files/delete", {"path": target, "recursive": False})
            key = "deleted"
        else:
            reply = ctx.bridge.call("files/write", {"path": target, "content": content.decode()})
            key = "written"
        if not isinstance(reply, dict) or reply.get(key) != target:
            raise GatewayError(f"Unconfirmed bridge write response at {path}")
        report["acknowledged"].append(path)
        live[path] = content
        report["phase"] = "readback"
        _check_paths(ctx, project, {path: content})
        report["read_back"].append(path)

    expected = _apply_patch(before, changes)
    report["phase"] = "scan"
    ctx.client.scan_projects()
    report["phase"] = "verification"
    actual = _verify(
        ctx, project, expected, changes, session_edit=_SESSION_PATH in changes,
    )
    report["verified"] = list(changes)
    report["phase"] = "receipt"
    receipt = {
        "project": project,
        "gateway": _sha(ctx.settings.gateway_url.encode()),
        "backup_sha256": backup["sha256"],
        # Restore still requires the exact exported state observed after the scan.
        "verified_state": _fingerprint(actual),
    }
    _write_private(Path(backup["path"]).with_name("verified.json"), _encode(receipt))
    report.update(status="success", phase="complete", restore_available=True)


def register(mcp: FastMCP, ctx: AppContext, backup_root: Path | None = None) -> None:
    root = backup_root if backup_root is not None else Path.cwd() / "backups" / "mcp-deployments"

    def run(
        project: str, dry_run: bool, confirm: bool, exclusive_access: bool,
        views: list[ViewChange], pages: list[PageChange], expected_state: str,
        backup_id: str = "", backup_sha256: str = "", configure_side_nav: bool = False,
    ) -> dict[str, Any]:
        report: dict[str, Any] = {
            "project": project, "phase": "planning", "attempted": [], "acknowledged": [],
            "read_back": [], "verified": [], "restore_available": False,
            "perspective_runtime": "not_tested",
        }
        try:
            if not PROJECT_NAME.fullmatch(project):
                raise ValidationError("Invalid project name")
            if not dry_run:
                require_deployment_write(ctx.settings, project, confirm, exclusive_access)
                if configure_side_nav and not expected_state:
                    raise ValidationError("SideNav deployment requires expected_state from dry-run")
            with _LOCK:
                data, before = _export(ctx, project)
                _check_inventory(ctx, project, before)
                state = _fingerprint(before)
                report["base_state"] = state
                if expected_state and state != expected_state:
                    raise Conflict("Project no longer matches expected_state")
                if backup_id:
                    changes = _restore_plan(ctx, root, project, backup_id, backup_sha256, before)
                else:
                    changes = _plan(before, views, pages, configure_side_nav)
                report["planned"] = list(changes)
                if not dry_run and ctx.settings.restricted_deployment:
                    _check_write_scope(changes)
                if dry_run or not changes:
                    _check_export_paths(ctx, project, before, changes, diagnose_manifests=dry_run)
                    return {**report, "status": "dry_run" if dry_run else "no_changes"}
                _execute(ctx, root, project, data, before, changes, report)
        except (IgnitionMcpError, httpx.HTTPError, OSError, UnicodeError) as exc:
            uncertain = bool(report["attempted"])
            report.update(status="uncertain" if uncertain else "error", message=str(exc))
            if isinstance(exc, IgnitionMcpError):
                report["error"] = exc.code
            if isinstance(exc, _ManifestConflict):
                report["manifest_comparison"] = exc.comparison
        return report

    @mcp.tool()
    @tool_result(ctx)
    def perspective_deploy(
        project: str,
        views: list[ViewChange] | None = None,
        pages: list[PageChange] | None = None,
        dry_run: bool = True,
        confirm: bool = False,
        expected_state: str = "",
        exclusive_access: bool = False,
        configure_side_nav: bool = False,
    ) -> dict[str, Any]:
        """Plan or apply Perspective changes to an existing project with a verified ZIP backup.
        Set expected_state to the base_state from dry_run to pin the reviewed project version.
        exclusive_access attests that other project editors have been coordinated.
        configure_side_nav adds the shared West Dock and scadaSelection to the same plan.
        The initial ZIP-to-Bridge check compares each declared file byte-for-byte and
        tolerates scan metadata changes in manifests. Dry-run reports manifest conflicts.
        """
        return run(
            project, dry_run, confirm, exclusive_access, views or [], pages or [], expected_state,
            configure_side_nav=configure_side_nav,
        )

    @mcp.tool()
    @tool_result(ctx)
    def perspective_restore(
        project: str,
        backup_id: str,
        backup_sha256: str,
        dry_run: bool = True,
        confirm: bool = False,
        exclusive_access: bool = False,
    ) -> dict[str, Any]:
        """Restore exactly the files changed by a verified operation, if the project is unchanged."""
        if not re.fullmatch(r"[0-9a-f]{32}", backup_id):
            raise ValidationError("Invalid backup_id")
        return run(
            project, dry_run, confirm, exclusive_access, [], [], "", backup_id, backup_sha256
        )


def _restore_plan(
    ctx: AppContext, root: Path, project: str, backup_id: str, checksum: str, current: FILES,
) -> PATCH:
    folder = root / project / backup_id
    data = (folder / "before.zip").read_bytes()
    if not re.fullmatch(r"[0-9a-f]{64}", checksum) or _sha(data) != checksum:
        raise Conflict("Backup ZIP checksum mismatch")
    original = _archive(data)
    receipt_path = folder / "verified.json"
    if not receipt_path.is_file():
        raise Conflict("No verified deployment receipt; reconcile the project manually")
    receipt = _object(receipt_path.read_bytes(), "verified.json")
    if (
        receipt.get("project") != project
        or receipt.get("gateway") != _sha(ctx.settings.gateway_url.encode())
        or receipt.get("backup_sha256") != checksum
        or receipt.get("verified_state") != _fingerprint(current)
    ):
        raise Conflict("Project, Gateway or current state does not match the verified receipt")

    changes: PATCH = {}
    for path in sorted(original.keys() | current.keys()):
        if _matches_after_scan(path, original.get(path), current.get(path)):
            continue
        if not (
            path in {
                f"{PAGE_CONFIG}/config.json", f"{PAGE_CONFIG}/resource.json",
                _SESSION_PATH, _SESSION_MANIFEST,
            }
            or re.fullmatch(rf"{re.escape(PERSPECTIVE)}/views/.+/(view|resource)\.json", path)
        ):
            raise Conflict(f"Backup differs outside the supported Perspective resources: {path}")
        changes[path] = original.get(path)
    return changes
