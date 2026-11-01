"""Model routing: primary model first, secondary on 429 or a 3000 ms timeout."""

import asyncio
import logging
import os
from dataclasses import dataclass

import httpx

log = logging.getLogger("gateway")

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


@dataclass(frozen=True)
class Provider:
    name: str
    url: str
    model: str
    api_key: str
    timeout: float  # total deadline in seconds, connect to last byte


PRIMARY = Provider(
    "primary",
    os.environ.get("PRIMARY_URL", GROQ_URL),
    os.environ.get("PRIMARY_MODEL", "openai/gpt-oss-120b"),
    os.environ.get("PRIMARY_API_KEY", ""),
    3.0,
)
SECONDARY = Provider(
    "secondary",
    os.environ.get("SECONDARY_URL", GROQ_URL),
    os.environ.get("SECONDARY_MODEL", "qwen/qwen3.8-27b"),
    os.environ.get("SECONDARY_API_KEY", ""),
    30.0,
)

# httpx timeouts are per read, not total; asyncio.timeout in call() sets the deadline.
client = httpx.AsyncClient(timeout=None)


class UpstreamError(Exception):
    """Upstream failure. `kind` is "timeout" or "error"; details are logged, not returned."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


async def complete(body: dict, request_id: str) -> tuple[dict, str]:
    """Return (completion, provider name); fail over on primary 429 or timeout."""
    try:
        response = await call(PRIMARY, body, request_id)
        if response.status_code != 429:
            return parse(PRIMARY, response, request_id)
        log.warning("[%s] primary returned 429, failing over", request_id)
    except TimeoutError:
        log.warning("[%s] primary timed out after %.0f ms, failing over", request_id, PRIMARY.timeout * 1000)

    try:
        response = await call(SECONDARY, body, request_id)
    except TimeoutError:
        log.error("[%s] secondary timed out after %.0f ms", request_id, SECONDARY.timeout * 1000)
        raise UpstreamError("timeout")
    return parse(SECONDARY, response, request_id)


async def call(provider: Provider, body: dict, request_id: str) -> httpx.Response:
    """POST to one provider within its deadline.

    On timeout the call is cancelled and its connection closed, so a late reply can't be used.
    """
    try:
        async with asyncio.timeout(provider.timeout):
            return await client.post(
                provider.url,
                json={**body, "model": provider.model},
                headers={"Authorization": f"Bearer {provider.api_key}"},
            )
    except httpx.HTTPError as e:
        log.error("[%s] %s unreachable: %r", request_id, provider.name, e)
        raise UpstreamError("error")


def parse(provider: Provider, response: httpx.Response, request_id: str) -> tuple[dict, str]:
    """Return the completion JSON; raise UpstreamError on any bad reply."""
    if response.status_code != 200:
        log.error("[%s] %s returned HTTP %s: %.300s", request_id, provider.name, response.status_code, response.text)
        raise UpstreamError("error")
    try:
        data = response.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        log.error("[%s] %s returned a body that is not a JSON object", request_id, provider.name)
        raise UpstreamError("error")
    return data, provider.name
