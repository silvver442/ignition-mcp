"""Cupper paths and validation shared by MCP and the Bridge installer."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .errors import GatewayError, ValidationError

CUPPER_FOLDER = "[default]Line_1/Cupper/Ignition/"
CUPPER_SPEED = CUPPER_FOLDER + "Speed"
CUPPER_PRODUCTION = CUPPER_FOLDER + "Production"
CUPPER_MIGRATE = "tags/scoped-cupper-migrate"
CUPPER_HISTORY = "tags/scoped-cupper-history"
CUPPER_VALIDATE = "tags/scoped-cupper-validate"
CUPPER_MAX_WINDOW_MS = 7 * 86_400_000


def check_history_window(start_ms: int, end_ms: int) -> None:
    if (type(start_ms) is not int or type(end_ms) is not int
            or not 0 <= start_ms < end_ms < 2**63
            or end_ms - start_ms > CUPPER_MAX_WINDOW_MS):
        raise ValidationError(
            "Supply start_ms and end_ms as Unix milliseconds for a closed window of at most seven days"
        )


def validate_cupper_history(result: Any, start_ms: int, end_ms: int) -> dict[str, Any]:
    error = "Bridge did not return complete Production historian samples for the requested window"
    if (not isinstance(result, dict) or result.get("path") != CUPPER_PRODUCTION
            or any(type(result.get(key)) is not int for key in ("start_ms", "end_ms", "count"))
            or (result["start_ms"], result["end_ms"]) != (start_ms, end_ms)
            or not isinstance(samples := result.get("samples"), list)
            or result["count"] != len(samples)):
        raise GatewayError(error)
    rows = []
    for sample in samples:
        if (not isinstance(sample, dict) or set(sample) != {"path", "timestamp_ms", "value", "quality"}
                or sample["path"] != CUPPER_PRODUCTION or type(sample["timestamp_ms"]) is not int
                or not start_ms <= sample["timestamp_ms"] <= end_ms
                or (sample["value"] is not None and type(sample["value"]) is not int)
                or (sample["quality"] is not None and not isinstance(sample["quality"], str))):
            raise GatewayError(error)
        encoded = json.dumps(sample, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        rows.append((sample["timestamp_ms"], encoded))
    digest = hashlib.sha256(b"\n".join(row[1] for row in rows)).hexdigest()
    if rows != sorted(rows) or result.get("sha256") != digest:
        raise GatewayError("Production historian sample order or SHA-256 did not match the Bridge response")
    return result
