"""Regression tests for the isolated Gateway route and additive library update."""

import ast
import json
import runpy
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

BRIDGE = Path(__file__).resolve().parents[2] / "bridge"
ROOT = "[default]Line_1/Cupper/Ignition"
PATH = ROOT + "/MCP_Test_SourceMigration"
NAME = PATH.rsplit("/", 1)[1]
EXPRESSION = {"name": NAME, "tagType": "AtomicTag", "dataType": "Int4", "valueSource": "expr",
              "expression": "getsecond(now())", "historyEnabled": False}
GOOD = SimpleNamespace(getName=lambda: "Good", getCode=lambda: 192, isGood=lambda: True)
BAD = SimpleNamespace(getName=lambda: "Bad_Test", getCode=lambda: 554, isGood=lambda: False)
CONFIGURATION_ERROR = SimpleNamespace(
    getName=lambda: "Error_Configuration", getCode=lambda: -1073741055, isGood=lambda: False,
)


class TestGatewayProbe(unittest.TestCase):
    def setUp(self):
        self.tag = Mock()
        self.tag.configure.return_value = [GOOD]
        self.snapshot = Mock()
        self.scope = Mock(return_value=(ROOT, NAME))
        self.result = Mock()
        self.route = runpy.run_path(str(BRIDGE / "src/cupper_source_test.py"), init_globals={
            "system": SimpleNamespace(tag=self.tag), "TAG_CONFIG_LOCK": threading.RLock(),
            "_scoped_tag_path": self.scope, "_scoped_tag_snapshot": self.snapshot,
            "_scoped_result": self.result, "json": json, "long": int, "unicode": str,
        })["handle_tags_scoped_cupper_source_test"]

    def call(self, action, config=None, **changes):
        self.snapshot.return_value = {"config": config, "effective": config or {}, "valueGood": True, "value": 42}
        return self.route({"path": PATH, "action": action, "expectedConfig": config,
                           "exclusiveAccess": True, **changes})

    def test_create_uses_a_native_tag_definition_with_abort(self):
        result = self.call("create")
        self.tag.configure.assert_called_once_with(ROOT, [EXPRESSION], "a")
        self.assertIs(self.tag.configure.call_args.args[1][0]["historyEnabled"], False)
        self.assertEqual(result, {"count": 1, "qualities": [{"name": "Good", "code": 192, "good": True}],
                                  "valueBefore": None})

    def test_create_reports_configuration_error_after_one_attempt(self):
        self.tag.configure.return_value = [CONFIGURATION_ERROR]

        result = self.call("create")

        self.tag.configure.assert_called_once_with(ROOT, [EXPRESSION], "a")
        self.tag.deleteTags.assert_not_called()
        self.assertEqual(result, {"count": 1, "qualities": [
            {"name": "Error_Configuration", "code": -1073741055, "good": False},
        ], "valueBefore": None})

    def test_create_returns_only_fixed_diagnostic_identifiers(self):
        quality = Mock(spec=["getName", "getCode", "isGood", "getDiagnosticMessage"])
        quality.getName.return_value = "Error_Configuration"
        quality.getCode.return_value = -1073741055
        quality.isGood.return_value = False
        self.tag.configure.return_value = [quality]
        secret = "opaque-value-that-must-stay-private"
        for message, expected in (
            (f"Could not parse expression: token={secret}", "invalid_expression"),
            (f"Unexpected error with {secret}", "redacted"),
            (f"{secret:>1024} access denied", "redacted"),
            (None, None),
        ):
            with self.subTest(expected=expected):
                quality.getDiagnosticMessage.return_value = message
                result = self.call("create")
                self.assertEqual(result.get("diagnostic"), expected)
                self.assertNotIn(secret, json.dumps(result))
        quality.getDiagnosticMessage.side_effect = RuntimeError(secret)
        self.assertNotIn("diagnostic", self.call("create"))

    def test_other_actions_and_ambiguous_results_do_not_read_diagnostics(self):
        getter = Mock(return_value="access denied")
        bad = SimpleNamespace(**vars(BAD), getDiagnosticMessage=getter)
        good = SimpleNamespace(**vars(GOOD), getDiagnosticMessage=getter)
        for results in ([good], [None], [bad, good], []):
            self.tag.configure.return_value = results
            self.assertNotIn("diagnostic", self.call("create"))
        self.tag.configure.return_value = [bad]
        for action in ("merge", "overwrite"):
            self.assertNotIn("diagnostic", self.call(action, EXPRESSION))
        getter.assert_not_called()

    def test_merge_passes_only_the_native_patch_and_preserves_quality_details(self):
        self.tag.configure.return_value = [BAD, GOOD]
        result = self.call("merge", EXPRESSION)
        self.tag.configure.assert_called_once_with(ROOT, [{
            "name": NAME, "valueSource": "memory", "defaultValue": 42, "value": 42,
        }], "m")
        self.assertEqual(result, {"count": 2, "qualities": [
            {"name": "Bad_Test", "code": 554, "good": False}, {"name": "Good", "code": 192, "good": True},
        ], "valueBefore": 42})

    def test_overwrite_passes_native_full_config_without_expression(self):
        config = {**EXPRESSION, "documentation": "kept", "tagGroup": "Default"}
        self.call("overwrite", config)
        expected = {**config, "valueSource": "memory", "defaultValue": 42, "value": 42}
        del expected["expression"]
        self.tag.configure.assert_called_once_with(ROOT, [expected], "o")
        self.assertEqual(config, {**EXPRESSION, "documentation": "kept", "tagGroup": "Default"})

    def test_failed_edits_preserve_expression_snapshot_and_do_not_retry(self):
        config = dict(EXPRESSION)
        self.tag.configure.return_value = [CONFIGURATION_ERROR]
        for action, policy in (("merge", "m"), ("overwrite", "o")):
            with self.subTest(action=action):
                self.tag.configure.reset_mock()
                result = self.call(action, config)
                self.tag.configure.assert_called_once()
                self.assertEqual(self.tag.configure.call_args.args[2], policy)
                self.assertEqual(result, {"count": 1, "qualities": [
                    {"name": "Error_Configuration", "code": -1073741055, "good": False},
                ], "valueBefore": 42})
                self.assertEqual(config, EXPRESSION)
        self.tag.deleteTags.assert_not_called()

    def test_rejects_live_tags_other_folders_injected_fields_and_stale_config(self):
        for path in (ROOT + "/Speed", ROOT + "/Production", ROOT + "/Status", ROOT + "/Outfeed_Counter",
                     PATH.replace("/Ignition/", "/PLC/")):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.call("merge", EXPRESSION, path=path)
        for changes in ({"exclusiveAccess": 1}, {"patch": EXPRESSION}, {"expectedConfig": {}}):
            with self.assertRaises(ValueError):
                self.call("merge", EXPRESSION, **changes)
        with self.assertRaises(ValueError):
            self.call("create", EXPRESSION)
        with self.assertRaises(ValueError):
            self.call("merge", {**EXPRESSION, "historyEnabled": True})
        self.tag.configure.assert_not_called()
        self.tag.deleteTags.assert_not_called()

    def test_undo_only_deletes_the_reviewed_expression_tag(self):
        with self.assertRaises(ValueError):
            self.call("undo_create", {**EXPRESSION, "valueSource": "memory"})
        self.assertEqual(self.call("undo_create", EXPRESSION), {"deleted": True})
        self.tag.deleteTags.assert_called_once_with([PATH])


