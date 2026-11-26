#!/usr/bin/env python3
"""Deterministically build bridge/mcp-bridge.zip — an importable Ignition project
containing the mcp-bridge WebDev endpoint and its script library.

The BRIDGE_SECRET placeholder stays intact; it is substituted at install time
(bridge_install tool) or by the operator during a manual import.

Usage: python bridge/build_zip.py [--check]
"""

from __future__ import annotations

import argparse
import io
import json
import zipfile
from pathlib import Path

HERE = Path(__file__).parent
SRC = HERE / "src"
OUT = HERE / "mcp-bridge.zip"
# Fixed timestamp for reproducible archives.
ZINFO_DATE = (2026, 1, 1, 0, 0, 0)


def _write(zf: zipfile.ZipFile, arcname: str, data: str) -> None:
    info = zipfile.ZipInfo(arcname, date_time=ZINFO_DATE)
    info.compress_type = zipfile.ZIP_DEFLATED
    zf.writestr(info, data)


def build() -> bytes:
    lib_code = (SRC / "mcp_bridge_lib.py").read_text(encoding="utf-8")
    endpoint_code = (SRC / "doPost.py").read_text(encoding="utf-8")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        _write(zf, "project.json", json.dumps({
            "title": "MCP Bridge",
            "description": "WebDev bridge for ignition-mcp",
            "parent": "",
            "enabled": True,
            "inheritable": False,
        }, indent=2))

        # Script library: ignition/script-python/mcp_bridge_lib/code.py
        lib_dir = "ignition/script-python/mcp_bridge_lib"
        _write(zf, f"{lib_dir}/code.py", lib_code)
        _write(zf, f"{lib_dir}/resource.json", json.dumps({
            "scope": "G", "version": 1, "restricted": False, "overridable": True,
            "files": ["code.py"], "attributes": {},
        }, indent=2))

        # WebDev endpoint: com.inductiveautomation.webdev/resources/api
        api_dir = "com.inductiveautomation.webdev/resources/api"
        _write(zf, f"{api_dir}/doPost", endpoint_code)
        _write(zf, f"{api_dir}/config.json", json.dumps({
            "resource-type": "python-resource",
            "doPost": {"enabled": True, "require-https": False, "require-auth": False},
        }, indent=2))
        _write(zf, f"{api_dir}/resource.json", json.dumps({
            "scope": "G", "version": 1, "restricted": False, "overridable": True,
            "files": ["config.json", "doPost"], "attributes": {},
        }, indent=2))

    return buf.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the mcp-bridge project archive")
    parser.add_argument(
        "--check", action="store_true", help="check the tracked archive against current sources",
    )
    args = parser.parse_args()
    built = build()
    if args.check:
        try:
            with zipfile.ZipFile(OUT) as current, zipfile.ZipFile(io.BytesIO(built)) as expected:
                if sorted(current.namelist()) == sorted(expected.namelist()) and all(
                    current.read(name) == expected.read(name) for name in expected.namelist()
                ):
                    return
        except (OSError, zipfile.BadZipFile):
            pass
        parser.exit(1, "Bridge archive differs from its sources. Run python bridge/build_zip.py.\n")
    OUT.write_bytes(built)
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
