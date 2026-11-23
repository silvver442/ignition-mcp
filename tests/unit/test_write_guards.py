"""NFR-S1 gate: every mutating tool must refuse when writes are disabled.

Enumerates all registered tools, calls each one whose signature has a `confirm`
parameter with confirm=True under read-only settings, and asserts it refuses.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest

from ignition_mcp.app import AppContext
from ignition_mcp.config import load_settings
from ignition_mcp.tools import (
    alarms,
    bridge_admin,
    database,
    diagnostics,
    gateway,
    history,
    perspective,
    projects,
    tags,
)

TOOL_MODULES = [
    tags, history, alarms, projects, perspective, gateway, database, bridge_admin, diagnostics
]

# db_query is only mutating for non-SELECT SQL; it takes confirm but a bare call
# with confirm=True and a SELECT is legitimately allowed. Exercise it separately.
SKIP = {"db_query"}


class _FakeMCP:
    """Local stand-in mirroring conftest's FakeMCP, kept self-contained to avoid
    importing the `tests` package (not installed/importable in CI)."""

    def __init__(self) -> None:
        self.tools: dict[str, Callable[..., Any]] = {}

    def tool(self, *args: Any, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[fn.__name__] = fn
            return fn

        return decorator

    def resource(
        self, *args: Any, **kwargs: Any
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            return fn

        return decorator


def _all_tools(ctx: AppContext) -> dict:
    fake = _FakeMCP()
    for m in TOOL_MODULES:
        m.register(fake, ctx)
        if hasattr(m, "register_extra"):
            m.register_extra(fake, ctx)
    return fake.tools


def _dummy_args(fn) -> dict:
    """Provide minimal placeholder args for required (non-confirm) parameters."""
    args = {}
    for name, param in inspect.signature(fn).parameters.items():
        if name == "confirm":
            continue
        if param.default is not inspect.Parameter.empty:
            continue
        anno = param.annotation
        if anno in (list, "list[str]") or "list" in str(anno):
            args[name] = ["x"]
        elif anno in (dict, "dict") or "dict" in str(anno):
            args[name] = {}
        else:
            args[name] = "x"
    return args


def test_all_mutating_tools_refuse_without_writes(settings_readonly, mock_client, mock_bridge):
    ctx = AppContext(settings=settings_readonly, client=mock_client, bridge=mock_bridge)
    ctx.trial_warning = lambda: None  # type: ignore[method-assign]
    tools = _all_tools(ctx)

    mutating = [
        name
        for name, fn in tools.items()
        if "confirm" in inspect.signature(fn).parameters and name not in SKIP
    ]
    assert mutating, "expected to find mutating tools"

    for name in mutating:
        fn = tools[name]
        result = fn(**_dummy_args(fn), confirm=True)
        assert isinstance(result, dict), f"{name} returned non-dict {result!r}"
        assert result.get("error") == "write_not_allowed", (
            f"{name} did not refuse in read-only mode: {result}"
        )


def test_db_query_select_allowed_but_update_refused(settings_readonly, mock_client, mock_bridge):
    ctx = AppContext(settings=settings_readonly, client=mock_client, bridge=mock_bridge)
    ctx.trial_warning = lambda: None  # type: ignore[method-assign]
    tools = _all_tools(ctx)
    update = tools["db_query"](sql="DELETE FROM t")
    assert update["error"] == "write_not_allowed"


def test_restricted_mode_blocks_other_gateway_mutations(app_ctx, mock_client, mock_bridge):
    app_ctx.settings = replace(app_ctx.settings, write_project_allowlist=("Example",))
    tools = _all_tools(app_ctx)
    for name, fn in tools.items():
        if "confirm" in inspect.signature(fn).parameters and name != "db_query":
            result = fn(**_dummy_args(fn), confirm=True)
            assert result["error"] == "write_not_allowed", name

    for result in (
        tools["db_query"](sql="SELECT 1"),
        tools["db_query"](sql="DELETE FROM t", confirm=True),
        tools["db_run_named_query"](project="Example", query_path="Update"),
        tools["project_scan"](),
    ):
        assert result["error"] == "write_not_allowed"
    assert not mock_client.mock_calls
    assert not mock_bridge.mock_calls

    mock_client.get_json.return_value = {"items": ["Example", "Other"]}
    assert tools["project_list"]() == {"projects": ["Example", "Other"]}


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, None),
        ({"IGNITION_RESTRICTED_DEPLOYMENT": "true"}, ()),
        ({"IGNITION_WRITE_PROJECT_ALLOWLIST": ""}, ()),
        ({"IGNITION_WRITE_PROJECT_ALLOWLIST": "Example, Other_2"}, ("Example", "Other_2")),
    ],
)
def test_load_project_write_scope(overrides, expected):
    settings = load_settings({
        "IGNITION_URL": "http://gw:8088", "IGNITION_API_TOKEN": "test:x",
        "IGNITION_ALLOW_WRITES": "true", **overrides,
    })
    assert settings.allow_writes
    assert settings.write_project_allowlist == expected


@pytest.mark.parametrize(
    "overrides",
    [
        {"IGNITION_WRITE_PROJECT_ALLOWLIST": "*"},
        {"IGNITION_WRITE_PROJECT_ALLOWLIST": "Example,../Other"},
        {"IGNITION_WRITE_PROJECT_ALLOWLIST": "Example,"},
        {"IGNITION_RESTRICTED_DEPLOYMENT": "typo"},
    ],
)
def test_invalid_project_write_scope_rejects_configuration(overrides):
    with pytest.raises(ValueError, match="IGNITION_(WRITE_PROJECT_ALLOWLIST|RESTRICTED_DEPLOYMENT)"):
        load_settings({
            "IGNITION_URL": "http://gw:8088", "IGNITION_API_TOKEN": "test:x", **overrides,
        })


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
