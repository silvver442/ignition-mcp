import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

from ignition_mcp.bridge import BridgeClient, sign
from ignition_mcp.config import load_settings
from ignition_mcp.cupper import validate_cupper_history
from ignition_mcp.errors import GatewayError

BRIDGE = Path(__file__).resolve().parents[2] / "bridge"
with patch.object(sys, "path", [str(BRIDGE), *sys.path]):
    import apply_upgrade_012 as upgrade
    import apply_upgrade_013 as install
    import apply_upgrade_014 as fixed
    import apply_upgrade_015 as diagnostic

ROOT = "[default]Line_1/Cupper/Ignition/"
QUALITY = SimpleNamespace(isGood=lambda: True)
NOW = 1_790_000_000_000
SECRET = "test-only-fixture"


def date(ms):
    return SimpleNamespace(getTime=lambda: ms)


def bridge_versions():
    candidate = (BRIDGE / "src" / "mcp_bridge_lib.py").read_bytes()
    source, tree, _, _ = diagnostic._library(candidate)
    scoped_result = diagnostic._functions(tree)["_scoped_result"]
    baseline = candidate.replace(upgrade.excerpt(source, scoped_result).encode(),
                                 diagnostic.LEGACY_RESULT.encode(), 1)
    legacy = install.prepare(baseline, install.FRAGMENT.read_bytes())
    live = fixed.prepare(legacy, fixed.FRAGMENT.read_bytes())
    return candidate, legacy, live


class CupperUpgradeTests(unittest.TestCase):
    def test_014_keeps_the_live_secret_and_supports_a_verified_redacted_backup(self):
        secret = "synthetic-secret-long-enough-for-local-tests"
        source = (BRIDGE / "src" / "mcp_bridge_lib.py").read_bytes()
        live = upgrade.rebind_secret(install.prepare(source, install.FRAGMENT.read_bytes()), secret)
        prepared = fixed.prepare(live, fixed.FRAGMENT.read_bytes())
        self.assertEqual(fixed._library(prepared)[3], secret)
        with tempfile.TemporaryDirectory(dir=BRIDGE) as root:
            result = upgrade.backup_library(Path(root) / "backup", live, secret, version=fixed.OLD)
            saved = Path(result["path"]).read_bytes()
            self.assertNotIn(secret.encode(), saved)
            self.assertEqual(upgrade.rebind_secret(saved, secret), live)
        divergent = live.replace(b"payload = system.util.jsonDecode(payload_str)", b"payload = {}")
        with self.assertRaisesRegex(fixed.UpgradeError, "differs from the reviewed"):
            fixed.prepare(divergent, fixed.FRAGMENT.read_bytes())

    def test_015_updates_only_quality_diagnostics_and_version(self):
        candidate, _, live = bridge_versions()
        secret = "sufficiently-long-synthetic-secret-for-this-test"
        live = upgrade.rebind_secret(live, secret)
        updated = diagnostic.prepare(live, candidate)
        before = diagnostic._library(live)
        after = diagnostic._library(updated)
        self.assertEqual(after[3], secret)
        self.assertEqual(diagnostic._constant(after[2], "BRIDGE_VERSION"), "0.1.5")
        self.assertEqual(ast.dump(upgrade._handlers(after[2])[1]), ast.dump(upgrade._handlers(before[2])[1]))
        self.assertEqual(
            ast.dump(diagnostic._one(after[2], "TAG_CONFIG_ROOTS")),
            ast.dump(diagnostic._one(before[2], "TAG_CONFIG_ROOTS")),
        )
        unexpected = live.replace(diagnostic.LEGACY_RESULT.encode(),
                                  diagnostic.LEGACY_RESULT.replace("!= 1", "< 1").encode())
        with self.assertRaisesRegex(fixed.UpgradeError, "differs from the reviewed"):
            diagnostic.prepare(unexpected, candidate)

    def test_log_probe_only_returns_quality_identifiers_from_matching_entries(self):
        gateway = Mock()
        gateway.get_json.side_effect = [
            {"entries": [{"message": "Speed Bad_Unsupported secret=do-not-display"},
                         {"message": "Unrelated Bad_AccessDenied"}]},
            [{"message": "Tag operation did not return one Good result"}],
        ]
        summary = diagnostic.read_speed_logs(gateway)
        self.assertEqual([(row["matching_events"], row["quality_names"]) for row in summary],
                         [(1, ["Bad_Unsupported"]), (1, [])])
        self.assertNotIn("do-not-display", json.dumps(summary))
        self.assertEqual([call.args for call in gateway.get_json.call_args_list], [("logs",), ("logs",)])


