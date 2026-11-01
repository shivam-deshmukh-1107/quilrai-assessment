# Customer Support MCP Server

A runnable MCP server written in Python. It uses the official `mcp` SDK and validates every tool input with Pydantic. It runs over the stdio transport.

## Tools

| Tool | Input | Rules |
|---|---|---|
| `get_customer_record` | `customer_id` | String in the format `CUST-XXXXX` (`CUST-` followed by 5 digits) |
| `trigger_refund` | `customer_id`, `amount`, `reason` | `customer_id` as above; `amount` is a positive float; `reason` is a string of at least 10 characters |

## Project structure

```
server.py       Server setup, stdio transport, input validation and JSON-RPC errors
schemas.py      Pydantic models: the input rules for each tool
tools.py        What each tool does
database.py     In-memory synthetic customer records (15 customers)
pyproject.toml  Dependencies: mcp, pydantic
uv.lock         Exact dependency versions
```

## Run

Requires Python 3.14+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run python server.py
```

The server reads JSON-RPC requests on stdin and writes replies on stdout.

To connect an MCP client, add it to the client's config:

```json
{
  "mcpServers": {
    "customer-support": {
      "command": "uv",
      "args": ["--directory", "<absolute path to this folder>", "run", "python", "server.py"]
    }
  }
}
```

## Requirement 1: strict input validation

Every tool input is validated against a Pydantic model in `schemas.py` before the tool runs.

- **`customer_id`:** must match `^CUST-[0-9]{5}$`. `[0-9]` is used instead of `\d`, because `\d` would also accept non-ASCII digits such as `CUST-١٢٣٤٥`.
- **`amount`:** must be greater than 0. `NaN` and `Infinity` are rejected.
- **`reason`:** must be at least 10 characters long.
- **Strict mode (`strict=True`):** no type conversion. `"10"` or `true` is not accepted as an amount, and `12345` is not accepted as a customer ID.
- **`extra="forbid"`:** unknown fields are rejected.
- **Missing fields** are rejected.

The same models produce the JSON Schema that `tools/list` sends to clients, so the advertised rules always match the enforced ones.

## Requirement 2: standard JSON-RPC error codes

| Case | Response |
|---|---|
| Invalid arguments (wrong format, wrong type, missing or unknown field) | JSON-RPC error `-32602` (Invalid params). `error.data` lists every invalid field as `{"field", "message"}` |
| Unknown tool name | JSON-RPC error `-32602` |
| Unknown method | JSON-RPC error `-32601` (Method not found) |
| Valid input, but the customer doesn't exist (for example `CUST-99999`) | Normal tool result with `isError: true`. Under the MCP spec this is a tool execution error, not a protocol error |

Example of an invalid call and its response:

```json
{"jsonrpc": "2.0", "id": 5, "method": "tools/call",
 "params": {"name": "trigger_refund", "arguments": {"customer_id": "CUST-00001", "amount": -5, "reason": "Item arrived damaged"}}}
```

```json
{"jsonrpc": "2.0", "id": 5, "error": {"code": -32602, "message": "Invalid arguments for trigger_refund",
 "data": [{"field": "amount", "message": "Input should be greater than 0"}]}}
```

## Requirement 3: stdio transport and stdout isolation

- The server uses the SDK's `stdio_server()` transport.
- stdout carries only JSON-RPC messages.
- All logs are written to stderr through Python's `logging` module. That includes the record of each triggered refund and of each rejected input.
- While the server runs, `sys.stdout` is redirected to stderr. Any accidental `print()` therefore goes to stderr and can never corrupt the JSON-RPC stream.
