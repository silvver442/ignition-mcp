"""Use native tag definitions for both edits in the existing source migration test."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from apply_upgrade_012 import main, same_ast
from apply_upgrade_016 import HANDLER, ROUTE, verify_after
from apply_upgrade_017 import FRAGMENT as BASELINE
from upgrade_011 import _check, _constant, _functions, _handlers, _library, _one

OLD = "0.1.7"
NEW = "0.1.8"
FRAGMENT = Path(__file__).with_name("src") / "cupper_source_test.py"
CONFIGURE_ARGUMENTS = (
    ("system.tag.configure(parent, json.dumps([patch]), 'm')",
     "system.tag.configure(parent, [patch], 'm')"),
    ("system.tag.configure(parent, json.dumps([definition]), 'o')",
     "system.tag.configure(parent, [definition], 'o')"),
)


def replace_configure_arguments(source):
    for old, new in CONFIGURE_ARGUMENTS:
        _check(source.count(old) == 1, "Expected one reviewed configure call for each edit")
        source = source.replace(old, new)
    return source


def prepare(live: bytes, candidate: bytes) -> bytes:
    code, tree, assignments, secret = _library(live)
    _check(_constant(assignments, "BRIDGE_VERSION") == OLD, f"Expected Bridge {OLD} on disk")
    scope = "TAG_CONFIG_ROOTS = frozenset(('[default]Line_1/Cupper/Ignition',))"
    _check(same_ast(scope, _one(assignments, "TAG_CONFIG_ROOTS")), "Unexpected tag write scope")
    route = _handlers(assignments)[0].get(ROUTE)
    _check(isinstance(route, ast.Name) and route.id == HANDLER, "Unexpected source test route")

    baseline = BASELINE.read_text(encoding="utf-8")
    expected = ast.parse(replace_configure_arguments(baseline))
    _check(ast.dump(ast.parse(candidate)) == ast.dump(expected),
           "The candidate must change only the two configure arguments")
    for name, reviewed in _functions(ast.parse(baseline)).items():
        matches = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name]
        _check(name not in assignments and len(matches) == 1 and ast.dump(matches[0]) == ast.dump(reviewed),
               "Live source test differs from the reviewed Bridge 0.1.7")

    lines = code.splitlines(keepends=True)
    version = _one(assignments, "BRIDGE_VERSION").lineno - 1
    eol = "\r\n" if lines[version].endswith("\r\n") else "\n"
    lines[version] = f"BRIDGE_VERSION = {NEW!r}{eol}"
    handler = _functions(tree)[HANDLER]
    start, end = handler.lineno - 1, handler.end_lineno
    lines[start:end] = [replace_configure_arguments("".join(lines[start:end]))]
    result = "".join(lines).encode("utf-8")

    _, new_tree, new_assignments, new_secret = _library(result)
    expected_handler = _functions(expected)[HANDLER]
    _check(secret == new_secret and _constant(new_assignments, "BRIDGE_VERSION") == NEW
           and ast.dump(_functions(new_tree)[HANDLER]) == ast.dump(expected_handler),
           "Prepared library differs from the reviewed update")
    return result


if __name__ == "__main__":
    sys.exit(main(candidate=FRAGMENT, lock_name=".upgrade-018.lock", old=OLD, new=NEW,
                  prepare_update=prepare, verify_after=verify_after, gateway_name="Ignition-Prox-Maker",
                  verify_existing_tags=False, verify_library_after_scan=True))
