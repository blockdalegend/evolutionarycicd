from __future__ import annotations

from tools.analysis.ast_extractor import extract_python_evidence


def test_extract_python_evidence_preserves_locations_and_test_details() -> None:
    source = '''
import pytest
from unittest.mock import patch


def validate_payment(amount, gateway):
    if amount == 0:
        return True
    if amount < 0:
        raise ValueError("amount must be positive")
    return gateway.charge(amount)


@pytest.mark.parametrize("amount", [0, 10])
def test_zero_and_positive_amounts(monkeypatch, amount):
    with patch("app.gateway.charge") as charge:
        assert validate_payment(amount, charge) is True
        charge.assert_called_once_with(amount)


def test_negative_amount_rejected():
    with pytest.raises(ValueError, match="positive"):
        validate_payment(-1, None)
'''

    evidence = extract_python_evidence("tests/test_payment.py", source)

    assert evidence["is_test_file"] is True
    assert [function["name"] for function in evidence["functions"]] == [
        "validate_payment",
        "test_zero_and_positive_amounts",
        "test_negative_amount_rejected",
    ]
    production = evidence["functions"][0]
    assert production["conditions"] == [
        {"expression": "amount == 0", "line": 7, "end_line": 7},
        {"expression": "amount < 0", "line": 9, "end_line": 9},
    ]
    parametrized_test = evidence["functions"][1]
    assert parametrized_test["fixtures"] == ["monkeypatch", "pytest.mark.parametrize"]
    assert parametrized_test["assertions"][0]["expression"] == (
        "validate_payment(amount, charge) is True"
    )
    assert parametrized_test["mocks"][0]["expression"].startswith("patch(")
    assert evidence["functions"][2]["exceptions"][0]["kind"] == "pytest_context"