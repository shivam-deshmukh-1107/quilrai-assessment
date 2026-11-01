"""
LLM gateway with streaming PII redaction.

POST /v1/chat/completions forwards an OpenAI-style chat request to Groq with
streaming on and streams the reply back. Emails, SSNs and credit card numbers
become [REDACTED] as chunks arrive, even when split across chunks.

Run: uv run --env-file .env python gateway.py   (http://127.0.0.1:8000/v1/chat/completions)
"""

import json
import logging
import os
from collections.abc import AsyncIterator

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from redactor import PII, REDACTED, StreamRedactor

# Configuration
# ---------------------------------------------------------------------------
PROVIDER_URL = os.environ.get("PROVIDER_URL", "https://api.groq.com/openai/v1/chat/completions")
API_KEY = os.environ.get("GROQ_API_KEY", "")

# Delta fields that carry generated text.
TEXT_FIELDS = ("content", "reasoning")

# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [gateway] %(message)s")
log = logging.getLogger("gateway")

# Shared HTTP client: 10 s to connect, at most 60 s between chunks.
client = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10))


# Endpoint
# ---------------------------------------------------------------------------
async def chat_completions(request: Request) -> Response:
    """Forward the request to the provider and stream back the redacted reply."""
    try:
        body = json.loads(await request.body())
    except ValueError:
        return error(400, "Request body must be valid JSON")
    if not isinstance(body, dict):
        return error(400, "Request body must be a JSON object")
    body["stream"] = True

    upstream_request = client.build_request(
        "POST", PROVIDER_URL, json=body, headers={"Authorization": f"Bearer {API_KEY}"}
    )
    try:
        upstream = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as e:
        log.error("Provider unreachable: %s", e)
        return error(502, "LLM provider unavailable")

    # Provider errors (bad key, unknown model) are passed through.
    if upstream.status_code != 200:
        content = await upstream.aread()
        await upstream.aclose()
        log.warning("Provider returned HTTP %s", upstream.status_code)
        return Response(content, status_code=upstream.status_code,
                        media_type=upstream.headers.get("content-type"))

    return StreamingResponse(redact_stream(upstream), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


async def redact_stream(upstream: httpx.Response) -> AsyncIterator[str]:
    """Yield the provider's SSE lines as they arrive, with PII redacted."""
    redactors: dict[tuple[int, str], StreamRedactor] = {}  # one per (choice index, field)
    last_chunk: dict = {}
    try:
        async for line in upstream.aiter_lines():
            if not line.startswith("data:"):
                yield line + "\n"  # separators and comments pass through
                continue

            data = line[5:].strip()
            if data == "[DONE]":
                leftover = flush_all(redactors, last_chunk)
                if leftover:
                    yield leftover
                yield line + "\n"
                continue

            try:
                chunk = json.loads(data)
            except ValueError:
                yield "data: " + PII.sub(REDACTED, data) + "\n"  # not JSON: redact the raw text
                continue

            redact_chunk(chunk, redactors)
            last_chunk = chunk
            yield "data: " + json.dumps(chunk) + "\n"

        # Stream ended without [DONE]: release held text.
        leftover = flush_all(redactors, last_chunk)
        if leftover:
            yield leftover
    finally:
        await upstream.aclose()
        total = sum(r.count for r in redactors.values())
        log.info("Stream finished: %d redaction(s)", total)


def redact_chunk(chunk: dict, redactors: dict[tuple[int, str], StreamRedactor]) -> None:
    """Redact one chunk's text deltas in place."""
    for choice in chunk.get("choices") or []:
        delta = choice.get("delta") or {}
        index = choice.get("index", 0)
        finished = choice.get("finish_reason") is not None
        for field in TEXT_FIELDS:
            text = delta.get(field)
            key = (index, field)
            if not isinstance(text, str) and not (finished and key in redactors):
                continue
            redactor = redactors.setdefault(key, StreamRedactor())
            safe = redactor.feed(text if isinstance(text, str) else "")
            if finished:
                safe += redactor.flush()  # last chunk for this choice: release held text
            delta[field] = safe
        if delta:
            choice["delta"] = delta


def flush_all(redactors: dict[tuple[int, str], StreamRedactor], last_chunk: dict) -> str:
    """Return held text as one extra SSE event, for choices that got no finish_reason."""
    choices: dict[int, dict] = {}
    for (index, field), redactor in redactors.items():
        text = redactor.flush()
        if text:
            choices.setdefault(index, {"index": index, "delta": {}, "finish_reason": None})["delta"][field] = text
    if not choices:
        return ""
    event = {k: last_chunk[k] for k in ("id", "object", "created", "model") if k in last_chunk}
    event["choices"] = list(choices.values())
    return "data: " + json.dumps(event) + "\n\n"


def error(status: int, message: str) -> JSONResponse:
    """OpenAI-style error response."""
    return JSONResponse({"error": {"message": message, "type": "gateway_error"}}, status_code=status)


# App
# ---------------------------------------------------------------------------
app = Starlette(routes=[Route("/v1/chat/completions", chat_completions, methods=["POST"])])

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