class CupperBridgeTests(unittest.TestCase):
    def setUp(self):
        candidate, previous, live = bridge_versions()
        updated = diagnostic.prepare(live, candidate)
        self.config = {"name": "Production", "valueSource": "memory", "defaultValue": 1230, "value": 1230,
                       "historyEnabled": True, "historyProvider": "Postgres_Historian", "historySampleRate": 30}
        self.effective = {"valueSource": "memory", "dataType": "Int4", "defaultValue": 1230,
                          "historyEnabled": True, "historyProvider": "Postgres_Historian"}
        self.value = 1267  # Live counter deliberately differs from the saved default and value.
        self.rows = [("Production", 1230, 192, date(NOW - 600000))]
        self.configure = Mock(side_effect=self.apply_patch)
        self.query = Mock(side_effect=lambda **kwargs: SimpleNamespace(
            getColumnCount=lambda: 4, getRowCount=lambda: len(self.rows),
            getValueAt=lambda row, column: self.rows[row][column]))
        self.system = SimpleNamespace(
            util=SimpleNamespace(getLogger=Mock(), getProperty=lambda _: ".",
                                 jsonDecode=Mock(side_effect=self.legacy_decoder)),
            tag=SimpleNamespace(configure=self.configure),
            historian=SimpleNamespace(queryRawPoints=self.query),
            date=SimpleNamespace(now=lambda: date(NOW), fromMillis=date,
                                 toMillis=lambda value: value.getTime()),
        )
        namespace = {"system": self.system, "long": int, "basestring": str, "unicode": str}
        self.legacy = dict(namespace)
        exec(compile(previous, "mcp_bridge_013.py", "exec"), self.legacy)
        exec(compile(updated, "mcp_bridge_lib.py", "exec"), namespace)
        namespace["_scoped_tag_path"] = lambda path: (ROOT[:-1], path.rsplit("/", 1)[1])
        namespace["_scoped_tag_snapshot"] = self.snapshot
        self.route = namespace["HANDLERS"]["tags/scoped-cupper-migrate"]
        self.history = namespace["HANDLERS"]["tags/scoped-cupper-history"]
        self.handle = namespace["handle"]
        namespace["BRIDGE_SECRET"] = SECRET
        self.assertEqual(namespace["BRIDGE_VERSION"], "0.1.5")

    @staticmethod
    def legacy_decoder(payload):
        value = json.loads(payload)
        if isinstance(value, dict) and value.get("exclusiveAccess") is True:
            value["exclusiveAccess"] = 1
        return value

    def client(self):
        def webdev(request):
            envelope = json.loads(request.content)
            payload = envelope["payload"]
            self.assertEqual(envelope["sig"], sign(SECRET, envelope["ts"], payload))
            path = request.url.path.split("/api/", 1)[1]
            return httpx.Response(200, json=self.handle({"remainingPath": path, "data": envelope})["json"])

        cfg = load_settings({"IGNITION_URL": "http://test:8088", "IGNITION_API_TOKEN": "test:token",
                             "IGNITION_BRIDGE_SECRET": SECRET})
        http = httpx.Client(base_url=cfg.gateway_url, transport=httpx.MockTransport(webdev))
        with patch("ignition_mcp.bridge.httpx.Client", return_value=http):
            client = BridgeClient(cfg)
        self.addCleanup(client.close)
        return client

    def snapshot(self, path, parent, name):
        return {"path": path, "config": dict(self.config), "effective": dict(self.effective),
                "value": self.value, "valueGood": True}

    def apply_patch(self, parent, source, policy):
        self.assertEqual(parent, ROOT[:-1])
        update, = json.loads(source)
        if policy == "m":
            self.config.update(update)
        else:
            self.config = update
        self.effective.update({k: v for k, v in self.config.items() if k != "value"})
        self.value = update.get("value", self.value)
        return [QUALITY]

    def production_request(self):
        path = ROOT + "Production"
        return {"path": path, "mode": "prepare", "exclusiveAccess": True,
                "expectedConfig": dict(self.config), "expectedValue": self.value,
                "historyProof": self.history({"path": path})}

    def test_history_path_and_identity_of_raw_samples(self):
        request = self.production_request()
        self.assertEqual(request["historyProof"]["count"], 1)
        self.assertEqual(self.query.call_args.kwargs["returnFormat"], "TALL")
        self.assertEqual(self.query.call_args.kwargs["paths"], [
            "histprov:Postgres_Historian:/drv:Ignition-Prox-Maker:default:/tag:Line_1/Cupper/Ignition/Production",
        ])
        self.rows[0] = ("Production", 1230, 0, date(NOW - 600000))
        with self.assertRaisesRegex(ValueError, "history"):
            self.route(request)
        self.configure.assert_not_called()

    def test_production_type_roundtrip_preserves_live_value_and_history(self):
        request = self.production_request()
        self.assertEqual(self.route(request), {"valueBefore": 1267})
        self.assertEqual(json.loads(self.configure.call_args.args[1]), [
            {"name": "Production", "dataType": "Int8", "value": 1267},
        ])
        self.assertEqual(self.config["defaultValue"], 1230)
        self.assertEqual(self.config["historyProvider"], "Postgres_Historian")
        self.assertTrue(self.config["historyEnabled"])
        self.assertEqual(self.config["historySampleRate"], 30)
        self.route({**request, "mode": "restore", "expectedConfig": dict(self.config)})
        self.assertEqual(self.config["dataType"], "Int4")
        self.assertEqual(self.value, 1267)
        self.assertEqual(self.history({"path": request["path"]}), request["historyProof"])

    def test_production_refuses_changed_value_and_outside_scope(self):
        request = self.production_request()
        self.value += 1
        with self.assertRaisesRegex(ValueError, "counter changed"):
            self.route(request)
        for path in (ROOT + "Status", ROOT + "Outfeed_Counter", "[default]Line_1/Cupper/PLC/Production"):
            with self.assertRaisesRegex(ValueError, "limited"):
                self.route({**request, "path": path})
        self.configure.assert_not_called()

    def test_speed_captures_live_value_and_restore_preserves_original_expression(self):
        self.config = {"name": "Speed", "valueSource": "expr", "expression": "getsecond(now())",
                       "historyEnabled": True, "historyProvider": "Postgres_Historian", "historySampleRate": 10}
        self.effective.update(valueSource="expr")
        self.value = 23
        before = dict(self.config)
        request = {"path": ROOT + "Speed", "mode": "prepare", "exclusiveAccess": True, "expectedConfig": before}
        self.assertEqual(self.route(request), {"valueBefore": 23})
        self.assertEqual(self.config["defaultValue"], 23)
        self.assertEqual(self.config["historySampleRate"], 10)
        self.route({**request, "mode": "restore", "expectedConfig": dict(self.config), "expectedValue": 23,
                    "beforeConfig": before})
        self.assertEqual(self.config, before)
        self.query.assert_not_called()

    def test_signed_speed_error_reports_the_result_shape_and_safe_quality_code(self):
        self.config = {"name": "Speed", "valueSource": "expr", "expression": "getsecond(now())",
                       "historyEnabled": True, "historyProvider": "Postgres_Historian", "historySampleRate": 10}
        self.effective.update(valueSource="expr")
        self.value = 23
        path = ROOT + "Speed"
        request = {"path": path, "mode": "prepare", "exclusiveAccess": True,
                   "expectedConfig": dict(self.config), "expectedValue": 59, "historyProof": None}
        bad = SimpleNamespace(isGood=lambda: False, getName=lambda: "Bad_Unsupported",
                              getCode=lambda: -2147483120, getDiagnosticMessage=Mock(return_value=SECRET))
        client = self.client()
        self.configure.side_effect = None
        cases = [
            (None, "result=null"),
            ([], "count=0, quality=[]"),
            ([bad], "count=1, quality=[Bad_Unsupported (-2147483120)]"),
            ([bad, bad], "count=2, quality=[Bad_Unsupported (-2147483120), Bad_Unsupported (-2147483120)]"),
            ([None], "count=1, quality=[null]"),
            ([object()], "count=1, quality=[invalid QualityCode]"),
        ]
        with patch("ignition_mcp.bridge.time.time", return_value=NOW // 1000):
            for results, expected in cases:
                self.configure.reset_mock()
                self.configure.return_value = results
                with self.subTest(results=expected), self.assertRaises(GatewayError) as error:
                    client.call(fixed.CUPPER_MIGRATE, request)
                self.assertIn(expected, str(error.exception))
                self.assertNotIn(SECRET, str(error.exception))
                self.configure.assert_called_once()
                parent, config, policy = self.configure.call_args.args
                self.assertEqual((parent, policy), (ROOT[:-1], "m"))
                self.assertEqual(json.loads(config), [
                    {"name": "Speed", "valueSource": "memory", "defaultValue": 23, "value": 23},
                ])
        bad.getDiagnosticMessage.assert_not_called()
        self.assertEqual(self.config, request["expectedConfig"])

    def test_signed_speed_validator_preserves_json_boolean_and_never_writes(self):
        client = self.client()
        request = {"path": ROOT + "Speed", "mode": "prepare", "exclusiveAccess": True}
        decoded = self.system.util.jsonDecode(json.dumps(request))
        self.assertIs(type(decoded["exclusiveAccess"]), int)
        with self.assertRaisesRegex(ValueError, "exclusive access"):
            self.legacy["handle_tags_scoped_cupper_migrate"](decoded)
        self.system.util.jsonDecode.reset_mock()
        with patch("ignition_mcp.bridge.time.time", return_value=NOW // 1000):
            self.assertEqual(client.call(fixed.CUPPER_VALIDATE, request), {**request, "valid": True, "name": "Speed"})
            invalid = [
                ({"path": request["path"], "mode": "prepare"}, "exclusiveAccess"),
                ({**request, "mode": "prepare_cupper"}, "operation"),
                ({**request, "path": ROOT + "Status"}, "limited"),
                ({**request, "path": "[default]Line_1/Cupper/PLC/Speed"}, "limited"),
                *[({**request, "exclusiveAccess": value}, "exclusiveAccess")
                  for value in (1, 0, False, None, "true", [])],
            ]
            for payload, error in invalid:
                with self.subTest(payload=payload), self.assertRaisesRegex(GatewayError, error):
                    client.call(fixed.CUPPER_VALIDATE, payload)
        self.system.util.jsonDecode.assert_not_called()
        self.query.assert_not_called()
        self.configure.assert_not_called()

    def test_tampered_signed_window_never_reaches_the_historian(self):
        payload = json.dumps({"path": ROOT + "Production", "start_ms": 10, "end_ms": 20})
        timestamp = str(NOW // 1000)
        envelope = {"ts": timestamp, "sig": sign(SECRET, timestamp, payload),
                    "payload": payload.replace('"start_ms": 10', '"start_ms": 11')}
        result = self.handle({"remainingPath": fixed.CUPPER_HISTORY, "data": envelope})["json"]
        self.assertEqual(result["code"], "auth")
        self.query.assert_not_called()

    def test_signed_raw_window_preserves_endpoints_duplicate_rows_nulls_and_int8(self):
        start, end = NOW - 2000, NOW - 1000
        path = ROOT + "Production"
        request = {"path": path, "start_ms": start, "end_ms": end}
        large = 2 ** 63 - 1
        self.rows = [
            (path, -12, 0, date(end)), (path, 100, 192, date(end + 1)),
            (path, large, 192, date(start + 1)), (path, None, 0, date(start)),
            (path, large, 192, date(start + 1)), (path, 500, 192, date(start - 1)),
            (path, large, 0, date(start + 1)),
        ]
        client = self.client()
        with patch("ignition_mcp.bridge.time.time", return_value=NOW // 1000):
            result = validate_cupper_history(client.call(fixed.CUPPER_HISTORY, request), start, end)
            self.rows.reverse()
            again = validate_cupper_history(client.call(fixed.CUPPER_HISTORY, request), start, end)
        self.assertEqual(result, again)
        self.assertEqual(result["count"], 5)
        self.assertEqual(result["samples"], [
            {"path": path, "timestamp_ms": start, "value": None, "quality": "0"},
            {"path": path, "timestamp_ms": start + 1, "value": large, "quality": "0"},
            {"path": path, "timestamp_ms": start + 1, "value": large, "quality": "192"},
            {"path": path, "timestamp_ms": start + 1, "value": large, "quality": "192"},
            {"path": path, "timestamp_ms": end, "value": -12, "quality": "0"},
        ])
        args = self.query.call_args.kwargs
        self.assertEqual(args["startTime"].getTime(), start)
        self.assertEqual(args["endTime"].getTime(), end)
        self.assertEqual((args["returnFormat"], args["returnSize"]), ("TALL", -1))
        self.assertEqual(args["paths"], [
            "histprov:Postgres_Historian:/drv:Ignition-Prox-Maker:default:/tag:Line_1/Cupper/Ignition/Production",
        ])
        tampered = [dict(sample) for sample in result["samples"]]
        tampered[0]["quality"] = "192"
        with self.assertRaisesRegex(GatewayError, "SHA-256"):
            validate_cupper_history({**result, "samples": tampered}, start, end)

    def test_raw_invalid_boundaries_do_not_fall_back_to_the_default_window(self):
        path = {"path": ROOT + "Production"}
        windows = [
            {"start_ms": 0}, {"end_ms": 1}, {"start_ms": 0, "end": 1},
            {"start": None, "end": None}, {"start_ms": False, "end_ms": 1},
            {"start_ms": "0", "end_ms": 1}, {"start_ms": 1.5, "end_ms": 2},
            {"start_ms": -1, "end_ms": 1}, {"start_ms": 1, "end_ms": 1},
            {"start_ms": 2, "end_ms": 1}, {"start_ms": NOW, "end_ms": NOW + 1},
            {"start_ms": 1, "end_ms": 7 * 86400000 + 2},
        ]
        for window in windows:
            with self.subTest(window=window), self.assertRaisesRegex(ValueError, "both|closed window"):
                self.history({**path, **window})
        with self.assertRaisesRegex(ValueError, "Only Cupper Production"):
            self.history({"path": ROOT + "Speed", "start_ms": 0, "end_ms": 1})
        self.query.assert_not_called()
        for end in (1, 7 * 86400000):
            result = self.history({**path, "start_ms": 0, "end_ms": end})
            self.assertEqual((result["start_ms"], result["end_ms"], result["samples"]), (0, end, []))

    def test_acceptance_compares_every_sample_and_reads_the_earlier_window(self):
        fixed_start, fixed_end = NOW - 2000, NOW - 1000
        earlier_start, earlier_end = NOW - 3000, NOW - 2001
        path = ROOT + "Production"
        self.rows = [(path, 1230, 192, date(fixed_end)), (path, 1200, 192, date(earlier_end))]
        client = self.client()
        with (patch("ignition_mcp.bridge.time.time", return_value=NOW // 1000),
              patch.multiple(fixed, START_MS=fixed_start, END_MS=fixed_end,
                             EARLIER_START_MS=earlier_start, EARLIER_END_MS=earlier_end)):
            report = fixed.verify_after(client)
        self.assertEqual(report["validator"], "passed")
        self.assertTrue(report["production_history"]["compared_samples"])
        self.assertTrue(report["production_history"]["identical_reads"])
        self.assertEqual(report["production_history"]["fixed_window"]["count"], 1)
        self.assertEqual(report["production_history"]["earlier_window"]["count"], 1)
        self.assertEqual([call.kwargs["endTime"].getTime() for call in self.query.call_args_list],
                         [fixed_end, fixed_end, earlier_end])
        self.configure.assert_not_called()

    def test_same_signed_history_route_uses_legacy_window_only_when_boundaries_are_absent(self):
        path = ROOT + "Production"
        start, end = NOW - 900_000, NOW - 600_000
        self.rows = [(path, 1229, 0, date(start)), (path, 1230, 192, date(start + 30_000)),
                     (path, 1231, 192, date(end + 30_000))]
        client = self.client()
        with patch("ignition_mcp.bridge.time.time", return_value=NOW // 1000):
            raw = client.call(fixed.CUPPER_HISTORY, {"path": path, "start_ms": start, "end_ms": end})
            legacy = client.call(fixed.CUPPER_HISTORY, {"path": path})
        self.assertEqual(validate_cupper_history(raw, start, end)["samples"], [
            {"path": path, "timestamp_ms": start, "value": 1229, "quality": "0"},
            {"path": path, "timestamp_ms": start + 30_000, "value": 1230, "quality": "192"},
        ])
        self.assertEqual(legacy["count"], 3)
        self.assertEqual(legacy["end"], NOW - 300_000)
        self.assertNotIn("samples", legacy)
        self.configure.assert_not_called()


if __name__ == "__main__":
    unittest.main()
