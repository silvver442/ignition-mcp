"""Deployment and restore tests using an in-memory Gateway and bridge."""

import io
import json
from base64 import b64decode
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
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
PNG = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aG2cAAAAASUVORK5CYII="
)
CHANGED_PNG = PNG[:-1] + bytes([PNG[-1] ^ 1])


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
        f"{VIEW_DIR}/thumbnail.png": PNG,
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
            if payload.get("format") == "sha256":
                return {"sha256": sha256(files[path]).hexdigest()}
            try:
                return {"content": files[path].decode()}
            except UnicodeDecodeError as exc:
                raise GatewayError("Bridge files/read requires UTF-8") from exc
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


@pytest.mark.parametrize(("difference", "classification", "fields"), [
    ("scope", "substantive_difference", ["scope"]),
    ("private_attribute", "substantive_difference", []),
    ("invalid", "invalid_manifest", None),
    ("invalid_files", "invalid_manifest", None),
    ("invalid_metadata", "invalid_manifest", None),
    ("missing", "missing_manifest", None),
])
def test_dry_run_reports_manifest_conflicts_without_exposing_values(
    gateway, mock_bridge, mock_client, difference, classification, fields,
):
    files, tools = gateway
    path = f"{VIEW_DIR}/resource.json"
    update_scan_metadata(files, path, "04")
    exported = files[path]
    edited = json.loads(exported)
    live = encode(edited)
    if difference == "scope":
        live = encode({**edited, "scope": "C"})
    elif difference == "private_attribute":
        edited["attributes"]["private-key"] = "private-value"
        live = encode(edited)
    elif difference == "invalid":
        live = b"invalid-manifest-content"
    elif difference == "invalid_files":
        live = encode({**edited, "files": "view.json"})
    elif difference == "invalid_metadata":
        edited["attributes"]["lastModification"]["timestamp"] = 42
        live = encode(edited)
    elif difference == "missing":
        live = None
    original = mock_bridge.call.side_effect

    def bridge(endpoint, payload):
        if endpoint == "files/read" and payload["path"] == f"{PROJECT}/{path}":
            if live is None:
                raise NotFoundError("missing")
            return {"content": live.decode()}
        return original(endpoint, payload)

    mock_bridge.call.side_effect = bridge
    preview = tools["perspective_deploy"](PROJECT, **changes())
    assert preview["error"] == "deployment_conflict"
    assert preview["message"] == f"Unexpected live content at {path}"
    assert preview["manifest_comparison"] == {
        "path": path, "classification": classification,
        "export_sha256": deployment._sha(exported),
        "bridge_sha256": deployment._sha(live) if live is not None else None,
        **({"differing_known_fields": fields} if fields is not None else {}),
    }
    for private in ("private-key", "private-value", "invalid-manifest-content"):
        assert private not in json.dumps(preview)
    result = apply(tools, expected_state=preview["base_state"], **changes())
    assert result["error"] == "deployment_conflict"
    assert "manifest_comparison" not in result
    assert not mutations(mock_bridge)
    mock_client.scan_projects.assert_not_called()


@pytest.mark.parametrize("filename", [None, "view.json", "thumbnail.png"])
def test_initial_bridge_comparison_tolerates_only_manifest_metadata(
    gateway, mock_bridge, mock_client, filename,
):
    files, tools = gateway
    manifest = f"{VIEW_DIR}/resource.json"
    update_scan_metadata(files, manifest, "04")
    bridge_files = {manifest: files[manifest]}
    update_scan_metadata(bridge_files, manifest, "05")
    if filename is not None:
        drifted = f"{VIEW_DIR}/{filename}"
        bridge_files[drifted] = CHANGED_PNG if filename == "thumbnail.png" else files[drifted] + b" "
    original = mock_bridge.call.side_effect

    def bridge(endpoint, payload):
        path = payload["path"].removeprefix(PROJECT + "/")
        if endpoint == "files/read" and path in bridge_files:
            if path.endswith("/thumbnail.png"):
                assert payload.get("format") == "sha256"
                return {"sha256": sha256(bridge_files[path]).hexdigest()}
            return {"content": bridge_files[path].decode()}
        return original(endpoint, payload)

    mock_bridge.call.side_effect = bridge
    edit = {"views": [deployment.ViewChange(view_path="Area/Existing", view_json=VIEW)]}
    preview = tools["perspective_deploy"](PROJECT, **edit)
    assert not mutations(mock_bridge)
    result = apply(tools, expected_state=preview["base_state"], **edit)
    if filename is None:
        assert preview["status"] == "dry_run", preview
        assert result["status"] == "success", result
        assert result["verified"] == [f"{VIEW_DIR}/view.json"]
        assert files[f"{VIEW_DIR}/thumbnail.png"] == PNG
        assert [call.args[1]["path"] for call in mutations(mock_bridge)] == [
            f"{PROJECT}/{VIEW_DIR}/view.json",
        ]
    else:
        for report in (preview, result):
            assert report["error"] == "deployment_conflict", report
            assert report["message"] == f"Unexpected live content at {drifted}"
        assert not mutations(mock_bridge)
        mock_client.scan_projects.assert_not_called()


