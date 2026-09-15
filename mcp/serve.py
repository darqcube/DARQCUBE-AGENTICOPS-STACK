"""Entry point. MCP_SERVER selects the module, PORT selects the port."""
from __future__ import annotations

import importlib
import os
import sys

VALID = ["infrahub", "prometheus", "loki", "grafana", "netmiko", "assurance"]


def main() -> int:
    name = os.environ.get("MCP_SERVER", "")
    if name not in VALID:
        print(f"MCP_SERVER must be one of: {', '.join(VALID)} (got {name!r})", file=sys.stderr)
        return 2

    port = int(os.environ.get("PORT", "9000"))
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    module = importlib.import_module(f"servers.{name}")
    from common import serve

    tools = getattr(module.mcp, "_tool_manager", None)
    count = len(tools.list_tools()) if tools else "?"
    print(f"mcp-{name} listening on :{port} with {count} tool(s)", flush=True)

    serve(module.mcp, port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
