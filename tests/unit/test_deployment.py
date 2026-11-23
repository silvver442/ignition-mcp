"""Deployment and restore tests using an in-memory Gateway and bridge."""

import io
import json
from dataclasses import replace
from zipfile import ZipFile

import httpx
import pytest

from ignition_mcp.errors import GatewayError, NotFoundError
from ignition_mcp.tools import deployment

PROJECT = "Example"
PREFIX = deployment.PERSPECTIVE
VIEW_DIR = f"{PREFIX}/views/Area/Existing"
NEW_DIR = f"{PREFIX}/views/New"
CONFIG = f"{deployment.PAGE_CONFIG}/config.json"
VIEW = {"root": {"type": "ia.container.flex", "meta": {"name": "root"}}}


def encode(value):
    return json.dumps(value).encode()


@pytest.fixture
def gateway(tmp_path, app_ctx, mock_client, mock_bridge, make_tools, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(deployment, "_VERIFY_DELAY", 0)
    files = {
        "project.json": encode({"title": "Example"}),
        f"{VIEW_DIR}/view.json": encode({"root": {}}),
        f"{VIEW_DIR}/resource.json": encode(
            {"files": ["view.json", "thumbnail.png"], "attributes": {"custom": True}}
        ),
        f"{VIEW_DIR}/thumbnail.png": b"\x89PNG",
        CONFIG: encode(
            {
                "pages": {"/": {"viewPath": "Area/Existing", "docks": {"left": []}}},
                "sharedDocks": {"top": []},
                "custom": True,
            }
        ),
        f"{deployment.PAGE_CONFIG}/resource.json": encode({"files": ["config.json"]}),
    }

    def export(endpoint):
        assert endpoint == f"projects/export/{PROJECT}"
        buffer = io.BytesIO()
        with ZipFile(buffer, "w") as archive:
            for path, data in files.items():
                archive.writestr(path, data)
        return buffer.getvalue()

    def bridge(endpoint, payload):
        assert payload["path"] == PROJECT or payload["path"].startswith(PROJECT + "/")
        path = payload["path"].removeprefix(PROJECT + "/")
        if endpoint == "files/list":
            return {
                "entries": [{"path": f"{PROJECT}/{p}", "isDir": False} for p in files]
            }
        if endpoint == "files/read":
            if path not in files:
                raise NotFoundError("missing")
            return {"content": files[path].decode()}
        if endpoint == "files/write":
            files[path] = payload["content"].encode()
            return {"written": payload["path"]}
        if endpoint == "files/delete":
            assert payload["recursive"] is False
            del files[path]
            return {"deleted": payload["path"]}
        raise AssertionError(endpoint)

    mock_client.get_bytes.side_effect = export
    mock_bridge.call.side_effect = bridge
    return files, make_tools(deployment)


def apply(tools, **kwargs):
    return tools["perspective_deploy"](
        PROJECT, dry_run=False, confirm=True, exclusive_access=True, **kwargs
    )


def changes():
    return {
        "views": [
            deployment.ViewChange(view_path="Area/Existing", view_json=VIEW),
            deployment.ViewChange(view_path="New", view_json=VIEW),
        ],
        "pages": [
            deployment.PageChange(page_url="/", view_path="New", title="New title"),
            deployment.PageChange(page_url="/details", view_path="Area/Existing"),
        ],
    }


def mutations(bridge):
    return [c for c in bridge.call.call_args_list if c.args[0] in {"files/write", "files/delete"}]


def restore(tools, report, **kwargs):
    backup = report["backup"]
    return tools["perspective_restore"](
        PROJECT, backup["id"], backup["sha256"],
        dry_run=False, confirm=True, exclusive_access=True, **kwargs,
    )


def test_successful_batch_and_restore(gateway, mock_bridge, mock_client, app_ctx, make_tools):
    files, _ = gateway
    app_ctx.settings = replace(app_ctx.settings, write_project_allowlist=(PROJECT,))
    tools = make_tools(deployment)
    before = files.copy()
    preview = tools["perspective_deploy"](PROJECT, **changes())
    assert preview["status"] == "dry_run"
    assert not mutations(mock_bridge)
    result = apply(tools, expected_state=preview["base_state"], **changes())
    assert result["status"] == "success", result
    assert result["verified"] == result["planned"]
    assert result["perspective_runtime"] == "not_tested"
    assert files[f"{VIEW_DIR}/resource.json"] == before[f"{VIEW_DIR}/resource.json"]
    assert files[f"{VIEW_DIR}/thumbnail.png"] == before[f"{VIEW_DIR}/thumbnail.png"]
    assert json.loads(files[CONFIG]) == {
        "pages": {
            "/": {"viewPath": "New", "title": "New title", "docks": {"left": []}},
            "/details": {"viewPath": "Area/Existing", "title": "Area/Existing"},
        },
        "sharedDocks": {"top": []},
        "custom": True,
    }
    assert result["restore_available"]
    restored = restore(tools, result)
    assert restored["status"] == "success", restored
    assert files == before
    assert mock_client.scan_projects.call_count == 2
    mock_client.post_bytes.assert_not_called()


@pytest.mark.parametrize("tool", ["perspective_deploy", "perspective_restore"])
@pytest.mark.parametrize(
    ("allowlist", "project", "confirm", "exclusive_access", "error"),
    [
        ((), PROJECT, True, True, "write_not_allowed"),
        ((PROJECT,), "Other", True, True, "write_not_allowed"),
        ((PROJECT,), PROJECT.lower(), True, True, "write_not_allowed"),
        ((PROJECT,), "../Other", True, True, "validation_error"),
        ((PROJECT,), PROJECT, False, True, "write_not_allowed"),
        ((PROJECT,), PROJECT, True, False, "validation_error"),
    ],
)
def test_restricted_write_denied_before_gateway(
    app_ctx, make_tools, mock_client, mock_bridge, tool, allowlist, project,
    confirm, exclusive_access, error,
):
    app_ctx.settings = replace(app_ctx.settings, write_project_allowlist=allowlist)
    tools = make_tools(deployment)
    args = {"backup_id": "0" * 32, "backup_sha256": "0" * 64} if tool.endswith("restore") else {}
    result = tools[tool](
        project, dry_run=False, confirm=confirm, exclusive_access=exclusive_access, **args
    )
    assert result["status"] == "error"
    assert result["error"] == error
    assert result["attempted"] == []
    assert not mock_client.mock_calls
    assert not mock_bridge.mock_calls


def test_restricted_empty_allowlist_still_allows_preview(gateway, app_ctx, make_tools, mock_bridge):
    app_ctx.settings = replace(app_ctx.settings, write_project_allowlist=())
    result = make_tools(deployment)["perspective_deploy"](PROJECT, **changes())
    assert result["status"] == "dry_run"
    assert not mutations(mock_bridge)


@pytest.mark.parametrize("view_path", ["../Other", ".. /Other", "Area/Name.", "Area/NUL"])
def test_restricted_write_rejects_unsafe_view_path(
    gateway, app_ctx, make_tools, mock_bridge, mock_client, tmp_path, view_path
):
    app_ctx.settings = replace(app_ctx.settings, write_project_allowlist=(PROJECT,))
    result = apply(
        make_tools(deployment), views=[deployment.ViewChange(view_path=view_path, view_json=VIEW)]
    )
    assert result["status"] == "error"
    assert result["attempted"] == []
    assert not (tmp_path / "backups").exists()
    assert not mutations(mock_bridge)
    mock_client.scan_projects.assert_not_called()


def test_create_page_config_resource(gateway):
    files, tools = gateway
    del files[CONFIG]
    manifest = f"{deployment.PAGE_CONFIG}/resource.json"
    del files[manifest]
    result = apply(tools, pages=[deployment.PageChange(page_url="/", view_path="Area/Existing")])
    assert result["status"] == "success", result
    assert json.loads(files[manifest])["files"] == ["config.json"]
    assert restore(tools, result)["status"] == "success"
    assert CONFIG not in files


@pytest.mark.parametrize("failure", ["corrupt_zip", "missing_asset", "disk"])
def test_backup_failure_blocks_every_write(gateway, mock_bridge, mock_client, monkeypatch, failure):
    files, tools = gateway
    if failure == "corrupt_zip":
        mock_client.get_bytes.side_effect = None
        mock_client.get_bytes.return_value = b"not a zip"
    elif failure == "missing_asset":
        del files[f"{VIEW_DIR}/thumbnail.png"]
    else:
        def fail(*args):
            raise OSError("disk full")
        monkeypatch.setattr(deployment, "_write_private", fail)
    result = apply(tools, **changes())
    assert result["status"] == "error"
    assert not mutations(mock_bridge)
    mock_client.scan_projects.assert_not_called()


def test_invalid_configuration_json_blocks_deployment(gateway, mock_bridge):
    files, tools = gateway
    files[CONFIG] = b"{"
    result = apply(tools, **changes())
    assert result["status"] == "error"
    assert not mutations(mock_bridge)


@pytest.mark.parametrize("failure", [GatewayError("write failed"), httpx.ReadTimeout("timeout")])
def test_partial_write_is_reported_without_retry(gateway, mock_bridge, mock_client, failure):
    _, tools = gateway
    original = mock_bridge.call.side_effect

    def bridge(endpoint, payload):
        if endpoint == "files/write" and f"/{NEW_DIR}/" in payload["path"]:
            raise failure
        return original(endpoint, payload)

    mock_bridge.call.side_effect = bridge
    result = apply(tools, **changes())
    assert result["status"] == "uncertain"
    assert result["read_back"] == [f"{VIEW_DIR}/view.json"]
    assert result["attempted"] == [f"{VIEW_DIR}/view.json", f"{NEW_DIR}/view.json"]
    assert result["backup"]["sha256"]
    assert result["verified"] == []
    assert not result["restore_available"]
    assert len(mutations(mock_bridge)) == 2
    assert restore(tools, result)["status"] == "error"
    assert len(mutations(mock_bridge)) == 2
    mock_client.scan_projects.assert_not_called()


def test_conflict_between_backup_and_first_write(gateway, mock_bridge, mock_client):
    files, tools = gateway
    export = mock_client.get_bytes.side_effect
    calls = 0

    def edited(endpoint):
        nonlocal calls
        calls += 1
        if calls == 2:
            files["project.json"] = b'{"title":"Edited"}'
        return export(endpoint)

    mock_client.get_bytes.side_effect = edited
    result = apply(tools, **changes())
    assert result["error"] == "deployment_conflict"
    assert result["backup"]["id"]
    assert not mutations(mock_bridge)


def test_stale_preview_and_missing_confirmation(gateway, mock_bridge):
    files, tools = gateway
    preview = tools["perspective_deploy"](PROJECT, **changes())
    files["project.json"] = b'{"title":"Edited"}'
    assert apply(tools, expected_state=preview["base_state"], **changes())["status"] == "error"
    refused = tools["perspective_deploy"](PROJECT, dry_run=False, **changes())
    assert refused["error"] == "write_not_allowed"
    assert not mutations(mock_bridge)


def test_changed_live_manifest_stops_batch(gateway, mock_bridge, mock_client):
    files, tools = gateway
    original = mock_bridge.call.side_effect
    manifest = f"{VIEW_DIR}/resource.json"

    def bridge(endpoint, payload):
        result = original(endpoint, payload)
        if endpoint == "files/write":
            files[manifest] = encode({"files": ["view.json", "thumbnail.png"], "edited": True})
        return result

    mock_bridge.call.side_effect = bridge
    result = apply(tools, **changes())
    assert result["error"] == "deployment_conflict"
    assert result["read_back"] == [f"{VIEW_DIR}/view.json"]
    assert len(mutations(mock_bridge)) == 1
    mock_client.scan_projects.assert_not_called()


def update_scan_metadata(files, path, suffix):
    manifest = json.loads(files[path])
    attributes = manifest.setdefault("attributes", {})
    attributes.setdefault("lastModification", {"actor": "external"})["timestamp"] = (
        f"2026-11-23T16:29:{suffix}Z"
    )
    attributes["lastModificationSignature"] = suffix * 32
    files[path] = encode(manifest)


def test_scan_metadata_changes_allow_deployment_and_scoped_restore(
    gateway, mock_client, mock_bridge
):
    files, tools = gateway
    manifests = [f"{VIEW_DIR}/resource.json", f"{deployment.PAGE_CONFIG}/resource.json"]
    for path in manifests:
        update_scan_metadata(files, path, "04")
    before = files.copy()

    def scan():
        for path in manifests:
            update_scan_metadata(files, path, "05")

    mock_client.scan_projects.side_effect = scan
    preview = tools["perspective_deploy"](PROJECT, **changes())
    result = apply(tools, expected_state=preview["base_state"], **changes())
    assert result["status"] == "success", result
    assert result["verified"] == result["planned"]
    assert result["restore_available"]
    after = files.copy()
    mock_bridge.call.reset_mock()
    restored = restore(tools, result)
    assert restored["status"] == "success", restored
    assert files == {**before, **{path: after[path] for path in manifests}}
    assert not any(
        call.args[1]["path"] in {f"{PROJECT}/{path}" for path in manifests}
        for call in mutations(mock_bridge)
    )


@pytest.mark.parametrize("stage", ["deploy", "restore"])
def test_scan_metadata_changes_do_not_relax_state_guards(gateway, mock_bridge, stage):
    files, tools = gateway
    path = f"{VIEW_DIR}/resource.json"
    update_scan_metadata(files, path, "04")
    if stage == "deploy":
        preview = tools["perspective_deploy"](PROJECT, **changes())
    else:
        deployed = apply(tools, **changes())
        assert deployed["restore_available"]
    update_scan_metadata(files, path, "05")
    mock_bridge.call.reset_mock()
    if stage == "deploy":
        refused = apply(tools, expected_state=preview["base_state"], **changes())
    else:
        refused = restore(tools, deployed)
    assert refused["error"] == "deployment_conflict"
    assert not mutations(mock_bridge)


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("project.json", b'{"title":"Concurrent change"}'),
        (f"{VIEW_DIR}/thumbnail.png", b"Changed"),
        (f"{NEW_DIR}/view.json", encode({"root": {}})),
        (CONFIG, encode({"pages": {}})),
        (f"{VIEW_DIR}/resource.json", encode({"files": ["view.json"], "attributes": {
            "custom": True,
        }})),
        (f"{VIEW_DIR}/resource.json", encode({"files": ["view.json", "thumbnail.png"],
            "attributes": {"custom": True, "lastModification": {"actor": "designer"}},
        })),
        ("unexpected.txt", b"New file"),
    ],
)
def test_post_scan_verification_checks_other_resources(gateway, mock_client, path, content):
    files, tools = gateway

    def scan():
        files[path] = content

    mock_client.scan_projects.side_effect = scan
    result = apply(tools, **changes())
    assert result["status"] == "uncertain"
    assert result["phase"] == "verification"
    assert result["read_back"] == result["planned"]
    assert not result["verified"]
    assert not result["restore_available"]


@pytest.mark.parametrize("later_edit", [False, True])
def test_restore_refuses_bad_checksum_or_later_edits(gateway, mock_bridge, later_edit):
    files, tools = gateway
    result = apply(tools, **changes())
    assert result["status"] == "success", result
    mock_bridge.call.reset_mock()
    if later_edit:
        files["project.json"] = b'{"title":"Edited later"}'
    else:
        result["backup"]["sha256"] = "0" * 64
    refused = restore(tools, result)
    assert refused["error"] == "deployment_conflict"
    assert not mutations(mock_bridge)
