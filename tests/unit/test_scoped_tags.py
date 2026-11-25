from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from unittest.mock import Mock

import pytest
from mcp.server.fastmcp import FastMCP

from ignition_mcp.config import load_settings
from ignition_mcp.errors import GatewayError
from ignition_mcp.tools import scoped_tags, tags

from .conftest import FakeMCP

ROOT = "[default]Line_1/Cupper/Ignition"
PATH = f"{ROOT}/Outfeed_Counter"
MEMORY = {"name": "Outfeed_Counter", "tagType": "AtomicTag", "dataType": "Int4",
          "valueSource": "memory", "historyEnabled": False, "value": 0}


@pytest.fixture
def scope(app_ctx, mock_bridge, tmp_path):
    app_ctx.settings = replace(app_ctx.settings, write_project_allowlist=("Test_Gemma_v1",),
                               tag_config_allowlist=(ROOT,))
    mock_bridge.ping.return_value = {"capabilities": sorted(scoped_tags.SCOPED_ENDPOINTS)}
    mcp = FakeMCP()
    scoped_tags.register(mcp, app_ctx, tmp_path)
    return mcp.tools


def snap(config, *, path=PATH, value=0):
    return {"path": path, "config": config, "effective": config or {},
            "value": value if config else None, "valueGood": config is not None}


@pytest.mark.parametrize("path", [
    "[default]Line_1/Cupper/PLC/Outfeed_Counter",
    "[default]Line_1/BM1/Ignition/Outfeed_Counter",
    "[default]Line_1/Cupper/Ignition2/Outfeed_Counter",
    ROOT + "/Nested/Outfeed_Counter", ROOT + "/../Outfeed_Counter",
    ROOT + "/Outfeed_Counter.value", ROOT + "/PLC", ROOT + "/",
    "[other]Line_1/Cupper/Ignition/Outfeed_Counter",
])
def test_deny_outside_scope(scope, mock_bridge, path):
    assert scope["tag_configure_scoped"](path, "create", confirm=True)["error"] in (
        "validation_error", "write_not_allowed",
    )
    mock_bridge.ping.assert_not_called()
    mock_bridge.call.assert_not_called()


@pytest.mark.parametrize("allow_writes,confirm,allowlist", [
    (False, True, (ROOT,)), (True, False, (ROOT,)), (True, True, ()),
])
def test_requires_three_write_gates(scope, app_ctx, mock_bridge, allow_writes, confirm, allowlist):
    app_ctx.settings = replace(app_ctx.settings, allow_writes=allow_writes,
                               tag_config_allowlist=allowlist)
    assert scope["tag_configure_scoped"](PATH, "create", confirm=confirm)["error"] == (
        "write_not_allowed"
    )
    mock_bridge.call.assert_not_called()


def test_generic_writes_remain_disabled(scope, make_tools, mock_bridge):
    generic = make_tools(tags)
    assert generic["tag_create"](ROOT, [MEMORY], confirm=True)["error"] == "write_not_allowed"
    assert generic["tag_config_import"]("default", ROOT, {}, confirm=True)["error"] == "write_not_allowed"
    assert generic["tag_write"]([{"path": PATH, "value": 1}], confirm=True)["error"] == (
        "write_not_allowed"
    )
    mock_bridge.call.assert_not_called()


def test_existing_tag_is_read_but_never_overwritten(scope, mock_bridge, tmp_path):
    mock_bridge.call.return_value = snap({**MEMORY, "historyEnabled": True}, value=91)
    result = scope["tag_configure_scoped"](PATH, "create", confirm=True)
    assert result["status"] == "already_exists"
    assert result["tag"]["config"]["historyEnabled"] is True
    assert result["tag"]["value"] == 91
    assert not list(tmp_path.iterdir())
    mock_bridge.call.assert_called_once_with("tags/scoped-state", {"path": PATH})


def test_create_requires_verified_backup_and_independent_readback(scope, mock_bridge, tmp_path):
    readback = snap(MEMORY)

    def bridge(endpoint, payload):
        assert payload["path"] == PATH
        if endpoint == "tags/scoped-state":
            return snap(None) if mock_bridge.call.call_count == 1 else readback
        saved = list(tmp_path.glob("*/before.json"))
        assert len(saved) == 1
        assert json.loads(saved[0].read_bytes())["snapshot"] == snap(None)
        assert endpoint == "tags/scoped-configure"
        assert payload == {"path": PATH, "action": "create", "expectedConfig": None,
                           "dataType": "Int4", "value": 0}
        return readback

    mock_bridge.call.side_effect = bridge
    result = scope["tag_configure_scoped"](PATH, "create", value=0, confirm=True)
    assert (result["status"], result["verified"], result["restore_available"]) == (
        "success", True, True,
    )
    assert [c.args[0] for c in mock_bridge.call.call_args_list] == [
        "tags/scoped-state", "tags/scoped-configure", "tags/scoped-state",
    ]


