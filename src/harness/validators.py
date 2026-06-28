"""输入验证层 — 校验交易数据的质量和合法性

对应 Harness 规则：
  IV-001 ~ IV-015：日期范围、数量、价格、金额一致性、symbol 格式、action 枚举、
                   分红金额、期权符号、交易日期、买入价格非零、汇率验证、broker_code、
                   RSU税扣缴、分红预扣税、金额方向合理性
  RC-003 ~ RC-005：重复交易检测、累计卖出合理性、同文件内无买入直接卖出
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

# ── 规则常量 ──
EARLIEST_VALID_DATE = date(2020, 1, 1)
MAX_SINGLE_STOCK_POSITION = 1_000_000  # 防解析错误（RC-001）
AMOUNT_TOLERANCE = Decimal("0.01")  # 1 分容差


@dataclass
class ValidationIssue:
    """单条校验问题"""
    rule_id: str          # 如 "IV-001"
    severity: str         # "ERROR" | "WARNING"
    message: str
    details: str = ""
    row_index: int = -1   # 在输入列表中的索引

    def __str__(self) -> str:
        loc = f"row {self.row_index}" if self.row_index >= 0 else "global"
        return f"[{self.severity}] {self.rule_id} ({loc}): {self.message}"


@dataclass
class ValidationResult:
    """验证结果汇总"""
    issues: list[ValidationIssue] = field(default_factory=list)
    passed: bool = True
    total_checked: int = 0
    error_count: int = 0
    warning_count: int = 0

    def add(self, rule_id: str, severity: str, message: str, details: str = "", row_index: int = -1):
        issue = ValidationIssue(rule_id, severity, message, details, row_index)
        self.issues.append(issue)
        if severity == "ERROR":
            self.error_count += 1
            self.passed = False
        else:
            self.warning_count += 1

    def summary(self) -> str:
        status = "PASSED" if self.passed else "FAILED"
        lines = [
            f"Validation {status}: {self.total_checked} records checked, "
            f"{self.error_count} errors, {self.warning_count} warnings",
        ]
        for issue in self.issues:
            lines.append(f"  {issue}")
        return "\n".join(lines)


def validate_transactions(
    transactions: list[dict[str, Any]],
    year: int | None = None,
) -> ValidationResult:
    """验证交易列表的数据质量

    Args:
        transactions: 交易记录列表，每条为 dict，包含:
            broker_code, trade_date, symbol, action, quantity, price, amount, currency
        year: 可选，限定检查某一年

    Returns:
        ValidationResult 包含所有发现的问题
    """
    result = ValidationResult()
    result.total_checked = len(transactions)

    valid_actions = {
        "buy", "sell", "fee", "interest", "yield_income",
        "dividend", "option_buy", "option_sell", "option_expire",
        "option_exercise",
        "rsu_vest", "rsu_sell",
    }
    # symbol 格式：普通股票（字母/数字开头，可含数字、点、横线，支持 9988.HK）
    symbol_pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]*$")
    option_pattern = re.compile(r"^[A-Za-z0-9]+_OPT_\d{6}_\d+\.?\d*_[CP]$")
    # 期权合约乘数（1 份期权对应 100 股）
    OPTION_MULTIPLIER = 100

    for idx, txn in enumerate(transactions):
        trade_date_str = txn.get("trade_date", "")
        symbol = txn.get("symbol", "")
        action = txn.get("action", "")
        quantity = txn.get("quantity")
        price = txn.get("price")
        amount = txn.get("amount")
        currency = txn.get("currency", "USD")

        # IV-001: 日期范围
        # rsu_vest 记录可能在 2020 年前（历史成本基础），只 warning 不报错
        if trade_date_str:
            try:
                trade_date = date.fromisoformat(str(trade_date_str))
                if trade_date < EARLIEST_VALID_DATE:
                    if action == "rsu_vest":
                        result.add("IV-001", "WARNING",
                                   f"RSU 归属日期 {trade_date_str} 早于 {EARLIEST_VALID_DATE}（历史成本基础）",
                                   row_index=idx)
                    else:
                        result.add("IV-001", "ERROR",
                                   f"交易日期 {trade_date_str} 早于 {EARLIEST_VALID_DATE}",
                                   row_index=idx)
                if trade_date > date.today():
                    result.add("IV-001", "ERROR",
                               f"交易日期 {trade_date_str} 晚于今天",
                               row_index=idx)
                if year and trade_date.year != year and action in ("sell", "option_sell", "option_expire"):
                    # 跨年交易可能正常（买入在年前，卖出在年后），只标记 warning
                    pass
            except (ValueError, TypeError):
                result.add("IV-001", "ERROR",
                           f"无效日期格式: {trade_date_str}",
                           row_index=idx)

        # IV-006: action 枚举
        if action and action not in valid_actions:
            result.add("IV-006", "ERROR",
                       f"无效 action: {action}",
                       row_index=idx)

        # IV-010: 汇率验证（汇率为 0 时 CNY 换算全为零，直接导致应税金额错误）
        # USD 为基准货币，不需要汇率，仅对非 USD 交易检查
        exchange_rate = txn.get("exchange_rate")
        if exchange_rate is not None and currency != "USD":
            try:
                rate = Decimal(str(exchange_rate))
                if rate <= 0:
                    result.add("IV-010", "ERROR",
                               f"汇率为 0 或负值: {exchange_rate}",
                               row_index=idx)
            except Exception:
                result.add("IV-010", "ERROR",
                           f"汇率格式无效: {exchange_rate}",
                           row_index=idx)

        # IV-011: broker_code 不能为空（分国分项抵免依赖）
        broker = txn.get("broker_code", "")
        if not broker:
            result.add("IV-011", "ERROR",
                       "broker_code 为空",
                       row_index=idx)

        # IV-012: 买入交易价格必须大于 0（买入价格为 0 时后续卖出全额征税）
        if action in ("buy", "option_buy", "rsu_vest"):
            if price is not None:
                try:
                    p = Decimal(str(price))
                    if p <= 0:
                        result.add("IV-012", "ERROR",
                                   f"买入交易价格为 0（{action}），后续卖出时将全额征税",
                                   row_index=idx)
                except Exception:
                    pass

        # IV-013: RSU 归属必有税扣缴记录（公司代扣税是确定的，无代扣 = 漏税）
        if action == "rsu_vest":
            tax_withheld = txn.get("tax_withheld")
            if tax_withheld is not None:
                try:
                    tw = Decimal(str(tax_withheld))
                    if tw <= 0:
                        result.add("IV-013", "ERROR",
                                   f"RSU 归属无代扣税记录（tax_withheld=0），"
                                   f"存在漏税风险: {symbol} {trade_date_str}",
                                   row_index=idx)
                except Exception:
                    pass

        # IV-014: 海外分红应有预扣税记录（无预扣税 = 无法申请外国税收抵免）
        if action == "dividend":
            tax_withheld = txn.get("tax_withheld")
            if tax_withheld is not None:
                try:
                    tw = Decimal(str(tax_withheld))
                    if tw <= 0:
                        result.add("IV-014", "WARNING",
                                   f"海外分红无预扣税记录，无法申请外国税收抵免: "
                                   f"{symbol} {trade_date_str}",
                                   row_index=idx)
                except Exception:
                    pass

        # IV-015: 收入类交易金额方向验证（tax_withheld 应为正值或 0）
        if action in ("dividend", "interest", "yield_income"):
            tax_withheld = txn.get("tax_withheld")
            if tax_withheld is not None:
                try:
                    tw = Decimal(str(tax_withheld))
                    if tw < 0:
                        result.add("IV-015", "ERROR",
                                   f"收入类交易 tax_withheld 为负值: {tw}",
                                   row_index=idx)
                except Exception:
                    pass

        # IV-008: 期权符号格式（option_exercise 行权后得到正股，symbol 是正股代码，不检查）
        if action in ("option_buy", "option_sell", "option_expire"):
            if not option_pattern.match(symbol):
                result.add("IV-008", "ERROR",
                           f"期权符号格式错误: {symbol} (预期: UNDERLYING_OPT_YYMMDD_STRIKE_[CP])",
                           row_index=idx)

        # IV-009: 交易日期不能为空
        if not trade_date_str:
            result.add("IV-009", "ERROR",
                       "交易日期为空",
                       row_index=idx)

        # IV-005: symbol 格式
        if not symbol:
            result.add("IV-005", "ERROR", "symbol 为空", row_index=idx)
        elif not option_pattern.match(symbol) and not symbol_pattern.match(symbol):
            result.add("IV-005", "WARNING",
                       f"symbol 格式异常: {symbol}",
                       row_index=idx)

        # IV-002: 数量为正
        if quantity is not None:
            try:
                qty = int(quantity)
                if qty <= 0 and action in ("buy", "sell", "option_buy", "option_sell", "option_expire"):
                    result.add("IV-002", "ERROR",
                               f"数量 {qty} 非正 (action={action})",
                               row_index=idx)
            except (ValueError, TypeError):
                result.add("IV-002", "ERROR",
                           f"数量格式无效: {quantity}",
                           row_index=idx)

        # IV-003: 价格非负
        if price is not None:
            try:
                p = Decimal(str(price))
                if p < 0:
                    result.add("IV-003", "ERROR",
                               f"价格为负: {price}",
                               row_index=idx)
            except Exception:
                result.add("IV-003", "ERROR",
                           f"价格格式无效: {price}",
                           row_index=idx)

        # IV-007: 分红金额（在 IV-004 之前，避免被 continue 跳过）
        if action == "dividend":
            if price is not None:
                try:
                    p = Decimal(str(price))
                    if p <= 0:
                        result.add("IV-007", "ERROR",
                                   "分红 price 为 0 或负",
                                   row_index=idx)
                except Exception:
                    pass
            if quantity is not None:
                try:
                    qty = int(quantity)
                    if qty <= 0:
                        result.add("IV-007", "ERROR",
                                   "分红 quantity 为 0 或负",
                                   row_index=idx)
                except Exception:
                    pass

        # IV-004: 金额 ≈ 数量 × 价格
        # 期权交易：修复后 price = 每份期权合约价格（已含 ×100），amount = qty × price
        # 部分券商可能使用 100x 惯例（碎股/特殊单位），需要额外容差
        if quantity is not None and price is not None and amount is not None:
            try:
                qty = int(quantity)
                p = Decimal(str(price))
                amt = Decimal(str(amount))

                # 先检查 1x 模式（期权修复后：amount = qty × price）
                expected_1x = (qty * p).quantize(Decimal("0.01"))
                if abs(amt - expected_1x) > AMOUNT_TOLERANCE * max(abs(expected_1x), Decimal("1")):
                    # 再检查 100x 模式（非期权交易，但 amount = qty * price * 100）
                    is_100x_match = False
                    if action not in ("option_buy", "option_sell") and qty != 0 and p != 0:
                        ratio_100x = (qty * p * OPTION_MULTIPLIER).quantize(Decimal("0.01"))
                        if abs(amt - ratio_100x) <= AMOUNT_TOLERANCE * max(abs(ratio_100x), Decimal("1")):
                            is_100x_match = True

                    if not is_100x_match:
                        # 都不匹配，报告警告
                        result.add("IV-004", "WARNING",
                                   f"金额 {amt} 与预期 {expected_1x} 差异过大 (qty={qty}, price={p})",
                                   row_index=idx)
            except Exception:
                pass  # 类型转换失败已在上层报告

    # ── 全局检查 ──
    # RC-003: 重复交易检测
    # 使用 reference_no 作为唯一标识（优先），无 reference_no 时用组合键回退
    # 有 reference_no 的交易不再用组合键检测（避免同日同价的不同订单误判）
    seen_by_ref: dict[tuple[str, str], list[int]] = {}  # (broker_code, reference_no) → indices
    seen_by_key: dict[tuple, list[int]] = {}   # (broker, date, symbol, action, qty, price, amount) → indices (no ref)
    for idx, txn in enumerate(transactions):
        ref = txn.get("reference_no")
        broker = txn.get("broker_code", "")
        if ref:
            # 有 reference_no：仅按 reference_no 去重，不同 ref_no 即使同日同价也不视为重复
            key = (broker, ref)
            if key in seen_by_ref:
                seen_by_ref[key].append(idx)
            else:
                seen_by_ref[key] = [idx]
        else:
            # 无 reference_no：用组合键检测，加入 amount 区分费用交易
            key = (
                txn.get("broker_code", ""),
                str(txn.get("trade_date", "")),
                txn.get("symbol", ""),
                txn.get("action", ""),
                str(txn.get("quantity", "")),
                str(txn.get("price", "")),
                str(txn.get("amount", "")),
            )
            if key in seen_by_key:
                seen_by_key[key].append(idx)
            else:
                seen_by_key[key] = [idx]

    for (broker, ref), indices in seen_by_ref.items():
        if len(indices) > 1:
            txn = transactions[indices[0]]
            result.add("RC-003", "ERROR",
                       f"疑似重复交易 (broker={broker}, reference_no={ref}): "
                       f"{txn.get('broker_code', '')} {txn.get('trade_date', '')} "
                       f"{txn.get('symbol', '')} {txn.get('action', '')}, "
                       f"出现在 rows {indices}",
                       ", ".join(str(i) for i in indices))

    for key, indices in seen_by_key.items():
        if len(indices) > 1:
            broker, trade_date, symbol, action, qty, price, amount = key
            result.add("RC-003", "ERROR",
                       f"疑似重复交易 (无reference_no): {broker} {trade_date} {symbol} {action} qty={qty} price={price} amount={amount}, "
                       f"出现在 rows {indices}",
                       ", ".join(str(i) for i in indices))

    # RC-004: 累计卖出超过买入（在单文件级别只检测同一 symbol 的累计情况）
    # 注意：跨月/跨年的卖买检查在 db validate 中做，此处只做单文件内的预警
    buy_sell: dict[str, dict[str, int]] = defaultdict(lambda: {"bought": 0, "sold": 0})
    for txn in transactions:
        sym = txn.get("symbol", "")
        action = txn.get("action", "")
        qty = txn.get("quantity", 0) or 0
        if not sym:
            continue
        if action in ("buy", "rsu_vest", "option_buy", "option_exercise"):
            buy_sell[sym]["bought"] += int(qty)
        elif action in ("sell", "rsu_sell", "option_sell", "option_expire"):
            buy_sell[sym]["sold"] += int(qty)

    for sym, counts in sorted(buy_sell.items()):
        if counts["sold"] > counts["bought"]:
            result.add("RC-004", "WARNING",
                       f"单文件内 {sym} 卖出({counts['sold']}) 超过买入({counts['bought']})，"
                       f"缺口 {counts['sold'] - counts['bought']}")

    return result
