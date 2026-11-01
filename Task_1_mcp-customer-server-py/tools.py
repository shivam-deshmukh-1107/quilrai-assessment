"""
MCP tool.
"""

import json
import logging

from mcp import types

from database import CUSTOMERS
from schemas import GetCustomerRecordInput, TriggerRefundInput

log = logging.getLogger("customer-support")

# Tools
# ---------------------------------------------------------------------------
def get_customer_record(records: GetCustomerRecordInput) -> types.CallToolResult:
    """Return the record of the given customer."""

    customer = CUSTOMERS.get(records.customer_id)
    if customer is None:
        return error_result(f"Customer {records.customer_id} not found")
    
    return success_result({"customer_id": records.customer_id, **customer})


def trigger_refund(records: TriggerRefundInput) -> types.CallToolResult:
    """Trigger a refund for the given customer and return a confirmation."""

    if records.customer_id not in CUSTOMERS:
        return error_result(f"Customer {records.customer_id} not found")

    refund = {
        "customer_id": records.customer_id,
        "amount": records.amount,
        "reason": records.reason,
        "status": "refund_triggered",
    }
    log.info("Refund triggered: %s", refund)
    return success_result(refund)
# ---------------------------------------------------------------------------

# Result helpers
# ---------------------------------------------------------------------------
def success_result(data: dict) -> types.CallToolResult:
    """Wrap data in a successful tool result, as JSON text and as structured content."""

    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(data))],
        structured_content=data,
    )


def error_result(message: str) -> types.CallToolResult:
    """Return a failed tool result, used when the input is valid but the tool can't
    complete the action (e.g. the customer does not exist).
    """
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=message)],
        is_error=True,
    )
# ---------------------------------------------------------------------------