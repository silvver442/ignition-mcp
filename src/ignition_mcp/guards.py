"""Write-guard logic shared by every mutating tool."""

from __future__ import annotations

from .config import PROJECT_NAME, Settings
from .errors import ValidationError, WriteNotAllowedError


def require_writes(settings: Settings, confirm: bool, action: str) -> None:
    """Authorize a write outside the controlled deployment workflow."""
    refuse_in_restricted_deployment(settings, action)
    _require_write_confirmation(settings, confirm, action)


def refuse_in_restricted_deployment(settings: Settings, action: str) -> None:
    if settings.restricted_deployment:
        raise WriteNotAllowedError(
            f"Refusing '{action}': restricted deployment permits writes only via "
            "perspective_deploy and perspective_restore."
        )


def require_deployment_write(
    settings: Settings, project: str, confirm: bool, exclusive_access: bool
) -> None:
    _require_write_confirmation(settings, confirm, "Perspective deployment or restore")
    if not PROJECT_NAME.fullmatch(project):
        raise ValidationError("Invalid project name")
    allowlist = settings.write_project_allowlist
    if allowlist is not None and project not in allowlist:
        raise WriteNotAllowedError(
            "Project is not authorized by IGNITION_WRITE_PROJECT_ALLOWLIST."
        )
    if exclusive_access is not True:
        raise ValidationError("Coordinate exclusive project access before writing")


def _require_write_confirmation(settings: Settings, confirm: bool, action: str) -> None:
    if not settings.allow_writes:
        raise WriteNotAllowedError(
            f"Refusing '{action}': the server is in read-only mode.",
            remediation=(
                "Start ignition-mcp with IGNITION_ALLOW_WRITES=true to enable mutating tools."
            ),
        )
    if confirm is not True:
        raise WriteNotAllowedError(
            f"Refusing '{action}': the call did not set confirm=true.",
            remediation=(
                "Re-call the tool with confirm=true after the user has approved this action."
            ),
        )


def require_name_echo(expected: str, provided: str, action: str) -> None:
    """Destructive ops (delete project/view) must echo the exact target name."""
    if provided != expected:
        raise ValidationError(
            f"Refusing '{action}': confirm_name {provided!r} does not match {expected!r}.",
            remediation="Pass confirm_name equal to the exact name of the target being deleted.",
        )


def require_tag_write_allowed(settings: Settings, tag_paths: list[str]) -> None:
    """Enforce IGNITION_TAG_WRITE_ALLOWLIST when configured."""
    blocked = [p for p in tag_paths if not settings.tag_write_allowed(p)]
    if blocked:
        raise WriteNotAllowedError(
            f"Tag writes blocked by IGNITION_TAG_WRITE_ALLOWLIST: {blocked}",
            remediation=(
                "Only tags matching the allowlist globs may be written. Adjust "
                "IGNITION_TAG_WRITE_ALLOWLIST if this write is intended."
            ),
        )
