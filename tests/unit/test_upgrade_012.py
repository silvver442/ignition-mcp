"""Verify the one-time upgrade without a Gateway or real credentials."""

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from zipfile import ZipFile

BRIDGE_DIR = Path(__file__).resolve().parents[2] / "bridge"
with patch.object(sys, "path", [str(BRIDGE_DIR), *sys.path]):
    import apply_upgrade_012 as update
    import apply_upgrade_017 as update017
    import apply_upgrade_018 as update018
    import upgrade_011 as previous


SECRET = "synthetic-test-value-for-a-bridge-credential"


def live_bridge(candidate):
    start, helper = candidate.decode().split("def _scoped_export_config(", 1)
    snapshot, end = "def _scoped_tag_snapshot(", helper.split("def _scoped_tag_snapshot(", 1)[1]
    start = (start.replace(update.NEW, update.OLD, 1).replace("import json\n", "", 1)
             .replace("'defaultValue', ", "", 1).replace(update.PLACEHOLDER, SECRET, 1))
    return (start + snapshot + end.replace(update.EXPORT_CALL, update.LEGACY_CONFIG, 1)).encode()


def project_zip(library):
    output = io.BytesIO()
    api = previous.WEBDEV
    with ZipFile(output, "w") as archive:
        archive.writestr("project.json", "{}")
        archive.writestr(update.LIBRARY, library)
        archive.writestr(update.LIBRARY.replace("code.py", "resource.json"), '{"files":["code.py"]}')
        archive.writestr(f"{api}/config.json", "{}")
        archive.writestr(f"{api}/doPost", "WebDev delegation")
        archive.writestr(f"{api}/resource.json", '{"files":["config.json","doPost"]}')
    return output.getvalue()


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.candidate = update.CANDIDATE.read_bytes()
        self.before = live_bridge(self.candidate)
        self.saved = self.before
        self.running = update.OLD
        self.log = []
        self.gateway = Mock()
        self.gateway.get_json.return_value = {"ignitionVersion": "8.3.9 (build)"}
        self.gateway.get_bytes.return_value = project_zip(self.before)
        self.gateway.scan_projects.side_effect = self.scan
        self.bridge = Mock()
        routes = previous._handlers(previous._library(self.before)[2])[0]
        self.bridge.ping.side_effect = lambda: {"version": self.running, "capabilities": list(routes)}
        self.bridge.call.side_effect = self.call
        temp = tempfile.TemporaryDirectory(dir=BRIDGE_DIR)
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)

    def scan(self):
        self.log.append("scan")
        self.running = update.NEW

    def call(self, endpoint, payload):
        self.log.append(endpoint)
        if endpoint == "files/read":
            return {"content": self.saved.decode()}
        if endpoint == "files/write":
            backup, = self.folder.glob("*/code.py.redacted")
            manifest = json.loads(backup.with_name("manifest.json").read_text())
            self.assertNotIn(SECRET.encode(), backup.read_bytes())
            self.assertEqual(previous._library(backup.read_bytes())[3], update.PLACEHOLDER)
            self.assertEqual(manifest["sha256_reconstructed"], update.sha(self.before))
            self.assertEqual(payload["path"], update.GATEWAY_LIBRARY)
            self.saved = payload["content"].encode()
            return {"written": payload["path"]}
        self.assertEqual(endpoint, "tags/scoped-state")
        name = payload["path"].rsplit("/", 1)[-1]
        config = {"name": name, "valueSource": "expr", "historyEnabled": True,
                  "historyProvider": "Postgres_Historian"}
        if name == "Outfeed_Counter":
            config = {"name": name, "valueSource": "memory", "dataType": "Int4",
                      "historyEnabled": False, "value": 0}
        return {"path": payload["path"], "config": config, "effective": {}, "valueGood": True, "value": 0}

    def run_upgrade(self, secret=SECRET):
        return update.upgrade(self.gateway, self.bridge, secret, self.folder, self.candidate)

    def test_update_contains_only_the_scoped_patch(self):
        after = update.prepare(self.before, self.candidate)
        _, old_tree, old, _ = previous._library(self.before)
        _, new_tree, new, secret = previous._library(after)
        self.assertEqual(secret, SECRET)
        self.assertEqual(previous._constant(new, "BRIDGE_VERSION"), update.NEW)
        self.assertTrue(update.same_ast(update.excerpt(self.before.decode(), old["TAG_CONFIG_ROOTS"][0]),
                                        new["TAG_CONFIG_ROOTS"][0]))
        for name, node in previous._functions(old_tree).items():
            if name != "_scoped_tag_snapshot":
                self.assertTrue(update.same_ast(update.excerpt(self.before.decode(), node),
                                                previous._functions(new_tree)[name]), name)

    def test_backup_precedes_single_write_and_all_four_tags_are_read_back(self):
        result = self.run_upgrade()
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(self.log, ["files/read", "files/read", "files/write", "files/read", "scan",
                                    *(["tags/scoped-state"] * 4)])
        self.assertEqual(result["tags"]["Status"]["historyProvider"], "Postgres_Historian")

    def test_cannot_write_when_the_backup_cannot_be_reconstructed(self):
        result = self.run_upgrade(secret="different-synthetic-bridge-credential")
        self.assertEqual(result["phase"], "backup")
        self.assertFalse(result["write_attempted"])
        self.assertEqual(self.log, ["files/read"])

    def test_bad_post_write_quality_is_not_success(self):
        def bad_quality(endpoint, payload):
            result = self.call(endpoint, payload)
            if endpoint == "tags/scoped-state" and payload["path"] == update.PATHS[0]:
                result["valueGood"] = False
            return result

        self.bridge.call.side_effect = bad_quality
        result = self.run_upgrade()
        self.assertEqual(result["phase"], "verify_tags")
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(self.log.count("files/write"), 1)

    def test_route_only_upgrade_verifies_capabilities_without_reading_existing_tags(self):
        verify_capabilities = Mock(return_value={"source_migration_test_available": True})
        result = update.upgrade(
            self.gateway, self.bridge, SECRET, self.folder, self.candidate,
            verify_existing_tags=False, verify_after=verify_capabilities,
        )
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(self.log, ["files/read", "files/read", "files/write", "files/read", "scan"])
        self.assertTrue(result["source_migration_test_available"])
        self.assertNotIn("tags", result)
        verify_capabilities.assert_called_once_with(self.bridge)


class SourceTestUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.release = update017
        self.fragment = update017.FRAGMENT.read_bytes()
        routes = (*previous.SCOPED, "files/read", "files/write", update017.ROUTE)
        self.before = (
            f"BRIDGE_VERSION = '{update017.OLD}'\nBRIDGE_SECRET = '{SECRET}'\n"
            "TAG_CONFIG_ROOTS = frozenset(('[default]Line_1/Cupper/Ignition',))\n"
        ).encode() + update017.BASELINE.read_bytes() + (
            "\nHANDLERS = {\n"
            + "".join(f"    {route!r}: {update017.HANDLER if route == update017.ROUTE else 'existing'},\n"
                      for route in routes)
            + "}\n"
        ).encode()
        self.saved = self.before
        self.running = update017.OLD
        self.events = []
        self.gateway = Mock()
        self.gateway.get_json.return_value = {"ignitionVersion": "8.3.9 (build)", "name": "Ignition-Prox-Maker"}
        self.gateway.get_bytes.side_effect = self.export
        self.gateway.scan_projects.side_effect = self.scan
        self.bridge = Mock()
        self.bridge.ping.side_effect = lambda: {"version": self.running, "capabilities": list(routes)}
        self.bridge.call.side_effect = self.call
        temp = tempfile.TemporaryDirectory(dir=BRIDGE_DIR)
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)

    def export(self, path):
        self.assertEqual(path, "projects/export/mcp-bridge")
        self.events.append("export")
        return project_zip(self.saved)

    def scan(self):
        self.events.append("scan")
        self.running = self.release.NEW

    def call(self, endpoint, payload):
        self.events.append(endpoint)
        self.assertEqual(payload["path"], update.GATEWAY_LIBRARY)
        if endpoint == "files/read":
            return {"content": self.saved.decode()}
        self.assertEqual(endpoint, "files/write")
        backup, = self.folder.glob("*/code.py.redacted")
        self.assertNotIn(SECRET.encode(), backup.read_bytes())
        self.assertEqual(update.rebind_secret(backup.read_bytes(), SECRET), self.before)
        self.saved = payload["content"].encode()
        return {"written": payload["path"]}

    def run_upgrade(self):
        return update.upgrade(
            self.gateway, self.bridge, SECRET, self.folder, self.fragment,
            old=self.release.OLD, new=self.release.NEW, prepare_update=self.release.prepare,
            verify_after=self.release.verify_after, gateway_name="Ignition-Prox-Maker",
            verify_existing_tags=False, verify_library_after_scan=True,
        )

    def use_release_018(self):
        self.before = update017.prepare(self.before, self.fragment)
        self.saved = self.before
        self.release = update018
        self.fragment = update018.FRAGMENT.read_bytes()
        self.running = update018.OLD

    def test_only_create_transport_and_diagnostic_change(self):
        for eol in (b"\n", b"\r\n"):
            with self.subTest(eol=eol):
                before = self.before.replace(b"\n", eol)
                after = update017.prepare(before, self.fragment)
                self.assertEqual(previous._library(after)[3], SECRET)
                self.assertEqual(update017.prepare(self.before, self.fragment).replace(b"\n", eol), after)
                self.assertIn(update017.NEW_CREATE.encode(), after)
                self.assertIn(b"json.dumps([patch]), 'm'", after)
                self.assertIn(b"json.dumps([definition]), 'o'", after)
                self.assertIn(b"'expression': 'getsecond(now())', 'historyEnabled': False", after)

    def test_rejects_drift_or_a_candidate_that_changes_other_behavior(self):
        for old, new in ((b"0.1.6", b"0.1.7"), (b"getsecond(now())", b"getminute(now())"),
                         (b"[default]Line_1/Cupper/Ignition',", b"[default]Line_1/Cupper/PLC',")):
            with self.subTest(old=old), self.assertRaises(previous.UpgradeError):
                update017.prepare(self.before.replace(old, new), self.fragment)
        for old, new in ((b"getsecond(now())", b"getminute(now())"), (b"[definition], 'a'", b"[definition], 'o'"),
                         (b"json.dumps([patch]), 'm'", b"json.dumps([patch]), 'o'")):
            with self.subTest(old=old), self.assertRaises(previous.UpgradeError):
                update017.prepare(self.before, self.fragment.replace(old, new))

    def test_upgrade_backs_up_and_verifies_runtime_against_two_reads(self):
        report = self.run_upgrade()
        self.assertEqual(report["status"], "success", report)
        self.assertEqual(report["runtime_version"], "0.1.7")
        self.assertTrue(report["active_library_verified"] and report["source_migration_test_available"])
        self.assertEqual(report["library_sha256"], update.sha(self.saved))
        self.assertNotIn(SECRET, json.dumps(report))
        self.assertEqual(self.events, ["export", "files/read", "files/read", "files/write", "files/read",
                                       "scan", "files/read", "export"])

    def test_failed_backup_prevents_write_and_scan(self):
        with patch.object(update, "backup_library", side_effect=OSError("disk full")):
            result = self.run_upgrade()
        self.assertFalse(result["write_attempted"])
        self.assertEqual(result["phase"], "backup")
        self.assertEqual(self.events, ["export", "files/read"])

    def test_post_scan_export_mismatch_prevents_success(self):
        self.gateway.get_bytes.side_effect = [project_zip(self.before), project_zip(self.before)]
        result = self.run_upgrade()
        self.assertEqual(result["status"], "uncertain", result)
        self.assertEqual(result["phase"], "verify_active_library")
        self.assertNotIn("active_library_verified", result)
        self.assertEqual(self.events.count("files/write"), 1)

    def test_018_patch_changes_exactly_two_arguments_and_the_version(self):
        self.use_release_018()
        for eol in (b"\n", b"\r\n"):
            with self.subTest(eol=eol):
                before = self.before.replace(b"\n", eol)
                expected = before.replace(b"BRIDGE_VERSION = '0.1.7'", b"BRIDGE_VERSION = '0.1.8'")
                expected = expected.replace(b"json.dumps([patch]), 'm'", b"[patch], 'm'")
                expected = expected.replace(b"json.dumps([definition]), 'o'", b"[definition], 'o'")
                self.assertEqual(update018.prepare(before, self.fragment), expected)
                self.assertEqual(previous._library(expected)[3], SECRET)

    def test_018_rejects_changes_to_reviewed_inputs_and_write_scope(self):
        self.use_release_018()
        for old, new in ((b"0.1.7", b"0.1.8"), (b"'memory'", b"'opc'"),
                         (b"/Cupper/Ignition',", b"/Cupper/PLC',"),
                         (b"'tags/scoped-cupper-source-test':", b"'tags/alternate':")):
            with self.subTest(old=old), self.assertRaises(previous.UpgradeError):
                update018.prepare(self.before.replace(old, new), self.fragment)
        with self.assertRaisesRegex(previous.UpgradeError, "two configure arguments"):
            update018.prepare(self.before, self.fragment.replace(b"'m'", b"'o'"))

    def test_018_verifies_backup_and_signed_readback_without_tag_calls(self):
        self.use_release_018()
        result = self.run_upgrade()
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(result["runtime_version"], "0.1.8")
        self.assertTrue(result["active_library_verified"] and result["source_migration_test_available"])
        self.assertEqual(result["backup"]["version"], "0.1.7")
        self.assertEqual(result["library_sha256"], update.sha(self.saved))
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(self.events, ["export", "files/read", "files/read", "files/write", "files/read",
                                       "scan", "files/read", "export"])

    def test_018_stops_when_backup_or_post_scan_export_cannot_be_verified(self):
        self.use_release_018()
        with patch.object(update, "backup_library", side_effect=OSError("disk full")):
            result = self.run_upgrade()
        self.assertEqual(result["phase"], "backup")
        self.assertFalse(result["write_attempted"])
        self.assertEqual(self.events, ["export", "files/read"])

        self.gateway.get_bytes.side_effect = [project_zip(self.before), project_zip(self.before)]
        result = self.run_upgrade()
        self.assertEqual(result["status"], "uncertain", result)
        self.assertEqual(result["phase"], "verify_active_library")
        self.assertNotIn("active_library_verified", result)
        self.assertEqual(self.events.count("files/write"), 1)


if __name__ == "__main__":
    unittest.main()
