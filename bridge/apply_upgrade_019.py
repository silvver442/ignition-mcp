"""Install the native Speed migration and fixed-window historian verification."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from apply_upgrade_012 import excerpt, main, same_ast
from apply_upgrade_013 import FRAGMENT as HISTORICAL_FRAGMENT, ROUTES, prepare as prepare_013
from upgrade_011 import CANDIDATE, _check, _constant, _functions, _handlers, _library, _one

OLD = "0.1.8"
NEW = "0.1.9"
FRAGMENT = Path(__file__).with_name("baselines") / "cupper_tag_migrations_019.py"
REPLACEMENTS = set(ROUTES.values()) | {"_cupper_history_proof"}


def prepare(live: bytes, candidate: bytes) -> bytes:
    from apply_upgrade_014 import FRAGMENT as READ_FRAGMENT, prepare as prepare_014

    code, tree, assignments, secret = _library(live)
    _check(_constant(assignments, "BRIDGE_VERSION") == OLD, f"Expected Bridge {OLD} on disk")
    scope = "TAG_CONFIG_ROOTS = frozenset(('[default]Line_1/Cupper/Ignition',))"
    _check(same_ast(scope, _one(assignments, "TAG_CONFIG_ROOTS")), "Unexpected tag write scope")
    routes, _ = _handlers(assignments)
    _check(all(isinstance(routes.get(route), ast.Name) and routes[route].id == name
               for route, name in ROUTES.items()), "Unexpected Cupper routes")

    fragment_013 = HISTORICAL_FRAGMENT.read_bytes()
    stage_013 = prepare_013(CANDIDATE.read_bytes(), fragment_013)
    reviewed = _functions(ast.parse(prepare_014(stage_013, READ_FRAGMENT.read_bytes())))
    fragment = candidate.decode("utf-8")
    fragment_tree = ast.parse(fragment)
    desired = _functions(fragment_tree)
    _check(len(fragment_tree.body) == len(desired) and desired.keys() == _functions(ast.parse(fragment_013)).keys(),
           "Unexpected Speed migration fragment")
    targets = []
    for name, replacement in desired.items():
        matches = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name]
        _check(name not in assignments and len(matches) == 1 and ast.dump(matches[0]) == ast.dump(reviewed[name]),
               "Live migration functions differ from the reviewed Bridge 0.1.8")
        if name in REPLACEMENTS:
            targets.append((matches[0], excerpt(fragment, replacement)))
        else:
            _check(ast.dump(replacement) == ast.dump(reviewed[name]), "Unexpected shared helper change")

    version = _one(assignments, "BRIDGE_VERSION")
    lines = code.splitlines(keepends=True)
    eol = "\r\n" if lines[0].endswith("\r\n") else "\n"
    targets.append((version, f"BRIDGE_VERSION = {NEW!r}"))
    for node, replacement in sorted(targets, key=lambda edit: edit[0].lineno, reverse=True):
        lines[node.lineno - 1:node.end_lineno] = [replacement.replace("\n", eol) + eol]
    result = "".join(lines).encode("utf-8")

    _, new_tree, new_assignments, new_secret = _library(result)
    new_functions = _functions(new_tree)
    replaced = {node for node, _ in targets}
    changed = {_one(new_assignments, "BRIDGE_VERSION"), *(new_functions[name] for name in REPLACEMENTS)}
    _check(new_secret == secret and _constant(new_assignments, "BRIDGE_VERSION") == NEW
           and [ast.dump(node) for node in tree.body if node not in replaced]
           == [ast.dump(node) for node in new_tree.body if node not in changed],
           "The update changed unrelated Bridge code")
    return result


if __name__ == "__main__":
    sys.exit(main(candidate=FRAGMENT, lock_name=".upgrade-019.lock", old=OLD, new=NEW,
                  prepare_update=prepare, gateway_name="Ignition-Prox-Maker",
                  verify_existing_tags=False, verify_library_after_scan=True))
