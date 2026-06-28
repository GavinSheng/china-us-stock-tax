"""数据质量监控 — 异常检测和端到端 Harness 运行

将 Input Validation + Reconciliation + Tax Verification 串联运行，
在关键节点（导入完成、计算完成）自动触发校验闭环。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.harness.validators import validate_transactions, ValidationResult
from src.harness.reconciliation import reconcile_import, ReconciliationResult
from src.harness.tax_verify import verify_tax_computation, VerificationResult, _get_year_end_rate
from src.harness.multi_account_verify import verify_multi_account, MultiAccountResult
from src.harness.overpayment_detect import detect_overpayment, OverpaymentResult
from src.harness.pre_calc import check_pre_calc_readiness, PreCalcReport


@dataclass
class HarnessReport:
    """完整 Harness 运行报告"""
    pre_calc: PreCalcReport | None = None
    validation: ValidationResult | None = None
    reconciliation: ReconciliationResult | None = None
    tax_verification: VerificationResult | None = None
    multi_account: MultiAccountResult | None = None
    overpayment: OverpaymentResult | None = None
    all_passed: bool = True

    def summary(self) -> str:
        lines = []
        lines.append("=" * 60)
        lines.append("  税务合规 Harness 校验报告")
        lines.append("=" * 60)

        section_num = 0
        total_sections = sum(1 for x in [
            self.pre_calc, self.validation, self.reconciliation, self.tax_verification,
            self.multi_account, self.overpayment
        ] if x is not None)

        if self.pre_calc:
            section_num += 1
            status = "✅ PASSED" if self.pre_calc.passed else "❌ FAILED"
            lines.append(f"\n[{section_num}/{total_sections}] 预计算就绪: {status}")
            for k, v in self.pre_calc.stats.items():
                lines.append(f"    {k}: {v}")
            for issue in self.pre_calc.issues:
                lines.append(f"    {issue}")

        if self.validation:
            section_num += 1
            status = "✅ PASSED" if self.validation.passed else "❌ FAILED"
            lines.append(f"\n[{section_num}/{total_sections}] 输入验证: {status}")
            lines.append(f"  {self.validation.total_checked} 条记录, "
                         f"{self.validation.error_count} errors, "
                         f"{self.validation.warning_count} warnings")
            for issue in self.validation.issues:
                lines.append(f"    {issue}")

        if self.reconciliation:
            section_num += 1
            status = "✅ PASSED" if self.reconciliation.passed else "❌ FAILED"
            lines.append(f"\n[{section_num}/{total_sections}] 对账校验: {status}")
            for issue in self.reconciliation.issues:
                lines.append(f"    {issue}")
            for k, v in self.reconciliation.stats.items():
                lines.append(f"    {k}: {v}")

        if self.tax_verification:
            section_num += 1
            status = "✅ PASSED" if self.tax_verification.passed else "❌ FAILED"
            lines.append(f"\n[{section_num}/{total_sections}] 计算验证: {status}")
            for issue in self.tax_verification.issues:
                lines.append(f"    {issue}")
            for k, v in self.tax_verification.verified_items.items():
                lines.append(f"    {k}: {v:,.2f}")

        if self.multi_account:
            section_num += 1
            status = "✅ PASSED" if self.multi_account.passed else "❌ FAILED"
            lines.append(f"\n[{section_num}/{total_sections}] 多账户核算验证: {status}")
            for issue in self.multi_account.issues:
                lines.append(f"    {issue}")
            for k, v in self.multi_account.verified_items.items():
                if isinstance(v, float):
                    lines.append(f"    {k}: {v:,.2f}")
                else:
                    lines.append(f"    {k}: {v}")

        if self.overpayment:
            section_num += 1
            status = "✅ PASSED" if self.overpayment.passed else "❌ FAILED"
            lines.append(f"\n[{section_num}/{total_sections}] 多缴税检测: {status}")
            for issue in self.overpayment.issues:
                lines.append(f"    {issue}")
            if self.overpayment.total_potential_overpayment_cny > 0:
                lines.append(f"  潜在多缴税额: ¥{self.overpayment.total_potential_overpayment_cny:,.2f}")
            for k, v in self.overpayment.verified_items.items():
                if isinstance(v, float):
                    lines.append(f"    {k}: {v:,.2f}")
                else:
                    lines.append(f"    {k}: {v}")

        overall = "✅ ALL PASSED" if self.all_passed else "❌ SOME CHECKS FAILED"
        lines.append(f"\n{'=' * 60}")
        lines.append(f"  总体: {overall}")
        lines.append(f"{'=' * 60}")

        return "\n".join(lines)


def run_full_harness(
    db_path: Path | None = None,
    year: int = 2025,
    transactions: list[dict[str, Any]] | None = None,
    skip_pre_calc: bool = False,
    skip_validation: bool = False,
    skip_reconciliation: bool = False,
    skip_verification: bool = False,
    skip_multi_account: bool = False,
    skip_overpayment: bool = False,
    usd_cny: float | None = None,
) -> HarnessReport:
    """运行完整的税务合规 Harness 校验

    Args:
        db_path: 数据库路径
        year: 校验年度
        transactions: 可选，直接传入交易列表
        skip_pre_calc: 跳过预计算就绪检查
        skip_validation: 跳过输入验证
        skip_reconciliation: 跳过对账校验
        skip_verification: 跳过计算验证
        skip_multi_account: 跳过多账户核算验证
        skip_overpayment: 跳过多缴税检测
        usd_cny: 用户指定的 USD/CNY 汇率（传给 pre_calc 检查）

    Returns:
        HarnessReport
    """
    report = HarnessReport()
    path = db_path or Path("output") / "tax.db"

    # 0. 预计算就绪检查
    if not skip_pre_calc and path.exists():
        report.pre_calc = check_pre_calc_readiness(
            db_path=path, year=year, usd_cny=usd_cny,
        )
        if not report.pre_calc.passed:
            report.all_passed = False

    if not skip_reconciliation or not skip_verification or not skip_multi_account or not skip_overpayment:
        if not path.exists():
            report.all_passed = False
            if not skip_reconciliation:
                report.reconciliation = ReconciliationResult()
                report.reconciliation.add("RC-000", "ERROR", f"数据库不存在: {path}")
            return report

    # 1. 输入验证 — 如果没有传入 transactions，从数据库自动加载
    if not skip_validation:
        if transactions is None:
            path = db_path or Path("output") / "tax.db"
            if path.exists():
                transactions = _load_transactions_from_db(path, year)
            else:
                report.validation = ValidationResult()
                report.validation.add("IV-000", "WARNING", "无法加载交易数据: 数据库不存在")
                report.validation.passed = False
                report.all_passed = False

        if transactions:
            report.validation = validate_transactions(transactions, year)
            if not report.validation.passed:
                report.all_passed = False
        else:
            report.validation = ValidationResult()
            report.validation.add("IV-000", "WARNING", "无交易数据可供验证")
            report.validation.passed = False
            report.all_passed = False

    # 2. 对账校验
    if not skip_reconciliation:
        report.reconciliation = reconcile_import(db_path, year)
        if not report.reconciliation.passed:
            report.all_passed = False

    # 3. 计算验证
    if not skip_verification:
        year_end_rate = _get_year_end_rate(db_path, year)
        report.tax_verification = verify_tax_computation(db_path, year, transactions, year_end_usd_rate=year_end_rate)
        if not report.tax_verification.passed:
            report.all_passed = False

    # 4. 多账户核算验证
    if not skip_multi_account:
        report.multi_account = verify_multi_account(db_path, year)
        if not report.multi_account.passed:
            report.all_passed = False

    # 5. 多缴税检测
    if not skip_overpayment:
        report.overpayment = detect_overpayment(db_path, year)
        if not report.overpayment.passed:
            report.all_passed = False

    return report


def run_db_only_harness(db_path: Path | None = None, year: int = 2025) -> HarnessReport:
    """仅运行数据库相关的校验（对账 + 计算验证，跳过输入验证）"""
    return run_full_harness(
        db_path=db_path,
        year=year,
        skip_validation=True,
        skip_reconciliation=False,
        skip_verification=False,
    )


def _load_transactions_from_db(db_path: Path, year: int) -> list[dict[str, Any]]:
    """从数据库加载交易记录供验证使用"""
    from decimal import Decimal

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT broker_code, trade_date, symbol, action, quantity, price, amount,
               currency, exchange_rate, reference_no
        FROM transactions
        WHERE strftime('%Y', trade_date) <= ?
          AND NOT (broker_code = 'boci' AND action = 'dividend')
          AND NOT (broker_code = 'boci' AND action = 'rsu_vest')
        ORDER BY trade_date,
            CASE action
                WHEN 'buy' THEN 0 WHEN 'option_buy' THEN 0 WHEN 'rsu_vest' THEN 0
                WHEN 'sell' THEN 1 WHEN 'option_sell' THEN 1 WHEN 'rsu_sell' THEN 1
                WHEN 'dividend' THEN 2
                WHEN 'option_expire' THEN 3
                WHEN 'fee' THEN 4
                ELSE 5 END,
            rowid
    """, (str(year),)).fetchall()

    txns = []
    for r in rows:
        txns.append({
            "broker_code": r["broker_code"],
            "trade_date": r["trade_date"],
            "symbol": r["symbol"],
            "action": r["action"],
            "quantity": r["quantity"],
            "price": Decimal(str(r["price"])) if r["price"] else Decimal("0"),
            "amount": Decimal(str(r["amount"])) if r["amount"] else Decimal("0"),
            "currency": r["currency"],
            "exchange_rate": Decimal(str(r["exchange_rate"])) if r["exchange_rate"] else None,
            "reference_no": r["reference_no"],
        })
    conn.close()
    return txns
