"""
LLM gateway with per-tenant token rate limiting and model failover.

POST /v1/chat/completions authenticates the tenant key, enforces 50,000
tokens/minute per tenant, and routes to the primary model, failing over to the
secondary on 429 or a 3000 ms timeout. Errors use one sanitized format.

Run: uv run --env-file .env python gateway.py   (http://127.0.0.1:8000/v1/chat/completions)
"""

import asyncio
import json
import logging
import math
import os
import uuid

import uvicorn
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from rate_limiter import RateLimiter
from router import UpstreamError, complete

# Configuration
# ---------------------------------------------------------------------------
TOKENS_PER_MINUTE = 50_000
DEFAULT_MAX_TOKENS = 1024  # reserved when the request sets no limit
DB_PATH = os.environ.get("DB_PATH", "rate_limits.db")

# Tenant API key -> tenant id (mock).
TENANTS = {"tenant-a-key": "tenant-a", "tenant-b-key": "tenant-b"}

# Upstream failure kind -> (status, type, message).
UPSTREAM_ERRORS = {
    "timeout": (504, "upstream_timeout", "The model provider did not respond in time."),
    "error": (502, "upstream_error", "The model provider could not complete the request."),
}

# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [gateway] %(message)s")
log = logging.getLogger("gateway")

limiter = RateLimiter(DB_PATH, TOKENS_PER_MINUTE)


# Endpoint
# ---------------------------------------------------------------------------
async def chat_completions(request: Request) -> JSONResponse:
    """Authenticate, validate, rate-limit, then route with failover."""
    request_id = uuid.uuid4().hex

    auth = request.headers.get("authorization", "")
    tenant = TENANTS.get(auth.removeprefix("Bearer ")) if auth.startswith("Bearer ") else None
    if tenant is None:
        return gateway_error(401, "unauthorized", "Missing or invalid API key.", request_id)

    try:
        body = json.loads(await request.body())
    except ValueError:
        return gateway_error(400, "invalid_request", "Request body must be valid JSON.", request_id)
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list) or not body["messages"]:
        return gateway_error(400, "invalid_request", "Request must be a JSON object with a non-empty messages list.", request_id)
    if body.get("stream"):
        return gateway_error(400, "invalid_request", "Streaming is not supported.", request_id)
    max_tokens = body.get("max_completion_tokens", body.get("max_tokens", DEFAULT_MAX_TOKENS))
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens < 1:
        return gateway_error(400, "invalid_request", "max_tokens must be a positive integer.", request_id)

    # Reserve the worst case now; settle to real usage after.
    # shortcut: ~4 chars/token estimate; use the model's tokenizer for exact pre-checks.
    prompt_tokens = len(json.dumps(body["messages"])) // 4
    estimate = prompt_tokens + max_tokens
    if estimate > TOKENS_PER_MINUTE:
        return gateway_error(400, "invalid_request", "Request exceeds the per-minute token limit.", request_id)

    usage_id, retry_after = await asyncio.to_thread(limiter.reserve, tenant, estimate)
    if usage_id is None:
        log.info("[%s] %s rate limited (needs %d tokens)", request_id, tenant, estimate)
        return gateway_error(429, "rate_limit_exceeded", "Token rate limit exceeded. Retry later.", request_id,
                             headers={"Retry-After": str(max(1, math.ceil(retry_after)))})

    try:
        data, provider = await complete(body, request_id)
    except UpstreamError as e:
        await asyncio.to_thread(limiter.settle, usage_id, prompt_tokens)  # charge the prompt; it was sent
        status, error_type, message = UPSTREAM_ERRORS[e.kind]
        return gateway_error(status, error_type, message, request_id)

    usage = data.get("usage")
    used = usage.get("total_tokens") if isinstance(usage, dict) else None
    if isinstance(used, int):  # else keep the estimate
        await asyncio.to_thread(limiter.settle, usage_id, used)
    log.info("[%s] %s served by %s, %s tokens", request_id, tenant, provider, used)
    return JSONResponse(data)


# Errors
# ---------------------------------------------------------------------------
def gateway_error(status: int, error_type: str, message: str, request_id: str,
                  headers: dict | None = None) -> JSONResponse:
    """Standard error payload."""
    return JSONResponse({"error": {"type": error_type, "message": message, "request_id": request_id}},
                        status_code=status, headers=headers)


async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
    """404/405 in the gateway format."""
    return gateway_error(exc.status_code, "invalid_request", exc.detail, uuid.uuid4().hex)


async def internal_error(request: Request, exc: Exception) -> JSONResponse:
    """Log the traceback; return no details."""
    request_id = uuid.uuid4().hex
    log.exception("[%s] Unhandled error", request_id)
    return gateway_error(500, "internal_error", "Internal gateway error.", request_id)


# App
# ---------------------------------------------------------------------------
app = Starlette(
    routes=[Route("/v1/chat/completions", chat_completions, methods=["POST"])],
    exception_handlers={HTTPException: http_error, Exception: internal_error},
)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
