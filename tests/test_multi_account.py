"""Multi-account verification rules tests (MA-001 ~ MA-007)"""
import sqlite3
import tempfile
from pathlib import Path

import pytest

from src.harness.multi_account_verify import verify_multi_account


def _create_test_db() -> Path:
    """创建包含基础表的测试数据库"""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = Path(tmp.name)
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
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
            withholding_country TEXT
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
            foreign_credit_cny REAL DEFAULT 0,
            tax_payable REAL
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
        CREATE TABLE rsu_vests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            broker_code TEXT,
            symbol TEXT,
            vest_date TEXT,
            quantity REAL,
            vested_quantity REAL,
            fmv_per_share REAL
        );
    """)
    conn.close()
    return db_path


class TestMA001FifoCrossBroker:
    """MA-001: 同一股票跨账户 FIFO 不混淆"""

    def test_no_cross_broker_symbol(self):
        db_path = _create_test_db()
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            INSERT INTO tax_lots (symbol, quantity, cost_per_share, acquisition_date, acquisition_type, broker_code, remaining)
            VALUES ('AAPL', 100, 50, '2025-01-01', 'buy', 'futu', 100)
        """)
        conn.commit()
        conn.close()

        result = verify_multi_account(db_path, 2025)
        ma001 = [i for i in result.issues if i.rule_id == "MA-001"]
        assert len(ma001) == 0

    def test_cross_broker_symbol_detected(self):
        db_path = _create_test_db()
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            INSERT INTO tax_lots (symbol, quantity, cost_per_share, acquisition_date, acquisition_type, broker_code, remaining)
            VALUES ('AAPL', 100, 50, '2025-01-01', 'buy', 'futu', 50),
                   ('AAPL', 100, 60, '2025-02-01', 'buy', 'lb', 100)
        """)
        conn.commit()
        conn.close()

        result = verify_multi_account(db_path, 2025)
        # MA-001 不产生 issue，只记录 detected symbols
        assert result.verified_items.get("multi_broker_symbols", 0) >= 1
        assert result.verified_items.get("cross_broker_tracking") == "OK"


class TestMA002NoDuplicateDividends:
    """MA-002: 分红不重复计入"""

    def test_no_duplicate(self):
        db_path = _create_test_db()
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            INSERT INTO dividends (broker_code, symbol, payment_date, gross_amount, withholding_tax, withholding_rate, withholding_country)
            VALUES ('futu', 'AAPL', '2025-03-01', 1000, 100, 0.10, 'US')
        """)
        conn.commit()
        conn.close()

        result = verify_multi_account(db_path, 2025)
        ma002 = [i for i in result.issues if i.rule_id == "MA-002"]
        assert len(ma002) == 0


class TestMA003RsuCostConsistency:
    """MA-003: RSU 成本基础在转仓保持一致"""

    def test_no_rsu_no_issue(self):
        db_path = _create_test_db()
        result = verify_multi_account(db_path, 2025)
        ma003 = [i for i in result.issues if i.rule_id == "MA-003"]
        assert len(ma003) == 0


class TestMA004NoDuplicateWithholding:
    """MA-004: 境外预扣税不重复抵免"""

    def test_no_duplicate_withholding(self):
        db_path = _create_test_db()
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            INSERT INTO tax_items (symbol, income_type, tax_year, date, gross_income, taxable_income, tax_amount, foreign_tax_credit, tax_payable)
            VALUES ('AAPL', 'dividend', 2025, '2025-03-01', 1000, 1000, 200, 100, 100)
        """)
        conn.commit()
        conn.close()

        result = verify_multi_account(db_path, 2025)
        ma004 = [i for i in result.issues if i.rule_id == "MA-004"]
        assert len(ma004) == 0


class TestMA005TransferNoTaxEvent:
    """MA-005: 账户间转仓不产生应税事件"""

    def test_no_transfer_no_issue(self):
        db_path = _create_test_db()
        result = verify_multi_account(db_path, 2025)
        ma005 = [i for i in result.issues if i.rule_id == "MA-005"]
        assert len(ma005) == 0


class TestMA006OptionWriteNoDoubleTax:
    """MA-006: 期权写仓收益不双重计税"""

    def test_no_option_write_no_issue(self):
        db_path = _create_test_db()
        result = verify_multi_account(db_path, 2025)
        ma006 = [i for i in result.issues if i.rule_id == "MA-006"]
        assert len(ma006) == 0


class TestMA007ExchangeRateConsistency:
    """MA-007: 汇率一致性"""

    def test_consistent_exchange_rate(self):
        db_path = _create_test_db()
        conn = sqlite3.connect(str(db_path))
        # 同一天同一币种使用相同汇率
        conn.execute("""
            INSERT INTO transactions (id, broker_code, trade_date, symbol, action, quantity, price, amount, currency, exchange_rate)
            VALUES ('t1', 'futu', '2025-03-01', 'AAPL', 'sell', 100, 80, 8000, 'USD', 7.1),
                   ('t2', 'lb', '2025-03-01', 'MSFT', 'sell', 50, 400, 20000, 'USD', 7.1)
        """)
        conn.commit()
        conn.close()

        result = verify_multi_account(db_path, 2025)
        ma007 = [i for i in result.issues if i.rule_id == "MA-007"]
        assert len(ma007) == 0

    def test_inconsistent_exchange_rate_flagged(self):
        db_path = _create_test_db()
        conn = sqlite3.connect(str(db_path))
        # 同一天同一币种使用不同汇率
        conn.execute("""
            INSERT INTO transactions (id, broker_code, trade_date, symbol, action, quantity, price, amount, currency, exchange_rate)
            VALUES ('t1', 'futu', '2025-03-01', 'AAPL', 'sell', 100, 80, 8000, 'USD', 7.1),
                   ('t2', 'lb', '2025-03-01', 'MSFT', 'sell', 50, 400, 20000, 'USD', 7.2)
        """)
        conn.commit()
        conn.close()

        result = verify_multi_account(db_path, 2025)
        ma007 = [i for i in result.issues if i.rule_id == "MA-007"]
        assert len(ma007) >= 1
