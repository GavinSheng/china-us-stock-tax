"""数据库访问层 — 数据仓储"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.database.connection import get_connection, _get_active_connection


# ============================================================
# 工具函数
# ============================================================

def _decimal_to_float(val) -> float | None:
    if val is None:
        return None
    if isinstance(val, Decimal):
        return float(val)
    return val


def _row_to_dict(row) -> dict:
    """sqlite3.Row → dict"""
    return dict(row) if row else {}


def _compute_file_hash(file_path: str | Path) -> str:
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def _get_conn(db_path):
    """获取数据库连接。优先使用 thread-local 共享连接（事务上下文内）。

    返回 (conn, owns_conn)：
    - 如果存在活跃共享连接，返回它且 owns_conn=False（调用方不应关闭）
    - 否则新建连接且 owns_conn=True（调用方必须在 finally 中关闭）
    """
    shared = _get_active_connection()
    if shared is not None:
        return shared, False
    return get_connection(db_path), True


# ============================================================
# Brokers 仓储
# ============================================================

class BrokerRepository:
    """券商信息 CRUD"""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = db_path

    def get_all(self) -> list[dict]:
        conn, owns = _get_conn(self.db_path)
        try:
            rows = conn.execute("SELECT * FROM brokers").fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            if owns:
                conn.close()

    def get_by_code(self, code: str) -> dict | None:
        conn, owns = _get_conn(self.db_path)
        try:
            row = conn.execute(
                "SELECT * FROM brokers WHERE code = ?", (code,)
            ).fetchone()
            return _row_to_dict(row)
        finally:
            if owns:
                conn.close()


# ============================================================
# Exchange Rates 仓储
# ============================================================

class ExchangeRateRepository:
    """汇率数据 CRUD"""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = db_path

    def upsert(self, date_str: str, from_currency: str, to_currency: str,
               rate: float, source: str = "user_provided"):
        conn, owns = _get_conn(self.db_path)
        try:
            conn.execute("""
                INSERT INTO exchange_rates (date, from_currency, to_currency, rate, source)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(date, from_currency, to_currency)
                DO UPDATE SET rate = excluded.rate, source = excluded.source
            """, (date_str, from_currency, to_currency, rate, source))
        finally:
            if owns:
                conn.close()

    def get_rate(self, date_str: str, from_currency: str, to_currency: str = "CNY") -> float | None:
        conn, owns = _get_conn(self.db_path)
        try:
            row = conn.execute("""
                SELECT rate FROM exchange_rates
                WHERE date = ? AND from_currency = ? AND to_currency = ?
            """, (date_str, from_currency, to_currency)).fetchone()
            return row["rate"] if row else None
        finally:
            if owns:
                conn.close()

    def get_rates_for_period(self, start_date: str, end_date: str,
                              from_currency: str, to_currency: str = "CNY") -> list[dict]:
        conn, owns = _get_conn(self.db_path)
        try:
            rows = conn.execute("""
                SELECT * FROM exchange_rates
                WHERE date BETWEEN ? AND ?
                  AND from_currency = ? AND to_currency = ?
                ORDER BY date
            """, (start_date, end_date, from_currency, to_currency)).fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            if owns:
                conn.close()


# ============================================================
# Statement Files 仓储
# ============================================================

class StatementFileRepository:
    """月结单文件记录"""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = db_path

    def insert(self, broker_code: str, file_path: str, statement_month: str,
               page_count: int = 0, has_password: bool = False,
               parser_version: str = "", notes: str = "") -> int:
        conn, owns = _get_conn(self.db_path)
        try:
            path = Path(file_path)
            file_hash = _compute_file_hash(path) if path.exists() else None
            cursor = conn.execute("""
                INSERT INTO statement_files
                    (broker_code, file_path, file_hash, statement_month,
                     page_count, has_password, parser_version, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (broker_code, str(file_path), file_hash, statement_month,
                  page_count, int(has_password), parser_version, notes))
            conn.commit()
            return cursor.lastrowid
        finally:
            if owns:
                conn.close()

    def update_status(self, file_id: int, status: str, error_message: str = ""):
        conn, owns = _get_conn(self.db_path)
        try:
            conn.execute("""
                UPDATE statement_files SET status = ?, error_message = ?
                WHERE id = ?
            """, (status, error_message, file_id))
        finally:
            if owns:
                conn.close()

    def get_by_hash(self, file_hash: str) -> dict | None:
        conn, owns = _get_conn(self.db_path)
        try:
            row = conn.execute("""
                SELECT * FROM statement_files WHERE file_hash = ?
            """, (file_hash,)).fetchone()
            return _row_to_dict(row)
        finally:
            if owns:
                conn.close()

    def get_by_month(self, broker_code: str, statement_month: str) -> dict | None:
        conn, owns = _get_conn(self.db_path)
        try:
            row = conn.execute("""
                SELECT * FROM statement_files
                WHERE broker_code = ? AND statement_month = ?
            """, (broker_code, statement_month)).fetchone()
            return _row_to_dict(row)
        finally:
            if owns:
                conn.close()

    def list_all(self, broker_code: str | None = None) -> list[dict]:
        conn, owns = _get_conn(self.db_path)
        try:
            if broker_code:
                rows = conn.execute(
                    "SELECT * FROM statement_files WHERE broker_code = ? ORDER BY statement_month",
                    (broker_code,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM statement_files ORDER BY broker_code, statement_month"
                ).fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            if owns:
                conn.close()


# ============================================================
# Transactions 仓储
# ============================================================

class TransactionRepository:
    """交易记录 CRUD"""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = db_path

    def insert(self, broker_code: str, trade_date: str, symbol: str,
               action: str, quantity: int | None = None,
               price: float | None = None, amount: float | None = None,
               settlement_date: str | None = None,
               reference_no: str | None = None,
               company_name: str | None = None,
               exchange: str | None = None,
               commission: float = 0, platform_fee: float = 0,
               sec_fee: float = 0, taf_fee: float = 0,
               delivery_fee: float = 0, other_fees: float = 0,
               fee_breakdown: dict | None = None,
               tax_withheld: float = 0,
               withholding_tax_type: str | None = None,
               currency: str = "USD", exchange_rate: float | None = None,
               statement_file_id: int | None = None,
               raw_data: str | None = None) -> int:

        fee_total = commission + platform_fee + sec_fee + taf_fee + delivery_fee + other_fees

        # 如果未提供汇率，自动查找
        if exchange_rate is None and currency:
            try:
                from src.calculator.exchange_rate import get_exchange_rate
                if trade_date:
                    from datetime import date as _dt
                    d = _dt.fromisoformat(str(trade_date))
                    exchange_rate = float(get_exchange_rate(d, currency))
            except Exception:
                pass

        amount_cny = round(amount * exchange_rate, 2) if exchange_rate and amount else None
        fee_total_cny = round(fee_total * exchange_rate, 2) if exchange_rate and fee_total else None
        tax_withheld_cny = round(tax_withheld * exchange_rate, 2) if exchange_rate and tax_withheld else None

        conn, owns = _get_conn(self.db_path)
        try:
            cursor = conn.execute("""
                INSERT INTO transactions
                    (broker_code, trade_date, settlement_date, reference_no,
                     symbol, company_name, exchange, action, quantity, price, amount,
                     commission, platform_fee, sec_fee, taf_fee, delivery_fee, other_fees,
                     fee_breakdown, tax_withheld, withholding_tax_type,
                     currency, exchange_rate,
                     amount_cny, fee_total_cny, tax_withheld_cny,
                     statement_file_id, raw_data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                broker_code, trade_date, settlement_date, reference_no,
                symbol, company_name, exchange, action, quantity, _decimal_to_float(price), _decimal_to_float(amount),
                commission, platform_fee, sec_fee, taf_fee, delivery_fee, other_fees,
                json.dumps(fee_breakdown) if fee_breakdown else None,
                tax_withheld, withholding_tax_type,
                currency, _decimal_to_float(exchange_rate),
                amount_cny, fee_total_cny, tax_withheld_cny,
                statement_file_id, raw_data
            ))
            return cursor.lastrowid
        finally:
            if owns:
                conn.close()

    def bulk_insert(self, records: list[dict]) -> int:
        """批量插入，使用 executemany，包含 CNY 转换列。返回插入数量。"""
        from src.calculator.exchange_rate import get_exchange_rate
        from datetime import date as _dt

        conn, owns = _get_conn(self.db_path)
        try:
            enriched = []
            for r in records:
                exchange_rate = r.get("exchange_rate")
                currency = r.get("currency", "USD")
                amount = r.get("amount")
                trade_date = r.get("trade_date")
                commission = r.get("commission", 0)
                platform_fee = r.get("platform_fee", 0)
                sec_fee = r.get("sec_fee", 0)
                taf_fee = r.get("taf_fee", 0)
                delivery_fee = r.get("delivery_fee", 0)
                other_fees = r.get("other_fees", 0)
                fee_total = commission + platform_fee + sec_fee + taf_fee + delivery_fee + other_fees
                tax_withheld = r.get("tax_withheld", 0)

                if exchange_rate is None and currency and trade_date:
                    try:
                        d = _dt.fromisoformat(str(trade_date))
                        exchange_rate = float(get_exchange_rate(d, currency))
                    except Exception:
                        pass

                amount_cny = round(amount * exchange_rate, 2) if exchange_rate and amount else None
                fee_total_cny = round(fee_total * exchange_rate, 2) if exchange_rate and fee_total else None
                tax_withheld_cny = round(tax_withheld * exchange_rate, 2) if exchange_rate and tax_withheld else None

                enriched.append({
                    "broker_code": r["broker_code"],
                    "trade_date": trade_date,
                    "settlement_date": r.get("settlement_date"),
                    "reference_no": r.get("reference_no"),
                    "symbol": r["symbol"],
                    "company_name": r.get("company_name"),
                    "exchange": r.get("exchange"),
                    "action": r["action"],
                    "quantity": r.get("quantity"),
                    "price": _decimal_to_float(r.get("price")),
                    "amount": _decimal_to_float(r.get("amount")),
                    "commission": commission,
                    "platform_fee": platform_fee,
                    "sec_fee": sec_fee,
                    "taf_fee": taf_fee,
                    "delivery_fee": delivery_fee,
                    "other_fees": other_fees,
                    "fee_breakdown": json.dumps(r.get("fee_breakdown")) if r.get("fee_breakdown") else None,
                    "tax_withheld": tax_withheld,
                    "withholding_tax_type": r.get("withholding_tax_type"),
                    "currency": currency,
                    "exchange_rate": _decimal_to_float(exchange_rate),
                    "amount_cny": amount_cny,
                    "fee_total_cny": fee_total_cny,
                    "tax_withheld_cny": tax_withheld_cny,
                    "statement_file_id": r.get("statement_file_id"),
                    "raw_data": json.dumps(r.get("raw_data")) if isinstance(r.get("raw_data"), dict) else r.get("raw_data"),
                })

            count = 0
            if enriched:
                conn.executemany("""
                    INSERT INTO transactions
                        (broker_code, trade_date, settlement_date, reference_no,
                         symbol, company_name, exchange, action, quantity, price, amount,
                         commission, platform_fee, sec_fee, taf_fee, delivery_fee, other_fees,
                         fee_breakdown, tax_withheld, withholding_tax_type,
                         currency, exchange_rate,
                         amount_cny, fee_total_cny, tax_withheld_cny,
                         statement_file_id, raw_data)
                    VALUES
                        (:broker_code, :trade_date, :settlement_date, :reference_no,
                         :symbol, :company_name, :exchange, :action, :quantity, :price, :amount,
                         :commission, :platform_fee, :sec_fee, :taf_fee, :delivery_fee, :other_fees,
                         :fee_breakdown, :tax_withheld, :withholding_tax_type,
                         :currency, :exchange_rate,
                         :amount_cny, :fee_total_cny, :tax_withheld_cny,
                         :statement_file_id, :raw_data)
                """, enriched)
                count = len(enriched)
            return count
        finally:
            if owns:
                conn.close()

    def update_raw_data(self, txn_id: int, raw_data: str):
        """更新交易的 raw_data 字段（用于补充后期计算的数据，如期权行权的权利金）"""
        conn, owns = _get_conn(self.db_path)
        try:
            conn.execute(
                "UPDATE transactions SET raw_data = ? WHERE id = ?",
                (raw_data, txn_id)
            )
            conn.commit()
        finally:
            if owns:
                conn.close()

    def get_by_symbol(self, symbol: str, action: str | None = None) -> list[dict]:
        conn, owns = _get_conn(self.db_path)
        try:
            if action:
                rows = conn.execute(
                    "SELECT * FROM transactions WHERE symbol = ? AND action = ? ORDER BY trade_date, id",
                    (symbol, action)
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM transactions WHERE symbol = ? ORDER BY trade_date,
                    CASE action
                        WHEN 'buy' THEN 0 WHEN 'option_buy' THEN 0 WHEN 'rsu_vest' THEN 0
                        WHEN 'sell' THEN 1 WHEN 'option_sell' THEN 1 WHEN 'rsu_sell' THEN 1
                        WHEN 'dividend' THEN 2
                        WHEN 'option_expire' THEN 3
                        WHEN 'fee' THEN 4
                        ELSE 5 END,
                    id""",
                    (symbol,)
                ).fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            if owns:
                conn.close()

    def get_by_year(self, year: int, action: str | None = None) -> list[dict]:
        conn, owns = _get_conn(self.db_path)
        try:
            if action:
                rows = conn.execute(
                    "SELECT * FROM transactions WHERE strftime('%Y', trade_date) = ? AND action = ? ORDER BY trade_date, id",
                    (str(year), action)
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM transactions WHERE strftime('%Y', trade_date) = ? ORDER BY trade_date,
                    CASE action
                        WHEN 'buy' THEN 0 WHEN 'option_buy' THEN 0 WHEN 'rsu_vest' THEN 0
                        WHEN 'sell' THEN 1 WHEN 'option_sell' THEN 1 WHEN 'rsu_sell' THEN 1
                        WHEN 'dividend' THEN 2
                        WHEN 'option_expire' THEN 3
                        WHEN 'fee' THEN 4
                        ELSE 5 END,
                    id""",
                    (str(year),)
                ).fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            if owns:
                conn.close()

    def get_all(self, broker_code: str | None = None, year: int | None = None) -> list[dict]:
        conn, owns = _get_conn(self.db_path)
        try:
            conditions = []
            params = []
            if broker_code:
                conditions.append("broker_code = ?")
                params.append(broker_code)
            if year:
                conditions.append("strftime('%Y', trade_date) = ?")
                params.append(str(year))

            where = " AND ".join(conditions) if conditions else "1=1"
            rows = conn.execute(
                f"""SELECT * FROM transactions WHERE {where} ORDER BY trade_date,
                CASE action
                    WHEN 'buy' THEN 0 WHEN 'option_buy' THEN 0 WHEN 'rsu_vest' THEN 0
                    WHEN 'sell' THEN 1 WHEN 'option_sell' THEN 1 WHEN 'rsu_sell' THEN 1
                    WHEN 'dividend' THEN 2
                    WHEN 'option_expire' THEN 3
                    WHEN 'fee' THEN 4
                    ELSE 5 END,
                id, symbol""", params
            ).fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            if owns:
                conn.close()

    def delete_by_statement_file(self, file_id: int) -> int:
        """删除指定月结单文件的所有交易"""
        conn, owns = _get_conn(self.db_path)
        try:
            cur = conn.execute("DELETE FROM transactions WHERE statement_file_id = ?", (file_id,))
            conn.commit()
            return cur.rowcount
        finally:
            if owns:
                conn.close()


# ============================================================
# Dividends 仓储
# ============================================================

class DividendRepository:
    """分红记录 CRUD"""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = db_path

    def insert(self, broker_code: str, payment_date: str, symbol: str,
               per_share_amount: float, share_quantity: int,
               gross_amount: float, net_amount: float,
               withholding_tax: float = 0, withholding_rate: float = 0,
               withholding_country: str | None = None,
               withholding_refund: float = 0,
               collection_fee: float = 0, adr_fee: float = 0,
               other_deductions: float = 0,
               company_name: str | None = None,
               settlement_date: str | None = None,
               currency: str = "USD", exchange_rate: float | None = None,
               china_tax_rate: float = 0.20,
               statement_file_id: int | None = None,
               raw_data: str | None = None) -> int:

        gross_cny = round(gross_amount * exchange_rate, 2) if exchange_rate else None
        witholding_cny = round(withholding_tax * exchange_rate, 2) if exchange_rate else None
        refund_cny = round(withholding_refund * exchange_rate, 2) if exchange_rate else None
        net_cny = round(net_amount * exchange_rate, 2) if exchange_rate else None
        china_tax = round(gross_amount * exchange_rate * china_tax_rate, 2) if exchange_rate else None
        tax_payable = round(gross_amount * exchange_rate * china_tax_rate - withholding_tax, 2) if exchange_rate else None

        conn, owns = _get_conn(self.db_path)
        try:
            cursor = conn.execute("""
                INSERT INTO dividends
                    (broker_code, payment_date, settlement_date, symbol, company_name,
                     per_share_amount, share_quantity, gross_amount,
                     withholding_tax, withholding_rate, withholding_country,
                     withholding_refund,
                     collection_fee, adr_fee, other_deductions, net_amount,
                     currency, exchange_rate,
                     gross_amount_cny, withholding_tax_cny, withholding_refund_cny, net_amount_cny,
                     china_tax_rate, china_tax_amount, foreign_credit, tax_payable,
                     statement_file_id, raw_data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                broker_code, payment_date, settlement_date, symbol, company_name,
                per_share_amount, share_quantity, gross_amount,
                withholding_tax, withholding_rate, withholding_country,
                withholding_refund,
                collection_fee, adr_fee, other_deductions, net_amount,
                currency, _decimal_to_float(exchange_rate),
                gross_cny, witholding_cny, refund_cny, net_cny,
                china_tax_rate, china_tax, 0, tax_payable,
                statement_file_id, raw_data
            ))
            return cursor.lastrowid
        finally:
            if owns:
                conn.close()

    def bulk_insert(self, records: list[dict]) -> int:
        """批量插入分红记录。返回插入数量。"""
        conn, owns = _get_conn(self.db_path)
        try:
            enriched = []
            for r in records:
                exchange_rate = r.get("exchange_rate")
                gross_amount = r.get("gross_amount", 0)
                withholding_tax = r.get("withholding_tax", 0)
                net_amount = r.get("net_amount", 0)
                china_tax_rate = r.get("china_tax_rate", 0.20)

                withholding_refund = r.get("withholding_refund", 0)

                gross_cny = round(gross_amount * exchange_rate, 2) if exchange_rate else None
                withholding_cny = round(withholding_tax * exchange_rate, 2) if exchange_rate else None
                refund_cny = round(withholding_refund * exchange_rate, 2) if exchange_rate else None
                net_cny = round(net_amount * exchange_rate, 2) if exchange_rate else None
                china_tax = round(gross_amount * exchange_rate * china_tax_rate, 2) if exchange_rate else None
                tax_payable = round(gross_amount * exchange_rate * china_tax_rate - withholding_tax, 2) if exchange_rate else None

                enriched.append({
                    "broker_code": r["broker_code"],
                    "payment_date": r["payment_date"],
                    "settlement_date": r.get("settlement_date"),
                    "symbol": r["symbol"],
                    "company_name": r.get("company_name"),
                    "per_share_amount": r["per_share_amount"],
                    "share_quantity": r["share_quantity"],
                    "gross_amount": gross_amount,
                    "withholding_tax": withholding_tax,
                    "withholding_rate": r.get("withholding_rate", 0),
                    "withholding_country": r.get("withholding_country"),
                    "withholding_refund": withholding_refund,
                    "collection_fee": r.get("collection_fee", 0),
                    "adr_fee": r.get("adr_fee", 0),
                    "other_deductions": r.get("other_deductions", 0),
                    "net_amount": net_amount,
                    "currency": r.get("currency", "USD"),
                    "exchange_rate": _decimal_to_float(exchange_rate),
                    "gross_amount_cny": gross_cny,
                    "withholding_tax_cny": withholding_cny,
                    "withholding_refund_cny": refund_cny,
                    "net_amount_cny": net_cny,
                    "china_tax_rate": china_tax_rate,
                    "china_tax_amount": china_tax,
                    "foreign_credit": 0,
                    "tax_payable": tax_payable,
                    "statement_file_id": r.get("statement_file_id"),
                    "raw_data": r.get("raw_data"),
                })

            count = 0
            if enriched:
                conn.executemany("""
                    INSERT INTO dividends
                        (broker_code, payment_date, settlement_date, symbol, company_name,
                         per_share_amount, share_quantity, gross_amount,
                         withholding_tax, withholding_rate, withholding_country,
                         withholding_refund,
                         collection_fee, adr_fee, other_deductions, net_amount,
                         currency, exchange_rate,
                         gross_amount_cny, withholding_tax_cny, withholding_refund_cny, net_amount_cny,
                         china_tax_rate, china_tax_amount, foreign_credit, tax_payable,
                         statement_file_id, raw_data)
                    VALUES
                        (:broker_code, :payment_date, :settlement_date, :symbol, :company_name,
                         :per_share_amount, :share_quantity, :gross_amount,
                         :withholding_tax, :withholding_rate, :withholding_country,
                         :withholding_refund,
                         :collection_fee, :adr_fee, :other_deductions, :net_amount,
                         :currency, :exchange_rate,
                         :gross_amount_cny, :withholding_tax_cny, :withholding_refund_cny, :net_amount_cny,
                         :china_tax_rate, :china_tax_amount, :foreign_credit, :tax_payable,
                         :statement_file_id, :raw_data)
                """, enriched)
                count = len(enriched)
            return count
        finally:
            if owns:
                conn.close()

    def get_all(self, broker_code: str | None = None, year: int | None = None) -> list[dict]:
        conn, owns = _get_conn(self.db_path)
        try:
            conditions = []
            params = []
            if broker_code:
                conditions.append("broker_code = ?")
                params.append(broker_code)
            if year:
                conditions.append("strftime('%Y', payment_date) = ?")
                params.append(str(year))

            where = " AND ".join(conditions) if conditions else "1=1"
            rows = conn.execute(
                f"SELECT * FROM dividends WHERE {where} ORDER BY payment_date", params
            ).fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            if owns:
                conn.close()

    def delete_by_statement_file(self, file_id: int) -> int:
        """删除指定月结单文件的所有分红"""
        conn, owns = _get_conn(self.db_path)
        try:
            cur = conn.execute("DELETE FROM dividends WHERE statement_file_id = ?", (file_id,))
            conn.commit()
            return cur.rowcount
        finally:
            if owns:
                conn.close()

    def get_by_statement_file(self, file_id: int) -> list[dict]:
        """获取指定月结单文件的分红"""
        conn, owns = _get_conn(self.db_path)
        try:
            rows = conn.execute(
                "SELECT * FROM dividends WHERE statement_file_id = ? ORDER BY payment_date",
                (file_id,)
            ).fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            if owns:
                conn.close()


# ============================================================
# RSU Grants 仓储
# ============================================================

class RSUGrantRepository:
    """RSU 授予记录 CRUD"""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = db_path

    def upsert(self, grant_number: str, symbol: str, total_shares: int,
               company_name: str | None = None,
               vested_shares: int = 0, unvested_shares: int | None = None,
               vesting_schedule: list | None = None,
               exercise_price: float | None = None,
               grant_date: str | None = None,
               expiry_date: str | None = None,
               currency: str = "USD", market: str | None = None,
               notes: str | None = None) -> int:

        conn, owns = _get_conn(self.db_path)
        try:
            cursor = conn.execute("""
                INSERT INTO rsu_grants
                    (grant_number, symbol, company_name, total_shares,
                     vested_shares, unvested_shares, vesting_schedule,
                     exercise_price, grant_date, expiry_date,
                     currency, market, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(grant_number)
                DO UPDATE SET
                    vested_shares = excluded.vested_shares,
                    unvested_shares = excluded.unvested_shares,
                    vesting_schedule = excluded.vesting_schedule
            """, (
                grant_number, symbol, company_name, total_shares,
                vested_shares, unvested_shares,
                json.dumps(vesting_schedule) if vesting_schedule else None,
                exercise_price, grant_date, expiry_date,
                currency, market, notes
            ))
            return cursor.lastrowid
        finally:
            if owns:
                conn.close()

    def get_all(self) -> list[dict]:
        conn, owns = _get_conn(self.db_path)
        try:
            rows = conn.execute("SELECT * FROM rsu_grants ORDER BY grant_number").fetchall()
            result = []
            for r in rows:
                d = _row_to_dict(r)
                if d.get("vesting_schedule"):
                    d["vesting_schedule"] = json.loads(d["vesting_schedule"])
                result.append(d)
            return result
        finally:
            if owns:
                conn.close()

    def get_by_number(self, grant_number: str) -> dict | None:
        conn, owns = _get_conn(self.db_path)
        try:
            row = conn.execute(
                "SELECT * FROM rsu_grants WHERE grant_number = ?", (grant_number,)
            ).fetchone()
            if row:
                d = _row_to_dict(row)
                if d.get("vesting_schedule"):
                    d["vesting_schedule"] = json.loads(d["vesting_schedule"])
                return d
            return None
        finally:
            if owns:
                conn.close()


# ============================================================
# RSU Vests 仓储
# ============================================================

class RSUVestRepository:
    """RSU 归属记录 CRUD"""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = db_path

    def insert(self, grant_number: str, vest_date: str, symbol: str,
               vested_quantity: int, fmv_per_share: float,
               taxable_income: float, tax_amount: float,
               grant_id: int | None = None,
               company_name: str | None = None,
               deposit_date: str | None = None,
               sell_to_cover: int = 0,
               shares_deposited: int | None = None,
               currency: str = "USD", exchange_rate: float | None = None,
               tax_method: str = "cash", tax_paid: bool = False,
               tax_paid_date: str | None = None,
               custody_broker: str | None = None,
               source_image: str | None = None) -> int:

        income_cny = round(taxable_income * exchange_rate, 2) if exchange_rate else None
        tax_cny = round(tax_amount * exchange_rate, 2) if exchange_rate else None

        conn, owns = _get_conn(self.db_path)
        try:
            cursor = conn.execute("""
                INSERT INTO rsu_vests
                    (grant_id, grant_number, vest_date, deposit_date,
                     symbol, company_name,
                     vested_quantity, sell_to_cover, shares_deposited,
                     fmv_per_share, taxable_income, tax_amount,
                     currency, exchange_rate,
                     taxable_income_cny, tax_amount_cny,
                     tax_method, tax_paid, tax_paid_date,
                     custody_broker, source_image)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                grant_id, grant_number, vest_date, deposit_date,
                symbol, company_name,
                vested_quantity, sell_to_cover, shares_deposited,
                fmv_per_share, taxable_income, tax_amount,
                currency, _decimal_to_float(exchange_rate),
                income_cny, tax_cny,
                tax_method, int(tax_paid), tax_paid_date,
                custody_broker, source_image
            ))
            return cursor.lastrowid
        finally:
            if owns:
                conn.close()

    def get_all(self, year: int | None = None) -> list[dict]:
        conn, owns = _get_conn(self.db_path)
        try:
            if year:
                rows = conn.execute(
                    "SELECT * FROM rsu_vests WHERE strftime('%Y', vest_date) = ? ORDER BY vest_date",
                    (str(year),)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM rsu_vests ORDER BY vest_date"
                ).fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            if owns:
                conn.close()

    def get_by_grant(self, grant_number: str) -> list[dict]:
        conn, owns = _get_conn(self.db_path)
        try:
            rows = conn.execute(
                "SELECT * FROM rsu_vests WHERE grant_number = ? ORDER BY vest_date",
                (grant_number,)
            ).fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            if owns:
                conn.close()


# ============================================================
# Cash Rewards 仓储
# ============================================================

class CashRewardRepository:
    """现金回报记录 CRUD"""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = db_path

    def insert(self, reward_name: str, rsu_type: str | None,
               currency: str, total_amount: float,
               vested_amount: float = 0, unvested_amount: float = 0,
               notes: str | None = None) -> int:
        conn, owns = _get_conn(self.db_path)
        try:
            cursor = conn.execute("""
                INSERT INTO cash_rewards
                    (reward_name, rsu_type, currency, total_amount,
                     vested_amount, unvested_amount, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (reward_name, rsu_type, currency, total_amount,
                  vested_amount, unvested_amount, notes))
            return cursor.lastrowid
        finally:
            if owns:
                conn.close()

    def get_all(self) -> list[dict]:
        conn, owns = _get_conn(self.db_path)
        try:
            rows = conn.execute("SELECT * FROM cash_rewards ORDER BY reward_name").fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            if owns:
                conn.close()


# ============================================================
# Tax Lots 仓储
# ============================================================

class TaxLotRepository:
    """持仓批次 CRUD"""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = db_path

    def add_lot(self, symbol: str, acquisition_date: str,
                acquisition_type: str, quantity: int,
                cost_per_share: float, currency: str = "USD",
                broker_code: str | None = None,
                source_txn_id: int | None = None,
                exchange_rate: float | None = None) -> int:
        total_cost = cost_per_share * quantity
        total_cost_cny = round(total_cost * exchange_rate, 2) if exchange_rate else None

        conn, owns = _get_conn(self.db_path)
        try:
            cursor = conn.execute("""
                INSERT INTO tax_lots
                    (symbol, broker_code, acquisition_date, acquisition_type,
                     source_txn_id, quantity, remaining, cost_per_share, total_cost,
                     currency, exchange_rate, total_cost_cny)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                symbol, broker_code, acquisition_date, acquisition_type,
                source_txn_id, quantity, quantity, cost_per_share, total_cost,
                currency, _decimal_to_float(exchange_rate), total_cost_cny
            ))
            return cursor.lastrowid
        finally:
            if owns:
                conn.close()

    def bulk_add(self, records: list[dict]) -> int:
        """批量添加持仓批次。返回插入数量。"""
        conn, owns = _get_conn(self.db_path)
        try:
            enriched = []
            for r in records:
                cost_per_share = r["cost_per_share"]
                quantity = r["quantity"]
                exchange_rate = r.get("exchange_rate")
                total_cost = cost_per_share * quantity
                total_cost_cny = round(total_cost * exchange_rate, 2) if exchange_rate else None

                enriched.append({
                    "symbol": r["symbol"],
                    "broker_code": r.get("broker_code"),
                    "acquisition_date": r["acquisition_date"],
                    "acquisition_type": r["acquisition_type"],
                    "source_txn_id": r.get("source_txn_id"),
                    "quantity": quantity,
                    "remaining": quantity,
                    "cost_per_share": cost_per_share,
                    "total_cost": total_cost,
                    "currency": r.get("currency", "USD"),
                    "exchange_rate": _decimal_to_float(exchange_rate),
                    "total_cost_cny": total_cost_cny,
                })

            count = 0
            if enriched:
                conn.executemany("""
                    INSERT INTO tax_lots
                        (symbol, broker_code, acquisition_date, acquisition_type,
                         source_txn_id, quantity, remaining, cost_per_share, total_cost,
                         currency, exchange_rate, total_cost_cny)
                    VALUES
                        (:symbol, :broker_code, :acquisition_date, :acquisition_type,
                         :source_txn_id, :quantity, :remaining, :cost_per_share, :total_cost,
                         :currency, :exchange_rate, :total_cost_cny)
                """, enriched)
                count = len(enriched)
            return count
        finally:
            if owns:
                conn.close()

    def consume_lot(self, lot_id: int, quantity: int,
                    sell_txn_id: int, sell_price: float,
                    currency: str = "USD") -> dict:
        """消耗一个持仓批次，返回消耗记录"""
        conn, owns = _get_conn(self.db_path)
        try:
            row = conn.execute(
                "SELECT * FROM tax_lots WHERE id = ? AND remaining > 0", (lot_id,)
            ).fetchone()
            if not row:
                raise ValueError(f"Tax lot {lot_id} not found or fully consumed")

            available = row["remaining"]
            consume_qty = min(quantity, available)
            cost_basis = row["cost_per_share"] * consume_qty
            proceeds = sell_price * consume_qty
            realized_gain = proceeds - cost_basis

            conn.execute(
                "UPDATE tax_lots SET remaining = remaining - ? WHERE id = ?",
                (consume_qty, lot_id)
            )

            cursor = conn.execute("""
                INSERT INTO lot_consumptions
                    (sell_txn_id, tax_lot_id, consumed_qty, cost_per_share,
                     cost_basis, realized_gain, currency)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (sell_txn_id, lot_id, consume_qty, row["cost_per_share"],
                  cost_basis, realized_gain, currency))

            return {
                "consumption_id": cursor.lastrowid,
                "lot_id": lot_id,
                "consumed_qty": consume_qty,
                "cost_basis": cost_basis,
                "proceeds": proceeds,
                "realized_gain": realized_gain,
                "remaining_after": available - consume_qty,
            }
        finally:
            if owns:
                conn.close()

    def get_available_lots(self, symbol: str) -> list[dict]:
        """获取某股票所有未完全消耗的批次（FIFO 顺序）"""
        conn, owns = _get_conn(self.db_path)
        try:
            rows = conn.execute("""
                SELECT * FROM tax_lots
                WHERE symbol = ? AND remaining > 0
                ORDER BY acquisition_date, id
            """, (symbol,)).fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            if owns:
                conn.close()

    def get_all_available_lots(self) -> list[dict]:
        """获取全部未完全消耗的批次（用于模糊匹配期权符号）"""
        conn, owns = _get_conn(self.db_path)
        try:
            rows = conn.execute("""
                SELECT * FROM tax_lots
                WHERE remaining > 0
                ORDER BY acquisition_date, id
            """).fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            if owns:
                conn.close()

    def delete_all(self) -> int:
        """删除所有税务批次记录"""
        conn, owns = _get_conn(self.db_path)
        try:
            cur = conn.execute("DELETE FROM tax_lots")
            conn.commit()
            return cur.rowcount
        finally:
            if owns:
                conn.close()

    def consume_lot_for_exercise(self, lot_id: int, exercise_txn_id: int) -> dict:
        """因期权行权消耗持仓批次（非卖出，不计算收益）"""
        conn, owns = _get_conn(self.db_path)
        try:
            row = conn.execute(
                "SELECT * FROM tax_lots WHERE id = ? AND remaining > 0", (lot_id,)
            ).fetchone()
            if not row:
                raise ValueError(f"Tax lot {lot_id} not found or fully consumed")

            consume_qty = row["remaining"]
            cost_basis = row["cost_per_share"] * consume_qty

            conn.execute(
                "UPDATE tax_lots SET remaining = remaining - ? WHERE id = ?",
                (consume_qty, lot_id)
            )

            cursor = conn.execute("""
                INSERT INTO lot_consumptions
                    (sell_txn_id, tax_lot_id, consumed_qty, cost_per_share,
                     cost_basis, realized_gain, consumption_type, currency)
                VALUES (?, ?, ?, ?, ?, 0, 'exercise', ?)
            """, (exercise_txn_id, lot_id, consume_qty, row["cost_per_share"],
                  cost_basis, row["currency"]))

            return {
                "consumption_id": cursor.lastrowid,
                "lot_id": lot_id,
                "consumed_qty": consume_qty,
                "cost_basis": cost_basis,
                "remaining_after": 0,
            }
        finally:
            if owns:
                conn.close()

    def consume_lot_for_expiration(self, lot_id: int, expire_txn_id: int | None) -> dict:
        """因期权到期消耗持仓批次（全部成本基础计为损失）"""
        conn, owns = _get_conn(self.db_path)
        try:
            row = conn.execute(
                "SELECT * FROM tax_lots WHERE id = ? AND remaining > 0", (lot_id,)
            ).fetchone()
            if not row:
                raise ValueError(f"Tax lot {lot_id} not found or fully consumed")

            consume_qty = row["remaining"]
            cost_basis = row["cost_per_share"] * consume_qty
            realized_gain = -cost_basis

            conn.execute(
                "UPDATE tax_lots SET remaining = remaining - ? WHERE id = ?",
                (consume_qty, lot_id)
            )

            cursor = conn.execute("""
                INSERT INTO lot_consumptions
                    (sell_txn_id, tax_lot_id, consumed_qty, cost_per_share,
                     cost_basis, realized_gain, consumption_type, currency)
                VALUES (?, ?, ?, ?, ?, ?, 'expire', ?)
            """, (expire_txn_id, lot_id, consume_qty, row["cost_per_share"],
                  cost_basis, realized_gain, row["currency"]))

            return {
                "consumption_id": cursor.lastrowid,
                "lot_id": lot_id,
                "consumed_qty": consume_qty,
                "cost_basis": cost_basis,
                "realized_gain": realized_gain,
                "remaining_after": 0,
            }
        finally:
            if owns:
                conn.close()


class LotConsumptionRepository:
    """批次消耗记录 CRUD"""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = db_path

    def delete_all(self) -> int:
        """删除所有批次消耗记录"""
        conn, owns = _get_conn(self.db_path)
        try:
            cur = conn.execute("DELETE FROM lot_consumptions")
            conn.commit()
            return cur.rowcount
        finally:
            if owns:
                conn.close()


# ============================================================
# Positions 仓储
# ============================================================

class PositionRepository:
    """持仓快照 CRUD"""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = db_path

    def upsert(self, broker_code: str, as_of_date: str, symbol: str,
               quantity: int, currency: str = "USD",
               company_name: str | None = None,
               exchange: str | None = None,
               avg_cost: float | None = None,
               closing_price: float | None = None,
               market_value: float | None = None,
               unrealized_pnl: float | None = None,
               exchange_rate: float | None = None,
               statement_file_id: int | None = None) -> int:

        conn, owns = _get_conn(self.db_path)
        try:
            cursor = conn.execute("""
                INSERT INTO positions
                    (broker_code, as_of_date, symbol, company_name, exchange,
                     quantity, avg_cost, closing_price, market_value, unrealized_pnl,
                     currency, exchange_rate, market_value_cny, statement_file_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(broker_code, as_of_date, symbol)
                DO UPDATE SET
                    quantity = excluded.quantity,
                    avg_cost = excluded.avg_cost,
                    closing_price = excluded.closing_price,
                    market_value = excluded.market_value,
                    unrealized_pnl = excluded.unrealized_pnl,
                    exchange_rate = excluded.exchange_rate,
                    market_value_cny = excluded.market_value_cny
            """, (
                broker_code, as_of_date, symbol, company_name, exchange,
                quantity, avg_cost, closing_price, market_value, unrealized_pnl,
                currency, _decimal_to_float(exchange_rate),
                exchange_rate * market_value if exchange_rate and market_value else None,
                statement_file_id
            ))
            return cursor.lastrowid
        finally:
            if owns:
                conn.close()

    def bulk_upsert(self, records: list[dict]) -> int:
        """批量 upsert 持仓快照。返回插入/更新数量。"""
        conn, owns = _get_conn(self.db_path)
        try:
            enriched = []
            for r in records:
                exchange_rate = r.get("exchange_rate")
                market_value = r.get("market_value")
                enriched.append({
                    "broker_code": r["broker_code"],
                    "as_of_date": r["as_of_date"],
                    "symbol": r["symbol"],
                    "company_name": r.get("company_name"),
                    "exchange": r.get("exchange"),
                    "quantity": r["quantity"],
                    "avg_cost": r.get("avg_cost"),
                    "closing_price": r.get("closing_price"),
                    "market_value": market_value,
                    "unrealized_pnl": r.get("unrealized_pnl"),
                    "currency": r.get("currency", "USD"),
                    "exchange_rate": _decimal_to_float(exchange_rate),
                    "market_value_cny": exchange_rate * market_value if exchange_rate and market_value else None,
                    "statement_file_id": r.get("statement_file_id"),
                })

            count = 0
            if enriched:
                conn.executemany("""
                    INSERT INTO positions
                        (broker_code, as_of_date, symbol, company_name, exchange,
                         quantity, avg_cost, closing_price, market_value, unrealized_pnl,
                         currency, exchange_rate, market_value_cny, statement_file_id)
                    VALUES
                        (:broker_code, :as_of_date, :symbol, :company_name, :exchange,
                         :quantity, :avg_cost, :closing_price, :market_value, :unrealized_pnl,
                         :currency, :exchange_rate, :market_value_cny, :statement_file_id)
                    ON CONFLICT(broker_code, as_of_date, symbol)
                    DO UPDATE SET
                        quantity = excluded.quantity,
                        avg_cost = excluded.avg_cost,
                        closing_price = excluded.closing_price,
                        market_value = excluded.market_value,
                        unrealized_pnl = excluded.unrealized_pnl,
                        exchange_rate = excluded.exchange_rate,
                        market_value_cny = excluded.market_value_cny
                """, enriched)
                count = len(enriched)
            return count
        finally:
            if owns:
                conn.close()

    def get_by_date(self, as_of_date: str, broker_code: str | None = None) -> list[dict]:
        conn, owns = _get_conn(self.db_path)
        try:
            if broker_code:
                rows = conn.execute(
                    "SELECT * FROM positions WHERE as_of_date = ? AND broker_code = ? ORDER BY symbol",
                    (as_of_date, broker_code)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM positions WHERE as_of_date = ? ORDER BY broker_code, symbol",
                    (as_of_date,)
                ).fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            if owns:
                conn.close()

    def delete_by_statement_file(self, file_id: int) -> int:
        """删除指定月结单文件的所有持仓快照"""
        conn, owns = _get_conn(self.db_path)
        try:
            cur = conn.execute("DELETE FROM positions WHERE statement_file_id = ?", (file_id,))
            conn.commit()
            return cur.rowcount
        finally:
            if owns:
                conn.close()

    def get_by_statement_file(self, file_id: int) -> list[dict]:
        """获取指定月结单文件的持仓快照"""
        conn, owns = _get_conn(self.db_path)
        try:
            rows = conn.execute(
                "SELECT * FROM positions WHERE statement_file_id = ? ORDER BY symbol",
                (file_id,)
            ).fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            if owns:
                conn.close()


# ============================================================
# Tax Items 仓储
# ============================================================

class TaxItemRepository:
    """税务计算结果 CRUD"""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = db_path

    def insert(self, tax_year: int, symbol: str, income_type: str,
               gross_income: float, taxable_income: float,
               tax_rate: float, tax_amount_cny: float,
               source_type: str = "transaction", source_id: int | None = None,
               source_ref: str | None = None,
               trade_date: str | None = None,
               quantity: int | None = None,
               deductible: float = 0,
               currency: str = "USD", exchange_rate: float | None = None,
               tax_withheld_cny: float = 0,
               foreign_credit_cny: float = 0,
               excess_withholding_cny: float = 0,
               tax_payable_cny: float = 0,
               detail: str | None = None) -> int:

        gross_cny = round(gross_income * exchange_rate, 2) if exchange_rate else gross_income
        taxable_cny = round(taxable_income * exchange_rate, 2) if exchange_rate else taxable_income

        conn, owns = _get_conn(self.db_path)
        try:
            cursor = conn.execute("""
                INSERT INTO tax_items
                    (tax_year, source_type, source_id, source_ref,
                     symbol, income_type, trade_date, quantity,
                     gross_income, deductible, taxable_income,
                     currency, exchange_rate,
                     gross_income_cny, taxable_income_cny,
                     tax_rate, tax_amount_cny,
                     tax_withheld_cny, foreign_credit_cny,
                     excess_withholding_cny, tax_payable_cny,
                     detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                tax_year, source_type, source_id, source_ref,
                symbol, income_type, trade_date, quantity,
                gross_income, deductible, taxable_income,
                currency, _decimal_to_float(exchange_rate),
                gross_cny, taxable_cny,
                tax_rate, tax_amount_cny,
                tax_withheld_cny, foreign_credit_cny,
                excess_withholding_cny, tax_payable_cny,
                detail
            ))
            return cursor.lastrowid
        finally:
            if owns:
                conn.close()

    def get_by_year(self, tax_year: int, income_type: str | None = None) -> list[dict]:
        conn, owns = _get_conn(self.db_path)
        try:
            if income_type:
                rows = conn.execute(
                    "SELECT * FROM tax_items WHERE tax_year = ? AND income_type = ? ORDER BY trade_date",
                    (tax_year, income_type)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM tax_items WHERE tax_year = ? ORDER BY income_type, trade_date",
                    (tax_year,)
                ).fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            if owns:
                conn.close()


# ============================================================
# Tax Summaries 仓储
# ============================================================

class TaxSummaryRepository:
    """年度税务汇总 CRUD"""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = db_path

    def upsert(self, tax_year: int, income_type: str,
               total_income_cny: float = 0,
               total_deductible_cny: float = 0,
               total_taxable_cny: float = 0,
               total_tax_cny: float = 0,
               total_withheld_cny: float = 0,
               total_credit_cny: float = 0,
               total_excess_cny: float = 0,
               total_payable_cny: float = 0,
               computation_method: str | None = None,
               notes: str | None = None):

        conn, owns = _get_conn(self.db_path)
        try:
            conn.execute("""
                INSERT INTO tax_summaries
                    (tax_year, income_type,
                     total_income_cny, total_deductible_cny, total_taxable_cny,
                     total_tax_cny, total_withheld_cny, total_credit_cny,
                     total_excess_cny, total_payable_cny,
                     computation_method, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tax_year, income_type)
                DO UPDATE SET
                    total_income_cny = excluded.total_income_cny,
                    total_deductible_cny = excluded.total_deductible_cny,
                    total_taxable_cny = excluded.total_taxable_cny,
                    total_tax_cny = excluded.total_tax_cny,
                    total_withheld_cny = excluded.total_withheld_cny,
                    total_credit_cny = excluded.total_credit_cny,
                    total_excess_cny = excluded.total_excess_cny,
                    total_payable_cny = excluded.total_payable_cny,
                    computation_method = excluded.computation_method,
                    notes = excluded.notes
            """, (
                tax_year, income_type,
                total_income_cny, total_deductible_cny, total_taxable_cny,
                total_tax_cny, total_withheld_cny, total_credit_cny,
                total_excess_cny, total_payable_cny,
                computation_method, notes
            ))
        finally:
            if owns:
                conn.close()

    def get_by_year(self, tax_year: int) -> list[dict]:
        conn, owns = _get_conn(self.db_path)
        try:
            rows = conn.execute(
                "SELECT * FROM tax_summaries WHERE tax_year = ? ORDER BY income_type",
                (tax_year,)
            ).fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            if owns:
                conn.close()


class ForeignTaxCreditCarryforwardRepository:
    """境外税收抵免结转 CRUD

    依据财税〔2020〕3号，超额境外税收抵免可向后结转 5 个纳税年度。
    """

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = db_path

    def insert(self, source_year: int, country: str, income_category: str,
               carryforward_amount: float):
        """插入结转抵免。如已存在同 (source_year, country, income_category)，
        则累加 carryforward_amount 和 remaining_amount。"""
        conn, owns = _get_conn(self.db_path)
        try:
            conn.execute("""
                INSERT INTO foreign_tax_credit_carryforward
                    (source_year, target_year, country, income_category,
                     carryforward_amount, remaining_amount, expires_year)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_year, country, income_category)
                DO UPDATE SET
                    carryforward_amount = carryforward_amount + excluded.carryforward_amount,
                    remaining_amount = remaining_amount + excluded.remaining_amount
            """, (
                source_year,
                source_year + 1,
                country,
                income_category,
                carryforward_amount,
                carryforward_amount,
                source_year + 5,
            ))
            conn.commit()
        finally:
            if owns:
                conn.close()

    def get_available(self, target_year: int, country: str, income_category: str) -> list[dict]:
        """获取指定年度可用的结转抵免额度（未过期且有余额）"""
        conn, owns = _get_conn(self.db_path)
        try:
            rows = conn.execute("""
                SELECT * FROM foreign_tax_credit_carryforward
                WHERE target_year <= ? AND expires_year >= ?
                  AND country = ? AND income_category = ?
                  AND remaining_amount > 0
                ORDER BY source_year ASC
            """, (target_year, target_year, country, income_category)).fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            if owns:
                conn.close()

    def use_carryforward(self, carryforward_id: int, amount: float):
        """使用结转抵免额度"""
        conn, owns = _get_conn(self.db_path)
        try:
            conn.execute("""
                UPDATE foreign_tax_credit_carryforward
                SET used_amount = used_amount + ?,
                    remaining_amount = remaining_amount - ?
                WHERE id = ?
            """, (amount, amount, carryforward_id))
            conn.commit()
        finally:
            if owns:
                conn.close()

    def get_by_year(self, tax_year: int) -> list[dict]:
        conn, owns = _get_conn(self.db_path)
        try:
            rows = conn.execute(
                "SELECT * FROM foreign_tax_credit_carryforward WHERE source_year = ? "
                "ORDER BY country, income_category",
                (tax_year,)
            ).fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            if owns:
                conn.close()
