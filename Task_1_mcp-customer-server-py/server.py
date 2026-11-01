"""Customer-support MCP server.

Exposes two tools over the stdio transport:
  - get_customer_record(customer_id)
  - trigger_refund(customer_id, amount, reason)

Run:  uv run python server.py

Project layout:
  server.py    -> MCP server setup, request handling, input validation (this file)
  schemas.py   -> Pydantic models that define the valid input for each tool
  tools.py     -> what each tool actually does
  database.py  -> in-memory sample customer data
"""

import logging
import sys

import anyio
from mcp import types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError
from pydantic import BaseModel, ValidationError

import tools
from schemas import GetCustomerRecordInput, TriggerRefundInput

# Logging
# ---------------------------------------------------------------------------
# stdout is reserved for JSON-RPC messages, so all logs go to stderr.
logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("customer-support")

# Tool definitions (returned to the client by tools/list)
# ---------------------------------------------------------------------------
TOOL_DEFINITIONS = [
    types.Tool(
        name="get_customer_record",
        description="Fetch a customer record by ID (format CUST-XXXXX).",
        input_schema=GetCustomerRecordInput.model_json_schema(),
    ),
    types.Tool(
        name="trigger_refund",
        description="Trigger a refund for a customer.",
        input_schema=TriggerRefundInput.model_json_schema(),
    ),
]

# Request handlers
# ---------------------------------------------------------------------------
async def list_tools(ctx: ServerRequestContext, params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
    """Handle tools/list: tell the client which tools exist and what input they accept."""

    return types.ListToolsResult(tools=TOOL_DEFINITIONS)


async def call_tool(ctx: ServerRequestContext, params: types.CallToolRequestParams) -> types.CallToolResult:
    """Handle tools/call: validate the arguments, then run the requested tool."""

    if params.name == "get_customer_record":
        args = validate(GetCustomerRecordInput, params)
        return tools.get_customer_record(args)

    if params.name == "trigger_refund":
        args = validate(TriggerRefundInput, params)
        return tools.trigger_refund(args)

    # Handling unknown tool calls
    raise MCPError(types.INVALID_PARAMS, f"Unknown tool: {params.name}")


def validate[M: BaseModel](model: type[M], params: types.CallToolRequestParams) -> M:
    """Validate the tool arguments against a Pydantic model.

    On failure, raise MCPError with code INVALID_PARAMS (-32602). 
    The SDK turns it into a standard JSON-RPC error response. 
    `data` parameter lists every bad field.
    """
    try:
        return model.model_validate(params.arguments or {})
    except ValidationError as e:
        errors = [{"field": ".".join(str(part) for part in err["loc"]), "message": err["msg"]} for err in e.errors()]
        log.warning("Rejected %s: %s", params.name, errors)
        raise MCPError(types.INVALID_PARAMS, f"Invalid arguments for {params.name}", data=errors) from None

# Server startup
# ---------------------------------------------------------------------------

server = Server("customer-support", version="1.0.0", on_list_tools=list_tools, on_call_tool=call_tool)

async def main() -> None:
    """Start the server"""

    async with stdio_server() as (read_stream, write_stream):
        # From here on, only the SDK writes to stdout (JSON-RPC messages).
        # Redirect sys.stdout to stderr so any print() in our code goes to stderr
        # instead and can never corrupt the JSON-RPC stream.
        sys.stdout = sys.stderr
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    anyio.run(main)
