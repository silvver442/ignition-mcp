"""Exercise the WebDev handlers with a simulated Ignition tag tree."""

import json
import runpy
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

FOLDER = "[default]Line_1/Cupper/Ignition"
PATH = FOLDER + "/Outfeed_Counter"
GOOD = SimpleNamespace(isGood=lambda: True)


class TagSystem:
    def __init__(self):
        self.folders = {
            "[default]": "Line_1",
            "[default]Line_1": "Cupper",
            "[default]Line_1/Cupper": "Ignition",
        }
        self.config = None
        self.value = 0
        self.writes = []
        self.exports = []

    def browse(self, parent):
        if parent in self.folders:
            nodes = [{"name": self.folders[parent], "tagType": "Folder"}]
        elif parent == FOLDER and self.config:
            nodes = [self.config]
        else:
            nodes = []
        return SimpleNamespace(getResults=lambda: nodes)

    def getConfiguration(self, path, recursive):
        raise AssertionError("The scoped handler must not serialize raw Java tag properties")

    def exportTags(self, *, tagPaths, recursive, exportType):
        self.exports.append((tagPaths, recursive, exportType))
        assert tagPaths == [PATH] and recursive is False and exportType == "json"
        return json.dumps({"tags": [self.config] if self.config else []})

    def readBlocking(self, paths):
        return [
            SimpleNamespace(value=self.value if path == PATH else self.config.get(path.split(".")[-1]),
                            quality=GOOD)
            for path in paths
        ]

    def configure(self, path, definitions, policy):
        self.writes.append((path, definitions, policy))
        update = definitions[0]
        self.config = {**self.config, **update} if policy == "m" else update
        return [GOOD]

    def deleteTags(self, paths):
        self.writes.append(("delete", paths))
        self.config = None
        return [GOOD]


class ScopedBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tags = TagSystem()
        system = SimpleNamespace(
            tag=self.tags,
            util=SimpleNamespace(getLogger=Mock(), getProperty=lambda _: ".",
                                 jsonEncode=json.dumps, jsonDecode=json.loads),
        )
        source = Path(__file__).resolve().parents[2] / "bridge" / "src" / "mcp_bridge_lib.py"
        self.bridge = runpy.run_path(str(source), init_globals={
            "system": system, "basestring": str, "long": int, "unicode": str,
        })
        self.create = self.bridge["handle_tags_scoped_configure"]
        self.restore = self.bridge["handle_tags_scoped_restore"]
        self.state = self.bridge["handle_tags_scoped_state"]
        self.request = {"path": PATH, "action": "create", "expectedConfig": None,
                        "dataType": "Int4", "value": 0}

    def test_scope_blocks_plc_other_machines_and_nested_tags(self):
        for path in (FOLDER + "/PLC/Counter", FOLDER + "/../Counter", FOLDER + "/Counter.value",
                     "[default]Line_1/Cupper/PLC/Counter", "[default]Line_1/BM1/Ignition/Counter"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.create({**self.request, "path": path})
        self.assertEqual(self.tags.writes, [])

    def test_folder_or_udt_at_target_cannot_be_changed(self):
        for kind in ("Folder", "UdtInstance"):
            self.tags.config = {"name": "Outfeed_Counter", "tagType": kind}
            with self.assertRaises(ValueError):
                self.create(self.request)
        self.assertEqual(self.tags.writes, [])

    def test_create_readback_conflict_and_scoped_undo(self):
        self.assertIsNone(self.state({"path": PATH})["config"])
        self.assertEqual(self.tags.exports, [])
        after = self.create(self.request)
        self.assertEqual(self.tags.writes[0][2], "a")
        self.assertEqual(len(self.tags.exports), 1)
        self.assertEqual(after, self.state({"path": PATH}))
        self.assertEqual(after["config"]["value"], 0)
        self.assertEqual(after["effective"]["dataType"], "Int4")
        self.assertEqual(after["effective"]["valueSource"], "memory")
        self.assertTrue(after["valueGood"])
        self.assertEqual(after["value"], 0)
        self.assertFalse(after["effective"]["historyEnabled"])
        with self.assertRaises(ValueError):
            self.create(self.request)
        self.assertEqual(len(self.tags.writes), 1)
        restore = {"path": PATH, "expectedConfig": after["config"], "expectedValue": 0,
                   "beforeConfig": None}
        self.tags.value = 1
        with self.assertRaises(ValueError):
            self.restore(restore)
        self.tags.value = 0
        self.assertIsNone(self.restore(restore)["config"])
        self.assertEqual(self.tags.writes[-1], ("delete", [PATH]))

    def test_webdev_response_preserves_nested_configuration_and_historian(self):
        self.tags.config = {
            "name": "Outfeed_Counter", "tagType": "AtomicTag", "dataType": "Float8",
            "valueSource": "expr", "historyEnabled": True, "historyProvider": "Postgres_Historian",
            "historicalDeadband": 0.125,
            "alarms": [{"name": "Próg", "setpointA": {"bindType": "Expression", "value": "1+2"}}],
            "eventScripts": [{"eventid": "valueChanged", "script": "pass"}],
        }
        handle = self.bridge["handle"]
        handle.__globals__["_verify"] = Mock(return_value=({"path": PATH}, None))
        resource = Path(__file__).resolve().parents[2] / "bridge" / "src" / "doPost.py"
        post = runpy.run_path(str(resource), init_globals={"mcp_bridge_lib": SimpleNamespace(handle=handle)})["doPost"]
        body = post({"remainingPath": "/tags/scoped-state"}, None)["json"]
        payload = json.loads(json.dumps(body, allow_nan=False))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["config"], self.tags.config)
        self.assertEqual(payload["data"]["effective"]["historyProvider"], "Postgres_Historian")
        self.assertEqual(payload["data"]["effective"]["historicalDeadband"], 0.125)

    def test_rejects_empty_multiple_mismatched_or_non_atomic_exports(self):
        tag = {"name": "Outfeed_Counter", "tagType": "AtomicTag"}
        self.tags.config = tag
        for export in ("{", "[]", '{"tags":[]}', json.dumps({"tags": [tag, tag]}),
                       json.dumps({"tags": [{**tag, "name": "Different"}]}),
                       json.dumps({"tags": [{**tag, "tags": []}]}),
                       json.dumps({"tags": [{**tag, "tagType": "UdtInstance"}]})):
            self.tags.exportTags = Mock(return_value=export)
            with self.subTest(export=export), self.assertRaises(ValueError):
                self.create(self.request)
        self.assertEqual(self.tags.writes, [])

    def test_effective_defaults_are_kept_separate_from_the_exported_configuration(self):
        self.tags.config = {"name": "Outfeed_Counter", "tagType": "AtomicTag"}
        defaults = {"dataType": "Int4", "valueSource": "memory", "historyEnabled": False, "defaultValue": 0}
        bad = SimpleNamespace(isGood=lambda: False)
        self.tags.readBlocking = Mock(side_effect=lambda paths: [
            SimpleNamespace(value=0, quality=GOOD) if path == PATH else SimpleNamespace(
                value=defaults.get(path.rsplit(".", 1)[1]), quality=GOOD if path.rsplit(".", 1)[1] in defaults else bad,
            ) for path in paths
        ])
        snapshot = self.state({"path": PATH})
        self.assertEqual(snapshot["config"], self.tags.config)
        self.assertEqual(snapshot["effective"], defaults)

    def test_accepts_single_atomic_object_exported_by_ignition_839(self):
        self.tags.config = {
            "name": "Outfeed_Counter", "tagType": "AtomicTag", "dataType": "Int4",
            "valueSource": "memory", "historyEnabled": False, "defaultValue": 0, "value": 0,
        }
        self.tags.exportTags = Mock(return_value=json.dumps(self.tags.config))
        snapshot = self.state({"path": PATH})
        self.assertEqual(snapshot["config"], self.tags.config)
        self.assertEqual(snapshot["effective"]["defaultValue"], 0)
        self.assertTrue(snapshot["valueGood"])
        self.tags.exportTags.assert_called_once_with(tagPaths=[PATH], recursive=False, exportType="json")

    def test_native_export_failure_after_configure_never_causes_a_second_write(self):
        native_export = self.tags.exportTags
        self.tags.exportTags = Mock(side_effect=RuntimeError("export unavailable"))
        with self.assertRaises(RuntimeError):
            self.create(self.request)
        self.tags.exportTags = native_export
        self.assertEqual(self.state({"path": PATH})["value"], 0)
        with self.assertRaises(ValueError):
            self.create(self.request)
        self.assertEqual(len(self.tags.writes), 1)

    def test_history_can_merge_and_refuses_unsafe_fields(self):
        before = self.create(self.request)["config"]
        request = {"path": PATH, "action": "history", "expectedConfig": before}
        for patch in ({"value": 100}, {"tags": []}, {"historySampleRate": True},
                      {"historyEnabled": "yes"}, {"sampleMode": "Unknown"}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                self.create({**request, "history": patch})
        after = self.create({**request, "history": {"sampleMode": "Periodic"}})
        self.assertEqual(after["config"], {**before, "sampleMode": "Periodic"})
        self.assertEqual(self.tags.writes[-1][2], "m")


if __name__ == "__main__":
    unittest.main()
