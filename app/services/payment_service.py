"""Payment domain logic used throughout the conference demo.

The service is intentionally small: it exists to give the agents something
real to reason about (branch coverage, validation, discounts, refunds)
without requiring a full e-commerce backend.
"""

from __future__ import annotations

import logging

from app.models import Order, PaymentMethod, PaymentResult

logger = logging.getLogger(__name__)

#: Gift cards are capped at this amount per transaction for the demo.
GIFT_CARD_MAX_AMOUNT = 500.0


class PaymentValidationError(ValueError):
    """Raised when a payment cannot be validated."""


class PaymentService:
    """Business logic for orders, discounts, payments, and refunds."""

    def calculate_total(self, order: Order) -> float:
        """Return the sum of ``unit_price * quantity`` for every order item.

        Discounts are intentionally *not* applied here; call
        :meth:`apply_discount` explicitly so each step stays testable in
        isolation.
        """
        subtotal = sum(item.unit_price * item.quantity for item in order.items)
        return round(subtotal, 2)

    def apply_discount(self, total: float, discount_percent: float) -> float:
        """Apply a percentage discount to ``total``.

        Raises:
            PaymentValidationError: if ``discount_percent`` is outside 0-100.
        """
        if discount_percent < 0 or discount_percent > 100:
            raise PaymentValidationError("discount_percent must be between 0 and 100")
        discounted = total * (1 - discount_percent / 100)
        return round(discounted, 2)

    def validate_payment(self, method: PaymentMethod, amount: float) -> PaymentResult:
        """Validate a payment method against a charge amount.

        Handles a handful of realistic edge cases:

        * expired payment methods are always rejected
        * a missing/blank token is rejected
        * negative amounts are rejected
        * gift cards above :data:`GIFT_CARD_MAX_AMOUNT` are rejected
        * a zero-dollar amount is treated as a valid "no-op" payment

        NOTE(demo): the zero-dollar branch below is intentionally left with
        weaker test coverage so the Test Quality Agent has something
        meaningful to flag during the live demo.
        """
        if method.expired:
            return PaymentResult(approved=False, reason="payment method expired")
        if not method.token or not method.token.strip():
            return PaymentResult(approved=False, reason="missing payment token")
        if amount < 0:
            return PaymentResult(approved=False, reason="amount cannot be negative")
        if amount == 0:
            # Under-tested edge case: zero-dollar transactions (e.g. fully
            # discounted orders) are approved without contacting a processor.
            return PaymentResult(approved=True, reason="zero-amount transaction")
        if method.type.value == "gift_card" and amount > GIFT_CARD_MAX_AMOUNT:
            return PaymentResult(
                approved=False,
                reason=f"gift card amount exceeds limit of {GIFT_CARD_MAX_AMOUNT}",
            )
        return PaymentResult(approved=True)

    def calculate_refund(self, original_amount: float, refund_percent: float = 100.0) -> float:
        """Return the refund amount for ``original_amount`` at ``refund_percent``.

        Raises:
            PaymentValidationError: if ``original_amount`` is negative or
                ``refund_percent`` is outside 0-100.
        """
        if original_amount < 0:
            raise PaymentValidationError("original_amount cannot be negative")
        if refund_percent < 0 or refund_percent > 100:
            raise PaymentValidationError("refund_percent must be between 0 and 100")
        refund = original_amount * (refund_percent / 100)
        return round(refund, 2)
