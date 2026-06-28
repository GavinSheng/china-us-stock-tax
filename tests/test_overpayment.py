"""Overpayment detection rules tests (OP-001 ~ OP-006)"""
import sqlite3
import tempfile
from decimal import Decimal
from pathlib import Path
from datetime import date

import pytest

from src.harness.overpayment_detect import detect_overpayment


def _create_test_db() -> Path:
    """创建包含基础表的测试数据库"""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = Path(tmp.name)
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE transactions (
            id TEXT PRIMARY KEY,
            broker_code TEXT,
            trade_date TEXT,
            symbol TEXT,
            action TEXT,
            quantity REAL,
            price REAL,
            amount REAL,
            currency TEXT,
            exchange_rate REAL,
            commission REAL DEFAULT 0,
            platform_fee REAL DEFAULT 0,
            sec_fee REAL DEFAULT 0,
            taf_fee REAL DEFAULT 0,
            delivery_fee REAL DEFAULT 0,
            other_fees REAL DEFAULT 0,
            tax_withheld REAL DEFAULT 0
        );
        CREATE TABLE dividends (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            broker_code TEXT,
            symbol TEXT,
            payment_date TEXT,
            gross_amount REAL,
            withholding_tax REAL DEFAULT 0,
            withholding_rate REAL DEFAULT 0,
            withholding_country TEXT,
            collection_fee REAL DEFAULT 0,
            adr_fee REAL DEFAULT 0,
            other_deductions REAL DEFAULT 0
        );
        CREATE TABLE tax_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            income_type TEXT,
            tax_year INTEGER,
            date TEXT,
            gross_income REAL,
            taxable_income REAL,
            tax_amount REAL,
            foreign_tax_credit REAL,
            tax_payable REAL,
            deductible REAL DEFAULT 0
        );
        CREATE TABLE tax_lots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            quantity REAL,
            cost_per_share REAL,
            acquisition_date TEXT,
            acquisition_type TEXT,
            broker_code TEXT,
            remaining REAL
        );
        CREATE TABLE lot_consumptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sell_txn_id TEXT,
            tax_lot_id INTEGER,
            consumption_type TEXT,
            consumed_qty REAL,
            cost_per_share REAL,
            cost_basis REAL,
            realized_gain REAL,
            sell_price REAL,
            proceeds REAL
        );
        CREATE TABLE tax_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tax_year INTEGER,
            income_type TEXT,
            computation_method TEXT,
            total_taxable_cny REAL,
            total_tax_cny REAL
        );
    """)
    conn.close()
    return db_path


class TestOP001DividendFees:
    """OP-001: 分红关联费用未从应纳税所得额扣除"""

    def test_no_fees_no_issue(self):
        db_path = _create_test_db()
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            INSERT INTO dividends (symbol, payment_date, gross_amount, withholding_tax, withholding_rate, withholding_country)
            VALUES ('AAPL', '2025-03-01', 1000, 100, 0.10, 'US')
        """)
        conn.execute("""
            INSERT INTO tax_items (symbol, income_type, tax_year, date, gross_income, taxable_income, tax_amount, foreign_tax_credit, tax_payable, deductible)
            VALUES ('AAPL', 'dividend', 2025, '2025-03-01', 1000, 1000, 200, 100, 100, 0)
        """)
        conn.commit()
        conn.close()

        result = detect_overpayment(db_path, 2025)
        op001 = [i for i in result.issues if i.rule_id == "OP-001"]
        assert len(op001) == 0

    def test_unclaimed_fees_warning(self):
        db_path = _create_test_db()
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            INSERT INTO dividends (symbol, payment_date, gross_amount, withholding_tax, withholding_rate, withholding_country, adr_fee)
            VALUES ('AAPL', '2025-03-01', 1000, 100, 0.10, 'US', 5)
        """)
        conn.execute("""
            INSERT INTO tax_items (symbol, income_type, tax_year, date, gross_income, taxable_income, tax_amount, foreign_tax_credit, tax_payable, deductible)
            VALUES ('AAPL', 'dividend', 2025, '2025-03-01', 1000, 1000, 200, 100, 100, 0)
        """)
        conn.commit()
        conn.close()

        result = detect_overpayment(db_path, 2025)
        op001 = [i for i in result.issues if i.rule_id == "OP-001"]
        assert len(op001) == 1


class TestOP002WithholdingCredit:
    """OP-002: 美股分红预扣税 10% 未被正确抵免"""

    def test_withholding_correctly_credited(self):
        db_path = _create_test_db()
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            INSERT INTO dividends (symbol, payment_date, gross_amount, withholding_tax, withholding_rate, withholding_country)
            VALUES ('AAPL', '2025-03-01', 1000, 100, 0.10, 'US')
        """)
        conn.commit()
        conn.close()

        result = detect_overpayment(db_path, 2025)
        op002 = [i for i in result.issues if i.rule_id == "OP-002"]
        assert len(op002) == 0

    def test_low_withholding_rate_flagged(self):
        db_path = _create_test_db()
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            INSERT INTO dividends (symbol, payment_date, gross_amount, withholding_tax, withholding_rate, withholding_country)
            VALUES ('AAPL', '2025-03-01', 1000, 0, 0.05, 'US')
        """)
        conn.commit()
        conn.close()

        result = detect_overpayment(db_path, 2025)
        op002 = [i for i in result.issues if i.rule_id == "OP-002"]
        assert len(op002) >= 1


class TestOP003DefaultExchangeRate:
    """OP-003: 使用默认汇率而非实际汇率"""

    def test_no_missing_rates(self):
        db_path = _create_test_db()
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            INSERT INTO transactions (id, broker_code, trade_date, symbol, action, quantity, price, amount, currency, exchange_rate)
            VALUES ('t1', 'futu', '2025-03-01', 'AAPL', 'sell', 100, 80, 8000, 'USD', 7.1)
        """)
        conn.commit()
        conn.close()

        result = detect_overpayment(db_path, 2025)
        op003 = [i for i in result.issues if i.rule_id == "OP-003"]
        assert len(op003) == 0

    def test_missing_rate_flagged(self):
        db_path = _create_test_db()
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            INSERT INTO transactions (id, broker_code, trade_date, symbol, action, quantity, price, amount, currency, exchange_rate)
            VALUES ('t1', 'futu', '2025-03-01', 'AAPL', 'sell', 100, 80, 8000, 'USD', 0)
        """)
        conn.commit()
        conn.close()

        result = detect_overpayment(db_path, 2025)
        op003 = [i for i in result.issues if i.rule_id == "OP-003"]
        assert len(op003) >= 1


class TestOP004LossOffset:
    """OP-004: 亏损未充分抵扣盈利"""

    def test_annual_net_method_selected(self):
        db_path = _create_test_db()
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            INSERT INTO tax_summaries (tax_year, income_type, computation_method, total_taxable_cny, total_tax_cny)
            VALUES (2025, 'capital_gain', 'annual_net', 7100, 1420)
        """)
        conn.execute("""
            INSERT INTO lot_consumptions (sell_txn_id, consumption_type, consumed_qty, cost_per_share, cost_basis, realized_gain)
            VALUES ('t1', 'sell', 100, 50, 5000, -2000)
        """)
        conn.execute("""
            INSERT INTO transactions (id, trade_date) VALUES ('t1', '2025-03-01')
        """)
        conn.commit()
        conn.close()

        result = detect_overpayment(db_path, 2025)
        op004 = [i for i in result.issues if i.rule_id == "OP-004"]
        assert len(op004) == 0


