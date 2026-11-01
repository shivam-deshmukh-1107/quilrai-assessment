# MCP Security Gateway

A lightweight HTTP/JSON-RPC reverse proxy written in Python with Starlette. It sits between an AI agent client and a downstream mock MCP server. It reads each request's Bearer token to get the user's role, then lets only admins call `admin_` tools.

```
AI agent ── HTTP ──▶ gateway.py (:8000) ── HTTP ──▶ downstream.py (:9000)
                     auth + authorization            mock MCP server
```

## Project structure

```
gateway.py      The gateway: Bearer auth middleware, JSON-RPC parsing, authorization, forwarding
downstream.py   Mock downstream MCP server with two tools: get_customer_record and admin_reset_key
pyproject.toml  Dependencies: starlette, uvicorn, httpx
uv.lock         Exact dependency versions
```

## Run

Requires Python 3.14+ and [uv](https://docs.astral.sh/uv/). Start each server in its own terminal:

```bash
uv sync
uv run python downstream.py     # mock MCP server on http://127.0.0.1:9000/mcp
uv run python gateway.py        # gateway on http://127.0.0.1:8000/mcp
```

Send requests to the gateway with one of the mock tokens:

| Token | Role |
|---|---|
| `admin-token` | `admin` |
| `viewer-token` | `viewer` |

Example: a viewer tries to call an admin tool and is blocked.

```bash
curl -s http://127.0.0.1:8000/mcp -H "Authorization: Bearer viewer-token" -H "Content-Type: application/json" -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"admin_reset_key\",\"arguments\":{}}}"
```

```json
{"jsonrpc":"2.0","id":1,"error":{"code":-32001,"message":"Unauthorized Tool Call"}}
```

## Requirement 1: read the Bearer token and get the role

`BearerAuthMiddleware` runs before every request. It reads `Authorization: Bearer <token>` and looks up the role in a mock token table (`TOKENS` in `gateway.py`).

- The scheme is case-insensitive (`Bearer`, `bearer`, `BEARER`).
- A missing header, another scheme (such as `Basic`), or an unknown token gets HTTP `401` with JSON-RPC error `-32001`. The body isn't read.
- The token is not forwarded to the downstream server. It's meant for the gateway only.

## Requirement 2: inspect the JSON-RPC payload

The body must be a single JSON-RPC 2.0 request: an object with `"jsonrpc": "2.0"`, a string `method`, and an optional `id` that is a string or an integer.

| `method` | What the gateway does |
|---|---|
| `tools/list` | Forwards it to the downstream server and returns the response unchanged. |
| `tools/call` | Reads `params.name`. If it starts with `admin_` and the role isn't `admin`, returns `-32001 Unauthorized Tool Call` itself. The downstream server is never called. Otherwise it forwards the request. |
| Anything else (`initialize`, notifications, ...) | Forwards it, so a real MCP client can connect through the gateway. |

## Errors

| Case | HTTP status | JSON-RPC error |
|---|---|---|
| Missing or invalid Bearer token | 401 | `-32001` Unauthorized |
| Non-admin calls an `admin_` tool | 200 | `-32001` Unauthorized Tool Call |
| Body is not valid JSON (including `NaN`, `Infinity`, numbers too large for a float, and extremely deep nesting) | 400 | `-32700` Parse error |
| Not a valid JSON-RPC 2.0 request (wrong `jsonrpc`, missing or non-string `method`, invalid `id`, batch array) | 400 | `-32600` Invalid Request |
| `tools/call` without a string `params.name` | 200 | `-32602` Invalid params |
| Downstream server unreachable or times out (30 s) | 502 | `-32603` Downstream MCP server unavailable |

The unauthorized tool call returns HTTP `200` on purpose. It's a valid JSON-RPC reply, and MCP clients only read the JSON-RPC error when the HTTP request itself succeeds.

When the `id` is invalid or can't be read, the error reply uses `"id": null`, as JSON-RPC requires.

## Forwarding

- **Request:** the gateway forwards the message it parsed, sent again as JSON, not the raw bytes. A payload with two `"name"` keys therefore can't show the gateway one tool name and the downstream server another.
- **Request headers passed on:** `Accept`, `Mcp-Session-Id`, `MCP-Protocol-Version`. All others, including `Authorization` and `Cookie`, are dropped.
- **Response:** the downstream server's status code and body are returned unchanged, with its `Content-Type` and `Mcp-Session-Id` headers.

## Authorization edge cases

- **Batches are rejected.** MCP doesn't use JSON-RPC batches, and an `admin_` call could otherwise hide inside an array.
- **Escaped names are caught.** A name written as `admin_reset_key` is decoded before the check, so it's blocked.
- **Notifications are checked too.** An `admin_` call without an `id` is also blocked.
- **The check is case-sensitive.** Names like `Admin_reset_key` pass the gateway, but tool names are case-sensitive, so the downstream server rejects them as unknown tools.

## Logging

The gateway logs to stderr:

| Event | Level |
|---|---|
| Missing or invalid token | WARNING |
| Blocked `admin_` call (tool name and role) | WARNING |
| Forwarded request (method and role) | INFO |
| Downstream server unreachable | ERROR |

The mock downstream server logs every request it receives, which shows that blocked calls never reach it.

## Limitations

- Responses are buffered before they're returned. That works for normal JSON replies, but a long-running SSE stream from the downstream server would only arrive once it ends.
- There's no limit on the request body size.
