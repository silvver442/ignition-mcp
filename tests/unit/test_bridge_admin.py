"""Exercise the bundled archive and create-only installer without a live Gateway."""

from __future__ import annotations

import ast
import hashlib
import json
import time
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import BytesIO, StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import httpx

from ignition_mcp.bridge import BridgeClient
from ignition_mcp.config import Settings
from ignition_mcp.errors import BridgeUnavailableError, GatewayError, NotFoundError
from ignition_mcp.tools import bridge_admin

BRIDGE = Path(__file__).resolve().parents[2] / "bridge"
SECRET = "synthetic-test-secret-for-fresh-install"
ROUTES = {
    "alarms/ack", "alarms/journal", "alarms/status", "db/named-query", "db/query",
    "files/delete", "files/list", "files/read", "files/write", "history/providers",
    "history/query", "history/raw", "ping", "tags/browse", "tags/configure",
    "tags/read", "tags/scoped-configure", "tags/scoped-cupper-history",
    "tags/scoped-cupper-migrate", "tags/scoped-cupper-source-test",
    "tags/scoped-cupper-validate", "tags/scoped-restore", "tags/scoped-state",
    "tags/write", "trial/reset",
}


class BridgeInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        settings = Settings("http://example.invalid", "test:fixture", SECRET, allow_writes=True)
        self.gateway = Mock()
        self.gateway.get_json.side_effect = NotFoundError("The test project is absent")
        self.context = SimpleNamespace(
            settings=settings, client=self.gateway, bridge=Mock(), trial_warning=lambda: None,
        )
        self.tools = {}
        server = Mock()
        server.tool.return_value.side_effect = lambda fn: self.tools.setdefault(fn.__name__, fn)
        bridge_admin.register(server, self.context)

    def test_zip_uses_canonical_library_and_only_injects_the_assignment(self) -> None:
        original = bridge_admin.BRIDGE_ZIP.read_bytes()
        secret = "synthetic-fixture-with-quotes-'é"
        payload, version, digest = bridge_admin._zip_with_secret(secret)
        with zipfile.ZipFile(BytesIO(original)) as pristine, zipfile.ZipFile(BytesIO(payload)) as zip:
            name = bridge_admin.LIB_ARCNAME
            source = pristine.read(name)
            assert source == (BRIDGE / "src/mcp_bridge_lib.py").read_bytes()
            injected = zip.read(name)
            assert set(zip.namelist()) == set(pristine.namelist()) == bridge_admin.BRIDGE_FILES
            for resource in set(zip.namelist()) - {name}:
                assert zip.read(resource) == pristine.read(resource)
        literal = ("u" + ascii(secret)).encode()
        assert injected.replace(literal, repr(bridge_admin.SECRET_PLACEHOLDER).encode(), 1) == source
        assert ast.literal_eval(literal.decode()) == secret
        assert digest == hashlib.sha256(injected).hexdigest()
        assert version == "0.1.13"
        assert bridge_admin.BRIDGE_ZIP.read_bytes() == original

    def test_fresh_install_exercises_the_signed_webdev_endpoint(self) -> None:
        logger = Mock()
        output = StringIO()
        library = {}
        webdev = {}
        def post(request: httpx.Request) -> httpx.Response:
            route = request.url.path.removeprefix("/system/webdev/mcp-bridge/api/")
            packet = {"remainingPath": route, "data": json.loads(request.content)}
            return httpx.Response(200, json=webdev["doPost"](packet, None)["json"])

        transport = httpx.MockTransport(post)
        with TemporaryDirectory(dir=BRIDGE) as folder, httpx.Client(
            base_url=self.context.settings.gateway_url, transport=transport,
        ) as http:
            with patch.object(httpx, "Client", return_value=http):
                client = BridgeClient(self.context.settings)
            self.context.bridge = client
            root = Path(folder)
            system = SimpleNamespace(
                util=SimpleNamespace(getLogger=lambda _: logger, getProperty=lambda _: folder,
                                     jsonDecode=json.loads),
                date=SimpleNamespace(now=lambda: None, toMillis=lambda _: int(time.time()) * 1000),
            )

            def import_zip(path, payload, content_type, *, params):
                assert path == "projects/import/mcp-bridge"
                assert content_type == "application/zip" and params == {"overwrite": "false"}
                with zipfile.ZipFile(BytesIO(payload)) as archive:
                    assert archive.testzip() is None
                    source = archive.read(bridge_admin.LIB_ARCNAME)
                    dest = root / bridge_admin.BRIDGE_PROJECT_LIBRARY
                    dest.parent.mkdir(parents=True)
                    dest.write_bytes(source)
                    library.update(system=system, long=int, unicode=str, basestring=str)
                    exec(compile(source, "simulated-bridge.py", "exec"), library)
                    library["PROJECTS_ROOT"] = str(root)
                    webdev["mcp_bridge_lib"] = SimpleNamespace(handle=library["handle"])
                    endpoint = archive.read(f"{bridge_admin.WEBDEV}/doPost")
                    exec(compile(endpoint, "simulated-webdev.py", "exec"), webdev)

            self.gateway.post_bytes.side_effect = import_zip
            png = b"\x89PNG\r\n\x1a\n"
            (root / "thumbnail.png").write_bytes(png)
            text = "Żółć\r\nSecond line\n"
            (root / "view.json").write_bytes(text.encode("utf-8"))
            with redirect_stdout(output), redirect_stderr(output):
                result = self.tools["bridge_install"](confirm=True)
            assert result == {
                "status": "success", "installed": True, "verified": True, "bridge_version": "0.1.13",
            }
            status = self.tools["bridge_status"]()
            assert status["version"] == "0.1.13" and set(status["capabilities"]) == ROUTES
            assert client.call("files/read", {"path": "view.json"}) == {"content": text}
            assert client.call("files/read", {"path": "thumbnail.png", "format": "sha256"}) == {
                "sha256": hashlib.sha256(png).hexdigest(),
            }
            with self.assertRaises(GatewayError):
                client.call("files/read", {"path": "../outside.png", "format": "sha256"})
            unauthenticated = {"remainingPath": "ping", "data": {}}
            assert webdev["doPost"](unauthenticated, None)["json"]["code"] == "auth"

        self.gateway.get_json.assert_called_once_with("projects/find/mcp-bridge")
        self.gateway.post_bytes.assert_called_once()
        assert [item[0] for item in self.gateway.method_calls] == ["get_json", "post_bytes"]
        assert SECRET not in output.getvalue()
        assert SECRET not in json.dumps(result)
        self.assertEqual(logger.mock_calls, [])

    def test_existing_project_is_never_imported_or_replaced(self) -> None:
        self.gateway.get_json.side_effect = None
        self.gateway.get_json.return_value = {"name": "mcp-bridge"}
        result = self.tools["bridge_install"](confirm=True)
        assert result["error"] == "validation_error"
        assert self.gateway.method_calls == [call.get_json("projects/find/mcp-bridge")]
        self.context.bridge.ping.assert_not_called()

    def test_lost_import_response_is_reported_without_retry_or_secret(self) -> None:
        self.gateway.post_bytes.side_effect = GatewayError("simulated response " + SECRET)
        result = self.tools["bridge_install"](confirm=True)
        assert result["status"] == "uncertain" and result["phase"] == "import"
        assert result["installed"] is None and result["verified"] is False
        assert SECRET not in json.dumps(result)
        assert [item[0] for item in self.gateway.method_calls] == ["get_json", "post_bytes"]
        self.context.bridge.ping.assert_not_called()

    def test_mismatched_running_version_or_digest_never_claims_success(self) -> None:
        for version in ("0.1.12", "0.1.13"):
            with self.subTest(version=version):
                self.gateway.reset_mock()
                self.context.bridge.reset_mock()
                self.context.bridge.ping.return_value = {"version": version}
                self.context.bridge.call.return_value = {"unexpected": SECRET}
                result = self.tools["bridge_install"](confirm=True)
                assert result["status"] == "uncertain" and result["phase"] == "verification"
                assert result["installed"] is True and result["verified"] is False
                assert SECRET not in json.dumps(result)
                assert [item[0] for item in self.gateway.method_calls] == ["get_json", "post_bytes"]
                if version == "0.1.13":
                    self.context.bridge.call.assert_called_once_with(
                        "files/read", {"path": bridge_admin.BRIDGE_PROJECT_LIBRARY, "format": "sha256"},
                    )
                else:
                    self.context.bridge.call.assert_not_called()

    def test_invalid_archive_prevents_any_write(self) -> None:
        config = f"{bridge_admin.WEBDEV}/config.json"
        with zipfile.ZipFile(bridge_admin.BRIDGE_ZIP) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        with TemporaryDirectory(dir=BRIDGE) as folder:
            invalid = Path(folder) / "invalid.zip"
            for content in (None, b'{"resource-type": "python-resource", "doPost": {"enabled": false}}'):
                with zipfile.ZipFile(invalid, "w") as archive:
                    for name, data in entries.items():
                        if name != config:
                            archive.writestr(name, data)
                        elif content is not None:
                            archive.writestr(name, content)
                with patch.object(bridge_admin, "BRIDGE_ZIP", invalid):
                    result = self.tools["bridge_install"](confirm=True)
                assert result["error"] == BridgeUnavailableError.code
        self.gateway.post_bytes.assert_not_called()

    def test_write_confirmation_still_applies(self) -> None:
        result = self.tools["bridge_install"]()
        assert result["error"] == "write_not_allowed"
        assert not self.gateway.method_calls

    def test_missing_or_placeholder_secret_prevents_any_write(self) -> None:
        for secret in (None, bridge_admin.SECRET_PLACEHOLDER):
            with self.subTest(secret=secret):
                self.context.settings = replace(self.context.settings, bridge_secret=secret)
                result = self.tools["bridge_install"](confirm=True)
                assert result["error"] == "validation_error"
        assert not self.gateway.method_calls
