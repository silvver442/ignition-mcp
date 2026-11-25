"""Exercise the scoped project import with a simulated REST Gateway."""

from dataclasses import replace
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

from ignition_mcp.app import AppContext
from ignition_mcp.config import load_settings
from ignition_mcp.errors import NotFoundError, PermissionDeniedError
from ignition_mcp.tools import projects


def zip_bytes(enabled, timer_source=b"def handleTimerEvent():\n    pass\n", extra=None):
    stream = BytesIO()
    with ZipFile(stream, "w") as archive:
        archive.writestr("project.json", json.dumps({"enabled": enabled, "inheritable": False, "parent": ""}))
        timer = "ignition/timer/Timer"
        archive.writestr(timer + "/resource.json", json.dumps({"files": ["handleTimerEvent.py"]}))
        archive.writestr(timer + "/handleTimerEvent.py", timer_source)
        if extra:
            archive.writestr(extra, "test")
    return stream.getvalue()


class ToolRegistry:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def register(function):
            self.tools[function.__name__] = function
            return function
        return register


class ProjectImportTests(unittest.TestCase):
    def setUp(self):
        workspace = TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(workspace.cleanup)
        previous = Path.cwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(workspace.name)
        self.target = "Simulator"
        self.zip_path = Path("project.zip")
        self.package = zip_bytes(False)
        self.zip_path.write_bytes(self.package)
        self.settings = load_settings({
            "IGNITION_URL": "http://gateway:8088", "IGNITION_API_TOKEN": "test:fixture",
            "IGNITION_ALLOW_WRITES": "true", "IGNITION_RESTRICTED_DEPLOYMENT": "true",
            "IGNITION_WRITE_PROJECT_ALLOWLIST": "ExistingPerspective",
            "IGNITION_PROJECT_IMPORT_ALLOWLIST": self.target,
        })
        self.client = Mock()
        self.bridge = Mock()
        self.ctx = AppContext(self.settings, self.client, self.bridge)
        self.ctx.trial_warning = lambda: None
        self.registry = ToolRegistry()
        projects.register(self.registry, self.ctx)
        self.import_project = self.registry.tools["project_import"]

    def plan(self):
        return self.import_project(self.target, str(self.zip_path), dry_run=True)

    def apply(self, plan, **overrides):
        return self.import_project(self.target, str(self.zip_path), **{
            "confirm": True, "exclusive_access": True, "expected_state": plan["base_state"],
            "expected_zip_sha256": plan["zip_sha256"], **overrides,
        })

    def test_import_and_other_writes_are_scoped(self):
        for name in ("ExistingPerspective", "Other", "simulator"):
            self.assertEqual(self.import_project(name, "unused.zip", confirm=True)["error"], "write_not_allowed")
        for result in (self.registry.tools["project_create"](self.target, confirm=True),
                       self.registry.tools["project_scan"]()):
            self.assertEqual(result["error"], "write_not_allowed")
        self.ctx.settings = replace(self.settings, project_import_allowlist=())
        self.assertEqual(self.plan()["error"], "write_not_allowed")
        self.assertEqual(self.client.mock_calls, [])

    def test_import_requires_writes_confirm_and_exclusive_access(self):
        self.assertEqual(self.import_project(self.target, "unused.zip")["error"], "write_not_allowed")
        self.assertEqual(self.import_project(self.target, "unused.zip", confirm=True)["error"], "validation_error")
        self.ctx.settings = replace(self.settings, allow_writes=False)
        result = self.import_project(self.target, "unused.zip", confirm=True, exclusive_access=True)
        self.assertEqual(result["error"], "write_not_allowed")
        self.assertEqual(self.client.mock_calls, [])

    def test_first_import_uses_no_overwrite_and_compares_actual_export(self):
        missing = NotFoundError("target absent")
        self.client.get_bytes.side_effect = [missing, missing, self.package]
        self.client.get_json.return_value = {"name": self.target, "enabled": False}
        plan = self.plan()
        self.assertEqual((plan["base_state"], plan["overwrite"], plan["enabled"]), ("absent", False, False))
        self.client.post_bytes.assert_not_called()
        result = self.apply(plan)
        self.assertTrue(result["verified"])
        self.assertNotIn("backup", result)
        self.client.post_bytes.assert_called_once_with(
            "projects/import/Simulator", self.package, "application/zip", params={"overwrite": "false"},
        )
        self.assertEqual(self.bridge.mock_calls, [])

    def test_replacement_saves_full_backup_before_post(self):
        before = self.package
        self.package = zip_bytes(True)
        self.zip_path.write_bytes(self.package)
        self.client.get_bytes.side_effect = [before, before, before, self.package]
        self.client.get_json.return_value = {"name": self.target, "enabled": True}

        def check_backup(*args, **kwargs):
            backups = list(Path("backups/mcp-project-imports/Simulator").glob("*/before.zip"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), before)

        self.client.post_bytes.side_effect = check_backup
        result = self.apply(self.plan())
        self.assertTrue(result["verified"])
        self.assertEqual(result["backup"]["sha256"], sha256(before).hexdigest())
        self.assertEqual(Path(result["backup"]["path"]).read_bytes(), before)
        self.client.post_bytes.assert_called_once_with(
            "projects/import/Simulator", self.package, "application/zip", params={"overwrite": "true"},
        )

    def test_backup_failure_and_live_drift_prevent_post(self):
        self.client.get_bytes.side_effect = [self.package, self.package]
        plan = self.plan()
        with patch.object(projects, "_save_backup", side_effect=OSError):
            self.assertEqual(self.apply(plan)["error"], "gateway_error")
        self.client.post_bytes.assert_not_called()
        self.client.get_bytes.side_effect = [self.package, zip_bytes(True)]
        self.assertEqual(self.apply(plan)["status"], "conflict")
        self.client.post_bytes.assert_not_called()

    def test_changed_zip_or_project_state_never_posts(self):
        self.client.get_bytes.side_effect = [NotFoundError("target absent"), self.package]
        plan = self.plan()
        self.assertEqual(self.apply(plan)["error"], "project_import_conflict")
        self.client.get_bytes.side_effect = NotFoundError("target absent")
        self.zip_path.write_bytes(zip_bytes(True))
        self.assertEqual(self.apply(plan)["error"], "project_import_conflict")
        self.client.post_bytes.assert_not_called()

    def test_permission_denial_and_mismatched_readback_are_not_retried(self):
        self.client.get_bytes.side_effect = NotFoundError("target absent")
        plan = self.plan()
        self.client.post_bytes.side_effect = PermissionDeniedError()
        self.assertEqual(self.apply(plan)["status"], "denied")
        self.client.post_bytes.assert_called_once()
        self.client.post_bytes.reset_mock(side_effect=True)
        self.client.get_bytes.side_effect = [NotFoundError("target absent"), zip_bytes(True)]
        self.client.get_json.return_value = {"name": self.target, "enabled": True}
        self.assertEqual(self.apply(plan)["status"], "uncertain")
        self.client.post_bytes.assert_called_once()

    def test_bad_package_and_invalid_import_allowlist_fail_closed(self):
        self.zip_path.write_bytes(zip_bytes(False, extra="Project.json"))
        self.assertEqual(self.plan()["error"], "validation_error")
        self.client.get_bytes.assert_not_called()
        with self.assertRaisesRegex(ValueError, "IGNITION_PROJECT_IMPORT_ALLOWLIST"):
            load_settings({"IGNITION_URL": "http://gateway:8088", "IGNITION_API_TOKEN": "test:fixture",
                           "IGNITION_PROJECT_IMPORT_ALLOWLIST": "*"})


if __name__ == "__main__":
    unittest.main()
