# LLM Gateway: Rate Limiting and Model Fallback Router

A Python/Starlette gateway that:
- limits each tenant API key to 50,000 tokens/minute, using a sliding window in on-disk SQLite
- routes requests to a primary model, failing over to a secondary on 429 or a 3000 ms timeout
- returns every error in one sanitized format

```
client ──POST /v1/chat/completions──▶ gateway.py (:8000)
                                        │ 1. tenant key → rate limiter (SQLite)
                                        │ 2. primary model ── 429 / >3000 ms ──▶ secondary model
       ◀──── completion or gateway error ┘
```

## Project structure

```
gateway.py       Endpoint: auth, validation, rate limiting, error format
rate_limiter.py  Token-aware sliding window in SQLite
router.py        Primary/secondary providers and failover
pyproject.toml   Dependencies: starlette, uvicorn, httpx
uv.lock          Pinned versions
.env             Provider API keys (in .gitignore)
```

`rate_limits.db` is created on first run.

## Run

Requires Python 3.14+, [uv](https://docs.astral.sh/uv/) and a Groq API key.

1. Put the keys in `.env`. Both providers default to Groq, so it's the same key twice:

   ```
   PRIMARY_API_KEY=gsk_...
   SECONDARY_API_KEY=gsk_...
   ```

2. Start the gateway:

   ```bash
   uv sync
   uv run --env-file .env python gateway.py      # http://127.0.0.1:8000/v1/chat/completions
   ```

```bash
curl http://127.0.0.1:8000/v1/chat/completions -H "Authorization: Bearer tenant-a-key" -H "Content-Type: application/json" -d "{\"messages\":[{\"role\":\"user\",\"content\":\"Hello\"}],\"max_tokens\":200}"
```

Tenant keys (mock table in `gateway.py`): `tenant-a-key`, `tenant-b-key`.

| Setting | Default |
|---|---|
| `PRIMARY_URL` / `PRIMARY_MODEL` | Groq, `openai/gpt-oss-120b` |
| `SECONDARY_URL` / `SECONDARY_MODEL` | Groq, `qwen/qwen3.8-27b` |
| `PRIMARY_API_KEY` / `SECONDARY_API_KEY` | set in `.env`; each provider gets only its own key |
| `DB_PATH` | `rate_limits.db` |

The router sets `model`; a client's `model` is overridden.

## Requirement 1: token-aware sliding-window rate limiter

- **Storage:** one row per admitted request in `token_usage(id, tenant, ts, tokens)`, indexed on `(tenant, ts)`. Usage is the sum of the tenant's rows from the last 60 s. This is an exact sliding window, not fixed buckets.
- **Token tracking:**
  1. Reserve the worst case before the call: prompt estimate (~4 chars/token) + `max_tokens` (default 1024).
  2. After the reply, settle the row to `usage.total_tokens`.
  3. If the request fails upstream, only the prompt estimate stays charged.
- **Eviction and check:** one `BEGIN IMMEDIATE` transaction deletes the tenant's expired rows, sums the rest, and inserts the new row only if it fits.
- **Rejection:**
  - Over the limit: `429` with `Retry-After`, the seconds until enough old rows expire.
  - A single request larger than the limit: `400`.
- **Concurrency:** check and insert are atomic, so parallel requests can't share the same free capacity.
  - In-process, a lock guards the shared connection.
  - DB work runs in `asyncio.to_thread`, so it doesn't block the event loop.
  - Across processes, SQLite's write lock covers it.

## Requirement 2: failover on 429 or 3000 ms timeout

- **Total deadline:** `asyncio.timeout(3.0)` covers connect through the last byte. httpx timeouts are per read, so they're turned off.
- **No race:** on timeout the primary call is cancelled and its connection closed. A late reply can't be used, and only one provider's reply is returned or charged.
- **Triggers:** only primary `429` or timeout fail over. Other primary errors return `502`.
- **Secondary:** a 30 s deadline.

## Requirement 3: standardized, sanitized errors

```json
{"error": {"type": "upstream_error", "message": "The model provider could not complete the request.", "request_id": "3f9c..."}}
```

| Case | Status | `type` |
|---|---|---|
| Missing or unknown tenant key | 401 | `unauthorized` |
| Invalid body, `stream: true`, bad `max_tokens`, request over the limit | 400 | `invalid_request` |
| Unknown path or method | 404 / 405 | `invalid_request` |
| Tenant over 50,000 tokens/minute | 429 + `Retry-After` | `rate_limit_exceeded` |
| Upstream failure (non-200, unreachable, bad JSON, both providers 429) | 502 | `upstream_error` |
| Secondary timeout | 504 | `upstream_timeout` |
| Unexpected gateway exception | 500 | `internal_error` |

Upstream status codes, bodies and stack traces are only logged, with the `request_id`; they're never returned to the client. An upstream 429 is not passed through: a client only gets `429` for its own limit.

## Logging

To stderr:
- the provider and token count for each request
- rate-limit rejections
- failovers
- upstream error details
- tracebacks of unexpected exceptions

## Limitations

- **Token estimate:** the pre-check estimates ~4 chars/token rather than using the model's tokenizer; settling to real usage corrects it.
- **No streaming:** `stream: true` is rejected.
