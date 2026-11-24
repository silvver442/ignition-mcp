"""Update or read-only verify the Cupper Bridge on the test Gateway."""

from __future__ import annotations

import ast
import json
import sys
from contextlib import closing
from pathlib import Path

from apply_upgrade_012 import excerpt, main, same_ast
from diagnose_scoped_tags import TEST_GATEWAY
from ignition_mcp.bridge import BridgeClient
from ignition_mcp.client import GatewayClient
from ignition_mcp.config import load_settings
from ignition_mcp.cupper import (
    CUPPER_HISTORY, CUPPER_MIGRATE, CUPPER_PRODUCTION, CUPPER_SPEED, CUPPER_VALIDATE,
    validate_cupper_history,
)
from ignition_mcp.errors import GatewayError, IgnitionMcpError
from upgrade_011 import UpgradeError, _check, _constant, _functions, _handlers, _library, _one

OLD = "0.1.3"
NEW = "0.1.4"
FRAGMENT = Path(__file__).with_name("src") / "cupper_tag_reads.py"
GATEWAY_NAME = "Ignition-Prox-Maker"
START_MS = 1794866042408
END_MS = 1795470842408
EARLIER_END_MS = START_MS - 1
EARLIER_START_MS = EARLIER_END_MS - (END_MS - START_MS)
VALIDATE_HANDLER = "handle_tags_scoped_cupper_validate"


def replace_once(code, old, new):
    _check(code.count(old) == 1, "Live library differs from the reviewed Bridge 0.1.3")
    return code.replace(old, new, 1)


def prepare(live, candidate):
    code, tree, assignments, secret = _library(live)
    _check(_constant(assignments, "BRIDGE_VERSION") == OLD, "Expected Bridge 0.1.3 on disk")
    scope = "TAG_CONFIG_ROOTS = frozenset(('[default]Line_1/Cupper/Ignition',))"
    _check(same_ast(scope, _one(assignments, "TAG_CONFIG_ROOTS")), "Unexpected tag write scope")
    previous, handlers = _handlers(assignments)
    expected_routes = {CUPPER_HISTORY: "handle_tags_scoped_cupper_history",
                       CUPPER_MIGRATE: "handle_tags_scoped_cupper_migrate"}
    _check(CUPPER_VALIDATE not in previous
           and all(getattr(previous.get(route), "id", None) == handler
                   for route, handler in expected_routes.items()), "Unexpected Cupper routes")
    functions = _functions(tree)
    fragment = candidate.decode("utf-8")
    fragment_tree = ast.parse(fragment)
    added = _functions(fragment_tree)
    _check(len(fragment_tree.body) == len(added) and VALIDATE_HANDLER in added
           and not added.keys() & functions.keys(), "Unexpected Cupper 0.1.4 fragment")

    verify = excerpt(code, functions["_verify"])
    verify = replace_once(verify, "def _verify(envelope):", "def _verify(envelope, cupper=False):")
    verify = replace_once(verify, "message = str(ts) + '.' + payload_bytes",
                          "message = (str(ts) + '.').encode('utf-8') + payload_bytes")
    verify = replace_once(
        verify, "    expected = hmac.new(str(BRIDGE_SECRET), message, hashlib.sha256).hexdigest()",
        "    secret_bytes = BRIDGE_SECRET.encode('utf-8') if isinstance(BRIDGE_SECRET, unicode) else BRIDGE_SECRET\n"
        "    expected = hmac.new(secret_bytes, message, hashlib.sha256).hexdigest()",
    )
    verify = replace_once(verify, "payload = system.util.jsonDecode(payload_str)",
                          "payload = json.loads(payload_str) if cupper else system.util.jsonDecode(payload_str)\n"
                          "        if cupper and not isinstance(payload, dict):\n"
                          "            raise ValueError('Expected a JSON object')")
    dispatch = replace_once(excerpt(code, functions["handle"]), "_verify(request.get('data'))",
                            "_verify(request.get('data'), endpoint.startswith('tags/scoped-cupper-'))")
    history = replace_once(
        excerpt(code, functions["handle_tags_scoped_cupper_history"]),
        "    return _cupper_history_proof(payload.get('start'), payload.get('end'))",
        """    if set(payload) == {'path', 'start_ms', 'end_ms'}:
        return _cupper_raw_history(payload['path'], payload['start_ms'], payload['end_ms'])
    if set(payload) not in ({'path'}, {'path', 'start', 'end'}):
        raise ValueError('Expected both start_ms and end_ms')
    if 'start' in payload and (payload['start'] is None or payload['end'] is None):
        raise ValueError('Expected both legacy history boundaries')
    return _cupper_history_proof(payload.get('start'), payload.get('end'))""",
    )
    migrate = replace_once(
        excerpt(code, functions["handle_tags_scoped_cupper_migrate"]),
        """    name = _cupper_tag_name(path)
    mode = payload.get('mode')
    if mode not in ('prepare', 'restore') or payload.get('exclusiveAccess') is not True:
        raise ValueError('An exact operation and exclusive access to the tag are required')""",
        "    name, mode = _cupper_migration_request(payload)",
    )
    edited = {
        "_verify": verify, "handle": dispatch,
        "handle_tags_scoped_cupper_history": history, "handle_tags_scoped_cupper_migrate": migrate,
    }
    version = _one(assignments, "BRIDGE_VERSION")
    routes = _one(assignments, "HANDLERS")
    edits = [(version.lineno - 1, version.end_lineno, f"BRIDGE_VERSION = {NEW!r}"),
             (routes.lineno - 1, routes.lineno - 1, fragment.rstrip() + "\n\n"),
             (handlers.end_lineno - 1, handlers.end_lineno - 1,
              f"    {CUPPER_VALIDATE!r}: {VALIDATE_HANDLER},")]
    edits.extend((functions[name].lineno - 1, functions[name].end_lineno, value)
                 for name, value in edited.items())
    lines = code.splitlines(keepends=True)
    eol = "\r\n" if lines[0].endswith("\r\n") else "\n"
    for first, last, value in sorted(edits, reverse=True):
        lines[first:last] = [value.replace("\r\n", "\n").replace("\n", eol) + eol]
    result = "".join(lines).encode("utf-8")
    _, updated_tree, updated, updated_secret = _library(result)
    new_routes, _ = _handlers(updated)
    updated_functions = _functions(updated_tree)
    _check(secret == updated_secret and same_ast(scope, _one(updated, "TAG_CONFIG_ROOTS"))
           and new_routes.keys() == previous.keys() | {CUPPER_VALIDATE}
           and all(ast.dump(new_routes[key]) == ast.dump(value) for key, value in previous.items())
           and updated_functions.keys() == functions.keys() | added.keys()
           and all(ast.dump(updated_functions[key]) == ast.dump(node)
                   for key, node in functions.items() if key not in edited),
           "Prepared library changes the secret, tag scope or an unrelated handler")
    return result


