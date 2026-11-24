"""Compare two live raw-history reads and optionally check a previous snapshot."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def read_twice(arguments):
    root = Path(__file__).resolve().parents[4]
    server = StdioServerParameters(
        command=str(root / ".local/start-ignition-mcp.sh"),
        args=["--serve-workspace"],
        cwd=root,
    )
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            results = []
            for _ in range(2):
                response = await session.call_tool("history_query_raw", arguments)
                result = response.structuredContent
                if response.isError or not isinstance(result, dict):
                    raise RuntimeError("MCP did not return a structured raw-history result")
                if "error" in result:
                    raise RuntimeError(result["message"])
                results.append(result)
            return results


def compare(first, second):
    fields = ("path", "start_ms", "end_ms", "count", "samples", "sha256")
    return all(first[key] == second[key] for key in fields)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path")
    parser.add_argument("start_ms", type=int)
    parser.add_argument("end_ms", type=int)
    parser.add_argument("--baseline", type=Path, help="artifact from the pre-migration check")
    parser.add_argument("--save-to", type=Path, help="save both reads to a new JSON file")
    args = parser.parse_args()
    request = {"path": args.path, "start_ms": args.start_ms, "end_ms": args.end_ms}

    first, second = asyncio.run(read_twice(request))
    summary = {
        **request, "counts": [first["count"], second["count"]],
        "sha256": [first["sha256"], second["sha256"]],
        "reads_match": compare(first, second),
    }
    if args.baseline:
        baseline = json.loads(args.baseline.read_bytes())["first"]
        summary["baseline_matches"] = compare(baseline, first)
    if args.save_to:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        with os.fdopen(os.open(args.save_to, flags, 0o600), "w", encoding="utf-8") as handle:
            json.dump({**summary, "first": first, "second": second}, handle, indent=2)
            handle.write("\n")
    print(json.dumps(summary, indent=2))
    return 0 if first["count"] and summary["reads_match"] and summary.get("baseline_matches", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
