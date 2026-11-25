"""Pass a native tag list to the Production migration on the test Gateway."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from apply_upgrade_012 import main
from apply_upgrade_019 import FRAGMENT as LEGACY_FRAGMENT
from upgrade_011 import _check, _constant, _functions, _handlers, _library, _one

OLD = "0.1.10"
NEW = "0.1.11"
FRAGMENT = Path(__file__).with_name("src") / "cupper_tag_migrations.py"
ROUTE = "tags/scoped-cupper-migrate"
HANDLER = "handle_tags_scoped_cupper_migrate"
OLD_CALL = b"system.tag.configure(parent, json.dumps([patch]), 'm')"
NEW_CALL = b"system.tag.configure(parent, [patch], 'm')"


def prepare(live: bytes, candidate: bytes) -> bytes:
    _, tree, assignments, secret = _library(live)
    _check(_constant(assignments, "BRIDGE_VERSION") == OLD, f"Expected Bridge {OLD} on disk")
    routes, _ = _handlers(assignments)
    _check(isinstance(routes.get(ROUTE), ast.Name) and routes[ROUTE].id == HANDLER,
           "Unexpected Production migration route")

    legacy = LEGACY_FRAGMENT.read_bytes().replace(b"\r\n", b"\n")
    _check(legacy.count(OLD_CALL) == 1 and candidate.replace(b"\r\n", b"\n") == legacy.replace(OLD_CALL, NEW_CALL, 1),
           "Candidate must change only the Production configure argument")
    handler = _functions(tree).get(HANDLER)
    _check(handler is not None and ast.dump(handler) == ast.dump(_functions(ast.parse(legacy))[HANDLER])
           and live.count(OLD_CALL) == 1, "Live migration code differs from the reviewed Bridge 0.1.10")

    version = _one(assignments, "BRIDGE_VERSION")
    lines = live.splitlines(keepends=True)
    index = version.lineno - 1
    _check(lines[index].count(OLD.encode()) == 1, "Unexpected Bridge version declaration")
    lines[index] = lines[index].replace(OLD.encode(), NEW.encode(), 1)
    result = b"".join(lines).replace(OLD_CALL, NEW_CALL, 1)

    _, updated, actual, updated_secret = _library(result)
    _check(updated_secret == secret and _constant(actual, "BRIDGE_VERSION") == NEW
           and ast.dump(_functions(updated)[HANDLER]) == ast.dump(_functions(ast.parse(candidate))[HANDLER]),
           "Prepared update must preserve the secret and use the reviewed Production patch")
    return result


if __name__ == "__main__":
    sys.exit(main(candidate=FRAGMENT, lock_name=".upgrade-0111.lock", old=OLD, new=NEW,
                  prepare_update=prepare, gateway_name="Ignition-Prox-Maker",
                  verify_existing_tags=False, verify_library_after_scan=True))
