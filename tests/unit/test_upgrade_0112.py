"""Exercise the narrow scope change prepared for Bridge 0.1.12."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

BRIDGE = Path(__file__).resolve().parents[2] / "bridge"
with patch.object(sys, "path", [str(BRIDGE), *sys.path]):
    import apply_upgrade_0112 as release
    from upgrade_011 import CANDIDATE, UpgradeError, _handlers, _library


class ScopedPathsUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.fragment = release.FRAGMENT.read_bytes()
        self.live = CANDIDATE.read_bytes().replace(b"BRIDGE_VERSION = '0.1.2'", b"BRIDGE_VERSION = '0.1.11'", 1)
        self.live = self.live.replace(
            b"HANDLERS = {\n", self.fragment + b"\n\nHANDLERS = {\n    'history/raw': handle_history_raw,\n", 1,
        )

    def test_routes_and_existing_scopes_are_preserved(self):
        for eol in (b"\n", b"\r\n"):
            with self.subTest(eol=eol):
                live = self.live.replace(b"\n", eol)
                updated = release.prepare(live, self.fragment)
                self.assertEqual(_library(live)[3], _library(updated)[3])
                self.assertEqual(_handlers(_library(live)[2])[0].keys(), _handlers(_library(updated)[2])[0].keys())
                if eol == b"\r\n":
                    self.assertNotIn(b"\n", updated.replace(eol, b""))

    def test_new_scope_allows_only_four_exact_paths_and_two_history_reads(self):
        updated = release.prepare(self.live, self.fragment)
        system = SimpleNamespace(util=SimpleNamespace(getLogger=Mock(), getProperty=lambda _: "."))
        namespace = {"system": system, "long": int, "basestring": str, "unicode": str}
        exec(compile(updated, "bridge_0112.py", "exec"), namespace)
        namespace["_scoped_find_child"] = lambda *_: "Folder"
        check_path = namespace["_scoped_tag_path"]
        self.assertEqual(namespace["TAG_CONFIG_PATHS"], frozenset(release.TAG_PATHS))
        for path in release.TAG_PATHS + ("[default]Line_1/Cupper/Ignition/MCP_Test_SourceMigration",):
            self.assertEqual(check_path(path), (path.rpartition("/")[0], path.rpartition("/")[2]))
        for path in ("[default]Line_1/BM1/Ignition/Extra", "[default]Line_1/BM1/Ignition/speed",
                     "[default]Line_1/BM1/Ignition/Speed.value", "[default]Line_1/BM1/PLC/Speed",
                     "[default]Line_1/Other/Ignition/Speed"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                check_path(path)
        self.assertEqual(set(namespace["RAW_HISTORY_PATHS"]),
                         set(release.HISTORY_PATHS) | {"[default]Line_1/Cupper/Ignition/Speed"})
        for path, qualified in release.HISTORY_PATHS.items():
            self.assertEqual(namespace["RAW_HISTORY_PATHS"][path], qualified)

    def test_existing_policy_drift_aborts_preparation(self):
        for drifted in (
            self.live.replace(b"Cupper/Ignition',))", b"Other/Ignition',))", 1),
            self.live.replace(b"parent not in TAG_CONFIG_ROOTS", b"parent in TAG_CONFIG_ROOTS", 1),
            self.live.replace(b"histprov:Postgres_Historian", b"histprov:Other", 1),
        ):
            with self.subTest(drift=drifted != self.live), self.assertRaises(UpgradeError):
                release.prepare(drifted, self.fragment)


if __name__ == "__main__":
    unittest.main()
