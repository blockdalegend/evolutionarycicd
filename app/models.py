"""Domain models for the demo payment/order application.

These models are intentionally simple so that conference attendees can read
and understand the whole domain in a few minutes.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class PaymentMethodType(str, Enum):
    """Supported payment method types for the demo."""

    CREDIT_CARD = "credit_card"
    DEBIT_CARD = "debit_card"
    GIFT_CARD = "gift_card"


class PaymentMethod(BaseModel):
    """A customer payment method."""

    type: PaymentMethodType
    token: str = Field(min_length=1, description="Opaque payment processor token.")
    expired: bool = False


class OrderItem(BaseModel):
    """A single line item in an order."""

    name: str
    unit_price: float = Field(ge=0)
    quantity: int = Field(gt=0)


class Order(BaseModel):
    """An order composed of one or more line items."""

    items: list[OrderItem]
    discount_percent: float = Field(default=0, ge=0, le=100)


class PaymentResult(BaseModel):
    """Result of validating/processing a payment."""

    approved: bool
    reason: str | None = None
