"""Project and Perspective tool tests."""

from __future__ import annotations

import json
from unittest.mock import call

import pytest

from ignition_mcp.errors import BridgeUnavailableError, GatewayError, NotFoundError
from ignition_mcp.tools import perspective, projects

MINIMAL_VIEW = {"root": {"type": "ia.container.flex", "meta": {"name": "root"}}}
VIEWS_PREFIX = "Demo/com.inductiveautomation.perspective/views"
PAGE_CONFIG = "Demo/com.inductiveautomation.perspective/page-config/config.json"


def test_project_list_happy(app_ctx, mock_client, make_tools):
    mock_client.get_json.return_value = {"items": [{"name": "P1"}]}
    tools = make_tools(projects)
    result = tools["project_list"]()
    assert result["projects"][0]["name"] == "P1"
    mock_client.get_json.assert_called_once_with("projects/list")


def test_project_create_requires_confirm(app_ctx, make_tools):
    tools = make_tools(projects)
    result = tools["project_create"](name="Demo")
    assert result["error"] == "write_not_allowed"


def test_project_create_bad_name(app_ctx, make_tools):
    tools = make_tools(projects)
    result = tools["project_create"](name="bad name!", confirm=True)
    assert result["error"] == "validation_error"


def test_project_delete_requires_name_echo(app_ctx, make_tools):
    tools = make_tools(projects)
    result = tools["project_delete"](name="Demo", confirm_name="Wrong", confirm=True)
    assert result["error"] == "validation_error"


def test_project_delete_happy(app_ctx, mock_client, make_tools):
    mock_client.delete_json.return_value = None
    tools = make_tools(projects)
    result = tools["project_delete"](name="Demo", confirm_name="Demo", confirm=True)
    assert result["deleted"] == "Demo"
    mock_client.delete_json.assert_called_once_with("projects/Demo")


def test_project_scan(app_ctx, mock_client, make_tools):
    mock_client.scan_projects.return_value = {"status": "ok"}
    tools = make_tools(projects)
    result = tools["project_scan"]()
    assert result["scan"] == "requested"


def test_list_views_strips_paths(app_ctx, mock_bridge, make_tools):
    mock_bridge.call.return_value = {
        "entries": [
            {"path": f"{VIEWS_PREFIX}/Home/view.json", "isDir": False, "size": 10},
            {"path": f"{VIEWS_PREFIX}/sub/Detail/view.json", "isDir": False, "size": 10},
            {"path": f"{VIEWS_PREFIX}/Home/resource.json", "isDir": False, "size": 5},
        ]
    }
    tools = make_tools(perspective)
    result = tools["perspective_list_views"](project="Demo")
    assert result["views"] == ["Home", "sub/Detail"]


def test_get_view_parses_json(app_ctx, mock_bridge, make_tools):
    mock_bridge.call.return_value = {"content": '{"root": {}}'}
    tools = make_tools(perspective)
    result = tools["perspective_get_view"](project="Demo", view_path="Home")
    assert result["view"] == {"root": {}}


def test_upsert_view_requires_confirm(app_ctx, make_tools):
    tools = make_tools(perspective)
    result = tools["perspective_upsert_view"](project="Demo", view_path="Home", view_json={})
    assert result["error"] == "write_not_allowed"


def test_upsert_view_rejects_traversal(app_ctx, make_tools):
    tools = make_tools(perspective)
    result = tools["perspective_upsert_view"](
        project="Demo", view_path="../evil", view_json=MINIMAL_VIEW, confirm=True
    )
    assert result["error"] == "validation_error"


def test_upsert_new_view_writes_manifest_and_scans(mock_client, mock_bridge, make_tools):
    mock_bridge.call.side_effect = [NotFoundError("missing"), NotFoundError("missing"), {}, {}]
    mock_client.scan_projects.return_value = {"scanActive": True}
    tools = make_tools(perspective)
    result = tools["perspective_upsert_view"](
        project="Demo", view_path="Home", view_json=MINIMAL_VIEW, confirm=True
    )
    assert result["deployed"] is True
    write_calls = [c for c in mock_bridge.call.call_args_list if c[0][0] == "files/write"]
    assert [c.args[1]["path"] for c in write_calls] == [
        f"{VIEWS_PREFIX}/Home/view.json",
        f"{VIEWS_PREFIX}/Home/resource.json",
    ]
    assert json.loads(write_calls[1].args[1]["content"])["files"] == ["view.json"]
    mock_client.scan_projects.assert_called_once()


