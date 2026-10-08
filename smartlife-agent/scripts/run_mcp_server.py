#!/usr/bin/env python3
"""启动 SmartLife Agent 的真实 MCP Server。"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.mcp_servers.protocol_server import build_mcp_server


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", choices=("stdio", "sse", "streamable-http"), default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--include-write-tools", action="store_true")
    args = parser.parse_args()

    auth_token = os.environ.get("SMARTLIFE_MCP_AUTH_TOKEN", "") if args.transport != "stdio" else ""
    public_url = os.environ.get(
        "SMARTLIFE_MCP_PUBLIC_URL",
        f"http://{args.host}:{args.port}",
    )
    server = build_mcp_server(
        include_write_tools=args.include_write_tools,
        auth_token=auth_token or None,
        public_url=public_url,
    )
    if args.transport == "stdio":
        server.run("stdio")
        return 0

    try:
        import uvicorn
    except ModuleNotFoundError as exc:
        raise SystemExit("HTTP transport 需要 uvicorn") from exc

    app = (
        server.streamable_http_app(host=args.host)
        if args.transport == "streamable-http"
        else server.sse_app(host=args.host)
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level=os.environ.get("LOG_LEVEL", "info"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
