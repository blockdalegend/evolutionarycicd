"""Minimal CLI entry point for the demo application.

This exists so the app is runnable and demonstrable end-to-end, not just a
library of functions. It is intentionally tiny.
"""

from __future__ import annotations

import logging

from app.models import Order, OrderItem, PaymentMethod, PaymentMethodType
from app.services.payment_service import PaymentService

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def run_demo_checkout() -> float:
    """Run a small end-to-end checkout and return the final charged amount."""
    order = Order(
        items=[
            OrderItem(name="Conference Ticket", unit_price=299.00, quantity=1),
            OrderItem(name="Workshop Add-on", unit_price=99.00, quantity=1),
        ],
        discount_percent=10,
    )
    service = PaymentService()
    total = service.calculate_total(order)
    charged = service.apply_discount(total, order.discount_percent)

    method = PaymentMethod(
        type=PaymentMethodType.CREDIT_CARD,
        token="tok_demo_123",  # nosec B106 - demo token, not a secret
    )
    result = service.validate_payment(method, charged)
    logger.info("Charged amount: %.2f | approved=%s", charged, result.approved)
    return charged


if __name__ == "__main__":
    run_demo_checkout()