def test_upsert_existing_view_leaves_manifest_untouched(mock_client, mock_bridge, make_tools):
    mock_bridge.call.return_value = {
        "content": '{"files":["view.json","thumbnail.png"],"attributes":{"custom":true}}'
    }
    result = make_tools(perspective)["perspective_upsert_view"](
        "Demo", "area/Detail", MINIMAL_VIEW, confirm=True
    )
    assert result["deployed"] is True
    assert mock_bridge.call.call_args_list == [
        call("files/read", {"path": f"{VIEWS_PREFIX}/area/Detail/resource.json"}),
        call(
            "files/write",
            {
                "path": f"{VIEWS_PREFIX}/area/Detail/view.json",
                "content": json.dumps(MINIMAL_VIEW, indent=2),
            },
        ),
    ]
    mock_client.scan_projects.assert_called_once()


@pytest.mark.parametrize(
    "responses",
    [
        [GatewayError("read failed")],
        [NotFoundError("missing manifest"), GatewayError("read failed")],
        [NotFoundError("missing manifest"), {"content": "{}"}],
        [{}],
    ],
)
def test_upsert_view_blocks_ambiguous_state(responses, mock_client, mock_bridge, make_tools):
    mock_bridge.call.side_effect = responses
    result = make_tools(perspective)["perspective_upsert_view"](
        "Demo", "Home", MINIMAL_VIEW, confirm=True
    )
    assert result["error"] == "gateway_error"
    assert mock_bridge.call.call_count == len(responses)
    assert all(c.args[0] == "files/read" for c in mock_bridge.call.call_args_list)
    mock_client.scan_projects.assert_not_called()


@pytest.mark.parametrize("route", ["/", "/new"])
def test_set_page_preserves_configuration(route, mock_client, mock_bridge, make_tools):
    config = {
        "pages": {"/": {"viewPath": "Home", "docks": {"left": []}}, "/other": {"title": "Other"}},
        "sharedDocks": {"top": [{"viewPath": "Nav"}]},
        "extra": {"flag": True},
    }
    mock_bridge.call.return_value = {"content": json.dumps(config)}
    result = make_tools(perspective)["perspective_page_config_set"](
        "Demo", route, "Updated", title="Updated title", confirm=True
    )
    assert result["page"] == route
    assert mock_bridge.call.call_args_list[0] == call("files/read", {"path": PAGE_CONFIG})
    endpoint, payload = mock_bridge.call.call_args.args
    assert endpoint == "files/write"
    assert payload["path"] == PAGE_CONFIG
    config["pages"].setdefault(route, {}).update(viewPath="Updated", title="Updated title")
    assert json.loads(payload["content"]) == config
    mock_client.scan_projects.assert_called_once()


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (NotFoundError("missing"), "not_found"),
        (GatewayError("read failed"), "gateway_error"),
        (BridgeUnavailableError(), "bridge_unavailable"),
        ({}, "gateway_error"),
        ({"content": "{"}, "gateway_error"),
        ({"content": "[]"}, "gateway_error"),
        ({"content": '{"pages": []}'}, "gateway_error"),
        ({"content": '{"pages": {"/": null}}'}, "gateway_error"),
    ],
)
def test_set_page_blocks_read_errors(response, error, mock_client, mock_bridge, make_tools):
    mock_bridge.call.side_effect = [response]
    result = make_tools(perspective)["perspective_page_config_set"](
        "Demo", "/", "Updated", confirm=True
    )
    assert result["error"] == error
    mock_bridge.call.assert_called_once_with("files/read", {"path": PAGE_CONFIG})
    mock_client.scan_projects.assert_not_called()


@pytest.mark.parametrize(
    ("failure", "error"),
    [(NotFoundError("missing"), "not_found"), (GatewayError("io"), "gateway_error")],
)
def test_get_page_distinguishes_failures(failure, error, mock_bridge, make_tools):
    mock_bridge.call.side_effect = failure
    assert make_tools(perspective)["perspective_page_config_get"]("Demo")["error"] == error


def test_delete_view_requires_name_echo(app_ctx, make_tools):
    tools = make_tools(perspective)
    result = tools["perspective_delete_view"](
        project="Demo", view_path="Home", confirm_name="Nope", confirm=True
    )
    assert result["error"] == "validation_error"
