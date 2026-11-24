"""Read-only diagnosis of the four Cupper tags through the existing bridge."""

from contextlib import closing
import json

TEST_GATEWAY = "https://192.168.50.20:8043"
FOLDER = "Line_1/Cupper/Ignition"
NAMES = ("Outfeed_Counter", "Status", "Speed", "Production")
PATHS = tuple(f"[default]{FOLDER}/{name}" for name in NAMES)
PROPERTIES = ("valueSource", "dataType", "defaultValue", "historyEnabled", "historyProvider")


def capture(call):
    from ignition_mcp.errors import IgnitionMcpError

    try:
        return call()
    except IgnitionMcpError as exc:
        message = str(exc)
        if "Java StackOverflowError" in message:
            return {"error": "Java StackOverflowError"}
        if "Cannot reach gateway" in message:
            return {"error": "gateway_unreachable"}
        return {"error": exc.code}


def diagnose(gateway, bridge):
    def browse():
        result = bridge.call("tags/browse", {"path": FOLDER, "provider": "default", "depth": 1})
        return [node for node in result["results"] if node["name"] in NAMES]

    def read():
        return bridge.call("tags/read", {"paths": list(PATHS)}).get("tags")

    def read_properties():
        paths = [f"{path}.{prop}" for path in PATHS for prop in PROPERTIES]
        return bridge.call("tags/read", {"paths": paths}).get("tags")

    def export():
        # The existing REST client supports this route if the Gateway exposes it.
        result = gateway.get_json("tags/export", params={"provider": "default", "path": FOLDER, "type": "json"})
        if isinstance(result, dict) and isinstance(result.get("tags"), list):
            return [node for node in result["tags"] if node.get("name") in NAMES]
        return {"error": "unrecognized_export"}

    return {
        "browse": capture(browse),
        "read": capture(read),
        "properties": capture(read_properties),
        "scoped_state": {path: capture(lambda: bridge.call("tags/scoped-state", {"path": path}))
                         for path in PATHS},
        "rest_export": capture(export),
    }


def main():
    from ignition_mcp.bridge import BridgeClient
    from ignition_mcp.client import GatewayClient
    from ignition_mcp.config import load_settings

    settings = load_settings()
    if settings.gateway_url != TEST_GATEWAY:
        raise SystemExit("Unexpected Gateway URL")
    with closing(GatewayClient(settings)) as gateway, closing(BridgeClient(settings)) as bridge:
        print(json.dumps(diagnose(gateway, bridge), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
