"""Exercise the Speed patch and historian proof without writing to a Gateway."""

import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from .test_upgrade_012 import SECRET, project_zip

BRIDGE = Path(__file__).resolve().parents[2] / "bridge"
with patch.object(sys, "path", [str(BRIDGE), *sys.path]):
    import apply_upgrade_012 as upgrade
    import apply_upgrade_013 as release013
    import apply_upgrade_014 as release014
    import apply_upgrade_016 as release016
    import apply_upgrade_017 as release017
    import apply_upgrade_018 as release018
    import apply_upgrade_019 as release019

ROOT = "[default]Line_1/Cupper/Ignition"
SPEED = ROOT + "/Speed"
NOW = 1_790_000_000_000
GOOD = SimpleNamespace(isGood=lambda: True, getName=lambda: "Good", getCode=lambda: 192)
BAD = SimpleNamespace(isGood=lambda: False, getName=lambda: "Error_Configuration", getCode=lambda: -1073741055)


def library_018():
    source = upgrade.rebind_secret(upgrade.CANDIDATE.read_bytes(), SECRET)
    for release in (release013, release014):
        source = release.prepare(source, release.FRAGMENT.read_bytes())
    source = source.replace(b"BRIDGE_VERSION = '0.1.4'", b"BRIDGE_VERSION = '0.1.5'", 1)
    for release in (release016, release017, release018):
        source = release.prepare(source, release.FRAGMENT.read_bytes())
    return source


def date(value):
    return SimpleNamespace(getTime=lambda: value)


