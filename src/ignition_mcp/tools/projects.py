from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from ..app import AppContext, tool_result
from ..errors import GatewayError, IgnitionMcpError, NotFoundError, PermissionDeniedError, ValidationError
from ..guards import (
    refuse_in_restricted_deployment, require_name_echo, require_project_import_scope,
    require_project_import_write, require_writes,
)
from .deployment import (
    FILES, _LOCK, _archive, _check_write_scope, _export, _fingerprint, _object, _save_backup, _sha,
)


class ProjectImportConflict(GatewayError):
    code = "project_import_conflict"


def _standalone_project(files: FILES, name: str) -> dict[str, Any]:
    project = _object(files["project.json"], "project.json")
    if (project.get("name", name) != name or project.get("parent") not in (None, "")
            or project.get("inheritable") is not False or type(project.get("enabled")) is not bool):
        raise ValidationError("Import requires a standalone, non-inheritable project with the target name")
    return project


def _import_archive(data: bytes) -> FILES:
    files = _archive(data)
    _check_write_scope(files)
    # Gateway hosts can use case-insensitive filesystems.
    paths: dict[str, str] = {}
    for path in files:
        parts = path.split("/")
        for i in range(1, len(parts) + 1):
            prefix = "/".join(parts[:i])
            if paths.setdefault(prefix.casefold(), prefix) != prefix or (i < len(parts) and prefix in files):
                raise ValidationError("Project ZIP contains colliding member paths")
    return files


def _import_state(ctx: AppContext, name: str) -> tuple[bytes | None, str]:
    try:
        data, files = _export(ctx, name)
    except NotFoundError:
        return None, "absent"
    _standalone_project(files, name)
    return data, _fingerprint(files)


def _import_readback(files: FILES) -> FILES:
    normalized = files.copy()
    for path, content in files.items():
        if path == "project.json" or path.endswith("/resource.json"):
            data = _object(content, path)
            attributes = data.get("attributes")
            if path != "project.json" and isinstance(attributes, dict):
                attributes.pop("lastModification", None)
                attributes.pop("lastModificationSignature", None)
            normalized[path] = json.dumps(data, sort_keys=True).encode()
    return normalized


