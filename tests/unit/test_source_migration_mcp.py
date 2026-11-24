"""Check the probe's MCP scope, backups, and independently verified readback."""

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from ignition_mcp.errors import GatewayError
from ignition_mcp.tools import bridge_admin, scoped_tags

from .conftest import FakeMCP

PATH = scoped_tags.SOURCE_TEST_PATH
ROOT, NAME = PATH.rsplit("/", 1)
EXPRESSION = {"name": NAME, "tagType": "AtomicTag", "dataType": "Int4", "valueSource": "expr",
              "expression": "getsecond(now())", "historyEnabled": False}
MEMORY = {**EXPRESSION, "valueSource": "memory", "defaultValue": 28, "value": 28}
GOOD = {"name": "Good", "code": 192, "good": True}
BAD = {"name": "Bad_Test", "code": 554, "good": False}
CONFIGURATION_ERROR = {"name": "Error_Configuration", "code": -1073741055, "good": False}


def snapshot(config, value=None):
    return {"path": PATH, "config": config, "effective": config or {},
            "valueGood": config is not None, "value": value}


@pytest.fixture
def probe(app_ctx, mock_bridge, tmp_path):
    app_ctx.settings = replace(app_ctx.settings, tag_config_allowlist=(ROOT,))
    mock_bridge.ping.return_value = {"version": "0.1.8", "capabilities": sorted(scoped_tags.SCOPED_ENDPOINTS | {
        scoped_tags.SOURCE_TEST_ENDPOINT,
    })}
    gateway = Mock()
    gateway.current = snapshot(None)
    gateway.after = snapshot(EXPRESSION, 28)
    gateway.qualities = [GOOD]
    gateway.diagnostic = None

    def call(endpoint, payload):
        assert payload["path"] == PATH
        if endpoint == "tags/scoped-state":
            return gateway.current
        assert list(tmp_path.glob("*/before.json"))
        gateway.current = gateway.after
        if endpoint == "tags/scoped-restore" or payload["action"] == "undo_create":
            return {}
        result = {"count": len(gateway.qualities), "qualities": gateway.qualities,
                  "valueBefore": None if payload["action"] == "create" else 28}
        if gateway.diagnostic is not None:
            result["diagnostic"] = gateway.diagnostic
        return result

    mock_bridge.call.side_effect = call
    mcp = FakeMCP()
    scoped_tags.register(mcp, app_ctx, tmp_path)

    def run(action, prior=None, **options):
        previous = ({"previous_backup_id": prior["id"], "previous_backup_sha256": prior["sha256"]}
                    if prior else {})
        params = {"expected_state": scoped_tags._state(gateway.current), "confirm": True,
                  "exclusive_access": True, **previous, **options}
        return mcp.tools["tag_source_migration_test"](action, **params)

    return run, gateway, mcp.tools


def test_verify_library_reads_only_fixed_file_and_hides_source(make_tools, mock_bridge):
    reference = bridge_admin.LOCAL_LIBRARY.read_text(encoding="utf-8")
    fake_secret = "example-test-secret-that-must-never-be-returned"
    live = (reference.replace("BRIDGE_VERSION = '0.1.2'", "BRIDGE_VERSION = '0.1.5'")
            .replace("BRIDGE_SECRET = '__BRIDGE_SECRET__'", f"BRIDGE_SECRET = '{fake_secret}'"))
    mock_bridge.ping.return_value = {"version": "0.1.5"}
    mock_bridge.call.return_value = {"content": live}
    verify = make_tools(bridge_admin)["bridge_scoped_result_verify"]
    report = verify()
    assert report["runtime_matches_library"] and report["scoped_result_matches_fork"]
    mock_bridge.call.assert_called_once_with("files/read", {"path": bridge_admin.BRIDGE_PROJECT_LIBRARY})
    mock_bridge.call.return_value = {"content": live.replace("code.getName()", "str(code)")}
    assert verify()["scoped_result_matches_fork"] is False
    mock_bridge.call.side_effect = GatewayError(fake_secret)
    report = verify()
    assert "error" in report and fake_secret not in str(report) and "content" not in report