def test_patch_preserves_historian_and_other_properties_and_can_restore(scope, mock_bridge):
    before = snap({**MEMORY, "historyEnabled": True, "historyProvider": "Postgres_Historian",
                   "sampleMode": "Periodic", "historySampleRateUnits": "SEC",
                   "historySampleRate": 30, "documentation": "do not change"})
    after = snap({**before["config"], "historySampleRate": 10})
    mock_bridge.call.side_effect = [before, before, after, after, after, before, before]
    state = scope["tag_config_get"](PATH)["state"]
    result = scope["tag_configure_scoped"](
        PATH, "history", history=scoped_tags.HistoryPatch(historySampleRate=10),
        expected_state=state, confirm=True,
    )
    assert result["status"] == "success"
    assert mock_bridge.call.call_args_list[2].args[1]["history"] == {"historySampleRate": 10}
    saved = result["backup"]
    restored = scope["tag_config_restore"](PATH, saved["id"], saved["sha256"], confirm=True)
    assert restored["status"] == "success"
    assert mock_bridge.call.call_args_list[5].args[1]["beforeConfig"] == before["config"]


def test_block_write_if_backup_fails_or_bridge_has_no_restore(scope, mock_bridge, monkeypatch):
    mock_bridge.ping.return_value = {"capabilities": ["tags/scoped-state", "tags/scoped-configure"]}
    assert scope["tag_configure_scoped"](PATH, "create", confirm=True)["attempted"] is False
    mock_bridge.call.assert_not_called()
    mock_bridge.ping.return_value = {"capabilities": list(scoped_tags.SCOPED_ENDPOINTS)}
    mock_bridge.call.return_value = snap(None)
    monkeypatch.setattr(scoped_tags, "_private_write", Mock(side_effect=OSError("disk full")))
    assert scope["tag_configure_scoped"](PATH, "create", confirm=True)["attempted"] is False
    mock_bridge.call.assert_called_once_with("tags/scoped-state", {"path": PATH})


def test_no_restore_after_uncertain_write(scope, mock_bridge):
    mock_bridge.call.side_effect = [snap(None), GatewayError("unexpected Bridge response")]
    result = scope["tag_configure_scoped"](PATH, "create", confirm=True)
    assert result["status"] == "uncertain"
    assert result["restore_available"] is False
    saved = result["backup"]
    restored = scope["tag_config_restore"](PATH, saved["id"], saved["sha256"], confirm=True)
    assert restored["attempted"] is False
    assert len(mock_bridge.call.call_args_list) == 2


def prepare_cupper_capabilities(mock_bridge):
    mock_bridge.ping.return_value["capabilities"] += [
        scoped_tags.CUPPER_HISTORY, scoped_tags.CUPPER_MIGRATE, scoped_tags.CUPPER_VALIDATE,
    ]


def history_response():
    path = scoped_tags.CUPPER_PRODUCTION
    samples = [{"path": path, "timestamp_ms": 20, "quality": "192", "value": 1230}] * 2
    return {"path": path, "start_ms": 10, "end_ms": 20, "count": len(samples), "samples": samples,
            "sha256": scoped_tags._sha(b"\n".join(scoped_tags._encode(row) for row in samples))}


def test_public_envelope_uses_exact_milliseconds_and_requires_the_014_bridge(scope, mock_bridge):
    read = scope["tag_cupper_production_history"]
    assert "0.1.4" in read(10, 20)["message"]
    mock_bridge.call.assert_not_called()
    prepare_cupper_capabilities(mock_bridge)
    mock_bridge.call.return_value = expected = history_response()
    assert read(10, 20) == expected
    mock_bridge.call.assert_called_once_with(scoped_tags.CUPPER_HISTORY, {
        "path": scoped_tags.CUPPER_PRODUCTION, "start_ms": 10, "end_ms": 20,
    })


@pytest.mark.parametrize("bad_fields", [
    {"start_ms": 11}, {"end_ms": 21}, {"count": 3}, {"sha256": "0" * 64}, {"samples": []},
    {"path": scoped_tags.CUPPER_SPEED},
])
def test_history_rejects_inconsistent_bridge_results(scope, mock_bridge, bad_fields):
    prepare_cupper_capabilities(mock_bridge)
    mock_bridge.call.return_value = {**history_response(), **bad_fields}
    assert scope["tag_cupper_production_history"](10, 20)["error"] == "gateway_error"