def register(mcp: FastMCP, ctx: AppContext) -> None:
    @mcp.tool()
    @tool_result(ctx)
    def project_list() -> dict[str, Any]:
        """Read-only, REST plane. List all projects."""
        result = ctx.client.get_json("projects/list")
        return {"projects": result.get("items", []) if isinstance(result, dict) else result}

    @mcp.tool()
    @tool_result(ctx)
    def project_get(name: str) -> dict[str, Any]:
        """Read-only, REST plane. Get details for one project."""
        if not name:
            raise ValidationError("name is required")
        result = ctx.client.get_json(f"projects/find/{name}")
        return {"project": result}

    @mcp.tool()
    @tool_result(ctx)
    def project_create(
        name: str,
        title: str = "",
        description: str = "",
        parent: str = "",
        inheritable: bool = False,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Mutating, REST plane. Create a new project."""
        require_writes(ctx.settings, confirm, "project_create")
        if not re.match(r"^[A-Za-z0-9_]+$", name):
            raise ValidationError("project name must match ^[A-Za-z0-9_]+$")
        result = ctx.client.post_json(
            "projects",
            body={
                "name": name,
                "title": title or name,
                "description": description,
                "parent": parent,
                "inheritable": inheritable,
                "enabled": True,
            },
        )
        return {"created": name, "result": result}

    @mcp.tool()
    @tool_result(ctx)
    def project_delete(name: str, confirm_name: str = "", confirm: bool = False) -> dict[str, Any]:
        """Mutating + destructive, REST plane. Delete a project."""
        require_writes(ctx.settings, confirm, "project_delete")
        require_name_echo(name, confirm_name, "project_delete")
        ctx.client.delete_json(f"projects/{name}")
        return {"deleted": name}

    @mcp.tool()
    @tool_result(ctx)
    def project_export(name: str, save_to: str = "") -> dict[str, Any]:
        """Read-only, REST plane. Export a project zip."""
        if not name:
            raise ValidationError("name is required")
        data = ctx.client.get_bytes(f"projects/export/{name}")
        if not save_to:
            return {
                "project": name,
                "size_bytes": len(data),
                "note": "pass save_to=<absolute path ending .zip> to write the archive to disk",
            }
        if not save_to.endswith(".zip"):
            raise ValidationError("save_to must end with .zip")
        Path(save_to).write_bytes(data)
        return {"project": name, "saved_to": save_to, "size_bytes": len(data)}

    @mcp.tool()
    @tool_result(ctx)
    def project_import(
        name: str, zip_path: str, confirm: bool = False, dry_run: bool = False,
        exclusive_access: bool = False, expected_state: str = "", expected_zip_sha256: str = "",
    ) -> dict[str, Any]:
        """Review or import a complete, standalone Ignition project ZIP through REST.

        Review with dry_run=true. Apply with confirm and exclusive_access plus the
        reported base_state and zip_sha256. An absent target uses overwrite=false.
        A replacement requires a verified full backup and a matching live state.
        """
        require_project_import_scope(ctx.settings, name)
        if not dry_run:
            require_project_import_write(ctx.settings, confirm, exclusive_access)
        workspace = Path.cwd().resolve()
        try:
            path = Path(zip_path).resolve(strict=True)
            if not path.is_relative_to(workspace) or path.suffix != ".zip" or not path.is_file():
                raise ValidationError("Supply a project ZIP inside this workspace")
            content = path.read_bytes()
        except (OSError, ValueError) as exc:
            raise ValidationError("Could not read the workspace project ZIP") from exc
        proposed = _import_archive(content)
        project_enabled = _standalone_project(proposed, name)["enabled"]
        checksum = _sha(content)
        expected = _import_readback(proposed)
        with _LOCK:
            before, base_state = _import_state(ctx, name)
            report: dict[str, Any] = {"project": name, "base_state": base_state, "zip_sha256": checksum}
            if dry_run:
                return {**report, "status": "planned", "overwrite": before is not None,
                        "files": sorted(proposed), "enabled": project_enabled}
            if expected_state != base_state or expected_zip_sha256 != checksum:
                raise ProjectImportConflict("Project state or ZIP differs from the reviewed import plan")
            if before is not None:
                try:
                    report["backup"] = _save_backup(workspace / "backups" / "mcp-project-imports", name, before)
                except OSError as exc:
                    raise GatewayError("Could not save and verify the full project backup") from exc
                if _import_state(ctx, name)[1] != base_state:
                    return {**report, "status": "conflict", "message": "Project changed after the backup"}

            try:
                ctx.client.post_bytes(f"projects/import/{name}", content, "application/zip",
                                      params={"overwrite": str(before is not None).lower()})
            except PermissionDeniedError as exc:
                return {**report, "status": "denied", "error": exc.code, "message": exc.message}
            except IgnitionMcpError as exc:
                return {**report, "status": "uncertain", **exc.to_dict()}
            except httpx.TransportError:
                return {**report, "status": "uncertain", "error": "transport_error",
                        "message": "Import disconnected or timed out. Read current state before retrying."}

            try:
                _, actual = _export(ctx, name)
                record = ctx.client.get_json(f"projects/find/{name}")
                if (_import_readback(actual) != expected or not isinstance(record, dict)
                        or record.get("name") != name
                        or record.get("enabled") is not project_enabled):
                    raise ProjectImportConflict("Gateway readback differs from the reviewed project")
            except PermissionDeniedError as exc:
                return {**report, "status": "uncertain", "error": exc.code, "message": exc.message}
            except IgnitionMcpError as exc:
                return {**report, "status": "uncertain", **exc.to_dict()}
            except httpx.TransportError:
                return {**report, "status": "uncertain", "error": "transport_error",
                        "message": "Readback disconnected or timed out. Read current state before retrying."}
            return {**report, "status": "success", "verified": True, "state": _fingerprint(actual)}

    @mcp.tool()
    @tool_result(ctx)
    def project_scan() -> dict[str, Any]:
        """Safe trigger, REST plane. Request a scan of external project files."""
        refuse_in_restricted_deployment(ctx.settings, "project_scan")
        result = ctx.client.scan_projects()
        return {"scan": "requested", "result": result}
