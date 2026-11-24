from __future__ import annotations

import hashlib
import json
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import StrictInt

from ..app import AppContext, tool_result
from ..errors import GatewayError, ValidationError

RAW_HISTORY_ROUTE = "history/raw"
MAX_RAW_WINDOW_MS = 7 * 86_400_000

AGGREGATION_MODES = (
    "Average",
    "MinMax",
    "LastValue",
    "SimpleAverage",
    "Sum",
    "Minimum",
    "Maximum",
    "Count",
    "Range",
    "Variance",
    "StdDev",
    "DurationOn",
    "DurationOff",
)


def _raw_history_result(
    result: Any, path: str, start_ms: int, end_ms: int,
) -> dict[str, Any]:
    error = "Bridge did not return complete raw history for the requested path and window"
    if (
        not isinstance(result, dict)
        or result.get("path") != path
        or any(type(result.get(key)) is not int for key in ("start_ms", "end_ms", "count"))
        or (result["start_ms"], result["end_ms"]) != (start_ms, end_ms)
        or not isinstance(samples := result.get("samples"), list)
        or result["count"] != len(samples)
    ):
        raise GatewayError(error)

    ordered = []
    for sample in samples:
        if (
            not isinstance(sample, dict)
            or set(sample) != {"path", "timestamp_ms", "value", "quality"}
            or sample["path"] != path
            or type(sample["timestamp_ms"]) is not int
            or not start_ms <= sample["timestamp_ms"] <= end_ms
            or type(sample["value"]) not in (type(None), bool, int, float, str)
            or not isinstance(sample["quality"], str)
        ):
            raise GatewayError(error)
        try:
            encoded = json.dumps(
                sample, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":"),
            ).encode("ascii")
        except ValueError as exc:
            raise GatewayError(error) from exc
        ordered.append((sample["timestamp_ms"], encoded, sample))

    ordered.sort(key=lambda row: row[:2])
    return {
        **result,
        "samples": [row[2] for row in ordered],
        "sha256": hashlib.sha256(b"\n".join(row[1] for row in ordered)).hexdigest(),
    }


def register(mcp: FastMCP, ctx: AppContext) -> None:
    @mcp.tool()
    @tool_result(ctx)
    def history_query_raw(path: str, start_ms: StrictInt, end_ms: StrictInt) -> dict[str, Any]:
        """Read stored samples for one Bridge-allowlisted tag in an inclusive millisecond window."""
        if (
            type(start_ms) is not int or type(end_ms) is not int
            or not 0 <= start_ms <= end_ms < 2**63 - 1
            or end_ms - start_ms > MAX_RAW_WINDOW_MS
        ):
            raise ValidationError("Supply an inclusive Unix millisecond window of at most seven days")
        result = ctx.bridge.call(
            RAW_HISTORY_ROUTE, {"path": path, "start_ms": start_ms, "end_ms": end_ms},
        )
        return _raw_history_result(result, path, start_ms, end_ms)

    @mcp.tool()
    @tool_result(ctx)
    def history_query(
        paths: list[str],
        start: str,
        end: str = "",
        aggregation: str = "Average",
        return_size: int = 300,
    ) -> dict[str, Any]:
        """Read-only, bridge plane. Query tag history."""
        if not paths or len(paths) > 20:
            raise ValidationError("paths must contain 1-20 tag paths")
        if aggregation not in AGGREGATION_MODES:
            raise ValidationError(f"aggregation must be one of {AGGREGATION_MODES}")
        if not 1 <= return_size <= 10000:
            raise ValidationError("return_size must be 1-10000")
        if not start:
            raise ValidationError("start must be non-empty")
        results = ctx.bridge.call(
            "history/query",
            {
                "paths": paths,
                "start": start,
                "end": end,
                "aggregation": aggregation,
                "returnSize": return_size,
            },
        )
        return {
            "query": {"paths": paths, "start": start, "end": end, "aggregation": aggregation},
            "data": results,
        }

    @mcp.tool()
    @tool_result(ctx)
    def history_providers() -> dict[str, Any]:
        """Read-only, bridge plane. List available history providers."""
        results = ctx.bridge.call("history/providers")
        return {"providers": results}