class SpeedMigrationBridgeTests(unittest.TestCase):
    def setUp(self):
        self.before = library_018()
        self.fragment = release019.FRAGMENT.read_bytes()
        self.prepared = release019.prepare(self.before, self.fragment)
        self.config = {
            "name": "Speed", "tagType": "AtomicTag", "valueSource": "expr", "expression": "getsecond(now())",
            "historyEnabled": True, "historyProvider": "Postgres_Historian", "historySampleRate": 10,
            "sampleMode": "Periodic", "historicalDeadbandStyle": "Discrete", "historicalDeadband": 0,
        }
        self.effective = dict(self.config, dataType="Int4", historySampleRateUnits="SEC", defaultValue=None)
        self.value = 57
        self.rows = [(SPEED, 12, 192, date(NOW - 305000))]
        self.configure = Mock(return_value=[GOOD])
        self.query = Mock(side_effect=lambda **_: SimpleNamespace(
            getColumnCount=lambda: 4, getRowCount=lambda: len(self.rows),
            getValueAt=lambda row, column: self.rows[row][column],
        ))
        system = SimpleNamespace(
            util=SimpleNamespace(getLogger=Mock(), getProperty=lambda _: "."),
            tag=SimpleNamespace(configure=self.configure), historian=SimpleNamespace(queryRawPoints=self.query),
            date=SimpleNamespace(now=lambda: date(NOW), fromMillis=date),
        )
        self.namespace = {"system": system, "long": int, "basestring": str, "unicode": str}
        exec(compile(self.prepared, "bridge_019.py", "exec"), self.namespace)
        self.snapshot = Mock(side_effect=lambda *_: {
            "config": dict(self.config), "effective": dict(self.effective), "value": self.value, "valueGood": True,
        })
        self.namespace.update(_scoped_tag_path=lambda path: (ROOT, path.rsplit("/", 1)[1]),
                              _scoped_tag_snapshot=self.snapshot)
        self.migrate = self.namespace["handle_tags_scoped_cupper_migrate"]
        self.history = self.namespace["handle_tags_scoped_cupper_history"]

    def request(self):
        return {"path": SPEED, "mode": "prepare", "exclusiveAccess": True,
                "expectedConfig": dict(self.config), "historyProof": self.history({"path": SPEED})}

    def test_native_merge_uses_value_4_captured_after_history_read(self):
        request = self.request()
        read = self.query.side_effect

        def tick_during_history_read(**kwargs):
            self.value = 4
            return read(**kwargs)

        self.query.side_effect = tick_during_history_read
        self.assertEqual(self.migrate(request), {"valueBefore": 4})
        self.assertEqual(self.snapshot.call_count, 2)
        self.configure.assert_called_once_with(ROOT, [
            {"name": "Speed", "valueSource": "memory", "defaultValue": 4, "value": 4},
        ], "m")
        self.assertEqual(request["expectedConfig"], self.config)

    def test_history_queries_exact_speed_path_and_same_window(self):
        proof = self.history({"path": SPEED})
        self.assertEqual(proof["end"] - proof["start"], 30 * 60 * 1000)
        self.assertEqual(proof["end"], NOW - 300000)
        window = {"path": SPEED, "start": proof["start"], "end": proof["end"]}
        self.rows.append((SPEED, 4, 192, date(proof["end"])))
        self.assertEqual(self.history(window), proof)
        self.rows.append((SPEED, 4, 192, date(proof["end"] - 1)))
        self.assertNotEqual(self.history(window)["sha256"], proof["sha256"])
        self.assertEqual(proof["count"], 1)
        args = self.query.call_args.kwargs
        self.assertEqual((args["startTime"].getTime(), args["endTime"].getTime()), (proof["start"], proof["end"]))
        self.assertEqual(args["returnSize"], -1)
        self.assertEqual(args["paths"], [
            "histprov:Postgres_Historian:/drv:Ignition-Prox-Maker:default:/tag:Line_1/Cupper/Ignition/Speed",
        ])

    def test_changed_history_or_configuration_blocks_write(self):
        request = self.request()
        self.rows.append((SPEED, 99, 192, date(NOW - 310000)))
        with self.assertRaisesRegex(ValueError, "history no longer matches"):
            self.migrate(request)
        request = self.request()
        before = self.snapshot()
        altered = {**before, "effective": dict(before["effective"], historySampleRate=11)}
        self.snapshot.side_effect = [before, altered]
        with self.assertRaisesRegex(ValueError, "10-second history"):
            self.migrate(request)
        self.configure.assert_not_called()

    def test_rejects_an_out_of_scope_tag_and_reports_configuration_quality(self):
        request = self.request()
        self.configure.return_value = [BAD]
        with self.assertRaisesRegex(ValueError, r"Error_Configuration \(-1073741055\)"):
            self.migrate(request)
        self.configure.assert_called_once()
        with self.assertRaisesRegex(ValueError, "limited"):
            self.migrate(dict(request, path=ROOT + "/Status"))
        self.configure.assert_called_once()

    def test_production_uses_existing_history_and_configure_arguments(self):
        path = ROOT + "/Production"
        self.config.update(name="Production", valueSource="memory")
        self.effective.update(valueSource="memory")
        proof = self.history({"path": path})
        self.assertNotIn("returnSize", self.query.call_args.kwargs)
        self.migrate({"path": path, "mode": "prepare", "exclusiveAccess": True, "expectedConfig": dict(self.config),
                      "expectedValue": 57, "historyProof": proof})
        self.configure.assert_called_once()
        parent, raw, policy = self.configure.call_args.args
        self.assertEqual((parent, policy), (ROOT, "m"))
        self.assertEqual(json.loads(raw), [{"name": "Production", "dataType": "Int8", "value": 57}])

    def test_speed_restore_uses_original_config_and_existing_overwrite_policy(self):
        original = dict(self.config)
        self.config.update(valueSource="memory", value=4, defaultValue=4)
        self.effective.update(valueSource="memory", defaultValue=4)
        self.value = 4
        request = {"path": SPEED, "mode": "restore", "exclusiveAccess": True, "expectedConfig": dict(self.config),
                   "expectedValue": 4, "beforeConfig": original}
        self.assertEqual(self.migrate(request), {"valueBefore": 4})
        self.configure.assert_called_once_with(ROOT, [original], "o")
        self.configure.reset_mock()
        self.value = 5
        with self.assertRaisesRegex(ValueError, "Speed has advanced"):
            self.migrate(request)
        self.configure.assert_not_called()

    def test_update_preserves_routes_scope_and_secret_and_refuses_live_drift(self):
        _, old, assignments, _ = release019._library(self.before)
        _, new, updated, secret = release019._library(self.prepared)
        self.assertEqual(secret, SECRET)
        self.assertEqual(release019._constant(updated, "BRIDGE_VERSION"), "0.1.9")
        for name in ("HANDLERS", "TAG_CONFIG_ROOTS", "BRIDGE_SECRET"):
            self.assertEqual(ast.dump(assignments[name][0]), ast.dump(updated[name][0]))
        old_funcs, new_funcs = release019._functions(old), release019._functions(new)
        self.assertEqual({name for name in old_funcs if ast.dump(old_funcs[name]) != ast.dump(new_funcs[name])},
                         release019.REPLACEMENTS)
        with self.assertRaisesRegex(Exception, "reviewed Bridge 0.1.8"):
            release019.prepare(self.before.replace(b"'defaultValue': value, 'value': value}",
                                                   b"'defaultValue': 0, 'value': value}", 1), self.fragment)

    def test_installer_reconstructs_backup_before_write_and_confirms_active_library(self):
        saved = self.before
        active = "0.1.8"
        gateway, bridge = Mock(), Mock()
        gateway.get_json.return_value = {"ignitionVersion": "8.3.9 (build)", "name": "Ignition-Prox-Maker"}
        gateway.get_bytes.side_effect = lambda _: project_zip(saved)
        routes = list(release019._handlers(release019._library(saved)[2])[0])
        bridge.ping.side_effect = lambda: {"version": active, "capabilities": routes}

        def scan():
            nonlocal active
            active = "0.1.9"

        def call(endpoint, payload):
            nonlocal saved
            self.assertEqual(payload["path"], upgrade.GATEWAY_LIBRARY)
            if endpoint == "files/read":
                return {"content": saved.decode()}
            self.assertEqual(endpoint, "files/write")
            backup, = root.glob("*/code.py.redacted")
            self.assertNotIn(SECRET.encode(), backup.read_bytes())
            self.assertEqual(upgrade.rebind_secret(backup.read_bytes(), SECRET), self.before)
            saved = payload["content"].encode()
            return {"written": payload["path"]}

        gateway.scan_projects.side_effect = scan
        bridge.call.side_effect = call
        with tempfile.TemporaryDirectory(dir=BRIDGE) as directory:
            root = Path(directory)
            result = upgrade.upgrade(
                gateway, bridge, SECRET, root, self.fragment, old=release019.OLD, new=release019.NEW,
                prepare_update=release019.prepare, gateway_name="Ignition-Prox-Maker",
                verify_existing_tags=False, verify_library_after_scan=True,
            )
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(result["runtime_version"], "0.1.9")
        self.assertTrue(result["active_library_verified"])
        self.assertNotIn(SECRET, str(result))
        self.assertEqual(gateway.get_bytes.call_count, 2)
        self.assertEqual([call.args[0] for call in bridge.call.call_args_list].count("files/write"), 1)
