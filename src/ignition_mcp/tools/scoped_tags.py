"""Configure one standalone tag inside a configured folder and keep a rollback record."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import uuid4

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from ..app import AppContext, tool_result
from ..cupper import (
    CUPPER_FOLDER, CUPPER_HISTORY, CUPPER_MIGRATE, CUPPER_PRODUCTION, CUPPER_SPEED, CUPPER_VALIDATE,
    check_history_window, validate_cupper_history,
)
from ..errors import GatewayError, IgnitionMcpError, ValidationError, WriteNotAllowedError
from ..guards import require_tag_configuration_write

TAG_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,254}")
BACKUP_ID = re.compile(r"[a-f0-9]{32}")
SHA256 = re.compile(r"[a-f0-9]{64}")
SCOPED_ENDPOINTS = {"tags/scoped-state", "tags/scoped-configure", "tags/scoped-restore"}
SOURCE_TEST_PATH = CUPPER_FOLDER + "MCP_Test_SourceMigration"
SOURCE_TEST_ENDPOINT = "tags/scoped-cupper-source-test"
SOURCE_TEST_VERSION = "0.1.8"
SPEED_MIGRATION_BRIDGE_VERSIONS = ("0.1.9", "0.1.10")
SOURCE_TEST_DIAGNOSTICS = frozenset({
    "name_collision", "invalid_path", "invalid_expression", "invalid_data_type", "invalid_tag_group",
    "invalid_json", "permission_denied", "redacted",
})
SOURCE_TEST_RECEIPTS = {"create_source_test", "merge_source_test", "overwrite_source_test"}
SOURCE_TEST_MUTABLE = {"valueSource", "value", "defaultValue", "expression"}
CUPPER_MUTABLE = {
    CUPPER_SPEED: {"valueSource", "expression", "value", "defaultValue"},
    CUPPER_PRODUCTION: {"dataType", "value"},
}


class HistoryPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    historyEnabled: bool | None = None
    historyProvider: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    sampleMode: Literal["OnChange", "Periodic", "TagGroup"] | None = None
    historySampleRate: Annotated[int, Field(gt=0, le=2**31 - 1)] | None = None
    historySampleRateUnits: (
        Literal["MS", "SEC", "MIN", "HOUR", "DAY", "WEEK", "MONTH", "YEAR"] | None
    ) = None
    historyTagGroup: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    historicalDeadbandStyle: Literal["Auto", "Analog_Compressed", "Discrete"] | None = None
    historicalDeadbandMode: Literal["Absolute", "Percent", "Off"] | None = None
    historicalDeadband: Annotated[float, Field(ge=0)] | None = None


class TagConflict(GatewayError):
    code = "tag_configuration_conflict"


def _encode(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sign(ctx: AppContext, value: Any) -> str:
    secret = ctx.settings.bridge_secret
    if not secret:
        raise GatewayError("Scoped tag configuration requires the authenticated WebDev bridge")
    return hmac.new(secret.encode(), b"tag-config-backup\x00" + _encode(value), "sha256").hexdigest()


def _check_path(ctx: AppContext, path: str) -> None:
    parent, _, name = path.rpartition("/")
    if not TAG_NAME.fullmatch(name) or name.lower() in {"plc", "_types_"}:
        raise ValidationError("Supply an exact, provider-qualified path to one standalone tag")
    if parent not in ctx.settings.tag_config_allowlist:
        raise WriteNotAllowedError("Tag folder is outside IGNITION_TAG_CONFIG_ALLOWLIST")


def _state(snapshot: dict[str, Any]) -> str:
    return _sha(_encode([snapshot["path"], snapshot["config"]]))


def _require_scoped_bridge(ctx: AppContext) -> None:
    capabilities = ctx.bridge.ping().get("capabilities")
    if not isinstance(capabilities, list) or not SCOPED_ENDPOINTS.issubset(capabilities):
        raise GatewayError("Bridge must support scoped read, configure and restore before a tag write")


def _snapshot(ctx: AppContext, path: str) -> dict[str, Any]:
    snap = ctx.bridge.call("tags/scoped-state", {"path": path})
    if (
        not isinstance(snap, dict) or snap.get("path") != path or "config" not in snap
        or (snap["config"] is not None and not isinstance(snap["config"], dict))
        or not isinstance(snap.get("effective"), dict) or type(snap.get("valueGood")) is not bool
        or "value" not in snap
    ):
        raise GatewayError("Bridge did not return a complete tag snapshot")
    return snap


def _private_write(path: Path, data: bytes) -> None:
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())
    if path.read_bytes() != data:
        raise OSError("Saved tag configuration differs from the Gateway snapshot")


def _backup(root: Path, ctx: AppContext, before: dict[str, Any]) -> dict[str, str]:
    backup_id = uuid4().hex
    folder = root / backup_id
    folder.mkdir(parents=True, mode=0o700)
    path = folder / "before.json"
    data = _encode({"gateway": _sha(ctx.settings.gateway_url.encode()), "snapshot": before})
    _private_write(path, data)
    return {"id": backup_id, "path": str(path), "sha256": _sha(data)}


def _receipt(
    ctx: AppContext, backup: dict[str, str], action: str, after: dict[str, Any],
) -> None:
    proof = {"id": backup["id"], "sha256": backup["sha256"], "action": action,
             "afterState": _state(after), "afterValue": after["value"]}
    _private_write(Path(backup["path"]).with_name("verified.json"), _encode({
        "proof": proof, "signature": _sign(ctx, proof),
    }))


def _verified_backup(
    ctx: AppContext, root: Path, backup_id: str, sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not BACKUP_ID.fullmatch(backup_id) or not SHA256.fullmatch(sha256):
        raise ValidationError("Supply the backup identifier and SHA-256 returned by the tool")
    folder = root / backup_id
    raw = (folder / "before.json").read_bytes()
    if not hmac.compare_digest(sha256, _sha(raw)):
        raise TagConflict("Backup checksum does not match")
    saved = json.loads(raw)
    signed = json.loads((folder / "verified.json").read_bytes())
    proof, signature = signed["proof"], signed["signature"]
    created = proof["action"] in {"create", "create_source_test"}
    if (
        saved["gateway"] != _sha(ctx.settings.gateway_url.encode()) or proof["id"] != backup_id
        or proof["sha256"] != sha256 or not hmac.compare_digest(signature, _sign(ctx, proof))
        or (saved["snapshot"]["config"] is None) != created
    ):
        raise TagConflict("Backup has no matching verified receipt for this Gateway")
    return saved["snapshot"], proof


def _quality_value(snapshot: dict[str, Any], key: str) -> Any:
    effective = snapshot["effective"]
    if key in effective:
        return effective[key]
    return (snapshot["config"] or {}).get(key)


def _same_setting(snapshot: dict[str, Any], key: str, value: Any) -> bool:
    actual = _quality_value(snapshot, key)
    if key == "valueSource" and isinstance(actual, str) and isinstance(value, str):
        return actual.lower() == value.lower()
    return actual == value


def _source_test_capabilities(ctx: AppContext, exclusive_access: bool) -> None:
    if exclusive_access is not True:
        raise ValidationError("The migration test requires exclusive access to the dedicated tag")
    runtime = ctx.bridge.ping()
    caps = runtime.get("capabilities")
    if (runtime.get("version") not in (SOURCE_TEST_VERSION, *SPEED_MIGRATION_BRIDGE_VERSIONS)
            or not isinstance(caps, list)
            or not (SCOPED_ENDPOINTS | {SOURCE_TEST_ENDPOINT}).issubset(caps)):
        raise GatewayError("The dedicated test requires Bridge 0.1.8, 0.1.9 or 0.1.10 and its scoped routes")


def _source_test_result(result: Any, *, creation: bool) -> bool:
    fields = {"count", "qualities", "valueBefore"}
    if not isinstance(result, dict) or set(result) - ({"diagnostic"} if creation else set()) != fields:
        raise GatewayError("Bridge did not return the test configuration result")
    count, qualities = result["count"], result["qualities"]
    if (not isinstance(qualities, list) or (count is not None and type(count) is not int)
            or (0 if count is None else count) != len(qualities)
            or any(code is not None and (
                not isinstance(code, dict) or set(code) != {"name", "code", "good"}
                or not isinstance(code["name"], str) or type(code["code"]) is not int
                or type(code["good"]) is not bool
            ) for code in qualities)):
        raise GatewayError("Bridge returned invalid QualityCode data for the test operation")
    single_result = count == 1 and qualities[0] is not None
    if "diagnostic" in result:
        diagnostic = result["diagnostic"]
        if (not single_result or qualities[0]["good"] or not isinstance(diagnostic, str)
                or diagnostic not in SOURCE_TEST_DIAGNOSTICS):
            raise GatewayError("Bridge returned an invalid creation diagnostic")
    return single_result and qualities[0]["good"]


def _source_test_memory(before: dict[str, Any], after: dict[str, Any], value: Any) -> bool:
    return (type(value) is int and 0 <= value < 60 and after["value"] == value
            and _same_setting(after, "defaultValue", value)
            and all({k: v for k, v in after[part].items() if k not in SOURCE_TEST_MUTABLE}
                    == {k: v for k, v in before[part].items() if k not in SOURCE_TEST_MUTABLE}
                    for part in ("config", "effective")))


def _source_test_readback(ctx: AppContext, path: str, *, wait_for_creation: bool) -> dict[str, Any]:
    after = _snapshot(ctx, path)
    if wait_for_creation:
        # A newly configured Expression can take a polling interval to evaluate.
        for _ in range(6):
            if after["valueGood"]:
                break
            time.sleep(0.5)
            after = _snapshot(ctx, path)
    return after


def _finish_error(report: dict[str, Any], exc: Exception) -> dict[str, Any]:
    report["status"] = "uncertain" if report["attempted"] else "error"
    if isinstance(exc, IgnitionMcpError):
        report.update(exc.to_dict())
    else:
        report.update(error="tag_backup_error", message=str(exc))
    return report


def _cupper_history(ctx: AppContext, path: str, window: dict[str, Any] | None = None) -> dict[str, Any]:
    # Preserve the fingerprint format used by existing migration backups and receipts.
    args = {} if window is None else {"start": window["start"], "end": window["end"]}
    proof = ctx.bridge.call(CUPPER_HISTORY, {"path": path, **args})
    if (not isinstance(proof, dict) or not {"start", "end", "count", "sha256"} <= proof.keys()
            or any(type(proof[key]) is not int for key in ("start", "end", "count"))
            or not 0 <= proof["start"] < proof["end"] or proof["count"] < 0
            or not isinstance(proof["sha256"], str) or not SHA256.fullmatch(proof["sha256"])
            or (window is not None and any(proof[key] != args[key] for key in args))):
        raise GatewayError("Bridge did not return a complete raw historian fingerprint")
    return proof


def _cupper_precondition(path: str, exclusive_access: bool) -> None:
    if path not in CUPPER_MUTABLE or exclusive_access is not True:
        raise ValidationError("Only Cupper Speed and Production may be migrated with exclusive access")


def _cupper_capabilities(ctx: AppContext, *, preparing_speed: bool = False) -> None:
    runtime = ctx.bridge.ping()
    caps = runtime.get("capabilities")
    expected = SCOPED_ENDPOINTS | {CUPPER_HISTORY, CUPPER_MIGRATE, CUPPER_VALIDATE}
    if not isinstance(caps, list) or not expected.issubset(caps):
        raise GatewayError("Bridge 0.1.4 with scoped Cupper validation and raw history is required")
    if preparing_speed and runtime.get("version") not in SPEED_MIGRATION_BRIDGE_VERSIONS:
        raise GatewayError("Speed migration requires active Bridge 0.1.9 or 0.1.10")


def _verify_cupper(ctx: AppContext, reference: dict[str, Any], after: dict[str, Any],
                   *, restoring: bool, value: Any, history_proof: dict[str, Any] | None) -> None:
    path = reference["path"]
    production = path == CUPPER_PRODUCTION
    source = "expr" if restoring and not production else "memory"
    data_type = "Int8" if production and not restoring else "Int4"
    ignored = CUPPER_MUTABLE[path] if not restoring or production else set()
    old_config = reference["config"]
    actual = after["config"]
    if (actual is None or not after["valueGood"] or type(after["value"]) is not int
            or not _same_setting(after, "dataType", data_type) or not _same_setting(after, "valueSource", source)
            or not _same_setting(after, "historyProvider", "Postgres_Historian")
            or _quality_value(after, "historyEnabled") is not True
            or (source == "memory" and (type(value) is not int or after["value"] != value))
            or (not production and not restoring and not _same_setting(after, "defaultValue", value))
            or (not production and not restoring and "expression" in actual
                and actual["expression"] != old_config.get("expression"))
            or {k: v for k, v in actual.items() if k not in ignored}
            != {k: v for k, v in old_config.items() if k not in ignored}
            or any(not _same_setting(after, key, item) for key, item in reference["effective"].items()
                   if key not in ignored)):
        raise TagConflict("Cupper readback did not retain the expected value, configuration and history settings")
    if history_proof is not None and _cupper_history(ctx, path, history_proof) != history_proof:
        raise TagConflict(f"{path.rsplit('/', 1)[-1]} raw historical samples differ from the preflight")


def _prepare_cupper(ctx: AppContext, root: Path, before: dict[str, Any],
                    report: dict[str, Any]) -> dict[str, Any]:
    path = before["path"]
    production = path == CUPPER_PRODUCTION
    if (before["config"] is None or not before["valueGood"] or type(before["value"]) is not int
            or not _same_setting(before, "dataType", "Int4")
            or not _same_setting(before, "valueSource", "memory" if production else "expr")
            or not _same_setting(before, "historyProvider", "Postgres_Historian")
            or _quality_value(before, "historyEnabled") is not True):
        raise TagConflict("Cupper is outside the expected starting state")
    if not production and not all(_same_setting(before, key, value) for key, value in (
        ("sampleMode", "Periodic"), ("historySampleRate", 10), ("historySampleRateUnits", "SEC"),
    )):
        raise TagConflict("Speed must use 10-second periodic history before migration")
    proof = _cupper_history(ctx, path)
    if not proof["count"]:
        raise TagConflict(f"{path.rsplit('/', 1)[-1]} has no raw samples in the preflight window")
    saved = _backup(root, ctx, {**before, "historyProof": proof})
    report.update(backup=saved, attempted=True, historyProof=proof)
    request = {
        "path": path, "mode": "prepare", "exclusiveAccess": True, "expectedConfig": before["config"],
        "historyProof": proof,
    }
    if production:
        request["expectedValue"] = before["value"]
    try:
        mutation = ctx.bridge.call(CUPPER_MIGRATE, request)
    except IgnitionMcpError:
        if not production:
            try:
                after = _snapshot(ctx, path)
                report.update(tag=after, configuration_unchanged=all(
                    after[key] == before[key] for key in ("config", "effective")))
            except IgnitionMcpError:
                report["readback_failed"] = True
        raise
    after = _snapshot(ctx, path)
    report["tag"] = after
    value = before["value"]
    if not production:
        if not isinstance(mutation, dict):
            raise GatewayError("The bridge did not return the captured Speed value")
        value = mutation.get("valueBefore")
        report["valueBefore"] = value
    _verify_cupper(ctx, before, after, restoring=False, value=value, history_proof=proof)
    _receipt(ctx, saved, "prepare_cupper", after)
    return {**report, "status": "success", "verified": True, "restore_available": True}


def _restore_cupper(ctx: AppContext, root: Path, before: dict[str, Any], current: dict[str, Any],
                    report: dict[str, Any]) -> dict[str, Any]:
    path = current["path"]
    original_proof = before["historyProof"]
    proof = None
    if path == CUPPER_PRODUCTION:
        if (not original_proof or _cupper_history(ctx, path, original_proof) != original_proof
                or current["value"] != before["value"]):
            raise TagConflict("Production changed since the original migration backup")
        proof = _cupper_history(ctx, path)
        if not proof["count"]:
            raise TagConflict("Production has no raw samples in the restore preflight window")
    elif original_proof is not None:
        if _cupper_history(ctx, path, original_proof) != original_proof:
            raise TagConflict("Original Speed historian samples changed since migration")
        proof = original_proof
    saved = _backup(root, ctx, {**current, "historyProof": proof})
    report.update(backup=saved, attempted=True)
    ctx.bridge.call(CUPPER_MIGRATE, {
        "path": path, "mode": "restore", "exclusiveAccess": True, "expectedConfig": current["config"],
        "expectedValue": current["value"], "beforeConfig": before["config"], "historyProof": proof,
    })
    after = _snapshot(ctx, path)
    report["tag"] = after
    _verify_cupper(ctx, before, after, restoring=True, value=current["value"], history_proof=proof)
    if (original_proof is not None and original_proof != proof
            and _cupper_history(ctx, path, original_proof) != original_proof):
        raise TagConflict("Original Production historian samples changed across restore")
    return {**report, "status": "success", "verified": True}


def register(mcp: FastMCP, ctx: AppContext, backup_root: Path | None = None) -> None:
    root = backup_root if backup_root is not None else Path.cwd() / "backups" / "mcp-tag-config"

    @mcp.tool()
    @tool_result(ctx)
    def tag_config_get(path: str) -> dict[str, Any]:
        """Read one allowlisted tag's configuration, effective settings and live value."""
        _check_path(ctx, path)
        snap = _snapshot(ctx, path)
        return {**snap, "exists": snap["config"] is not None, "state": _state(snap)}

    @mcp.tool()
    @tool_result(ctx)
    def tag_cupper_production_history(
        start_ms: StrictInt, end_ms: StrictInt, path: str = CUPPER_PRODUCTION,
    ) -> dict[str, Any]:
        """Read every raw Production sample in an inclusive Unix millisecond window."""
        _check_path(ctx, path)
        if path != CUPPER_PRODUCTION:
            raise ValidationError("Only Cupper Production historical samples are supported")
        check_history_window(start_ms, end_ms)
        _cupper_capabilities(ctx)
        result = ctx.bridge.call(CUPPER_HISTORY, {"path": path, "start_ms": start_ms, "end_ms": end_ms})
        return validate_cupper_history(result, start_ms, end_ms)

    @mcp.tool()
    @tool_result(ctx)
    def tag_cupper_validate(
        path: str, mode: Literal["prepare", "restore"], exclusive_access: StrictBool = False,
    ) -> dict[str, Any]:
        """Validate the Cupper operation and scope on the Bridge without changing a tag."""
        _check_path(ctx, path)
        _cupper_precondition(path, exclusive_access)
        if mode not in ("prepare", "restore"):
            raise ValidationError("Expected Cupper operation prepare or restore")
        _cupper_capabilities(ctx)
        return ctx.bridge.call(CUPPER_VALIDATE, {"path": path, "mode": mode, "exclusiveAccess": exclusive_access})

    @mcp.tool()
    @tool_result(ctx)
    def tag_source_migration_test(
        action: Literal["create", "merge", "overwrite"], expected_state: str,
        previous_backup_id: str = "", previous_backup_sha256: str = "",
        confirm: StrictBool = False, exclusive_access: StrictBool = False,
    ) -> dict[str, Any]:
        """Probe Expression to Memory on the dedicated Cupper test tag with backups and readback.

        Merge requires the verified creation backup. Overwrite requires the backup from a
        failed merge whose readback matched the original configuration. Undo uses tag_config_restore.
        """
        path = SOURCE_TEST_PATH
        _check_path(ctx, path)
        require_tag_configuration_write(ctx.settings, confirm)
        if action not in ("create", "merge", "overwrite") or not SHA256.fullmatch(expected_state):
            raise ValidationError("Supply a test operation and the reviewed state from tag_config_get")
        if action == "create" and (previous_backup_id or previous_backup_sha256):
            raise ValidationError("Creation requires an absent test tag")
        report: dict[str, Any] = {"path": path, "attempted": False, "verified": False}
        try:
            _source_test_capabilities(ctx, exclusive_access)
            before = _snapshot(ctx, path)
            if action == "create" and before["config"] is not None:
                return {**report, "status": "already_exists", "tag": before, "state": _state(before)}
            if _state(before) != expected_state:
                raise TagConflict("Test tag changed since the reviewed snapshot")
            if action != "create":
                prior, proof = _verified_backup(ctx, root, previous_backup_id, previous_backup_sha256)
                required = "create_source_test" if action == "merge" else "failed_source_test_merge"
                if prior["path"] != path or proof["action"] != required or proof["afterState"] != expected_state:
                    raise TagConflict("Backup does not prove the required previous test stage")
            saved = _backup(root, ctx, before)
            report.update(backup=saved, attempted=True)
            accepted = False
            try:
                result = ctx.bridge.call(SOURCE_TEST_ENDPOINT, {
                    "path": path, "action": action, "expectedConfig": before["config"], "exclusiveAccess": True,
                })
                accepted = _source_test_result(result, creation=action == "create")
                report["configure"] = result
            finally:
                # Check actual Gateway state even if the configure RPC fails.
                try:
                    after = _source_test_readback(ctx, path, wait_for_creation=action == "create" and accepted)
                    configuration_unchanged = all(after[key] == before[key] for key in ("config", "effective"))
                    report.update(tag=after, state=_state(after), configuration_unchanged=configuration_unchanged)
                except IgnitionMcpError:
                    report["readback_failed"] = True
                    raise
            config = after["config"]
            if (action == "merge" and result["count"] == 1 and result["qualities"][0] is not None
                    and configuration_unchanged and after["valueGood"]
                    and type(after["value"]) is int and 0 <= after["value"] < 60):
                _receipt(ctx, saved, "failed_source_test_merge", after)
                return {**report, "status": "not_applied" if accepted else "rejected", "overwrite_available": True}
            if (not accepted or config is None or not after["valueGood"] or type(after["value"]) is not int
                    or not _same_setting(after, "dataType", "Int4") or _quality_value(after, "historyEnabled") is not False
                    or not _same_setting(after, "valueSource", "expr" if action == "create" else "memory")):
                raise TagConflict("The test tag readback did not match the requested operation and Good result")
            expression = config.get("expression")
            if action == "create":
                if expression != "getsecond(now())" or not 0 <= after["value"] < 60:
                    raise TagConflict("The new tag did not evaluate the expected expression")
            elif (not _source_test_memory(before, after, result["valueBefore"])
                  or ("expression" in config and (action == "overwrite" or expression != before["config"]["expression"]))):
                raise TagConflict("Memory readback differs from the backed up configuration and captured value")
            _receipt(ctx, saved, f"{action}_source_test", after)
            return {**report, "status": "success", "verified": True, "restore_available": True,
                    "expression_present": "expression" in config, "expression": expression}
        except (IgnitionMcpError, OSError, ValueError, KeyError, TypeError) as exc:
            return _finish_error(report, exc)

    @mcp.tool()
    @tool_result(ctx)
    def tag_configure_scoped(
        path: str,
        action: Literal["create", "history", "prepare_cupper"],
        data_type: Literal["Int4", "Int8"] = "Int4",
        value: StrictInt = 0,
        history: HistoryPatch | None = None,
        expected_state: str = "",
        confirm: bool = False,
        exclusive_access: StrictBool = False,
    ) -> dict[str, Any]:
        """Create a Memory integer tag or patch only history on a reviewed standalone tag.

        Creation aborts if the tag exists. For a history patch, pass the state from
        tag_config_get. Saves a verified backup before any Gateway mutation.
        prepare_cupper accepts only Cupper Speed and Production, a reviewed state
        and exclusive access. Both check raw historian samples before and after.
        """
        _check_path(ctx, path)
        require_tag_configuration_write(ctx.settings, confirm)
        patch = history.model_dump(exclude_none=True) if history is not None else {}
        if action == "create":
            bits = {"Int4": 32, "Int8": 64}.get(data_type)
            if (
                patch or bits is None or type(value) is not int
                or not -(2 ** (bits - 1)) <= value < 2 ** (bits - 1)
            ):
                raise ValidationError("Creation accepts one valid Memory integer with history disabled")
        elif action == "prepare_cupper":
            _cupper_precondition(path, exclusive_access)
            if patch or data_type != "Int4" or value != 0 or not SHA256.fullmatch(expected_state):
                raise ValidationError("Cupper migration takes its native properties and a reviewed state")
        elif action != "history" or not patch or not SHA256.fullmatch(expected_state):
            raise ValidationError("A history patch requires settings and the state from tag_config_get")

        report: dict[str, Any] = {"path": path, "attempted": False, "verified": False,
                                  "restore_available": False}
        try:
            if action == "prepare_cupper":
                _cupper_capabilities(ctx, preparing_speed=path == CUPPER_SPEED)
            else:
                _require_scoped_bridge(ctx)
            before = _snapshot(ctx, path)
            config = before["config"]
            if action == "create" and config is not None:
                return {**report, "status": "already_exists", "tag": before, "state": _state(before)}
            if expected_state and expected_state != _state(before):
                raise TagConflict("Tag changed since the reviewed snapshot")
            if action == "prepare_cupper":
                return _prepare_cupper(ctx, root, before, report)
            if action == "history":
                if config is None:
                    raise TagConflict("Cannot patch a missing tag")
                if all(_same_setting(before, key, item) for key, item in patch.items()):
                    return {**report, "status": "no_changes", "tag": before}
            saved = _backup(root, ctx, before)
            report["backup"] = saved
            report["attempted"] = True
            change = {"dataType": data_type, "value": value} if action == "create" else {"history": patch}
            ctx.bridge.call("tags/scoped-configure", {
                "path": path, "action": action, "expectedConfig": config, **change,
            })
            after = _snapshot(ctx, path)
            report["tag"] = after
            if after["config"] is None:
                raise TagConflict("Tag is missing after configuration")
            if action == "create":
                if not (
                    _same_setting(after, "dataType", data_type)
                    and _same_setting(after, "valueSource", "memory")
                    and _quality_value(after, "historyEnabled") is False
                    and after["valueGood"] and type(after["value"]) is int and after["value"] == value
                ):
                    raise TagConflict("New tag type, value, quality or history did not match")
            elif (
                not all(_same_setting(after, key, item) for key, item in patch.items())
                or {k: v for k, v in config.items() if k not in patch}
                != {k: v for k, v in after["config"].items() if k not in patch}
                or any(not _same_setting(after, key, item) for key, item in before["effective"].items()
                       if key not in patch)
            ):
                raise TagConflict("Readback differs from the requested history-only patch")
            _receipt(ctx, saved, action, after)
            return {**report, "status": "success", "verified": True, "restore_available": True}
        except (IgnitionMcpError, OSError, ValueError) as exc:
            return _finish_error(report, exc)

    @mcp.tool()
    @tool_result(ctx)
    def tag_config_restore(
        path: str, backup_id: str, backup_sha256: str, confirm: bool = False,
        exclusive_access: StrictBool = False,
    ) -> dict[str, Any]:
        """Undo one verified scoped tag operation if its configuration still matches."""
        _check_path(ctx, path)
        require_tag_configuration_write(ctx.settings, confirm)
        report: dict[str, Any] = {"path": path, "attempted": False, "verified": False}
        try:
            before, proof = _verified_backup(ctx, root, backup_id, backup_sha256)
            if before["path"] != path:
                raise TagConflict("Backup is for a different tag")
            migrating = proof["action"] == "prepare_cupper"
            source_test = proof["action"] in SOURCE_TEST_RECEIPTS
            if proof["action"] == "failed_source_test_merge":
                raise ValidationError("This test receipt recorded an unchanged configuration")
            if migrating:
                _cupper_precondition(path, exclusive_access)
                _cupper_capabilities(ctx)
            elif source_test:
                if path != SOURCE_TEST_PATH:
                    raise TagConflict("Invalid source test backup scope")
                _source_test_capabilities(ctx, exclusive_access)
            else:
                _require_scoped_bridge(ctx)
            current = _snapshot(ctx, path)
            if _state(current) != proof["afterState"]:
                raise TagConflict("Current tag configuration no longer matches the verified receipt")
            if proof["action"] == "create" or _same_setting(current, "valueSource", "memory"):
                if not current["valueGood"] or current["value"] != proof["afterValue"]:
                    raise TagConflict("Current Memory tag value no longer matches the verified receipt")
            if migrating:
                return _restore_cupper(ctx, root, before, current, report)
            report["backup"] = _backup(root, ctx, current)
            report["attempted"] = True
            if proof["action"] == "create_source_test":
                ctx.bridge.call(SOURCE_TEST_ENDPOINT, {
                    "path": path, "action": "undo_create", "expectedConfig": current["config"],
                    "exclusiveAccess": True,
                })
            else:
                ctx.bridge.call("tags/scoped-restore", {
                    "path": path, "expectedConfig": current["config"], "expectedValue": proof["afterValue"],
                    "beforeConfig": before["config"],
                })
            after = _snapshot(ctx, path)
            report["tag"] = after
            if _state(after) != _state(before):
                raise TagConflict("Restored tag did not match the backed up configuration")
            return {**report, "status": "success", "verified": True}
        except (IgnitionMcpError, OSError, ValueError, KeyError, TypeError) as exc:
            return _finish_error(report, exc)
