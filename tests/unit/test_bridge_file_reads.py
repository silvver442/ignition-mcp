"""Exercise the bridge file-read error contract without a gateway."""

import errno
from json import loads
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
import pytest

from ignition_mcp.bridge import BridgeClient
from ignition_mcp.errors import BridgeUnavailableError, GatewayError, NotFoundError


@pytest.fixture
def bridge_handler():
    source = Path(__file__).resolve().parents[2] / "bridge/src/mcp_bridge_lib.py"
    system = SimpleNamespace(util=Mock())
    system.util.getProperty.return_value = "."
    namespace = {"system": system}
    exec(compile(source.read_bytes(), str(source), "exec"), namespace)
    namespace["_verify"] = lambda payload: (payload, None)
    return namespace["handle"]


@pytest.mark.parametrize(
    ("failure", "expected"),
    [(errno.ENOENT, NotFoundError), (errno.EACCES, GatewayError), (errno.EIO, GatewayError)],
)
def test_file_read_error_round_trip(settings, bridge_handler, failure, expected):
    def respond(url, *, json):
        assert url.endswith("/files/read")
        with patch("os.stat", side_effect=OSError(failure, "test failure")):
            result = bridge_handler(
                {"remainingPath": "files/read", "data": loads(json["payload"])}
            )
        return httpx.Response(200, json=result["json"])

    client = BridgeClient(settings)
    with patch.object(client._http, "post", side_effect=respond), pytest.raises(expected):
        try:
            client.call("files/read", {"path": "Demo/resource.json"})
        finally:
            client.close()


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(404), BridgeUnavailableError),
        (httpx.Response(200, json={"ok": False, "error": "file not found: Demo"}), GatewayError),
        (httpx.Response(200, text="invalid response"), GatewayError),
    ],
)
def test_bridge_does_not_infer_file_absence(settings, response, expected):
    client = BridgeClient(settings)
    with patch.object(client._http, "post", return_value=response), pytest.raises(expected):
        try:
            client.call("files/read", {"path": "Demo/resource.json"})
        finally:
            client.close()