class TestOP005OptionWriteDoubleTax:
    """OP-005: 期权写仓权利金被双重计税"""

    def test_no_double_tax(self):
        db_path = _create_test_db()
        result = detect_overpayment(db_path, 2025)
        op005 = [i for i in result.issues if i.rule_id == "OP-005"]
        assert len(op005) == 0


class TestOP006ZeroCostBasis:
    """OP-006: 跨年持仓成本基础为 $0"""

    def test_no_zero_cost(self):
        db_path = _create_test_db()
        result = detect_overpayment(db_path, 2025)
        op006 = [i for i in result.issues if i.rule_id == "OP-006"]
        assert len(op006) == 0

    def test_zero_cost_basis_flagged(self):
        db_path = _create_test_db()
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            INSERT INTO tax_lots (symbol, quantity, cost_per_share, acquisition_date, acquisition_type, broker_code, remaining)
            VALUES ('AAPL', 100, 0, '2024-01-01', 'carryforward', 'futu', 50)
        """)
        conn.execute("""
            INSERT INTO transactions (id, broker_code, trade_date, symbol, action, quantity, price, amount, currency, exchange_rate)
            VALUES ('t1', 'futu', '2025-03-01', 'AAPL', 'sell', 50, 80, 4000, 'USD', 7.1)
        """)
        conn.execute("""
            INSERT INTO lot_consumptions (sell_txn_id, tax_lot_id, consumption_type, consumed_qty, cost_per_share, cost_basis, realized_gain, sell_price, proceeds)
            VALUES ('t1', 1, 'sell', 50, 0, 0, 4000, 80, 4000)
        """)
        conn.commit()
        conn.close()

        result = detect_overpayment(db_path, 2025)
        op006 = [i for i in result.issues if i.rule_id == "OP-006"]
        assert len(op006) >= 1
