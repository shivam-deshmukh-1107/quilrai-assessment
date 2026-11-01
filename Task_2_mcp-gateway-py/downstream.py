"""
Mock downstream MCP server: answers MCP JSON-RPC requests over HTTP.

I wrote this so the gateway has something to forward to. It exposes one
normal tool and one admin tool, and logs every request it receives, so you can
see which calls the gateway let through.
"""

import logging

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [downstream] %(message)s")
log = logging.getLogger("downstream")

# Tools
#---------------------------------------------------------------------------
TOOLS = [
    {
        "name": "get_customer_record",
        "description": "Fetch a customer record by ID.",
        "inputSchema": {"type": "object", "properties": {"customer_id": {"type": "string"}}, "required": ["customer_id"]},
    },
    {
        "name": "admin_reset_key",
        "description": "Reset an API key. Admin only.",
        "inputSchema": {"type": "object", "properties": {"key_id": {"type": "string"}}, "required": ["key_id"]},
    },
]
TOOL_NAMES = {tool["name"] for tool in TOOLS}
#---------------------------------------------------------------------------

# MCP JSON-RPC handler
#---------------------------------------------------------------------------
async def mcp(request: Request) -> Response:
    """Handle one JSON-RPC message."""
    msg = await request.json()
    method = msg.get("method")
    log.info("Received %s %s", method, msg.get("params", {}).get("name", ""))

    # Notifications (no id) get no JSON-RPC reply.
    if "id" not in msg:
        return Response(status_code=202)

    if method == "initialize":
        return reply(msg, {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "mock-downstream", "version": "1.0.0"},
        })

    if method == "tools/list":
        return reply(msg, {"tools": TOOLS})

    if method == "tools/call":
        name = msg["params"]["name"]
        if name not in TOOL_NAMES:
            return error(msg, -32602, f"Unknown tool: {name}")
        arguments = msg["params"].get("arguments", {})
        return reply(msg, {"content": [{"type": "text", "text": f"{name} executed with {arguments}"}], "isError": False})

    return error(msg, -32601, f"Method not found: {method}")
#---------------------------------------------------------------------------

# Response helpers
#---------------------------------------------------------------------------
def reply(msg: dict, result: dict) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": msg["id"], "result": result})

def error(msg: dict, code: int, message: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": code, "message": message}})

# Server startup
#---------------------------------------------------------------------------
app = Starlette(routes=[Route("/mcp", mcp, methods=["POST"])])

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=9000)