@pytest.mark.parametrize("filename", ["resource.json", "view.json", "thumbnail.png"])
def test_bridge_change_after_initial_preflight_blocks_first_write(
    gateway, mock_bridge, mock_client, filename,
):
    files, tools = gateway
    path = f"{VIEW_DIR}/{filename}"
    if filename == "resource.json":
        update_scan_metadata(files, path, "04")
        live = {path: files[path]}
        update_scan_metadata(live, path, "05")
        initial = live[path]
        update_scan_metadata(live, path, "06")
        changed = live[path]
    else:
        initial = files[path]
        changed = CHANGED_PNG if filename == "thumbnail.png" else initial + b" "
    original = mock_bridge.call.side_effect
    reads = 0

    def bridge(endpoint, payload):
        nonlocal reads
        if endpoint == "files/read" and payload["path"] == f"{PROJECT}/{path}":
            reads += 1
            data = initial if reads == 1 else changed
            if filename == "thumbnail.png":
                assert payload.get("format") == "sha256"
                return {"sha256": sha256(data).hexdigest()}
            return {"content": data.decode()}
        return original(endpoint, payload)

    mock_bridge.call.side_effect = bridge
    result = apply(tools, views=[deployment.ViewChange(view_path="Area/Existing", view_json=VIEW)])
    assert result["error"] == "deployment_conflict", result
    assert result["message"] == f"Unexpected live content at {path}"
    assert reads == 2
    assert not mutations(mock_bridge)
    mock_client.scan_projects.assert_not_called()


@pytest.mark.parametrize("failure", ["missing_manifest", "invalid_manifest"])
def test_incomplete_resource_blocks_deployment(gateway, mock_bridge, mock_client, failure):
    files, tools = gateway
    path = f"{VIEW_DIR}/resource.json"
    if failure == "missing_manifest":
        del files[path]
    else:
        files[path] = b"{}"
    edit = {"views": [deployment.ViewChange(view_path="Area/Existing", view_json=VIEW)]}
    for report in (tools["perspective_deploy"](PROJECT, **edit), apply(tools, **edit)):
        assert report["status"] == "error", report
        assert path in report["message"] or VIEW_DIR in report["message"]
        assert not report["attempted"]
    assert not mutations(mock_bridge)
    mock_client.scan_projects.assert_not_called()


@pytest.mark.parametrize(("failure", "code"), [
    ("missing", "deployment_conflict"),
    ("legacy_text", "gateway_error"),
    ("malformed_digest", "gateway_error"),
    ("unreadable", "gateway_error"),
])
def test_unverified_binary_file_blocks_deployment(gateway, mock_bridge, mock_client, failure, code):
    _, tools = gateway
    path = f"{VIEW_DIR}/thumbnail.png"
    original = mock_bridge.call.side_effect

    def bridge(endpoint, payload):
        if endpoint == "files/read" and payload["path"] == f"{PROJECT}/{path}":
            assert payload.get("format") == "sha256"
            if failure == "missing":
                raise NotFoundError("missing")
            if failure == "unreadable":
                raise GatewayError("read failed")
            if failure == "legacy_text":
                return {"content": "unexpected text response"}
            return {"sha256": "z" * 64}
        return original(endpoint, payload)

    mock_bridge.call.side_effect = bridge
    edit = {"views": [deployment.ViewChange(view_path="Area/Existing", view_json=VIEW)]}
    for report in (tools["perspective_deploy"](PROJECT, **edit), apply(tools, **edit)):
        assert report["error"] == code, report
        assert path in report["message"]
    assert not mutations(mock_bridge)
    mock_client.scan_projects.assert_not_called()


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


