"""
MCP security gateway: an HTTP reverse proxy between an AI agent and a downstream MCP server.

  1. Authenticates the Bearer token and gets the user's role.
  2. Parses the JSON-RPC message.
  3. Blocks tools/call on admin_* tools for non-admins with error -32001 (downstream is never called).
  4. Forwards everything else to the downstream server.

Run: uv run python gateway.py   (http://127.0.0.1:8000/mcp)
"""

import json
import logging
import math

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

# Configuration
# ---------------------------------------------------------------------------
DOWNSTREAM_URL = "http://127.0.0.1:9000/mcp"

# Mock tokens: token -> role.
TOKENS = {
    "admin-token": "admin",
    "viewer-token": "viewer",
}

# JSON-RPC error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
UNAUTHORIZED = -32001

# Headers passed through the gateway (all others are dropped).
FORWARDED_REQUEST_HEADERS = ("accept", "mcp-session-id", "mcp-protocol-version")
FORWARDED_RESPONSE_HEADERS = ("content-type", "mcp-session-id")

# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [gateway] %(message)s")
log = logging.getLogger("gateway")

# Shared HTTP client for calls to the downstream server.
client = httpx.AsyncClient(timeout=30)


# Authentication middleware
# ---------------------------------------------------------------------------
class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Runs before every request: maps the Bearer token to a role (request.state.role), or returns 401."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        role = TOKENS.get(token.strip()) if scheme.lower() == "bearer" else None

        if role is None:
            log.warning("Rejected request: missing or invalid bearer token")
            return rpc_error(None, UNAUTHORIZED, "Unauthorized: missing or invalid bearer token",
                             status=401, headers={"WWW-Authenticate": "Bearer"})

        request.state.role = role
        return await call_next(request)


# Proxy endpoint
# ---------------------------------------------------------------------------
async def proxy(request: Request) -> Response:
    """Parse the JSON-RPC message, authorize it, then forward it downstream."""

    # 1. Parse. RecursionError: very deeply nested JSON.
    try:
        msg = json.loads(await request.body(), parse_constant=reject_constant, parse_float=finite_float)
    except (ValueError, RecursionError):
        return rpc_error(None, PARSE_ERROR, "Parse error", status=400)

    # Batches are rejected too, so an admin_ call can't hide inside an array.
    if not is_valid_request(msg):
        # Echo the id only if it is valid; otherwise reply with id null.
        msg_id = msg.get("id") if isinstance(msg, dict) and is_valid_id(msg.get("id")) else None
        return rpc_error(msg_id, INVALID_REQUEST, "Invalid Request", status=400)

    msg_id = msg.get("id")

    # 2. Authorize: only tools/call is restricted.
    if msg["method"] == "tools/call":
        params = msg.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        if not isinstance(name, str):
            return rpc_error(msg_id, INVALID_PARAMS, "Invalid params: params.name must be a string")

        if name.startswith("admin_") and request.state.role != "admin":
            log.warning("Blocked tools/call %s for role %s", name, request.state.role)
            # HTTP 200: MCP clients only read the JSON-RPC error on a successful HTTP response.
            return rpc_error(msg_id, UNAUTHORIZED, "Unauthorized Tool Call")

    # 3. Forward the parsed message (not the raw bytes), so downstream gets exactly what was checked.
    log.info("Forwarding %s for role %s", msg["method"], request.state.role)
    headers = {h: request.headers[h] for h in FORWARDED_REQUEST_HEADERS if h in request.headers}
    headers["content-type"] = "application/json"
    try:
        downstream = await client.post(DOWNSTREAM_URL, content=json.dumps(msg), headers=headers)
    except httpx.HTTPError as e:
        log.error("Downstream server unreachable: %s", e)
        return rpc_error(msg_id, INTERNAL_ERROR, "Downstream MCP server unavailable", status=502)

    # shortcut: the response is buffered, so an SSE stream arrives only when it ends; stream it if needed.
    return Response(
        content=downstream.content,
        status_code=downstream.status_code,
        headers={h: downstream.headers[h] for h in FORWARDED_RESPONSE_HEADERS if h in downstream.headers},
    )


# Helpers
# ---------------------------------------------------------------------------
def is_valid_request(msg: object) -> bool:
    """True if msg is a JSON-RPC 2.0 request: an object with jsonrpc "2.0", a string method and a valid id (if any)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return False
    return "id" not in msg or is_valid_id(msg["id"])


def is_valid_id(msg_id: object) -> bool:
    """True if the id is a string or an integer (bool excluded: Python treats it as an int)."""
    return isinstance(msg_id, (str, int)) and not isinstance(msg_id, bool)


def reject_constant(name: str):
    """Reject NaN / Infinity / -Infinity: Python's json accepts them, but they aren't valid JSON."""
    raise ValueError(f"Invalid JSON constant: {name}")


def finite_float(text: str) -> float:
    """Parse a JSON number, rejecting ones too large for a float (e.g. 1e999), which become inf."""
    value = float(text)
    if math.isinf(value):
        raise ValueError(f"Number out of range: {text}")
    return value


def rpc_error(msg_id, code: int, message: str, status: int = 200, headers: dict | None = None) -> JSONResponse:
    """Build a JSON-RPC error response."""
    body = {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
    return JSONResponse(body, status_code=status, headers=headers)


# App
# ---------------------------------------------------------------------------
app = Starlette(
    routes=[Route("/mcp", proxy, methods=["POST"])],
    middleware=[Middleware(BearerAuthMiddleware)],
)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
