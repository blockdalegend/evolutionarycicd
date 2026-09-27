import pytest

from app.models import Order, OrderItem, PaymentMethod, PaymentMethodType
from app.services.payment_service import PaymentService, PaymentValidationError


@pytest.fixture
def service() -> PaymentService:
    return PaymentService()


@pytest.fixture
def order() -> Order:
    return Order(
        items=[
            OrderItem(name="Widget", unit_price=10.0, quantity=2),
            OrderItem(name="Gadget", unit_price=5.5, quantity=1),
        ]
    )


def test_calculate_total(service: PaymentService, order: Order) -> None:
    assert service.calculate_total(order) == 25.5


def test_apply_discount(service: PaymentService) -> None:
    assert service.apply_discount(100.0, 20) == 80.0


def test_apply_discount_rejects_out_of_range(service: PaymentService) -> None:
    with pytest.raises(PaymentValidationError):
        service.apply_discount(100.0, 150)


def test_validate_payment_approves_valid_card(service: PaymentService) -> None:
    method = PaymentMethod(type=PaymentMethodType.CREDIT_CARD, token="tok_123")
    result = service.validate_payment(method, 50.0)
    assert result.approved is True


def test_validate_payment_rejects_expired_method(service: PaymentService) -> None:
    method = PaymentMethod(type=PaymentMethodType.CREDIT_CARD, token="tok_123", expired=True)
    result = service.validate_payment(method, 50.0)
    assert result.approved is False
    assert result.reason == "payment method expired"


def test_validate_payment_rejects_missing_token(service: PaymentService) -> None:
    method = PaymentMethod(type=PaymentMethodType.CREDIT_CARD, token=" ", expired=False)
    result = service.validate_payment(method, 50.0)
    assert result.approved is False
    assert result.reason == "missing payment token"


def test_validate_payment_rejects_negative_amount(service: PaymentService) -> None:
    method = PaymentMethod(type=PaymentMethodType.CREDIT_CARD, token="tok_123")
    result = service.validate_payment(method, -5.0)
    assert result.approved is False
    assert result.reason == "amount cannot be negative"


def test_validate_payment_approves_zero_amount(service: PaymentService) -> None:
    method = PaymentMethod(type=PaymentMethodType.CREDIT_CARD, token="tok_123")
    result = service.validate_payment(method, 0.0)
    assert result.approved is True
    assert result.reason == "zero-amount transaction"


def test_validate_payment_rejects_gift_card_above_limit(service: PaymentService) -> None:
    method = PaymentMethod(type=PaymentMethodType.GIFT_CARD, token="gift_123")
    result = service.validate_payment(method, 500.01)
    assert result.approved is False
    assert result.reason == "gift card amount exceeds limit of 500.0"


def test_calculate_refund(service: PaymentService) -> None:
    assert service.calculate_refund(100.0, 50) == 50.0


def test_calculate_refund_rejects_negative_amount(service: PaymentService) -> None:
    with pytest.raises(PaymentValidationError):
        service.calculate_refund(-10.0)
