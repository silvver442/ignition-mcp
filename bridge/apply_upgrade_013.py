"""Install the reviewed Cupper migration handlers without changing any tags."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from apply_upgrade_012 import main, same_ast
from upgrade_011 import _check, _constant, _functions, _handlers, _library, _one

OLD = "0.1.2"
NEW = "0.1.3"
FRAGMENT = Path(__file__).with_name("baselines") / "cupper_tag_migrations_013.py"
ROUTES = {
    "tags/scoped-cupper-history": "handle_tags_scoped_cupper_history",
    "tags/scoped-cupper-migrate": "handle_tags_scoped_cupper_migrate",
}
PRODUCTION = "[default]Line_1/Cupper/Ignition/Production"


def prepare(live, candidate):
    code, tree, assignments, secret = _library(live)
    _check(_constant(assignments, "BRIDGE_VERSION") == OLD, "Expected Bridge 0.1.2 on disk")
    scope = "TAG_CONFIG_ROOTS = frozenset(('[default]Line_1/Cupper/Ignition',))"
    _check(same_ast(scope, _one(assignments, "TAG_CONFIG_ROOTS")), "Unexpected tag write scope")
    previous, handlers = _handlers(assignments)
    _check(not previous.keys() & ROUTES.keys(), "Cupper routes already exist")
    previous_functions = _functions(tree)
    fragment = candidate.decode("utf-8")
    fragment_tree = ast.parse(fragment)
    functions = _functions(fragment_tree)
    _check(len(fragment_tree.body) == len(functions) and set(ROUTES.values()) <= functions.keys()
           and not functions.keys() & previous_functions.keys(), "Unexpected migration fragment")

    lines = code.splitlines(keepends=True)
    eol = "\r\n" if lines[0].endswith("\r\n") else "\n"
    version = _one(assignments, "BRIDGE_VERSION").lineno - 1
    dispatch = _one(assignments, "HANDLERS").lineno - 1
    new_routes = "".join(f"    {route!r}: {handler},{eol}" for route, handler in ROUTES.items())
    lines[handlers.lineno:handlers.lineno] = [new_routes]
    lines[dispatch:dispatch] = [fragment.replace("\r\n", "\n").rstrip().replace("\n", eol) + eol * 3]
    lines[version] = f"BRIDGE_VERSION = {NEW!r}{eol}"
    result = "".join(lines).encode("utf-8")
    _, updated_tree, updated, active_secret = _library(result)
    routes, _ = _handlers(updated)
    _check(secret == active_secret and set(routes) == set(previous) | set(ROUTES)
           and all(ast.dump(routes[key]) == ast.dump(value) for key, value in previous.items()),
           "The update changed the secret or a previous route")
    for name, node in _functions(updated_tree).items():
        if name in previous_functions:
            _check(ast.dump(node) == ast.dump(previous_functions[name]), "An existing handler was changed")
    return result


def verify_history_read(bridge):
    proof = bridge.call("tags/scoped-cupper-history", {"path": PRODUCTION})
    _check(isinstance(proof, dict) and {"start", "end", "count", "sha256"} <= proof.keys(),
           "Production historian did not return a raw history fingerprint")
    return {"production_history": proof}


if __name__ == "__main__":
    sys.exit(main(candidate=FRAGMENT, lock_name=".upgrade-013.lock", old=OLD, new=NEW, prepare_update=prepare,
                  added_routes=frozenset(ROUTES), verify_after=verify_history_read, gateway_name="Ignition-Prox-Maker"))
