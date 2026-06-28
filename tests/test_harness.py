"""Harness 校验模块测试"""

from datetime import date
from decimal import Decimal

from src.harness.validators import validate_transactions, ValidationResult
from src.harness.reconciliation import reconcile_import, ReconciliationResult
from src.harness.tax_verify import verify_tax_computation


class TestInputValidation:
    """测试输入验证 (IV-001 ~ IV-007)"""

    def test_valid_transactions_pass(self):
        txns = [
            {
                "broker_code": "futu",
                "trade_date": "2025-03-15",
                "symbol": "AAPL",
                "action": "buy",
                "quantity": 100,
                "price": 150.0,
                "amount": 15000.0,
                "currency": "USD",
            },
        ]
        result = validate_transactions(txns)
        assert result.passed
        assert result.error_count == 0

    def test_invalid_date_too_early(self):
        txns = [{
            "broker_code": "futu",
            "trade_date": "2019-01-01",
            "symbol": "AAPL",
            "action": "buy",
            "quantity": 100,
            "price": 150.0,
            "amount": 15000.0,
            "currency": "USD",
        }]
        result = validate_transactions(txns)
        assert not result.passed
        assert any(i.rule_id == "IV-001" and i.severity == "ERROR" for i in result.issues)

    def test_invalid_date_future(self):
        txns = [{
            "broker_code": "futu",
            "trade_date": "2099-01-01",
            "symbol": "AAPL",
            "action": "buy",
            "quantity": 100,
            "price": 150.0,
            "amount": 15000.0,
            "currency": "USD",
        }]
        result = validate_transactions(txns)
        assert not result.passed

    def test_negative_quantity(self):
        txns = [{
            "broker_code": "futu",
            "trade_date": "2025-03-15",
            "symbol": "AAPL",
            "action": "buy",
            "quantity": -100,
            "price": 150.0,
            "amount": 15000.0,
            "currency": "USD",
        }]
        result = validate_transactions(txns)
        assert not result.passed
        assert any(i.rule_id == "IV-002" for i in result.issues)

    def test_negative_price(self):
        txns = [{
            "broker_code": "futu",
            "trade_date": "2025-03-15",
            "symbol": "AAPL",
            "action": "buy",
            "quantity": 100,
            "price": -50.0,
            "amount": 15000.0,
            "currency": "USD",
        }]
        result = validate_transactions(txns)
        assert not result.passed
        assert any(i.rule_id == "IV-003" for i in result.issues)

    def test_invalid_action(self):
        txns = [{
            "broker_code": "futu",
            "trade_date": "2025-03-15",
            "symbol": "AAPL",
            "action": "invalid_action",
            "quantity": 100,
            "price": 150.0,
            "amount": 15000.0,
            "currency": "USD",
        }]
        result = validate_transactions(txns)
        assert not result.passed
        assert any(i.rule_id == "IV-006" for i in result.issues)

    def test_amount_mismatch_warning(self):
        txns = [{
            "broker_code": "futu",
            "trade_date": "2025-03-15",
            "symbol": "AAPL",
            "action": "buy",
            "quantity": 100,
            "price": 150.0,
            "amount": 999999.0,  # 明显不对
            "currency": "USD",
        }]
        result = validate_transactions(txns)
        assert any(i.rule_id == "IV-004" and i.severity == "WARNING" for i in result.issues)

    def test_duplicate_detection(self):
        txn = {
            "broker_code": "futu",
            "trade_date": "2025-03-15",
            "symbol": "AAPL",
            "action": "buy",
            "quantity": 100,
            "price": 150.0,
            "amount": 15000.0,
            "currency": "USD",
        }
        txns = [txn, txn.copy(), txn.copy()]
        result = validate_transactions(txns)
        assert not result.passed
        assert any(i.rule_id == "RC-003" for i in result.issues)

    def test_empty_symbol(self):
        txns = [{
            "broker_code": "futu",
            "trade_date": "2025-03-15",
            "symbol": "",
            "action": "buy",
            "quantity": 100,
            "price": 150.0,
            "amount": 15000.0,
            "currency": "USD",
        }]
        result = validate_transactions(txns)
        assert not result.passed
        assert any(i.rule_id == "IV-005" for i in result.issues)

    def test_dividend_zero_price(self):
        txns = [{
            "broker_code": "futu",
            "trade_date": "2025-03-15",
            "symbol": "AAPL",
            "action": "dividend",
            "quantity": 100,
            "price": 0,
            "amount": 0,
            "currency": "USD",
        }]
        result = validate_transactions(txns)
        assert not result.passed
        assert any(i.rule_id == "IV-007" for i in result.issues)


class TestTaxVerification:
    """测试税务计算验证"""

    def test_fifo_sanity_check(self):
        """验证有买入后有卖出通过"""
        txns = [
            {
                "broker_code": "futu",
                "trade_date": "2025-01-10",
                "symbol": "AAPL",
                "action": "buy",
                "quantity": 100,
                "price": Decimal("150"),
                "amount": Decimal("15000"),
                "currency": "USD",
                "exchange_rate": Decimal("7.10"),
                "fee": Decimal("0"),
                "tax_withheld": Decimal("0"),
            },
            {
                "broker_code": "futu",
                "trade_date": "2025-03-15",
                "symbol": "AAPL",
                "action": "sell",
                "quantity": 50,
                "price": Decimal("180"),
                "amount": Decimal("9000"),
                "currency": "USD",
                "exchange_rate": Decimal("7.10"),
                "fee": Decimal("0"),
                "tax_withheld": Decimal("0"),
            },
        ]
        result = verify_tax_computation(transactions=txns, year=2025)
        assert not any(i.rule_id == "CV-002" for i in result.issues)

    def test_fifo_insufficient_shares(self):
        """验证卖出超过持仓被捕获"""
        txns = [
            {
                "broker_code": "futu",
                "trade_date": "2025-03-15",
                "symbol": "AAPL",
                "action": "sell",
                "quantity": 100,  # 没买过就卖
                "price": Decimal("180"),
                "amount": Decimal("9000"),
                "currency": "USD",
                "exchange_rate": Decimal("7.10"),
                "fee": Decimal("0"),
                "tax_withheld": Decimal("0"),
            },
        ]
        result = verify_tax_computation(transactions=txns, year=2025)
        assert any(i.rule_id == "CV-002" for i in result.issues)
