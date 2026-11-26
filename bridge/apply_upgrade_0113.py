"""Add an opt-in raw SHA-256 response to the existing signed files/read route."""

from __future__ import annotations

import ast
import sys
from copy import deepcopy
from pathlib import Path

from apply_upgrade_012 import GATEWAY_LIBRARY, main, read_library, sha
from upgrade_011 import CANDIDATE, _check, _constant, _functions, _handlers, _library, _one

OLD = "0.1.12"
NEW = "0.1.13"
FRAGMENT = Path(__file__).with_name("src") / "files_read_sha256.py"
HANDLER = "handle_files_read"
DIGEST_MODE = "payload.get('format') == 'sha256'"


def prepare(live: bytes, candidate: bytes) -> bytes:
    source, tree, assignments, secret = _library(live)
    _check(_constant(assignments, "BRIDGE_VERSION") == OLD, f"Expected Bridge {OLD} on disk")
    route = _handlers(assignments)[0].get("files/read")
    _check(isinstance(route, ast.Name) and route.id == HANDLER, "Unexpected files/read route")
    handler = _functions(tree).get(HANDLER)
    reviewed = _functions(ast.parse(CANDIDATE.read_bytes()))[HANDLER]
    _check(handler is not None and ast.dump(handler) == ast.dump(reviewed),
           "Live files/read differs from the reviewed text handler")

    replacement = ast.parse(candidate).body
    _check(len(replacement) == 1 and isinstance(replacement[0], ast.FunctionDef),
           "Candidate must contain exactly one file-read handler")
    without_digest = deepcopy(replacement[0])
    read = without_digest.body[-1]
    _check(isinstance(read, ast.Try) and len(read.body) == 3
           and isinstance(read.body[0], ast.If)
           and ast.dump(read.body[0].test) == ast.dump(ast.parse(DIGEST_MODE, mode="eval").body),
           "Candidate must add the explicit SHA-256 mode to the existing file read")
    del read.body[0]
    _check(ast.dump(without_digest) == ast.dump(reviewed),
           "Candidate changed the existing text read or its path checks")

    lines = source.splitlines(keepends=True)
    version = _one(assignments, "BRIDGE_VERSION")
    eol = "\r\n" if lines[version.lineno - 1].endswith("\r\n") else "\n"
    edits = {version: f"BRIDGE_VERSION = {NEW!r}", handler: candidate.decode("utf-8").strip()}
    for node, new_source in sorted(edits.items(), key=lambda item: item[0].lineno, reverse=True):
        lines[node.lineno - 1:node.end_lineno] = [eol.join(new_source.splitlines()) + eol]
    updated = "".join(lines).encode("utf-8")
    _, after, actual, actual_secret = _library(updated)
    new_nodes = {_one(actual, "BRIDGE_VERSION"), _functions(after)[HANDLER]}
    _check(actual_secret == secret and _constant(actual, "BRIDGE_VERSION") == NEW
           and [ast.dump(node) for node in tree.body if node not in edits]
           == [ast.dump(node) for node in after.body if node not in new_nodes],
           "Prepared update changed unrelated Bridge behavior")
    return updated


def verify_file_read(bridge):
    content = read_library(bridge)
    digest = bridge.call("files/read", {"path": GATEWAY_LIBRARY, "format": "sha256"})
    _check(digest == {"sha256": sha(content)}, "Signed raw digest does not match the verified library")
    return {"raw_sha256_verified": True}


if __name__ == "__main__":
    sys.exit(main(candidate=FRAGMENT, lock_name=".upgrade-0113.lock", old=OLD, new=NEW,
                  prepare_update=prepare, gateway_name="Ignition-Prox-Maker", verify_existing_tags=False,
                  verify_library_after_scan=True, verify_after=verify_file_read))