@pytest.fixture
def side_nav(gateway):
    files, _ = gateway
    config = json.loads(files[CONFIG])
    config["pages"]["/codex-test"] = {"viewPath": "CodexMCPTest"}
    config["sharedDocks"].update(
        cornerPriority="top-bottom", top=[{"viewPath": "TopNav", "size": 68}],
        left=[{"viewPath": "ExistingWest", "id": "other"}],
    )
    files[CONFIG] = encode(config)
    files[deployment._SESSION_PATH] = encode({
        "custom": {"counter": 42},
        "props": {"locale": "pl-PL"},
        "propConfig": {"custom.counter": {"persistent": True}, "props.id": {"access": "Private"}},
    })
    files[deployment._SESSION_MANIFEST] = encode({
        "scope": "G", "version": 1, "restricted": False, "overridable": True,
        "files": ["props.json"],
        "attributes": {"lastModification": {"actor": "designer", "timestamp": "2026-11-19T20:51:40Z"}},
    })
    return {
        "configure_side_nav": True,
        "views": [
            deployment.ViewChange(view_path=path, view_json=VIEW)
            for path in ("SideNav", "Shared/SideNavArea")
        ],
    }


@pytest.mark.parametrize("difference", [
    "metadata", "actor", "manifest", "config", "session_manifest", "stale", "concurrent",
])
def test_side_nav_export_bridge_manifest_comparison(
    gateway, side_nav, mock_bridge, mock_client, difference,
):
    files, tools = gateway
    page_manifest = f"{deployment.PAGE_CONFIG}/resource.json"
    update_scan_metadata(files, page_manifest, "04")
    update_scan_metadata(files, deployment._SESSION_MANIFEST, "04")
    original = mock_bridge.call.side_effect
    accepted_preview = difference in {"metadata", "session_manifest", "stale", "concurrent"}
    wrote = False

    def bridge(endpoint, payload):
        nonlocal wrote
        response = original(endpoint, payload)
        path = payload["path"].removeprefix(PROJECT + "/")
        if endpoint == "files/write":
            wrote = True
        if endpoint != "files/read":
            return response
        if path == CONFIG and difference == "config":
            config = json.loads(response["content"])
            config["pages"]["/"]["title"] = "Edited"
            return {"content": encode(config).decode()}
        if path == page_manifest or (
            path == deployment._SESSION_MANIFEST and difference == "session_manifest"
        ):
            live = {path: response["content"].encode()}
            update_scan_metadata(live, path, "06" if difference == "concurrent" and wrote else "05")
            manifest = json.loads(live[path])
            if difference == "actor":
                manifest["attributes"]["lastModification"]["actor"] = "another-actor"
            elif difference == "manifest":
                manifest["overridable"] = False
            return {"content": encode(manifest).decode()}
        return response

    mock_bridge.call.side_effect = bridge
    preview = tools["perspective_deploy"](PROJECT, **side_nav)
    assert not mutations(mock_bridge)
    if accepted_preview:
        assert preview["status"] == "dry_run", preview
        assert set(preview["planned"]) == {CONFIG, deployment._SESSION_PATH} | {
            f"{PREFIX}/views/{view.view_path}/{name}"
            for view in side_nav["views"] for name in ("view.json", "resource.json")
        }
    else:
        assert preview["error"] == "deployment_conflict", preview
    if difference == "stale":
        files["project.json"] = b'{"title":"Edited"}'
    result = apply(tools, expected_state=preview["base_state"], **side_nav)

    if difference in {"metadata", "session_manifest"}:
        assert result["status"] == "success", result
        assert result["verified"] == preview["planned"]
        assert page_manifest not in result["attempted"]
        return
    assert result["error"] == "deployment_conflict", result
    assert len(mutations(mock_bridge)) == (1 if difference == "concurrent" else 0)
    mock_client.scan_projects.assert_not_called()
    if difference == "stale":
        assert "expected_state" in result["message"]
    else:
        path = CONFIG if difference == "config" else page_manifest
        assert result["message"] == f"Unexpected live content at {path}"


