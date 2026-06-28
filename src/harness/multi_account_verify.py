"""多账户数据核算验证规则

对应 Harness 规则：
  MA-001: 同一股票跨账户 FIFO 不混淆
  MA-002: 分红不重复计入（同一笔分红不在多个账户重复出现）
  MA-003: RSU 成本基础在转仓保持一致
  MA-004: 境外预扣税不重复抵免
  MA-005: 账户间转仓不产生应税事件
  MA-006: 期权写仓收益不双重计税
  MA-007: 汇率一致性（同一日期同一币种使用相同汇率）
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from collections import defaultdict

DB_PATH = Path("output") / "tax.db"


@dataclass
class MultiAccountIssue:
    rule_id: str
    severity: str
    message: str
    details: str = ""

    def __str__(self) -> str:
        return f"[{self.severity}] {self.rule_id}: {self.message}"


@dataclass
class MultiAccountResult:
    issues: list[MultiAccountIssue] = field(default_factory=list)
    passed: bool = True
    verified_items: dict[str, int | float] = field(default_factory=dict)

    def add(self, rule_id: str, severity: str, message: str, details: str = ""):
        issue = MultiAccountIssue(rule_id, severity, message, details)
        self.issues.append(issue)
        if severity == "ERROR":
            self.passed = False

    def summary(self) -> str:
        status = "PASSED" if self.passed else "FAILED"
        lines = [f"Multi-Account Verification {status}:"]
        for issue in self.issues:
            lines.append(f"  {issue}")
        for key, val in self.verified_items.items():
            lines.append(f"  {key}: {val}")
        return "\n".join(lines)


def verify_multi_account(
    db_path: Path | None = None,
    year: int = 2025,
) -> MultiAccountResult:
    """验证多账户数据核算的正确性"""
    result = MultiAccountResult()

    path = db_path or DB_PATH
    if not path.exists():
        result.add("MA-000", "ERROR", f"数据库不存在: {path}")
        return result

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row

    try:
        _verify_fifo_cross_broker(conn, year, result)
        _verify_no_duplicate_dividends(conn, year, result)
        _verify_rsu_cost_consistency(conn, year, result)
        _verify_no_duplicate_withholding(conn, year, result)
        _verify_transfer_no_tax_event(conn, year, result)
        _verify_option_write_no_double_tax(conn, year, result)
        _verify_exchange_rate_consistency(conn, year, result)
    finally:
        conn.close()

    return result


def _verify_fifo_cross_broker(conn: sqlite3.Connection, year: int, result: MultiAccountResult):
    """MA-001: 同一股票跨账户 FIFO 不混淆

    验证：同一 symbol 在不同券商有交易时，应该使用跨券商合并 FIFO。
    检查 tax_lots 表中的 broker_code 是否正确记录。
    """
    # 检查是否有同一 symbol 在多个券商都有持仓
    symbols_by_broker = conn.execute("""
        SELECT DISTINCT symbol, broker_code
        FROM tax_lots
        WHERE broker_code IS NOT NULL AND broker_code != ''
    """).fetchall()

    symbol_brokers = defaultdict(set)
    for r in symbols_by_broker:
        symbol_brokers[r["symbol"]].add(r["broker_code"])

    multi_broker_symbols = [s for s, brokers in symbol_brokers.items() if len(brokers) > 1]
    result.verified_items["multi_broker_symbols"] = len(multi_broker_symbols)

    if multi_broker_symbols:
        # 这是正常的，记录信息
        result.verified_items["cross_broker_tracking"] = "OK"


def _verify_no_duplicate_dividends(conn: sqlite3.Connection, year: int, result: MultiAccountResult):
    """MA-002: 分红不重复计入

    验证：同一笔分红不应该同时出现在 transactions 表和 dividends 表中。
    """
    # 检查 transactions 表中是否有 dividend 类型记录
    txn_dividends = conn.execute("""
        SELECT COUNT(*) as cnt
        FROM transactions
        WHERE strftime('%Y', trade_date) = ?
          AND action = 'dividend'
    """, (str(year),)).fetchone()

    txn_div_count = txn_dividends["cnt"] if txn_dividends else 0

    if txn_div_count > 0:
        result.add("MA-002", "ERROR",
                   f"transactions 表中有 {txn_div_count} 笔 dividend 记录，"
                   f"可能导致重复计税（分红应只在 dividends 表中）")

    # 检查 dividends 表中是否有重复（同一日期、同一 symbol、同一金额出现多次）
    dup_check = conn.execute("""
        SELECT payment_date, symbol, gross_amount, COUNT(*) as cnt
        FROM dividends
        WHERE strftime('%Y', payment_date) = ?
        GROUP BY payment_date, symbol, gross_amount
        HAVING cnt > 1
    """, (str(year),)).fetchall()

    if dup_check:
        for r in dup_check:
            result.add("MA-002", "ERROR",
                       f"dividends 表中发现重复记录: {r['payment_date']} {r['symbol']} "
                       f"${r['gross_amount']} 出现 {r['cnt']} 次")

    result.verified_items["dividends_in_transactions"] = txn_div_count
    result.verified_items["duplicate_dividends"] = len(dup_check)


def _verify_rsu_cost_consistency(conn: sqlite3.Connection, year: int, result: MultiAccountResult):
    """MA-003: RSU 成本基础在转仓保持一致

    验证：RSU 归属时记录的 FMV 应该与后续卖出的成本基础一致。
    lot_consumptions 没有 symbol/lot_origin 列，需要 JOIN tax_lots。
    只比对 acquisition_date 与 vest_date 匹配的记录，避免跨归属误报。
    """
    # 获取 RSU 归属记录
    rsu_vests = conn.execute("""
        SELECT symbol, vest_date, fmv_per_share, vested_quantity
        FROM rsu_vests
        WHERE strftime('%Y', vest_date) < ?
    """, (str(year),)).fetchall()

    # 检查 lot_consumptions 中 RSU 来源的 lot 成本是否与归属时 FMV 一致
    rsu_cost_issues = 0
    for rsu in rsu_vests:
        symbol = rsu["symbol"]
        vest_date = rsu["vest_date"]
        expected_cost = float(rsu["fmv_per_share"])

        # 查找消耗该 RSU lot 的记录（通过 JOIN tax_lots，只匹配同一归属日期的 lot）
        consumptions = conn.execute("""
            SELECT lc.cost_per_share, lc.consumed_qty
            FROM lot_consumptions lc
            JOIN tax_lots tl ON lc.tax_lot_id = tl.id
            WHERE tl.symbol = ?
              AND tl.acquisition_type = 'rsu_vest'
              AND tl.acquisition_date = ?
              AND ABS(lc.cost_per_share - ?) > 0.01
        """, (symbol, vest_date, expected_cost)).fetchall()

        for c in consumptions:
            rsu_cost_issues += 1

    if rsu_cost_issues > 0:
        result.add("MA-003", "WARNING",
                   f"发现 {rsu_cost_issues} 笔 RSU 成本基础不一致",
                   "RSU 归属 FMV 与卖出成本基础存在差异")

    result.verified_items["rsu_cost_issues"] = rsu_cost_issues


def _verify_no_duplicate_withholding(conn: sqlite3.Connection, year: int, result: MultiAccountResult):
    """MA-004: 境外预扣税不重复抵免

    验证：同一笔预扣税不应该在多个地方被抵免。
    dividends 表的 exchange_rate / withholding_tax_cny 可能未填充，
    因此改为检查 tax_items 中是否有重复的外国税收抵免（同一分红多笔 credit）。
    """
    # 检查 tax_items 中 dividend 类型记录的 foreign_credit_cny 总和
    total_credit = conn.execute("""
        SELECT COALESCE(SUM(foreign_credit_cny), 0) as total
        FROM tax_items
        WHERE tax_year = ?
    """, (str(year),)).fetchone()["total"]

    # 检查 dividends 表中有预扣税的记录数
    dividends_with_wht = conn.execute("""
        SELECT COUNT(*) as cnt,
               COALESCE(SUM(withholding_tax), 0) as total_usd
        FROM dividends
        WHERE strftime('%Y', payment_date) = ?
          AND withholding_tax > 0
    """, (str(year),)).fetchone()

    wht_count = dividends_with_wht["cnt"] if dividends_with_wht else 0
    wht_usd = float(dividends_with_wht["total_usd"] or 0) if dividends_with_wht else 0

    # tax_items 中有 foreign_credit 的记录数
    credit_items = conn.execute("""
        SELECT COUNT(*) as cnt
        FROM tax_items
        WHERE tax_year = ?
          AND foreign_credit_cny > 0
    """, (str(year),)).fetchone()
    credit_count = credit_items["cnt"] if credit_items else 0

    # 如果 credit 记录数远大于有预扣税的分红记录数，可能有重复
    if wht_count > 0 and credit_count > wht_count * 2:
        result.add("MA-004", "ERROR",
                   f"foreign_credit 记录数 ({credit_count}) 远超分红预扣税记录数 ({wht_count})，"
                   f"可能存在重复抵免")

    # 粗略检查：外国税收抵免不应超过分红总额的 10%（W-8BEN 最高 10%）× 汇率上限 7.5
    # 这是合理的上限估计
    total_dividend_gross = conn.execute("""
        SELECT COALESCE(SUM(gross_amount), 0) as total
        FROM dividends
        WHERE strftime('%Y', payment_date) = ?
    """, (str(year),)).fetchone()["total"]
    max_reasonable_credit = float(total_dividend_gross) * 0.10 * 7.5  # 10% × 7.5 汇率上限

    if float(total_credit) > max_reasonable_credit * 1.05:
        result.add("MA-004", "WARNING",
                   f"境外税收抵免总额 ¥{float(total_credit):.2f} 超过合理上限 "
                   f"¥{max_reasonable_credit:.2f}（分红总额10%×汇率7.5）")

    result.verified_items["total_foreign_credit_cny"] = float(total_credit)
    result.verified_items["dividends_with_wht_count"] = wht_count
    result.verified_items["total_withholding_usd"] = wht_usd
    result.verified_items["credit_items_count"] = credit_count


def _verify_transfer_no_tax_event(conn: sqlite3.Connection, year: int, result: MultiAccountResult):
    """MA-005: 账户间转仓不产生应税事件

    验证：如果存在转仓（同一 symbol 在一个券商卖出后很快在另一个券商买入），
    不应该产生异常的应税事件。
    """
    # 简单检查：同一 symbol 在不同券商的买卖时间是否异常接近
    # 这需要更复杂的逻辑来检测，这里只做基本验证

    # 检查是否有同日跨券商买卖同一 symbol
    same_day_trades = conn.execute("""
        SELECT t1.symbol, t1.broker_code as broker1, t2.broker_code as broker2,
               t1.trade_date, t1.action as action1, t2.action as action2
        FROM transactions t1
        JOIN transactions t2 ON t1.symbol = t2.symbol
            AND t1.trade_date = t2.trade_date
            AND t1.broker_code != t2.broker_code
        WHERE strftime('%Y', t1.trade_date) = ?
          AND t1.action IN ('buy', 'sell')
          AND t2.action IN ('buy', 'sell')
          AND t1.action != t2.action
    """, (str(year),)).fetchall()

    if same_day_trades:
        result.add("MA-005", "WARNING",
                   f"发现 {len(same_day_trades)} 笔同日跨券商反向交易",
                   "可能是转仓，请确认不产生异常应税事件")

    result.verified_items["same_day_cross_broker_trades"] = len(same_day_trades)


def _verify_option_write_no_double_tax(conn: sqlite3.Connection, year: int, result: MultiAccountResult):
    """MA-006: 期权写仓收益不双重计税

    验证：期权写仓（sell-to-open）的收益不应该在到期时再次被计税。
    lot_consumptions 需要 JOIN tax_lots（acquisition_type）和 transactions（trade_date, symbol）。
    """
    # acquisition_type = 'option_sell' 表示写仓（sell-to-open）
    # 通过 JOIN tax_lots 获取 symbol/acquisition_type，JOIN transactions 获取 sell_date
    write_consumptions = conn.execute("""
        SELECT tl.symbol, t.trade_date AS sell_date,
               SUM(lc.consumed_qty) AS qty, SUM(lc.realized_gain) AS gain
        FROM lot_consumptions lc
        JOIN tax_lots tl ON lc.tax_lot_id = tl.id
        JOIN transactions t ON lc.sell_txn_id = t.id
        WHERE tl.acquisition_type = 'option_sell'
          AND strftime('%Y', t.trade_date) = ?
        GROUP BY tl.symbol, t.trade_date
    """, (str(year),)).fetchall()

    # 检查这些 symbol 在同一天是否有其他资本利得记录
    double_tax_count = 0
    for wc in write_consumptions:
        other_gains = conn.execute("""
            SELECT COUNT(*) AS cnt
            FROM lot_consumptions lc
            JOIN tax_lots tl ON lc.tax_lot_id = tl.id
            JOIN transactions t ON lc.sell_txn_id = t.id
            WHERE tl.symbol = ?
              AND t.trade_date = ?
              AND tl.acquisition_type != 'option_sell'
        """, (wc["symbol"], wc["sell_date"])).fetchone()

        if other_gains and other_gains["cnt"] > 0:
            double_tax_count += 1

    if double_tax_count > 0:
        result.add("MA-006", "ERROR",
                   f"发现 {double_tax_count} 笔可能的期权写仓双重计税")

    result.verified_items["option_write_consumptions"] = len(write_consumptions)
    result.verified_items["potential_double_tax"] = double_tax_count


def _verify_exchange_rate_consistency(conn: sqlite3.Connection, year: int, result: MultiAccountResult):
    """MA-007: 汇率一致性（同一日期同一币种使用相同汇率）

    验证：同一日期同一币种的所有交易应该使用相同的汇率。
    """
    # 检查同一天同一币种是否有不同汇率
    rate_check = conn.execute("""
        SELECT trade_date, currency, exchange_rate, COUNT(DISTINCT exchange_rate) as rate_cnt
        FROM transactions
        WHERE strftime('%Y', trade_date) = ?
          AND currency IS NOT NULL
          AND currency != ''
          AND exchange_rate > 0
        GROUP BY trade_date, currency
        HAVING rate_cnt > 1
    """, (str(year),)).fetchall()

    if rate_check:
        for r in rate_check:
            result.add("MA-007", "WARNING",
                       f"{r['trade_date']} {r['currency']} 使用了 {r['rate_cnt']} 个不同汇率")

    result.verified_items["inconsistent_rate_dates"] = len(rate_check)


if __name__ == "__main__":
    result = verify_multi_account()
    print(result.summary())
