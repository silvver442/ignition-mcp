"""Add exact simulation tag paths to the existing scoped Bridge routes."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from upgrade_011 import CANDIDATE, _check, _constant, _functions, _handlers, _library, _one

OLD = "0.1.11"
NEW = "0.1.12"
GATEWAY = "Ignition-Prox-Maker"
FOLDER = "Line_1/BM1/Ignition"
TAG_PATHS = tuple(f"[default]{FOLDER}/{name}" for name in ("Status", "Speed", "Outfeed_Counter", "Production"))
HISTORY_PATHS = {
    f"[default]{FOLDER}/{name}": f"histprov:Postgres_Historian:/drv:{GATEWAY}:default:/tag:{FOLDER}/{name}"
    for name in ("Speed", "Production")
}
FRAGMENT = Path(__file__).with_name("src") / "raw_history.py"
OLD_SCOPE = "parent not in TAG_CONFIG_ROOTS"
NEW_SCOPE = "(parent not in TAG_CONFIG_ROOTS and path not in TAG_CONFIG_PATHS)"


def prepare(live: bytes, candidate: bytes) -> bytes:
    source, tree, assignments, secret = _library(live)
    _check(_constant(assignments, "BRIDGE_VERSION") == OLD, f"Expected Bridge {OLD} on disk")
    roots = _one(assignments, "TAG_CONFIG_ROOTS")
    expected_roots = ast.parse("TAG_CONFIG_ROOTS = frozenset(('[default]Line_1/Cupper/Ignition',))").body[0]
    functions = _functions(tree)
    _check(ast.dump(roots) == ast.dump(expected_roots) and "TAG_CONFIG_PATHS" not in assignments.keys() | functions.keys(),
           "Unexpected scoped tag policy")
    scoped = functions["_scoped_tag_path"]
    reviewed_scoped = _functions(ast.parse(CANDIDATE.read_bytes()))["_scoped_tag_path"]
    _check(ast.dump(scoped) == ast.dump(reviewed_scoped), "Scoped tag check differs from the reviewed Bridge")

    raw_nodes = ast.parse(candidate).body
    raw_paths = _one(assignments, "RAW_HISTORY_PATHS")
    routes, _ = _handlers(assignments)
    _check(len(raw_nodes) == 2 and ast.dump(raw_paths) == ast.dump(raw_nodes[0])
           and ast.dump(functions["handle_history_raw"]) == ast.dump(raw_nodes[1])
           and isinstance(routes.get("history/raw"), ast.Name) and routes["history/raw"].id == "handle_history_raw",
           "Raw history policy differs from the reviewed Bridge")
    history_paths = ast.literal_eval(raw_paths.value)
    _check(not history_paths.keys() & HISTORY_PATHS.keys(), "Raw history paths already exist")

    lines = source.splitlines(keepends=True)
    scope = "\n".join(source.splitlines()[scoped.lineno - 1:scoped.end_lineno])
    _check(scope.count(OLD_SCOPE) == 1, "Unexpected tag path check")
    version = _one(assignments, "BRIDGE_VERSION")
    eol = "\r\n" if lines[version.lineno - 1].endswith("\r\n") else "\n"
    updated_history = "RAW_HISTORY_PATHS = {\n" + "".join(
        f"    {path!r}: {historical_path!r},\n" for path, historical_path in {**history_paths, **HISTORY_PATHS}.items()
    ) + "}"
    edits = {
        version: f"BRIDGE_VERSION = {NEW!r}",
        roots: lines[roots.lineno - 1].rstrip("\r\n") + f"\nTAG_CONFIG_PATHS = frozenset({TAG_PATHS!r})",
        scoped: scope.replace(OLD_SCOPE, NEW_SCOPE, 1),
        raw_paths: updated_history,
    }
    for node, replacement in sorted(edits.items(), key=lambda item: item[0].lineno, reverse=True):
        lines[node.lineno - 1:node.end_lineno] = [replacement.replace("\n", eol) + eol]
    result = "".join(lines).encode("utf-8")

    _, updated, actual, updated_secret = _library(result)
    changed = {_one(actual, name) for name in ("BRIDGE_VERSION", "TAG_CONFIG_ROOTS", "TAG_CONFIG_PATHS", "RAW_HISTORY_PATHS")}
    changed.add(_functions(updated)["_scoped_tag_path"])
    _check(updated_secret == secret and _constant(actual, "BRIDGE_VERSION") == NEW
           and [ast.dump(node) for node in tree.body if node not in edits]
           == [ast.dump(node) for node in updated.body if node not in changed],
           "The update changed unrelated Bridge behavior")
    return result


def verify_scoped_reads(bridge):
    from ignition_mcp.errors import IgnitionMcpError

    for path in TAG_PATHS:
        result = bridge.call("tags/scoped-state", {"path": path})
        _check(result.get("path") == path and "config" in result, "Could not confirm an approved tag path")
    for path in (f"[default]{FOLDER}/Unlisted", "[default]Line_1/BM1/PLC/Speed"):
        try:
            bridge.call("tags/scoped-state", {"path": path})
        except IgnitionMcpError as exc:
            _check("approved Ignition folder" in exc.message, "Unexpected out-of-scope response")
        else:
            _check(False, "Bridge accepted an out-of-scope tag path")
    return {"scoped_tag_paths_verified": list(TAG_PATHS), "unlisted_paths_rejected": True}


if __name__ == "__main__":
    from apply_upgrade_012 import main

    sys.exit(main(candidate=FRAGMENT, lock_name=".upgrade-0112.lock", old=OLD, new=NEW,
                  prepare_update=prepare, gateway_name=GATEWAY, verify_existing_tags=False,
                  verify_library_after_scan=True, verify_after=verify_scoped_reads))
