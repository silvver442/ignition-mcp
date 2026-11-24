"""Environment-driven configuration with fail-fast validation."""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field

PROJECT_NAME = re.compile(r"[A-Za-z0-9_]+")
TAG_CONFIG_ROOT = re.compile(r"\[default\](?:[A-Za-z0-9_]+/)*[A-Za-z0-9_]+")


@dataclass(frozen=True)
class Settings:
    gateway_url: str
    api_token: str
    bridge_secret: str | None = None
    allow_writes: bool = False
    tls_verify: bool = True
    timeout_s: float = 30.0
    tag_write_allowlist: tuple[str, ...] = field(default_factory=tuple)
    write_project_allowlist: tuple[str, ...] | None = None
    tag_config_allowlist: tuple[str, ...] = ()

    @property
    def restricted_deployment(self) -> bool:
        return self.write_project_allowlist is not None

    @property
    def bridge_enabled(self) -> bool:
        return self.bridge_secret is not None

    def tag_write_allowed(self, tag_path: str) -> bool:
        """True when no allowlist is set, or the path matches one of its globs."""
        if not self.tag_write_allowlist:
            return True
        return any(fnmatch.fnmatch(tag_path, pattern) for pattern in self.tag_write_allowlist)


def _env_bool(env: dict[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def load_settings(environ: dict[str, str] | None = None) -> Settings:
    """Build Settings from environment variables. Raises ValueError listing every
    missing/invalid variable at once so users fix their config in one pass."""
    env = environ if environ is not None else dict(os.environ)
    problems: list[str] = []

    gateway_url = env.get("IGNITION_URL", "").strip().rstrip("/")
    if not gateway_url:
        problems.append("IGNITION_URL is required (e.g. http://localhost:8088)")
    elif not gateway_url.startswith(("http://", "https://")):
        problems.append("IGNITION_URL must start with http:// or https://")

    api_token = env.get("IGNITION_API_TOKEN", "").strip()
    if not api_token:
        problems.append("IGNITION_API_TOKEN is required (format: '<name>:<secret>')")
    elif ":" not in api_token:
        problems.append("IGNITION_API_TOKEN must have the form '<name>:<secret>'")

    timeout_raw = env.get("IGNITION_TIMEOUT_S", "30")
    try:
        timeout_s = float(timeout_raw)
        if timeout_s <= 0:
            raise ValueError
    except ValueError:
        problems.append(f"IGNITION_TIMEOUT_S must be a positive number, got {timeout_raw!r}")
        timeout_s = 30.0

    restricted_raw = env.get("IGNITION_RESTRICTED_DEPLOYMENT", "false").strip().lower()
    if restricted_raw not in ("1", "true", "yes", "on", "0", "false", "no", "off"):
        problems.append("IGNITION_RESTRICTED_DEPLOYMENT must be a boolean")

    project_allowlist = None
    if (
        _env_bool(env, "IGNITION_RESTRICTED_DEPLOYMENT", False)
        or "IGNITION_WRITE_PROJECT_ALLOWLIST" in env
    ):
        raw = env.get("IGNITION_WRITE_PROJECT_ALLOWLIST", "").strip()
        project_allowlist = tuple(name.strip() for name in raw.split(",")) if raw else ()
        if any(not PROJECT_NAME.fullmatch(name) for name in project_allowlist):
            problems.append("IGNITION_WRITE_PROJECT_ALLOWLIST must contain exact project names")

    config_raw = env.get("IGNITION_TAG_CONFIG_ALLOWLIST", "").strip()
    tag_config_allowlist = tuple(path.strip() for path in config_raw.split(",")) if config_raw else ()
    if any(
        not TAG_CONFIG_ROOT.fullmatch(path)
        or {"plc", "_types_"}.intersection(path[9:].lower().split("/"))
        for path in tag_config_allowlist
    ):
        problems.append("IGNITION_TAG_CONFIG_ALLOWLIST must contain exact non-PLC folder paths")

    if problems:
        raise ValueError("Invalid ignition-mcp configuration:\n  - " + "\n  - ".join(problems))

    allowlist_raw = env.get("IGNITION_TAG_WRITE_ALLOWLIST", "").strip()
    allowlist = (
        tuple(p.strip() for p in allowlist_raw.split(",") if p.strip()) if allowlist_raw else ()
    )

    bridge_secret = env.get("IGNITION_BRIDGE_SECRET", "").strip() or None

    return Settings(
        gateway_url=gateway_url,
        api_token=api_token,
        bridge_secret=bridge_secret,
        allow_writes=_env_bool(env, "IGNITION_ALLOW_WRITES", False),
        tls_verify=_env_bool(env, "IGNITION_TLS_VERIFY", True),
        timeout_s=timeout_s,
        tag_write_allowlist=allowlist,
        write_project_allowlist=project_allowlist,
        tag_config_allowlist=tag_config_allowlist,
    )
