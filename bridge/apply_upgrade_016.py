"""Prepare the additive Bridge update for the dedicated Cupper migration test."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from upgrade_011 import CANDIDATE, _check, _constant, _functions, _handlers, _library, _one

OLD = "0.1.5"
NEW = "0.1.6"
FRAGMENT = Path(__file__).with_name("baselines") / "cupper_source_test_016.py"
ROUTE = "tags/scoped-cupper-source-test"
HANDLER = "handle_tags_scoped_cupper_source_test"
FRAGMENT_FUNCTIONS = {HANDLER, "_source_test_expression", "_source_test_quality"}


def prepare(live: bytes, candidate: bytes) -> bytes:
    code, tree, assignments, secret = _library(live)
    _check(_constant(assignments, "BRIDGE_VERSION") == OLD, f"Expected Bridge {OLD} on disk")
    scope = ast.parse("TAG_CONFIG_ROOTS = frozenset(('[default]Line_1/Cupper/Ignition',))").body[0]
    _check(ast.dump(_one(assignments, "TAG_CONFIG_ROOTS")) == ast.dump(scope), "Unexpected tag write scope")
    routes, handlers = _handlers(assignments)
    _check(ROUTE not in routes and "tags/scoped-cupper-migrate" in routes, "Unexpected Cupper routes")
    functions = _functions(tree)
    local = _functions(ast.parse(CANDIDATE.read_bytes()))
    _check(ast.dump(functions.get("_scoped_result", ast.Pass())) == ast.dump(local["_scoped_result"]),
           "The live Bridge does not contain the reviewed 0.1.5 result diagnostics")
    decoder = ast.parse("_verify(request.get('data'), endpoint.startswith('tags/scoped-cupper-'))", mode="eval")
    _check(any(ast.dump(node) == ast.dump(decoder.body) for node in ast.walk(functions["handle"])
               if isinstance(node, ast.Call)), "The Bridge is missing native Cupper JSON dispatch")

    fragment = candidate.decode("utf-8")
    fragment_tree = ast.parse(fragment)
    added = _functions(fragment_tree)
    _check(len(fragment_tree.body) == len(added) and added.keys() == FRAGMENT_FUNCTIONS
           and not added.keys() & (functions.keys() | assignments.keys()),
           "Unexpected source migration test fragment")

    eol = "\r\n" if code.splitlines(keepends=True)[0].endswith("\r\n") else "\n"
    version = _one(assignments, "BRIDGE_VERSION")
    dispatch = _one(assignments, "HANDLERS")
    _check(handlers.lineno == dispatch.lineno and handlers.end_lineno > handlers.lineno,
           "Expected a multiline Bridge dispatch table")
    lines = code.splitlines(keepends=True)
    for index, old_lines, replacement in sorted([
        (handlers.lineno, 0, f"    {ROUTE!r}: {HANDLER},"),
        (dispatch.lineno - 1, 0, fragment.replace("\r\n", "\n").rstrip() + "\n\n"),
        (version.lineno - 1, 1, f"BRIDGE_VERSION = {NEW!r}"),
    ], reverse=True):
        lines[index:index + old_lines] = [replacement.replace("\n", eol) + eol]
    result = "".join(lines).encode("utf-8")

    _, updated, actual, active_secret = _library(result)
    actual_routes, _ = _handlers(actual)
    updated_functions = _functions(updated)
    before = [ast.dump(node) for node in tree.body if node not in (version, dispatch)]
    expected_changes = {_one(actual, "BRIDGE_VERSION"), _one(actual, "HANDLERS")}
    expected_changes.update(updated_functions[name] for name in added)
    route = actual_routes.get(ROUTE)
    _check(secret == active_secret and _constant(actual, "BRIDGE_VERSION") == NEW
           and isinstance(route, ast.Name) and route.id == HANDLER
           and set(actual_routes) == set(routes) | {ROUTE}
           and all(ast.dump(actual_routes[key]) == ast.dump(value) for key, value in routes.items())
           and before == [ast.dump(node) for node in updated.body if node not in expected_changes],
           "The update changed existing Bridge behavior")
    return result


def verify_after(bridge):
    _check(ROUTE in bridge.ping().get("capabilities", []), "Source migration test route is not available")
    return {"source_migration_test_available": True}


if __name__ == "__main__":
    from apply_upgrade_012 import main

    sys.exit(main(candidate=FRAGMENT, lock_name=".upgrade-016.lock", old=OLD, new=NEW,
                  prepare_update=prepare, added_routes=frozenset({ROUTE}), verify_after=verify_after,
                  gateway_name="Ignition-Prox-Maker", verify_existing_tags=False))
