"""Update only the scoped QualityCode diagnostics on the Cupper test Gateway."""

from __future__ import annotations

import ast
import json
import re
import sys
from contextlib import closing

from apply_upgrade_012 import CANDIDATE, excerpt, main, same_ast
from apply_upgrade_014 import GATEWAY_NAME, verify_after
from diagnose_scoped_tags import TEST_GATEWAY
from upgrade_011 import _check, _constant, _functions, _library, _one

OLD = "0.1.4"
NEW = "0.1.5"
LEGACY_RESULT = """def _scoped_result(results):
    if len(results) != 1 or not results[0].isGood():
        raise ValueError('Tag operation did not return one Good result')"""
LOG_SEARCHES = ("Speed", "Tag operation did not return one Good result")
QUALITY_NAME = re.compile(r"\b(?:Bad|Error|Uncertain)(?:_[A-Z][A-Za-z0-9]{1,30})\b")


def prepare(live, candidate):
    code, tree, assignments, secret = _library(live)
    _check(_constant(assignments, "BRIDGE_VERSION") == OLD, f"Expected Bridge {OLD} on disk")
    old_result = _functions(tree).get("_scoped_result")
    _check(old_result is not None and same_ast(LEGACY_RESULT, old_result),
           "Live result validation differs from the reviewed Bridge 0.1.4")
    candidate_code, candidate_tree, _, _ = _library(candidate)
    new_result = _functions(candidate_tree)["_scoped_result"]
    _check(ast.dump(old_result) != ast.dump(new_result), "Missing QualityCode diagnostics")

    version = _one(assignments, "BRIDGE_VERSION")
    lines = code.splitlines(keepends=True)
    eol = "\r\n" if lines[0].endswith("\r\n") else "\n"
    edits = [(version, f"BRIDGE_VERSION = {NEW!r}"),
             (old_result, excerpt(candidate_code, new_result))]
    for node, replacement in sorted(edits, key=lambda edit: edit[0].lineno, reverse=True):
        lines[node.lineno - 1:node.end_lineno] = [replacement.replace("\n", eol) + eol]
    result = "".join(lines).encode("utf-8")
    _, updated, updated_assignments, updated_secret = _library(result)
    _check(secret == updated_secret and _constant(updated_assignments, "BRIDGE_VERSION") == NEW
           and len(updated.body) == len(tree.body), "Update changed the secret or module structure")
    for original, actual in zip(tree.body, updated.body):
        if original is old_result:
            expected = new_result
        elif original is version:
            expected = ast.parse(f"BRIDGE_VERSION = {NEW!r}").body[0]
        else:
            expected = original
        _check(ast.dump(expected) == ast.dump(actual), "Update changed unrelated WebDev code")
    return result


def read_speed_logs(gateway):
    summary = []
    for search in LOG_SEARCHES:
        data = gateway.get_json("logs", params={"minimumLevel": "WARN", "message": search, "limit": 100})
        entries = data
        if isinstance(data, dict):
            entries = next((data[key] for key in ("items", "logs", "events", "entries", "results")
                            if isinstance(data.get(key), list)), None)
        _check(isinstance(entries, list), "Gateway log response did not contain a recognized event list")
        messages = [event["message"] for event in entries if isinstance(event, dict)
                    and isinstance(event.get("message"), str) and search.lower() in event["message"].lower()]
        summary.append({"filter": search, "matching_events": len(messages),
                        "quality_names": sorted({name for message in messages
                                                 for name in QUALITY_NAME.findall(message)})})
    return summary


def verify_logs():
    from ignition_mcp.client import GatewayClient
    from ignition_mcp.config import load_settings

    try:
        settings = load_settings()
        _check(settings.gateway_url == TEST_GATEWAY, "Unexpected Gateway URL")
        with closing(GatewayClient(settings)) as gateway:
            info = gateway.get_json("gateway-info")
            _check(info.get("name") == GATEWAY_NAME and info.get("ignitionVersion", "").split()[:1] == ["8.3.9"],
                   "Expected the test Gateway on Ignition 8.3.9")
            result = {"status": "success", "logs": read_speed_logs(gateway)}
    except Exception as exc:
        result = {"status": "error", "error": type(exc).__name__}
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    if sys.argv[1:] == ["--logs"]:
        sys.exit(verify_logs())
    sys.exit(main(candidate=CANDIDATE, lock_name=".upgrade-015.lock", old=OLD, new=NEW, prepare_update=prepare,
                  verify_after=verify_after, gateway_name=GATEWAY_NAME))