def test_mcp_schema_requires_explicit_window_and_strict_input_types(app_ctx, tmp_path):
    mcp = FastMCP("Cupper schema test")
    scoped_tags.register(mcp, app_ctx, tmp_path)
    tools = {tool.name: tool.inputSchema for tool in asyncio.run(mcp.list_tools())}
    history = tools["tag_cupper_production_history"]
    assert set(history["required"]) == {"start_ms", "end_ms"}
    assert history["properties"]["start_ms"]["type"] == history["properties"]["end_ms"]["type"] == "integer"
    validate = tools["tag_cupper_validate"]["properties"]
    assert validate["mode"]["enum"] == ["prepare", "restore"]
    assert validate["exclusive_access"]["type"] == "boolean"


@pytest.mark.parametrize("mode,exclusive,path", [
    ("prepare", False, scoped_tags.CUPPER_SPEED), ("prepare", 1, scoped_tags.CUPPER_SPEED),
    ("create", True, scoped_tags.CUPPER_SPEED), ("prepare", True, PATH),
])
def test_mcp_validator_rejects_invalid_inputs_without_bridge_calls(scope, mock_bridge, mode, exclusive, path):
    assert scope["tag_cupper_validate"](path, mode, exclusive_access=exclusive)["error"] == "validation_error"
    mock_bridge.call.assert_not_called()


def test_mcp_validator_forwards_boolean_without_enabling_writes(scope, app_ctx, mock_bridge):
    app_ctx.settings = replace(app_ctx.settings, allow_writes=False)
    prepare_cupper_capabilities(mock_bridge)
    scope["tag_cupper_validate"](scoped_tags.CUPPER_SPEED, "prepare", exclusive_access=True)
    mock_bridge.call.assert_called_once_with(scoped_tags.CUPPER_VALIDATE, {
        "path": scoped_tags.CUPPER_SPEED, "mode": "prepare", "exclusiveAccess": True,
    })


@pytest.mark.parametrize("history_case", ["unchanged", "empty", "changed_after_write"])
def test_cupper_production_backup_history_readback_and_safe_restore(scope, mock_bridge, tmp_path, history_case):
    path = scoped_tags.CUPPER_PRODUCTION
    old_config = {**MEMORY, "name": "Production", "historyEnabled": True, "value": 1230,
                  "defaultValue": 1230, "historyProvider": "Postgres_Historian", "historySampleRate": 30}
    live = 1284
    before = snap(old_config, path=path, value=live)
    after = snap({**old_config, "dataType": "Int8", "value": live}, path=path, value=live)
    for snapshot in (before, after):
        snapshot["effective"] = {key: item for key, item in snapshot["config"].items() if key != "value"}
    current = before
    proof = {"start": 10, "end": 20, "count": 0 if history_case == "empty" else 1, "sha256": "a" * 64}
    prepare_cupper_capabilities(mock_bridge)
    calls = []

    def bridge(endpoint, payload):
        nonlocal current
        assert payload["path"] == path
        calls.append(endpoint)
        if endpoint == scoped_tags.CUPPER_HISTORY:
            if history_case == "changed_after_write" and current is after:
                return {**proof, "sha256": "b" * 64}
            return proof
        if endpoint == "tags/scoped-state":
            return current
        assert endpoint == scoped_tags.CUPPER_MIGRATE
        assert payload["historyProof"] == proof and payload["expectedValue"] == live
        backups = [json.loads(item.read_bytes()) for item in tmp_path.glob("*/before.json")]
        assert any(backup["snapshot"]["historyProof"] == proof for backup in backups)
        current = after if payload["mode"] == "prepare" else before
        return {"valueBefore": live}

    mock_bridge.call.side_effect = bridge
    result = scope["tag_configure_scoped"](path, "prepare_cupper", expected_state=scoped_tags._state(before),
                                           confirm=True, exclusive_access=True)
    if history_case == "empty":
        assert not result["attempted"] and scoped_tags.CUPPER_MIGRATE not in calls
        return
    if history_case == "changed_after_write":
        assert result["status"] == "uncertain" and not result["restore_available"]
        assert not list(tmp_path.glob("*/verified.json"))
        return
    assert result["status"] == "success" and result["historyProof"] == proof
    assert calls == ["tags/scoped-state", scoped_tags.CUPPER_HISTORY, scoped_tags.CUPPER_MIGRATE,
                     "tags/scoped-state", scoped_tags.CUPPER_HISTORY]
    backup = result["backup"]
    current = {**after, "value": live + 1}
    refused = scope["tag_config_restore"](path, backup["id"], backup["sha256"], confirm=True, exclusive_access=True)
    assert not refused["attempted"]
    current = after
    restored = scope["tag_config_restore"](path, backup["id"], backup["sha256"], confirm=True, exclusive_access=True)
    assert restored["verified"] and restored["tag"]["value"] == live


