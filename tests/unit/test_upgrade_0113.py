"""Exercise the 0.1.13 file-read handler and upgrade with real local file bytes."""

import ast
import sys
import unittest
from base64 import b64decode
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from .test_upgrade_012 import SECRET, project_zip

BRIDGE = Path(__file__).resolve().parents[2] / "bridge"
with patch.object(sys, "path", [str(BRIDGE), *sys.path]):
    import apply_upgrade_0113 as release
    from apply_upgrade_012 import backup_library, excerpt, rebind_secret, sha, upgrade
    from upgrade_011 import CANDIDATE, UpgradeError, _functions

PNG = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aG2cAAAAASUVORK5CYII="
)


class BinaryReadUpgradeTests(unittest.TestCase):
    def setUp(self):
        work = TemporaryDirectory(dir=BRIDGE)
        self.addCleanup(work.cleanup)
        self.root = Path(work.name)
        self.before = CANDIDATE.read_bytes().replace(
            b"BRIDGE_VERSION = '0.1.2'", b"BRIDGE_VERSION = '0.1.12'", 1,
        ).replace(b"__BRIDGE_SECRET__", SECRET.encode(), 1)
        self.candidate = release.FRAGMENT.read_bytes()
        self.after = release.prepare(self.before, self.candidate)

    def handlers(self, source):
        system = SimpleNamespace(util=SimpleNamespace(getLogger=Mock(), getProperty=lambda _: "."))
        module = {"system": system}
        exec(compile(source, "bridge_library.py", "exec"), module)
        module["PROJECTS_ROOT"] = str(self.root)
        module["_verify"] = lambda payload: (payload, None)
        return module

    def test_digests_of_png_and_one_byte_change(self):
        read = self.handlers(self.after)["handle_files_read"]
        thumbnail = self.root / "thumbnail.png"
        thumbnail.write_bytes(PNG)
        request = {"path": thumbnail.name, "format": "sha256"}
        expected = sha(PNG)
        self.assertEqual(read(request), {"sha256": expected})
        self.assertEqual(read(request), {"sha256": expected})
        changed = PNG[:-1] + bytes([PNG[-1] ^ 1])
        thumbnail.write_bytes(changed)
        self.assertEqual(read(request), {"sha256": sha(changed)})
        self.assertNotEqual(sha(changed), expected)

    def test_text_response_and_path_checks_are_preserved(self):
        read = self.handlers(self.after)["handle_files_read"]
        source = "Wiersz żółty\r\nDrugi wiersz\n"
        (self.root / "view.json").write_bytes(source.encode("utf-8"))
        self.assertEqual(read({"path": "view.json"}), {"content": source})
        self.assertEqual(read({"path": "view.json", "format": "sha256"}),
                         {"sha256": sha(source.encode("utf-8"))})
        self.assertEqual(read({"path": "absent.png", "format": "sha256"})["code"], "not_found")
        with self.assertRaises(ValueError):
            read({"path": "../outside", "format": "sha256"})
        (self.root / "thumbnail.png").write_bytes(PNG)
        self.assertTrue(read({"path": "thumbnail.png"})["__error__"])
        with patch("builtins.open", side_effect=OSError("unreadable")):
            self.assertTrue(read({"path": "thumbnail.png", "format": "sha256"})["__error__"])

    def test_release_rejects_drift_and_changes_to_text_mode(self):
        with self.assertRaises(UpgradeError):
            release.prepare(self.before.replace(b"with codecs.open(full, 'r', 'utf-8')", b"with open(full)"),
                            self.candidate)
        with self.assertRaises(UpgradeError):
            release.prepare(self.before, self.candidate.replace(b"return {'content': content}", b"return {}"))

    def test_upgrade_of_complete_library_matches_fresh_install_source(self):
        current = (BRIDGE / "src/mcp_bridge_lib.py").read_bytes()
        historical = CANDIDATE.read_text(encoding="utf-8")
        text_handler = excerpt(historical, _functions(ast.parse(historical))[release.HANDLER]).encode()
        self.assertEqual(current.count(self.candidate.strip()), 1)
        before = current.replace(self.candidate.strip(), text_handler, 1).replace(
            b"BRIDGE_VERSION = '0.1.13'", b"BRIDGE_VERSION = '0.1.12'", 1,
        )
        self.assertEqual(release.prepare(before, self.candidate), current)

    def run_upgrade(self, *, bad_digest=False, fail_backup=False):
        library = self.root / release.GATEWAY_LIBRARY
        library.parent.mkdir(parents=True)
        library.write_bytes(self.before)
        running = self.handlers(self.before)
        export = project_zip(self.before)
        writes = []

        def scan():
            nonlocal running, export
            running = self.handlers(library.read_bytes())
            export = project_zip(library.read_bytes())

        def call(endpoint, payload):
            if endpoint == "files/write":
                backup, = self.root.glob("bridge-*/code.py.redacted")
                self.assertNotIn(SECRET.encode(), backup.read_bytes())
                self.assertEqual(rebind_secret(backup.read_bytes(), SECRET), self.before)
                self.assertEqual(payload["path"], release.GATEWAY_LIBRARY)
                requested = payload["content"].encode("utf-8")
                self.assertEqual(requested, self.after)
                writes.append(payload["path"])
                library.write_bytes(requested)
                return {"written": payload["path"]}
            self.assertEqual(endpoint, "files/read")
            if bad_digest and payload.get("format") == "sha256":
                return {"sha256": "0" * 64}
            return running["handle"]({"remainingPath": endpoint, "data": payload})["json"]["data"]

        bridge = SimpleNamespace(call=call, ping=lambda: running["handle_ping"]({}))
        gateway = SimpleNamespace(
            get_json=lambda _: {"ignitionVersion": "8.3.9 (build)"},
            get_bytes=lambda _: export, scan_projects=scan,
        )
        with patch("apply_upgrade_012.backup_library", side_effect=OSError("disk full") if fail_backup else None,
                   wraps=backup_library):
            report = upgrade(
                gateway, bridge, SECRET, self.root, self.candidate,
                old=release.OLD, new=release.NEW, prepare_update=release.prepare,
                verify_existing_tags=False, verify_library_after_scan=True, verify_after=release.verify_file_read,
            )
        self.assertEqual(writes, [] if fail_backup else [release.GATEWAY_LIBRARY])
        return report

    def test_backup_and_independent_verification(self):
        report = self.run_upgrade()
        self.assertEqual(report["status"], "success", report)
        self.assertEqual(report["runtime_version"], "0.1.13")
        self.assertTrue(report["active_library_verified"] and report["raw_sha256_verified"])

    def test_invalid_digest_cannot_confirm_upgrade(self):
        report = self.run_upgrade(bad_digest=True)
        self.assertEqual(report["status"], "uncertain", report)
        self.assertEqual(report["phase"], "verify_capabilities")

    def test_failed_backup_blocks_upgrade(self):
        report = self.run_upgrade(fail_backup=True)
        self.assertEqual(report["phase"], "backup")
        self.assertFalse(report["write_attempted"])
