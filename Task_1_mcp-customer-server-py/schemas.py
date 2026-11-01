"""
Input schemas for the MCP tools.
"""

from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# Customer ID format: "CUST-XXXXX", e.g. CUST-00042.
CustomerId = Annotated[str, StringConstraints(pattern=r"^CUST-[0-9]{5}$")]


class StrictModel(BaseModel):
    """
    Base class for all tool inputs.
    no type coercion & unknown fields are rejected.
    """

    model_config = ConfigDict(strict=True, extra="forbid")


class GetCustomerRecordInput(StrictModel):
    """Arguments for the get_customer_record tool."""

    customer_id: CustomerId


class TriggerRefundInput(StrictModel):
    """Arguments for the trigger_refund tool."""

    customer_id: CustomerId

    amount: float = Field(gt=0, allow_inf_nan=False)

    reason: str = Field(min_length=10)
