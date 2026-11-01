# LLM Gateway: Streaming PII Guardrail

An LLM gateway proxy endpoint written in Python with Starlette. It routes chat requests to the Groq API, streams the response back to the client, and redacts emails, SSNs and credit card numbers in real time, replacing them with `[REDACTED]`.

```
client ──POST /v1/chat/completions──▶ gateway.py (:8000) ──▶ Groq API
       ◀──────── redacted SSE stream ───────────────────── streamed response
```

## Project structure

```
gateway.py      Proxy endpoint: forwards the request, reads the SSE stream, redacts each chunk, streams it back
redactor.py     StreamRedactor: PII redaction over a text stream that arrives in pieces
pyproject.toml  Dependencies: starlette, uvicorn, httpx, regex
uv.lock         Exact dependency versions
.env            GROQ_API_KEY (not committed: listed in .gitignore)
```

## Run

Requires Python 3.14+, [uv](https://docs.astral.sh/uv/) and a Groq API key.

1. Put your key in `.env` (the file is listed in `.gitignore`, so it isn't committed):

   ```
   GROQ_API_KEY=gsk_...
   ```

2. Start the gateway. uv loads `.env` into the environment:

   ```bash
   uv sync
   uv run --env-file .env python gateway.py      # http://127.0.0.1:8000/v1/chat/completions
   ```

The endpoint accepts the OpenAI-style chat request that Groq uses. An OpenAI-compatible client can point its base URL at `http://127.0.0.1:8000/v1`.

```bash
curl -N http://127.0.0.1:8000/v1/chat/completions -H "Content-Type: application/json" -d "{\"model\":\"openai/gpt-oss-20b\",\"messages\":[{\"role\":\"user\",\"content\":\"Write a sample customer record with an email and SSN.\"}]}"
```

| Setting | Default | Purpose |
|---|---|---|
| `GROQ_API_KEY` | (none; set in `.env`) | Sent to the provider as `Authorization: Bearer ...` |
| `PROVIDER_URL` | `https://api.groq.com/openai/v1/chat/completions` | Where requests are routed |

## Requirement 1: route requests and stream the response back

- The client's JSON body is forwarded to the provider with `"stream": true` forced, so the response is always streamed.
- The provider's Server-Sent Events are read line by line (`httpx` `aiter_lines`). Each line is sent on to the client as soon as it's processed, through Starlette's `StreamingResponse`.
- The SSE format is kept: `data: {chunk}` lines, blank separator lines, comment lines, and the final `data: [DONE]`.

## Requirement 2: detect and redact PII across stream deltas

Each chunk's `choices[].delta.content` is redacted, and so is `delta.reasoning`, which Groq's reasoning models (gpt-oss) stream.

| Pattern | Regex |
|---|---|
| Email | `[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}` |
| SSN | `\b\d{3}-\d{2}-\d{4}\b` |
| Credit card | `\b(?:\d[ -]?){12,18}\d\b` (13–19 digits, optionally separated by spaces or dashes) |

**Patterns split across chunks.** A model streams a few characters at a time, so a pattern often arrives in pieces (`"john.d"`, `"oe@exa"`, `"mple.com"`). `StreamRedactor` keeps one small piece of state per stream, `pending`: the end of the text that could still turn into a match.

- When a chunk arrives, it's appended to `pending`, and complete matches are redacted.
- Text that can no longer be part of any match is sent at once.
- Only the suffix that could still become a match, or grow into a longer one, is held back.

**Finding that suffix.** This uses the `regex` package's partial matching, which reports when the end of the text is the start of a possible match.

- A plain partial search prefers a later *complete* match over an earlier *partial* one, which would release text too early.
- So the search pattern is PII followed by a character that never appears in text (U+FFFF). It can never fully match, and the search returns the earliest point that could still become a match.
- Example: `a@b.co.` is held, because it may become `a@b.co.uk`.

**End of stream.** When a choice's `finish_reason` arrives, its held text is redacted and released in that same chunk. If the provider sends no `finish_reason`, or the connection drops before `[DONE]`, held text is still released as one extra chunk, redacted.

Each choice (`n > 1`) and each text field has its own redactor, so streams never mix.

## Requirement 3: responsive, without buffering the full response

- **Low TTFT.** Text is sent as soon as it's known to be safe. Only the last word or number is held while it could still be PII, so latency is about one token. In testing, the first token reached the client in about 30 ms while the provider paused for 2 s.
- **Bounded memory.** The full response is never accumulated. Every quantifier in the patterns has a maximum, so the held-back text per stream is at most about 343 characters (64 for a run of letters). In testing, a 75 MB response streamed through without the gateway's memory growing.
- **Cheap per chunk.** Each chunk scans only the new text plus the short held-back suffix, not the whole response.
- **Client disconnect.** If the client goes away, the provider stream is closed right away.

## Errors

| Case | Response |
|---|---|
| Request body is not a JSON object | `400` `{"error": {...}}` |
| Provider unreachable | `502` `{"error": {"message": "LLM provider unavailable"}}` |
| Provider returns an error (bad key, unknown model, ...) | The provider's status code and body, passed through |

## Logging

The gateway logs to stderr: the number of redactions when each stream finishes, provider errors, and an unreachable provider.

## Limitations

- **Detection is regex-based.** Any 13–19 digit number is treated as a card number; there's no Luhn check. Emails and SSNs in unusual formats aren't caught.
- **Only known text fields are redacted.** A provider that streams generated text under another delta field name would pass it through unredacted.
