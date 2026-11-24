"""Prepare the Bridge update for the dedicated Expression tag creation test."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from apply_upgrade_012 import excerpt, same_ast
from apply_upgrade_016 import FRAGMENT as BASELINE
from apply_upgrade_016 import HANDLER, ROUTE, FRAGMENT_FUNCTIONS, verify_after
from upgrade_011 import _check, _constant, _functions, _handlers, _library, _one

OLD = "0.1.6"
NEW = "0.1.7"
FRAGMENT = Path(__file__).with_name("baselines") / "cupper_source_test_017.py"
DIAGNOSTIC = "_source_test_create_result"
OLD_CREATE = "return _source_test_quality(system.tag.configure(parent, json.dumps([definition]), 'a'), None)"
NEW_CREATE = "return _source_test_create_result(system.tag.configure(parent, [definition], 'a'))"


def prepare(live: bytes, candidate: bytes) -> bytes:
    code, tree, assignments, secret = _library(live)
    _check(_constant(assignments, "BRIDGE_VERSION") == OLD, f"Expected Bridge {OLD} on disk")
    scope = "TAG_CONFIG_ROOTS = frozenset(('[default]Line_1/Cupper/Ignition',))"
    _check(same_ast(scope, _one(assignments, "TAG_CONFIG_ROOTS")), "Unexpected tag write scope")
    route = _handlers(assignments)[0].get(ROUTE)
    _check(isinstance(route, ast.Name) and route.id == HANDLER, "Unexpected source test route")

    reviewed = _functions(ast.parse(BASELINE.read_bytes()))
    functions = _functions(tree)
    _check(DIAGNOSTIC not in functions and not (FRAGMENT_FUNCTIONS | {DIAGNOSTIC}) & assignments.keys(),
           "Unexpected source test binding")
    for name in FRAGMENT_FUNCTIONS:
        matches = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name]
        _check(len(matches) == 1 and ast.dump(matches[0]) == ast.dump(reviewed[name]),
               "Live source test differs from the reviewed Bridge 0.1.6")

    fragment = candidate.decode("utf-8")
    fragment_tree = ast.parse(fragment)
    updated_functions = _functions(fragment_tree)
    _check(len(fragment_tree.body) == len(updated_functions)
           and updated_functions.keys() == FRAGMENT_FUNCTIONS | {DIAGNOSTIC},
           "Unexpected 0.1.7 source test fragment")
    for name in FRAGMENT_FUNCTIONS - {HANDLER}:
        _check(ast.dump(updated_functions[name]) == ast.dump(reviewed[name]), "Unexpected source test helper change")
    handler = functions[HANDLER]
    old_handler = excerpt(code, handler)
    _check(old_handler.count(OLD_CREATE) == 1
           and same_ast(old_handler.replace(OLD_CREATE, NEW_CREATE), updated_functions[HANDLER]),
           "The update must change only the create argument and diagnostic readout")

    lines = code.splitlines(keepends=True)
    eol = "\r\n" if lines[0].endswith("\r\n") else "\n"
    version = _one(assignments, "BRIDGE_VERSION")
    replacement = (excerpt(fragment, updated_functions[DIAGNOSTIC]) + "\n\n\n"
                   + excerpt(fragment, updated_functions[HANDLER]))
    for node, text in sorted([(version, f"BRIDGE_VERSION = {NEW!r}"), (handler, replacement)],
                             key=lambda edit: edit[0].lineno, reverse=True):
        lines[node.lineno - 1:node.end_lineno] = [text.replace("\n", eol) + eol]
    result = "".join(lines).encode("utf-8")

    _, new_tree, new_assignments, new_secret = _library(result)
    new_functions = _functions(new_tree)
    new_version = _one(new_assignments, "BRIDGE_VERSION")
    changed = {new_version, new_functions[HANDLER], new_functions[DIAGNOSTIC]}
    _check(secret == new_secret and _constant(new_assignments, "BRIDGE_VERSION") == NEW
           and [ast.dump(node) for node in tree.body if node not in (version, handler)]
           == [ast.dump(node) for node in new_tree.body if node not in changed],
           "The update changed unrelated Bridge code")
    return result


if __name__ == "__main__":
    from apply_upgrade_012 import main

    sys.exit(main(candidate=FRAGMENT, lock_name=".upgrade-017.lock", old=OLD, new=NEW,
                  prepare_update=prepare, verify_after=verify_after, gateway_name="Ignition-Prox-Maker",
                  verify_existing_tags=False, verify_library_after_scan=True))
