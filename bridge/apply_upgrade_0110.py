"""Install the read-only raw historian route on the test Gateway."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from apply_upgrade_012 import excerpt, main
from apply_upgrade_019 import FRAGMENT as MIGRATION_FRAGMENT
from upgrade_011 import _check, _constant, _functions, _handlers, _library, _one

OLD = "0.1.9"
NEW = "0.1.10"
ROUTE = "history/raw"
HANDLER = "handle_history_raw"
ALLOWLIST = "RAW_HISTORY_PATHS"
FRAGMENT = Path(__file__).with_name("src") / "raw_history.py"
NATIVE_DECODE = "endpoint.startswith('tags/scoped-cupper-')"


def prepare(live: bytes, candidate: bytes) -> bytes:
    source, tree, assignments, secret = _library(live)
    _check(_constant(assignments, "BRIDGE_VERSION") == OLD, f"Expected Bridge {OLD} on disk")
    routes, dispatch_table = _handlers(assignments)
    functions = _functions(tree)
    _check(ROUTE not in routes and not {ALLOWLIST, HANDLER} & (assignments.keys() | functions.keys()),
           "Unexpected existing raw history route or symbols")
    reviewed = _functions(ast.parse(MIGRATION_FRAGMENT.read_bytes()))
    _check(all(name in functions and ast.dump(functions[name]) == ast.dump(node)
               for name, node in reviewed.items()),
           "The live Bridge does not match the reviewed Speed migration code")

    fragment = candidate.decode("utf-8").replace("\r\n", "\n").rstrip()
    fragment_nodes = ast.parse(fragment).body
    _check(len(fragment_nodes) == 2, "Unexpected raw history fragment")
    allowlist, handler = fragment_nodes
    _check(isinstance(allowlist, ast.Assign) and len(allowlist.targets) == 1
           and isinstance(allowlist.targets[0], ast.Name) and allowlist.targets[0].id == ALLOWLIST
           and isinstance(handler, ast.FunctionDef) and handler.name == HANDLER,
           "Unexpected raw history fragment")

    version, dispatch = (_one(assignments, name) for name in ("BRIDGE_VERSION", "HANDLERS"))
    request_handler = functions["handle"]
    request_source = excerpt(source, request_handler)
    _check(request_source.count(NATIVE_DECODE) == 1, "Unexpected Bridge request decoder")
    request_source = request_source.replace(NATIVE_DECODE, f"{NATIVE_DECODE} or endpoint == {ROUTE!r}")
    _check(dispatch_table.lineno == dispatch.lineno and dispatch_table.end_lineno > dispatch.lineno,
           "Expected a multiline Bridge dispatch table")
    lines = source.splitlines(keepends=True)
    eol = "\r\n" if lines[version.lineno - 1].endswith("\r\n") else "\n"
    edits = [
        (request_handler.lineno - 1, request_handler.end_lineno - request_handler.lineno + 1,
         request_source + "\n"),
        (dispatch_table.lineno, 0, f"    {ROUTE!r}: {HANDLER},\n"),
        (dispatch.lineno - 1, 0, fragment + "\n\n\n"),
        (version.lineno - 1, 1, f"BRIDGE_VERSION = {NEW!r}\n"),
    ]
    for index, count, replacement in sorted(edits, reverse=True):
        lines[index:index + count] = [replacement.replace("\n", eol)]
    result = "".join(lines).encode("utf-8")

    _, updated, actual, updated_secret = _library(result)
    new_routes, _ = _handlers(actual)
    new_functions = _functions(updated)
    ignored = {
        _one(actual, name) for name in ("BRIDGE_VERSION", "HANDLERS", ALLOWLIST)
    } | {new_functions[HANDLER], new_functions["handle"]}
    _check(secret == updated_secret and _constant(actual, "BRIDGE_VERSION") == NEW
           and set(new_routes) == set(routes) | {ROUTE}
           and ast.dump(new_routes[ROUTE]) == ast.dump(ast.Name(id=HANDLER, ctx=ast.Load()))
           and all(ast.dump(new_routes[key]) == ast.dump(value) for key, value in routes.items())
           and ast.dump(new_functions["handle"]) == ast.dump(ast.parse(request_source).body[0])
           and [ast.dump(node) for node in tree.body if node not in (version, dispatch, request_handler)]
           == [ast.dump(node) for node in updated.body if node not in ignored],
           "The update changed existing Bridge behavior")
    return result


if __name__ == "__main__":
    sys.exit(main(candidate=FRAGMENT, lock_name=".upgrade-0110.lock", old=OLD, new=NEW,
                  prepare_update=prepare, added_routes=frozenset({ROUTE}),
                  gateway_name="Ignition-Prox-Maker",
                  verify_existing_tags=False, verify_library_after_scan=True))
