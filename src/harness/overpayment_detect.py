"""多缴税检测规则

从注册税务师视角审核计税结果，检测可能导致客户多缴税的情况。

对应 Harness 规则：
  OP-001: 分红关联费用未从应纳税所得额扣除
  OP-002: 美股分红预扣税 10% 未被正确抵免
  OP-003: 使用默认汇率而非实际汇率
  OP-004: 亏损未充分抵扣盈利
  OP-005: 期权写仓权利金被双重计税
  OP-006: 跨年持仓成本基础为 $0（RSU 未正确追溯）
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from collections import defaultdict

from src.calculator.exchange_rate import get_exchange_rate

DB_PATH = Path("output") / "tax.db"


@dataclass
class OverpaymentIssue:
    rule_id: str
    severity: str
    message: str
    potential_overpayment_cny: float = 0.0
    details: str = ""

    def __str__(self) -> str:
        if self.potential_overpayment_cny > 0:
            return f"[{self.severity}] {self.rule_id}: {self.message} (可能多缴 ¥{self.potential_overpayment_cny:.2f})"
        return f"[{self.severity}] {self.rule_id}: {self.message}"


@dataclass
class OverpaymentResult:
    issues: list[OverpaymentIssue] = field(default_factory=list)
    passed: bool = True
    total_potential_overpayment_cny: float = 0.0
    verified_items: dict[str, float] = field(default_factory=dict)

    def add(self, rule_id: str, severity: str, message: str,
            potential_overpayment_cny: float = 0.0, details: str = ""):
        issue = OverpaymentIssue(rule_id, severity, message, potential_overpayment_cny, details)
        self.issues.append(issue)
        self.total_potential_overpayment_cny += potential_overpayment_cny
        if severity == "ERROR":
            self.passed = False

    def summary(self) -> str:
        status = "PASSED" if self.passed else "FAILED"
        lines = [f"Overpayment Detection {status}:"]
        for issue in self.issues:
            lines.append(f"  {issue}")
        if self.total_potential_overpayment_cny > 0:
            lines.append(f"  TOTAL POTENTIAL OVERPAYMENT: ¥{self.total_potential_overpayment_cny:.2f}")
        for key, val in self.verified_items.items():
            lines.append(f"  {key}: {val:.2f}" if isinstance(val, float) else f"  {key}: {val}")
        return "\n".join(lines)


def detect_overpayment(
    db_path: Path | None = None,
    year: int = 2025,
) -> OverpaymentResult:
    """检测可能导致多缴税的情况"""
    result = OverpaymentResult()

    path = db_path or DB_PATH
    if not path.exists():
        result.add("OP-000", "ERROR", f"数据库不存在: {path}")
        return result

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row

    try:
        _check_dividend_fees(conn, year, result)
        _check_withholding_credit(conn, year, result)
        _check_default_exchange_rate(conn, year, result)
        _check_loss_offset(conn, year, result)
        _check_option_write_double_tax(conn, year, result)
        _check_zero_cost_basis(conn, year, result)
    finally:
        conn.close()

    return result


def _get_usd_rate(year: int) -> float:
    """获取指定年度的 USD/CNY 汇率"""
    try:
        rate = get_exchange_rate(date(year, 12, 31), "USD", year=year)
        return float(rate)
    except Exception:
        return 7.1  # fallback


def _check_dividend_fees(conn: sqlite3.Connection, year: int, result: OverpaymentResult):
    """OP-001: 分红关联费用未从应纳税所得额扣除

    分红收到的金额可能已被扣除收款费、ADR 费等。
    应税所得应该是扣除费用后的净收入，而非毛收入。
    """
    # 检查 dividends 表中的费用
    dividends_with_fees = conn.execute("""
        SELECT id, symbol, gross_amount, collection_fee, adr_fee, other_deductions,
               gross_amount - collection_fee - adr_fee - other_deductions as net_amount
        FROM dividends
        WHERE strftime('%Y', payment_date) = ?
          AND (collection_fee > 0 OR adr_fee > 0 OR other_deductions > 0)
    """, (str(year),)).fetchall()

    total_fees = 0.0
    for d in dividends_with_fees:
        fees = float(d["collection_fee"] or 0) + float(d["adr_fee"] or 0) + float(d["other_deductions"] or 0)
        total_fees += fees

    # 检查 tax_items 中是否按毛收入计税
    dividend_tax_items = conn.execute("""
        SELECT symbol, gross_income, deductible
        FROM tax_items
        WHERE tax_year = ? AND income_type = 'dividend'
    """, (str(year),)).fetchall()

    # 如果 deductible 为 0 但有费用，说明费用未被扣除
    unclaimed_fees = 0.0
    for item in dividend_tax_items:
        if float(item["deductible"] or 0) == 0:
            # 查找对应的分红费用
            for d in dividends_with_fees:
                if d["symbol"] == item["symbol"]:
                    fees = float(d["collection_fee"] or 0) + float(d["adr_fee"] or 0) + float(d["other_deductions"] or 0)
                    unclaimed_fees += fees

    usd_rate = _get_usd_rate(year)

    if unclaimed_fees > 0:
        # 未扣除的费用 × 20% = 多缴税款
        overpayment = unclaimed_fees * usd_rate * 0.20
        result.add("OP-001", "WARNING",
                   f"分红关联费用 ¥{unclaimed_fees * usd_rate:.2f} 未从应纳税所得额扣除",
                   potential_overpayment_cny=overpayment)

    result.verified_items["dividend_fees_cny"] = total_fees * usd_rate
    result.verified_items["unclaimed_fees_cny"] = unclaimed_fees * usd_rate


def _check_withholding_credit(conn: sqlite3.Connection, year: int, result: OverpaymentResult):
    """OP-002: 美股分红预扣税 10% 未被正确抵免

    美股分红通常被预扣 10% 税款（W-8BEN），应该在中国个税中抵免。
    """
    # 获取所有分红的预扣税
    dividends = conn.execute("""
        SELECT symbol, gross_amount, withholding_tax, withholding_rate
        FROM dividends
        WHERE strftime('%Y', payment_date) = ?
          AND withholding_country = 'US'
    """, (str(year),)).fetchall()

    total_withholding = 0.0
    missing_credit_count = 0

    for d in dividends:
        wht = float(d["withholding_tax"] or 0)
        rate = float(d["withholding_rate"] or 0)

        if rate < 0.09 and float(d["gross_amount"] or 0) > 0:
            # 预扣率低于 9%，可能是未被正确记录
            expected_wht = float(d["gross_amount"]) * 0.10
            missing_wht = expected_wht - wht
            total_withholding += missing_wht
            missing_credit_count += 1

    if missing_credit_count > 0:
        usd_rate = _get_usd_rate(year)
        overpayment = total_withholding * usd_rate
        result.add("OP-002", "WARNING",
                   f"{missing_credit_count} 笔美股分红预扣税未被正确抵免，"
                   f"涉及金额 ${total_withholding:.2f}",
                   potential_overpayment_cny=overpayment)

    result.verified_items["missing_withholding_usd"] = total_withholding


def _check_default_exchange_rate(conn: sqlite3.Connection, year: int, result: OverpaymentResult):
    """OP-003: 使用默认汇率而非实际汇率

    如果交易有实际成交汇率，应该使用实际汇率而非年度平均汇率。
    """
    # 检查 transactions 表中汇率为 0 或很接近默认值的记录
    default_rate_issues = conn.execute("""
        SELECT COUNT(*) as cnt
        FROM transactions
        WHERE strftime('%Y', trade_date) = ?
          AND currency = 'USD'
          AND (exchange_rate = 0 OR exchange_rate IS NULL)
          AND action IN ('sell', 'dividend')
    """, (str(year),)).fetchone()

    issue_count = default_rate_issues["cnt"] if default_rate_issues else 0

    if issue_count > 0:
        result.add("OP-003", "WARNING",
                   f"{issue_count} 笔交易使用默认汇率而非实际汇率",
                   details="可能影响应纳税所得额的精确计算")

    result.verified_items["default_rate_transactions"] = issue_count


def _check_loss_offset(conn: sqlite3.Connection, year: int, result: OverpaymentResult):
    """OP-004: 亏损未充分抵扣盈利

    年度净额法下，所有亏损应该充分抵扣盈利。
    检查是否使用了逐笔法而非年度净额法（当年度净额法更有利时）。
    """
    # 检查 tax_summaries 中的计算方法
    summary = conn.execute("""
        SELECT computation_method, total_taxable_cny, total_tax_cny
        FROM tax_summaries
        WHERE tax_year = ? AND income_type = 'capital_gain'
    """, (str(year),)).fetchone()

    if not summary:
        return

    method = summary["computation_method"]
    tax_payable = float(summary["total_tax_cny"] or 0)

    # 检查是否有亏损记录
    losses = conn.execute("""
        SELECT COUNT(*) as cnt, SUM(lc.realized_gain) as total_loss
        FROM lot_consumptions lc
        JOIN transactions t ON lc.sell_txn_id = t.id
        WHERE strftime('%Y', t.trade_date) = ?
          AND lc.realized_gain < 0
    """, (str(year),)).fetchone()

    if losses and losses["cnt"] > 0 and method == "per_transaction":
        # 有亏损但使用逐笔法，检查年度净额法是否更优
        total_loss = abs(float(losses["total_loss"] or 0))
        # 简化估算：如果亏损 × 20% > 100，可能是年度净额法更优
        potential_saving = total_loss * _get_usd_rate(year) * 0.20
        if potential_saving > 100:
            result.add("OP-004", "WARNING",
                       f"有 {losses['cnt']} 笔亏损交易但使用逐笔法，"
                       f"年度净额法可能更优",
                       potential_overpayment_cny=potential_saving)

    result.verified_items["loss_count"] = losses["cnt"] if losses else 0
    result.verified_items["computation_method"] = method


def _check_option_write_double_tax(conn: sqlite3.Connection, year: int, result: OverpaymentResult):
    """OP-005: 期权写仓权利金被双重计税

    期权写仓（sell-to-open）时收到权利金，如果到期归不应该只对权利金计税一次。
    lot_consumptions 需要 JOIN tax_lots 和 transactions。
    """
    # 检查写仓（sell-to-open）类型的 lot 消耗
    write_lots = conn.execute("""
        SELECT tl.symbol, t.trade_date AS sell_date, SUM(lc.realized_gain) as total_gain
        FROM lot_consumptions lc
        JOIN tax_lots tl ON lc.tax_lot_id = tl.id
        JOIN transactions t ON lc.sell_txn_id = t.id
        WHERE tl.acquisition_type = 'option_sell'
          AND strftime('%Y', t.trade_date) = ?
        GROUP BY tl.symbol, t.trade_date
    """, (str(year),)).fetchall()

    double_tax_count = 0
    for wl in write_lots:
        # 检查是否有对应的 option_expire 记录
        expire_records = conn.execute("""
            SELECT COUNT(*) as cnt
            FROM lot_consumptions lc
            JOIN tax_lots tl ON lc.tax_lot_id = tl.id
            JOIN transactions t ON lc.sell_txn_id = t.id
            WHERE tl.symbol = ?
              AND t.trade_date = ?
              AND tl.acquisition_type = 'option_expire'
        """, (wl["symbol"], wl["sell_date"])).fetchone()

        if expire_records and expire_records["cnt"] > 0:
            double_tax_count += 1

    if double_tax_count > 0:
        result.add("OP-005", "ERROR",
                   f"发现 {double_tax_count} 笔期权写仓可能被双重计税")

    result.verified_items["option_write_lots"] = len(write_lots)
    result.verified_items["potential_double_tax"] = double_tax_count


def _check_zero_cost_basis(conn: sqlite3.Connection, year: int, result: OverpaymentResult):
    """OP-006: 跨年持仓成本基础为 $0（RSU 未正确追溯）

    如果 RSU 归属后跨年卖出，成本基础应该是归属时的 FMV，而非 $0。
    lot_consumptions 需要 JOIN tax_lots 和 transactions。
    """
    # 检查 cost_per_share = 0 的 lot 消耗
    zero_cost = conn.execute("""
        SELECT tl.symbol, tl.acquisition_date AS lot_date, tl.acquisition_type AS lot_origin,
               lc.consumed_qty, lc.realized_gain
        FROM lot_consumptions lc
        JOIN tax_lots tl ON lc.tax_lot_id = tl.id
        WHERE lc.cost_per_share = 0
          AND tl.acquisition_type IN ('carryforward', 'carryforward_gap_fill')
          AND EXISTS (
              SELECT 1 FROM transactions t
              WHERE t.id = lc.sell_txn_id
                AND strftime('%Y', t.trade_date) = ?
          )
    """, (str(year),)).fetchall()

    if zero_cost:
        # 计算因 $0 成本导致的多缴税
        # 实际成本应该从历史数据追溯，这里只能估算
        total_gain = sum(float(z["realized_gain"] or 0) for z in zero_cost)
        # 如果成本是 $0，gain 就是全部 proceeds，多缴税 ≈ gain × 20%
        # 但实际上成本可能不为 0，这里只是一个上限估计
        overpayment_upper_bound = total_gain * _get_usd_rate(year) * 0.20

        result.add("OP-006", "WARNING",
                   f"发现 {len(zero_cost)} 笔 $0 成本基础的交易（可能是 RSU 未正确追溯）",
                   potential_overpayment_cny=overpayment_upper_bound,
                   details=f"涉及盈利 ${total_gain:.2f}")

    result.verified_items["zero_cost_basis_count"] = len(zero_cost)


if __name__ == "__main__":
    result = detect_overpayment()
    print(result.summary())
