"""Verify raw history through the installed Bridge handler and the MCP tool."""

import math
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from ignition_mcp.app import AppContext
from ignition_mcp.tools import history

from .test_speed_migration_bridge import BRIDGE, NOW, SPEED, date, library_018, release019

with patch.object(sys, "path", [str(BRIDGE), *sys.path]):
    import apply_upgrade_0110 as release0110


@pytest.fixture
def bridge_history():
    live = release019.prepare(library_018(), release019.FRAGMENT.read_bytes())
    updated = release0110.prepare(live, release0110.FRAGMENT.read_bytes())
    query = Mock()
    system = SimpleNamespace(
        util=SimpleNamespace(getLogger=Mock(), getProperty=lambda _: "."),
        historian=SimpleNamespace(queryRawPoints=query),
        date=SimpleNamespace(now=lambda: date(NOW), fromMillis=date),
    )
    namespace = {"system": system, "long": int, "basestring": str, "unicode": str}
    exec(compile(updated, "bridge_0110.py", "exec"), namespace)
    assert namespace["BRIDGE_VERSION"] == "0.1.10"
    decoder = Mock(return_value=({}, None))
    with patch.dict(namespace, {"_verify": decoder}):
        namespace["handle"]({"remainingPath": "history/raw", "data": {}})
        namespace["handle"]({"remainingPath": "history/query", "data": {}})
    assert [call.args for call in decoder.call_args_list] == [({}, True), ({}, False)]
    return namespace["HANDLERS"]["history/raw"], query


def dataset(rows):
    return SimpleNamespace(
        getColumnCount=lambda: 4, getRowCount=lambda: len(rows),
        getValueAt=lambda row, column: rows[row][column],
    )


def test_raw_history_keeps_boundaries_and_duplicates_with_writes_disabled(
    bridge_history, settings_readonly, mock_client, mock_bridge, make_tools,
):
    handle, query = bridge_history
    start, end = NOW - 2000, NOW - 1000
    duplicate = (SPEED, 12, 192, date(start))
    rows = [duplicate, (SPEED, 35, 192, date(end)), (SPEED, 14, 0, date(start)), duplicate]
    seeds = [(SPEED, 99, 192, date(start - 1)), (SPEED, 99, 192, date(end + 1))]
    query.side_effect = [dataset(rows + seeds), dataset(seeds + rows[::-1])]
    mock_bridge.call.side_effect = lambda route, args: handle(args)
    ctx = AppContext(settings=settings_readonly, client=mock_client, bridge=mock_bridge)
    ctx.trial_warning = lambda: None
    read = make_tools(history, ctx=ctx)["history_query_raw"]
    first, second = (read(SPEED, start, end) for _ in range(2))
    assert first == second
    assert first["sha256"] == "26cad5d621b2ea8b6b516ded77889e97563f5e909393728c440d9624fcadfd12"
    assert first["count"] == 4
    assert [row["value"] for row in first["samples"]] == [14, 12, 12, 35]
    assert [row["timestamp_ms"] for row in first["samples"]] == [start, start, start, end]
    mock_bridge.call.assert_called_with("history/raw", {"path": SPEED, "start_ms": start, "end_ms": end})
    args = query.call_args.kwargs
    assert args["paths"] == [
        "histprov:Postgres_Historian:/drv:Ignition-Prox-Maker:default:/tag:Line_1/Cupper/Ignition/Speed",
    ]
    assert (args["startTime"].getTime(), args["endTime"].getTime()) == (start - 1, end + 1)
    assert (args["returnSize"], args["returnFormat"], args["includeBounds"]) == (-1, "TALL", False)


def test_bridge_rejects_unlisted_paths_and_excess_samples(bridge_history):
    handle, query = bridge_history
    timestamp = NOW - 1000
    request = {"path": SPEED, "start_ms": timestamp, "end_ms": timestamp}
    with pytest.raises(ValueError, match="not allowed"):
        handle({**request, "path": SPEED.lower()})
    with pytest.raises(ValueError, match="millisecond window"):
        handle({**request, "start_ms": True})
    query.assert_not_called()
    query.return_value = dataset([(SPEED, 27, 192, date(timestamp))] * 10001)
    with pytest.raises(ValueError, match="exceeds 10000"):
        handle(request)


def test_raw_history_rejects_incomplete_response(app_ctx, mock_bridge, make_tools):
    start, end = NOW - 2000, NOW - 1000
    result = {"path": SPEED, "start_ms": start, "end_ms": end, "samples": [], "count": 1}
    read = make_tools(history)["history_query_raw"]
    mock_bridge.call.return_value = result
    assert read(SPEED, start, end)["error"] == "gateway_error"
    result["samples"].append({"path": SPEED, "timestamp_ms": start, "value": math.nan, "quality": "192"})
    assert read(SPEED, start, end)["error"] == "gateway_error"