def test_probe_refuses_existing_tag_and_writes_without_backups(probe, mock_bridge, tmp_path, monkeypatch):
    run, gateway, _ = probe
    assert run("create", confirm=False)["error"] == "write_not_allowed"
    assert run("create", exclusive_access=False)["attempted"] is False
    assert not list(tmp_path.iterdir())
    gateway.current = snapshot(EXPRESSION, 2)
    assert run("create")["status"] == "already_exists"
    assert run("merge")["attempted"] is False
    gateway.current = snapshot(None)
    monkeypatch.setattr(scoped_tags, "_private_write", Mock(side_effect=OSError("disk full")))
    assert run("create")["attempted"] is False
    assert all(call.args[0] == "tags/scoped-state" for call in mock_bridge.call.call_args_list)


def test_probe_requires_active_018_even_if_route_is_advertised(probe, mock_bridge, tmp_path):
    run, _, _ = probe
    mock_bridge.ping.return_value["version"] = "0.1.7"
    assert run("create")["attempted"] is False
    mock_bridge.call.assert_not_called()
    assert not list(tmp_path.iterdir())


def test_create_reports_only_recognized_diagnostics_and_keeps_readback(probe, mock_bridge):
    run, gateway, _ = probe
    gateway.after = snapshot(None)
    gateway.qualities = [BAD]
    gateway.diagnostic = "invalid_expression"
    report = run("create")
    assert report["configure"]["diagnostic"] == "invalid_expression"
    assert report["configuration_unchanged"] and report["tag"]["config"] is None
    assert [call.args[0] for call in mock_bridge.call.call_args_list] == [
        "tags/scoped-state", scoped_tags.SOURCE_TEST_ENDPOINT, "tags/scoped-state",
    ]

    gateway.diagnostic = "bearer private-value"
    report = run("create")
    assert "configure" not in report and "private-value" not in str(report)
    assert report["configuration_unchanged"]


def test_unexpected_diagnostic_is_rejected_for_good_create_and_for_merge(probe):
    run, gateway, _ = probe
    gateway.diagnostic = "redacted"
    rejected = run("create")
    assert rejected["attempted"] and not rejected["verified"] and "configure" not in rejected

    gateway.current = snapshot(None)
    gateway.diagnostic = None
    created = run("create")
    assert created["verified"]
    gateway.diagnostic = "redacted"
    gateway.qualities = [BAD]
    rejected = run("merge", created["backup"])
    assert rejected["attempted"] and not rejected["verified"] and "configure" not in rejected


def test_probe_waits_for_new_expression_with_read_only_calls(probe, mock_bridge, monkeypatch):
    run, gateway, _ = probe
    gateway.after = {**snapshot(EXPRESSION), "valueGood": False}

    def evaluate_expression(seconds):
        assert seconds == 0.5
        gateway.current = snapshot(EXPRESSION, 29)

    wait = Mock(side_effect=evaluate_expression)
    monkeypatch.setattr(scoped_tags.time, "sleep", wait)
    created = run("create")
    assert created["verified"] and created["tag"]["value"] == 29
    wait.assert_called_once_with(0.5)
    assert [call.args[0] for call in mock_bridge.call.call_args_list] == [
        "tags/scoped-state", scoped_tags.SOURCE_TEST_ENDPOINT, "tags/scoped-state", "tags/scoped-state",
    ]


def test_merge_reports_retained_expression_and_refuses_overwrite_after_success(probe, mock_bridge):
    run, gateway, tools = probe
    created = run("create")
    assert created["verified"] and created["tag"]["valueGood"] and created["expression_present"]
    assert Path(created["backup"]["path"]).is_file()
    gateway.after = snapshot(MEMORY, 28)
    merged = run("merge", created["backup"])
    assert merged["verified"] and merged["expression"] == "getsecond(now())"
    assert merged["configure"] == {"count": 1, "qualities": [GOOD], "valueBefore": 28}
    assert run("overwrite", merged["backup"])["attempted"] is False

    restore = tools["tag_config_restore"]
    backup = merged["backup"]
    gateway.after = snapshot(EXPRESSION, 31)
    assert restore(PATH, backup["id"], backup["sha256"], confirm=True, exclusive_access=True)["verified"]
    gateway.after = snapshot(None)
    backup = created["backup"]
    assert restore(PATH, backup["id"], backup["sha256"], confirm=True, exclusive_access=True)["verified"]
    assert mock_bridge.call.call_args_list[-2].args[1]["action"] == "undo_create"