def verify_history(bridge, *, legacy=False):
    def read(start, end):
        if legacy:
            proof = bridge.call(CUPPER_HISTORY, {"path": CUPPER_PRODUCTION, "start": start, "end": end})
            _check(isinstance(proof, dict) and (proof.get("start"), proof.get("end")) == (start, end)
                   and type(proof.get("count")) is int and proof["count"] >= 0
                   and isinstance(proof.get("sha256"), str) and len(proof["sha256"]) == 64,
                   "Legacy bridge did not return a fingerprint for the exact window")
            return proof
        raw = bridge.call(CUPPER_HISTORY, {"path": CUPPER_PRODUCTION, "start_ms": start, "end_ms": end})
        return validate_cupper_history(raw, start, end)

    first = read(START_MS, END_MS)
    second = read(START_MS, END_MS)
    _check(first == second, "Repeated fixed-window Production reads differ")
    _check(first["count"] > 0, "The acceptance window returned no Production samples")
    earlier = read(EARLIER_START_MS, EARLIER_END_MS)
    return {"path": CUPPER_PRODUCTION, "identical_reads": True, "compared_samples": not legacy,
            "fixed_window": {key: value for key, value in first.items() if key != "samples"},
            "earlier_window": {key: value for key, value in earlier.items() if key != "samples"}}


def verify_after(bridge):
    request = {"path": CUPPER_SPEED, "mode": "prepare", "exclusiveAccess": True}
    good = bridge.call(CUPPER_VALIDATE, request)
    _check(good == {**request, "name": "Speed", "valid": True}, "Read-only validator rejected valid Speed request")
    for invalid, expected in (
        ({"path": CUPPER_SPEED, "mode": "prepare"}, "exclusiveAccess"),
        ({**request, "exclusiveAccess": 1}, "exclusiveAccess"),
        ({**request, "mode": "create"}, "operation"),
        ({**request, "path": CUPPER_SPEED.replace("/Speed", "/Status")}, "limited"),
    ):
        try:
            bridge.call(CUPPER_VALIDATE, invalid)
        except GatewayError as exc:
            _check(expected in str(exc), "Read-only validator returned an unexpected rejection")
        else:
            raise UpgradeError("Read-only validator accepted an invalid request")
    return {"validator": "passed", "production_history": verify_history(bridge)}


def verify_only():
    try:
        settings = load_settings()
        _check(settings.gateway_url == TEST_GATEWAY and settings.bridge_secret,
               "Run with the existing authenticated test Gateway launcher")
        with closing(GatewayClient(settings)) as gateway, closing(BridgeClient(settings)) as bridge:
            info = gateway.get_json("gateway-info")
            _check(info.get("name") == GATEWAY_NAME and info.get("ignitionVersion", "").split()[:1] == ["8.3.9"],
                   "Expected the test Gateway on Ignition 8.3.9")
            version = bridge.ping().get("version")
            _check(version in (OLD, NEW), "Unexpected bridge version")
            checks = (verify_after(bridge) if version == NEW else
                      {"production_history": verify_history(bridge, legacy=True)})
        result = {"status": "success", "runtime_version": version, **checks}
    except (UpgradeError, IgnitionMcpError, ValueError) as exc:
        result = {"status": "error", "error": str(exc)}
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    if sys.argv[1:] == ["--verify"]:
        sys.exit(verify_only())
    sys.exit(main(candidate=FRAGMENT, lock_name=".upgrade-014.lock", old=OLD, new=NEW, prepare_update=prepare,
                  added_routes=frozenset({CUPPER_VALIDATE}), verify_after=verify_after, gateway_name=GATEWAY_NAME))