@pytest.mark.parametrize("case", ["success", "empty", "history_changed", "rate_changed", "quality_error"])
def test_speed_migration_uses_fresh_value_and_verifies_same_history_window(scope, mock_bridge, tmp_path, case):
    path = scoped_tags.CUPPER_SPEED
    config = {"name": "Speed", "tagType": "AtomicTag", "valueSource": "expr", "expression": "getsecond(now())",
              "historyEnabled": True, "historyProvider": "Postgres_Historian", "historySampleRate": 10,
              "sampleMode": "Periodic", "historicalDeadband": 0, "historicalDeadbandStyle": "Discrete"}
    effective = {**config, "dataType": "Int4", "historySampleRateUnits": "SEC", "defaultValue": None}
    before = {**snap(config, path=path, value=57), "effective": effective}
    memory = {"valueSource": "memory", "defaultValue": 4}
    after = {**snap({**config, **memory, "value": 4}, path=path, value=4), "effective": {**effective, **memory}}
    if case == "rate_changed":
        after["effective"]["historySampleRate"] = 11
    proof = {"start": 100, "end": 200, "count": 0 if case == "empty" else 2, "sha256": "a" * 64}
    current = before
    prepare_cupper_capabilities(mock_bridge)
    mock_bridge.ping.return_value["version"] = "0.1.11"

    def bridge(endpoint, payload):
        nonlocal current
        assert payload["path"] == path
        if endpoint == "tags/scoped-state":
            return current
        if endpoint == scoped_tags.CUPPER_HISTORY:
            if current is before:
                assert payload == {"path": path}
                return proof
            assert payload == {"path": path, "start": 100, "end": 200}
            return {**proof, "sha256": "b" * 64} if case == "history_changed" else proof
        assert endpoint == scoped_tags.CUPPER_MIGRATE
        assert payload["historyProof"] == proof and payload["expectedConfig"] == config
        assert "expectedValue" not in payload
        saved, = tmp_path.glob("*/before.json")
        assert json.loads(saved.read_bytes())["snapshot"] == {**before, "historyProof": proof}
        if case == "quality_error":
            raise GatewayError("Tag operation: Error_Configuration (-1073741055)")
        current = after
        return {"valueBefore": 4}

    mock_bridge.call.side_effect = bridge
    result = scope["tag_configure_scoped"](path, "prepare_cupper", expected_state=scoped_tags._state(before),
                                           confirm=True, exclusive_access=True)
    endpoints = [entry.args[0] for entry in mock_bridge.call.call_args_list]
    if case == "empty":
        assert not result["attempted"] and not list(tmp_path.iterdir())
        assert endpoints == ["tags/scoped-state", scoped_tags.CUPPER_HISTORY]
        return
    if case in ("history_changed", "rate_changed", "quality_error"):
        assert result["status"] == "uncertain" and not result["restore_available"]
        assert not list(tmp_path.glob("*/verified.json"))
        if case == "quality_error":
            assert result["configuration_unchanged"] and result["tag"]["config"] == config
        return
    assert result["verified"] and result["valueBefore"] == result["tag"]["value"] == 4
    assert result["tag"]["effective"]["historySampleRate"] == 10
    assert result["tag"]["config"]["expression"] == "getsecond(now())"
    assert result["historyProof"] == proof and result["restore_available"]
    assert endpoints == ["tags/scoped-state", scoped_tags.CUPPER_HISTORY, scoped_tags.CUPPER_MIGRATE,
                         "tags/scoped-state", scoped_tags.CUPPER_HISTORY]


def test_speed_migration_blocks_018_before_read_or_backup(scope, mock_bridge, tmp_path):
    prepare_cupper_capabilities(mock_bridge)
    mock_bridge.ping.return_value["version"] = "0.1.8"
    result = scope["tag_configure_scoped"](scoped_tags.CUPPER_SPEED, "prepare_cupper", expected_state="a" * 64,
                                           confirm=True, exclusive_access=True)
    assert not result["attempted"] and "0.1.9" in result["message"]
    mock_bridge.call.assert_not_called()
    assert not list(tmp_path.iterdir())


def test_cannot_delete_created_tag_after_value_changes(scope, mock_bridge):
    mock_bridge.call.side_effect = [snap(None), snap(MEMORY), snap(MEMORY), snap(MEMORY, value=17)]
    saved = scope["tag_configure_scoped"](PATH, "create", confirm=True)["backup"]
    result = scope["tag_config_restore"](PATH, saved["id"], saved["sha256"], confirm=True)
    assert result["error"] == "tag_configuration_conflict"
    assert result["attempted"] is False


@pytest.mark.parametrize("root", [
    "[default]Line_1/*", "[default]Line_1/Cupper/PLC", "[default]_types_/Cupper",
])
def test_config_rejects_glob_and_reserved_folders(root):
    with pytest.raises(ValueError, match="IGNITION_TAG_CONFIG_ALLOWLIST"):
        load_settings({"IGNITION_URL": "http://gw:8088", "IGNITION_API_TOKEN": "test:token",
                       "IGNITION_TAG_CONFIG_ALLOWLIST": root})