class TestBridgePreparation(unittest.TestCase):
    def test_preparation_adds_one_route_to_verified_015(self):
        with patch.object(sys, "path", [str(BRIDGE), *sys.path]):
            upgrade = runpy.run_path(str(BRIDGE / "apply_upgrade_016.py"))
        source = (BRIDGE / "src/mcp_bridge_lib.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        quality = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                       and node.name == "_scoped_result")
        fixture = ("BRIDGE_VERSION = '0.1.5'\nBRIDGE_SECRET = '__BRIDGE_SECRET__'\n"
                   "TAG_CONFIG_ROOTS = frozenset(('[default]Line_1/Cupper/Ignition',))\n"
                   + ast.get_source_segment(source, quality) + "\n"
                   "HANDLERS = {\n    'tags/scoped-cupper-migrate': existing,\n}\n"
                   "def handle(request):\n    return _verify(request.get('data'), endpoint.startswith('tags/scoped-cupper-'))\n")
        prepare = upgrade["prepare"]
        fragment = upgrade["FRAGMENT"].read_bytes()
        for eol in ("\n", "\r\n"):
            payload = fixture.replace("\n", eol).encode()
            candidate = prepare(payload, fragment)
            self.assertIn(b"BRIDGE_VERSION = '0.1.6'", candidate)
            self.assertIn(b"'tags/scoped-cupper-source-test': handle_tags_scoped_cupper_source_test", candidate)
            self.assertIn(b"system.tag.configure(parent, json.dumps([definition]), 'a')", candidate)
            self.assertIn(ast.get_source_segment(source, quality).replace("\n", eol).encode(), candidate)
            self.assertIn(b"BRIDGE_SECRET = '__BRIDGE_SECRET__'", candidate)
        altered = fixture.replace("code.getName()", "str(code)").encode()
        with self.assertRaisesRegex(Exception, "reviewed 0.1.5 result diagnostics"):
            prepare(altered, fragment)

        expanded_scope = fixture.replace("/Ignition',", "/Ignition', '[default]Other',").encode()
        with self.assertRaisesRegex(Exception, "Unexpected tag write scope"):
            prepare(expanded_scope, fragment)

        conflicting_global = fixture + "\n_source_test_quality = existing\n"
        with self.assertRaisesRegex(Exception, "Unexpected source migration test fragment"):
            prepare(conflicting_global.encode(), fragment)


if __name__ == "__main__":
    unittest.main()
