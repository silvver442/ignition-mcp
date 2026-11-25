"""Exercise the Production patch through the prepared Bridge 0.1.11 library."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from .test_upgrade_012 import SECRET

BRIDGE = Path(__file__).resolve().parents[2] / "bridge"
with patch.object(sys, "path", [str(BRIDGE), *sys.path]):
    import apply_upgrade_0111 as release
    from apply_upgrade_012 import CANDIDATE, rebind_secret
    from upgrade_011 import UpgradeError

ROOT = "[default]Line_1/Cupper/Ignition"
NOW = 1_790_000_000_000


def date(ms):
    return SimpleNamespace(getTime=lambda: ms)


def library_0110():
    source = rebind_secret(CANDIDATE.read_bytes(), SECRET)
    fragments = (BRIDGE / "src/cupper_tag_reads.py", release.LEGACY_FRAGMENT, BRIDGE / "src/raw_history.py")
    handlers = b"""HANDLERS = {
    'tags/scoped-cupper-migrate': handle_tags_scoped_cupper_migrate,
    'tags/scoped-cupper-history': handle_tags_scoped_cupper_history,
    'history/raw': handle_history_raw,
"""
    return source.replace(b"BRIDGE_VERSION = '0.1.2'", b"BRIDGE_VERSION = '0.1.10'", 1).replace(
        b"HANDLERS = {\n", b"\n\n".join(fragment.read_bytes() for fragment in fragments) + b"\n\n" + handlers, 1,
    )


class ProductionUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.live = library_0110()
        self.candidate = release.FRAGMENT.read_bytes()

    def test_update_changes_only_version_and_production_configure_argument(self):
        for eol in (b"\n", b"\r\n"):
            with self.subTest(eol=eol):
                live = self.live.replace(b"\n", eol)
                expected = live.replace(b"BRIDGE_VERSION = '0.1.10'", b"BRIDGE_VERSION = '0.1.11'", 1)
                expected = expected.replace(release.OLD_CALL, release.NEW_CALL, 1)
                self.assertEqual(release.prepare(live, self.candidate), expected)

    def test_update_rejects_drift_in_the_reviewed_migration(self):
        drifted = self.live.replace(b"latest != before", b"latest == before", 1)
        with self.assertRaisesRegex(UpgradeError, "Live migration code differs"):
            release.prepare(drifted, self.candidate)

    def test_migration_passes_native_patch_and_merge_policy(self):
        updated = release.prepare(self.live, self.candidate)
        cases = (
            ("Production", "prepare", "memory", "Int4", 1267, {"dataType": "Int8", "value": 1267}),
            ("Production", "restore", "memory", "Int8", 1267, {"dataType": "Int4", "value": 1267}),
            ("Speed", "prepare", "expr", "Int4", 4, {"valueSource": "memory", "defaultValue": 4, "value": 4}),
        )
        for name, mode, source, data_type, value, patch_args in cases:
            with self.subTest(name=name, mode=mode):
                path = ROOT + "/" + name
                config = {"name": name, "tagType": "AtomicTag", "valueSource": source, "dataType": data_type,
                          "defaultValue": 1230 if name == "Production" else None, "historyEnabled": True,
                          "historyProvider": "Postgres_Historian", "sampleMode": "Periodic",
                          "historySampleRate": 30 if name == "Production" else 10, "historySampleRateUnits": "SEC"}
                configure = Mock(return_value=[SimpleNamespace(isGood=lambda: True)])
                sample = (path, value, 192, date(NOW - 305000))
                system = SimpleNamespace(
                    util=SimpleNamespace(getLogger=Mock(), getProperty=lambda _: "."),
                    tag=SimpleNamespace(configure=configure),
                    historian=SimpleNamespace(queryRawPoints=lambda **_: SimpleNamespace(
                        getColumnCount=lambda: 4, getRowCount=lambda: 1, getValueAt=lambda _, column: sample[column],
                    )),
                    date=SimpleNamespace(now=lambda: date(NOW), fromMillis=date),
                )
                namespace = {"system": system, "long": int, "basestring": str, "unicode": str}
                exec(compile(updated, "bridge_0111.py", "exec"), namespace)
                snapshot = {"path": path, "config": config, "effective": config, "value": value, "valueGood": True}
                namespace.update(_scoped_tag_path=lambda _: (ROOT, name), _scoped_tag_snapshot=lambda *_: snapshot)
                proof = namespace["HANDLERS"]["tags/scoped-cupper-history"]({"path": path})
                request = {"path": path, "mode": mode, "exclusiveAccess": True,
                           "expectedConfig": dict(config), "historyProof": proof}
                if name == "Production":
                    request["expectedValue"] = value
                result = namespace["HANDLERS"][release.ROUTE](request)
                self.assertEqual(result, {"valueBefore": value})
                configure.assert_called_once_with(ROOT, [{"name": name, **patch_args}], "m")