def test_side_nav_plan_apply_and_restore(gateway, side_nav, mock_bridge, mock_client):
    files, tools = gateway
    before = files.copy()

    def scan():
        manifest = json.loads(files[deployment._SESSION_MANIFEST])
        manifest["attributes"]["lastModification"] = {
            "actor": "external", "timestamp": "2026-11-23T18:00:00Z",
        }
        files[deployment._SESSION_MANIFEST] = encode(manifest)

    mock_client.scan_projects.side_effect = scan
    preview = tools["perspective_deploy"](PROJECT, **side_nav)
    assert preview["status"] == "dry_run"
    assert {CONFIG, deployment._SESSION_PATH} <= set(preview["planned"])
    assert not mutations(mock_bridge)
    result = apply(tools, expected_state=preview["base_state"], **side_nav)
    assert result["status"] == "success", result
    assert before == deployment._archive(Path(result["backup"]["path"]).read_bytes())
    original = json.loads(before[CONFIG])
    config = json.loads(files[CONFIG])
    dock = config["sharedDocks"]["left"].pop()
    assert dock == {
        "anchor": "fixed", "autoBreakpoint": 480, "content": "push", "handle": "hide",
        "iconUrl": "", "id": "SideNav", "modal": False, "resizable": False,
        "show": "visible", "size": 232, "viewParams": {}, "viewPath": "SideNav",
    }
    assert config == original
    session = json.loads(files[deployment._SESSION_PATH])
    assert session["custom"].pop("scadaSelection") == {
        "line": "Line_1", "tagPath": "Line_1/Line/Ignition",
    }
    assert session == json.loads(before[deployment._SESSION_PATH])
    assert tools["perspective_deploy"](PROJECT, **side_nav)["planned"] == []
    assert restore(tools, result)["status"] == "success"
    assert files.keys() == before.keys()
    assert all(value == files[path] for path, value in before.items()
               if path != deployment._SESSION_MANIFEST)


@pytest.mark.parametrize("conflict", ["dock", "selection", "binding", "manifest", "stale", "no_state"])
def test_side_nav_rejects_conflicts(gateway, side_nav, mock_bridge, conflict):
    files, tools = gateway
    plan = tools["perspective_deploy"](PROJECT, **side_nav)
    session = json.loads(files[deployment._SESSION_PATH])
    if conflict == "dock":
        config = json.loads(files[CONFIG])
        config["sharedDocks"]["right"] = [{"id": "SideNav", "viewPath": "Other"}]
        files[CONFIG] = encode(config)
    elif conflict == "selection":
        session["custom"]["scadaSelection"] = None
    elif conflict == "binding":
        session["propConfig"]["custom.scadaSelection.tagPath"] = {"binding": {}}
    elif conflict == "manifest":
        del files[deployment._SESSION_MANIFEST]
    elif conflict == "stale":
        session["custom"]["counter"] = 43
    files[deployment._SESSION_PATH] = encode(session)
    kwargs = {"expected_state": plan["base_state"]} if conflict == "stale" else {}
    result = (
        apply(tools, **kwargs, **side_nav) if conflict in {"stale", "no_state"}
        else tools["perspective_deploy"](PROJECT, **side_nav)
    )
    assert result["status"] == "error"
    if conflict == "no_state":
        assert "expected_state" in result["message"]
    else:
        assert result["error"] == "deployment_conflict"
    assert not mutations(mock_bridge)


def test_partial_side_nav_write_refuses_automatic_restore(gateway, side_nav, mock_bridge, mock_client):
    files, tools = gateway
    before = files.copy()
    bridge = mock_bridge.call.side_effect

    def fail_after_write(endpoint, payload):
        result = bridge(endpoint, payload)
        if endpoint == "files/write" and payload["path"].endswith(deployment._SESSION_PATH):
            raise httpx.ReadTimeout("session write acknowledgement lost")
        return result

    plan = tools["perspective_deploy"](PROJECT, **side_nav)
    mock_bridge.call.side_effect = fail_after_write
    result = apply(tools, expected_state=plan["base_state"], **side_nav)
    assert result["status"] == "uncertain"
    assert result["attempted"] == plan["planned"]
    assert result["read_back"][-1] == CONFIG
    assert not result["restore_available"]
    assert before == deployment._archive(Path(result["backup"]["path"]).read_bytes())
    assert restore(tools, result)["status"] == "error"
    assert len(mutations(mock_bridge)) == len(plan["planned"])
    mock_client.scan_projects.assert_not_called()