def test_overwrite_requires_a_failed_merge_with_unchanged_readback(probe, mock_bridge):
    run, gateway, _ = probe
    created = run("create")
    assert run("overwrite", created["backup"])["attempted"] is False
    gateway.qualities = [BAD]
    failed = run("merge", created["backup"])
    assert failed["status"] == "rejected" and failed["overwrite_available"]
    assert failed["configuration_unchanged"] is True
    assert failed["configure"]["qualities"] == [BAD]
    assert mock_bridge.call.call_args.args[0] == "tags/scoped-state"
    unreviewed = {**failed["backup"], "sha256": "0" * 64}
    assert run("overwrite", unreviewed)["attempted"] is False
    gateway.qualities = [GOOD]
    gateway.after = snapshot({key: val for key, val in MEMORY.items() if key != "expression"}, 28)
    overwritten = run("overwrite", failed["backup"])
    assert overwritten["verified"] and not overwritten["expression_present"]


def test_failed_overwrite_keeps_expression_and_reports_uncertain_without_a_receipt(probe, mock_bridge):
    run, gateway, _ = probe
    created = run("create")
    gateway.qualities = [CONFIGURATION_ERROR]
    merged = run("merge", created["backup"])
    assert merged["status"] == "rejected" and merged["overwrite_available"]
    assert merged["configure"]["qualities"] == [CONFIGURATION_ERROR]

    gateway.after = snapshot(EXPRESSION, 29)
    mock_bridge.call.reset_mock()
    overwritten = run("overwrite", merged["backup"])

    assert overwritten["status"] == "uncertain" and overwritten["error"] == "tag_configuration_conflict"
    assert overwritten["configuration_unchanged"] and not overwritten["verified"]
    assert overwritten["tag"]["config"] == EXPRESSION
    assert overwritten["configure"] == {"count": 1, "qualities": [CONFIGURATION_ERROR], "valueBefore": 28}
    assert "overwrite_available" not in overwritten and "restore_available" not in overwritten
    backup = Path(overwritten["backup"]["path"])
    assert json.loads(backup.read_bytes())["snapshot"]["config"] == EXPRESSION
    assert not backup.with_name("verified.json").exists()
    assert [call.args[0] for call in mock_bridge.call.call_args_list] == [
        "tags/scoped-state", scoped_tags.SOURCE_TEST_ENDPOINT, "tags/scoped-state",
    ]
    assert mock_bridge.call.call_args_list[1].args[1]["action"] == "overwrite"


@pytest.mark.parametrize("qualities", [[], [None], [BAD, GOOD]])
def test_ambiguous_quality_does_not_authorize_overwrite(probe, qualities):
    run, gateway, _ = probe
    created = run("create")
    gateway.qualities = qualities
    merged = run("merge", created["backup"])
    assert merged["status"] == "uncertain" and merged["configuration_unchanged"]
    assert merged["configure"]["count"] == len(qualities)
    assert merged["configure"]["qualities"] == qualities
    assert run("overwrite", merged["backup"])["attempted"] is False


def test_failed_quality_with_partial_write_never_authorizes_overwrite(probe):
    run, gateway, _ = probe
    created = run("create")
    gateway.qualities = [BAD]
    gateway.after = snapshot(MEMORY, 28)
    failed = run("merge", created["backup"])
    assert failed["status"] == "uncertain" and failed["tag"]["config"] == MEMORY
    assert failed["configure"]["qualities"] == [BAD]
    assert run("overwrite", failed["backup"])["attempted"] is False


def test_lost_configure_response_still_reads_gateway_and_blocks_overwrite(probe, mock_bridge):
    run, gateway, _ = probe
    created = run("create")
    normal_call = mock_bridge.call.side_effect

    def lost_response(endpoint, payload):
        if endpoint == scoped_tags.SOURCE_TEST_ENDPOINT:
            gateway.current = snapshot(MEMORY, 28)
            raise GatewayError("Response lost after configure")
        return normal_call(endpoint, payload)

    mock_bridge.call.side_effect = lost_response
    uncertain = run("merge", created["backup"])
    assert uncertain["status"] == "uncertain" and uncertain["tag"]["config"] == MEMORY
    assert uncertain["configuration_unchanged"] is False
    assert mock_bridge.call.call_args.args[0] == "tags/scoped-state"
    assert run("overwrite", uncertain["backup"])["attempted"] is False
