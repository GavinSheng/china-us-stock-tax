"""数据库级税务合规验证 — 跨文件、跨券商的全局检查

对应 Harness 规则：
  RC-005: 期权生命周期完整性
  RC-006: 税 lot 完整性
  RC-007: 全局累计卖出 > 买入
  RC-008: 卖出交易的汇率有效性
  RC-009: 分红收入类型与税率一致性

用法:
    from src.harness.db_validators import validate_database
    result = validate_database("output/tax.db")
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any


@dataclass
class ValidationIssue:
    rule_id: str
    severity: str  # "ERROR" | "WARNING"
    message: str
    details: str = ""

    def __str__(self) -> str:
        return f"[{self.severity}] {self.rule_id}: {self.message}"


@dataclass
class ValidationResult:
    issues: list[ValidationIssue] = field(default_factory=list)
    passed: bool = True
    error_count: int = 0
    warning_count: int = 0

    def add(self, rule_id: str, severity: str, message: str, details: str = ""):
        issue = ValidationIssue(rule_id, severity, message, details)
        self.issues.append(issue)
        if severity == "ERROR":
            self.error_count += 1
            self.passed = False
        else:
            self.warning_count += 1

    def summary(self) -> str:
        status = "PASSED" if self.passed else "FAILED"
        lines = [f"DB Validation {status}: {self.error_count} errors, {self.warning_count} warnings"]
        for issue in self.issues:
            lines.append(f"  {issue}")
        return "\n".join(lines)


def validate_database(db_path: str | Path) -> ValidationResult:
    """对数据库进行全局税务合规验证

    检查项覆盖：
    - 期权生命周期完整性（有卖必有买）
    - 税 lot 与交易的一致性
    - 累计卖买缺口
    - 卖出交易的汇率有效性
    - 分红税率一致性
    """
    path = Path(db_path) if db_path else Path(__file__).parent.parent.parent / "output" / "tax.db"
    if not path.exists():
        result = ValidationResult()
        result.add("RC-DB-001", "ERROR", f"数据库不存在: {path}")
        return result

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    result = ValidationResult()

    # ── RC-005: 期权生命周期完整性 ──
    # 每个有 option_sell/exercise/expire 的期权，必须有对应的 option_buy
    rows = conn.execute("""
        SELECT symbol,
            COALESCE(SUM(CASE WHEN action='option_buy' THEN quantity ELSE 0 END), 0) as bought,
            COALESCE(SUM(CASE WHEN action='option_sell' THEN quantity ELSE 0 END), 0) as sold,
            COALESCE(SUM(CASE WHEN action='option_expire' THEN quantity ELSE 0 END), 0) as expired,
            COALESCE(SUM(CASE WHEN action='option_exercise' THEN quantity ELSE 0 END), 0) as exercised
        FROM transactions
        WHERE action IN ('option_buy','option_sell','option_expire','option_exercise')
        GROUP BY symbol
    """).fetchall()

    for r in rows:
        total_out = r["sold"] + r["expired"] + r["exercised"]
        if total_out > r["bought"]:
            result.add("RC-005", "ERROR",
                       f"期权 {r['symbol']} 生命周期不完整: "
                       f"买入 {r['bought']}，卖出+过期+行权 {total_out} "
                       f"(sell={r['sold']}, expire={r['expired']}, exercise={r['exercised']})，"
                       f"缺口 {total_out - r['bought']}")

    # ── RC-006: 税 lot 完整性 ──
    # 每个 tax_lot 的 source_txn_id 必须对应存在的 transaction
    rows = conn.execute("""
        SELECT tl.id, tl.symbol, tl.acquisition_type, tl.source_txn_id
        FROM tax_lots tl
        WHERE tl.source_txn_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM transactions t WHERE t.id = tl.source_txn_id
          )
    """).fetchall()

    for r in rows:
        result.add("RC-006", "ERROR",
                   f"税 lot {r['id']} ({r['symbol']}, {r['acquisition_type']}) "
                   f"引用的交易 source_txn_id={r['source_txn_id']} 不存在")

    # ── RC-007: 全局累计卖出 > 买入 ──
    rows = conn.execute("""
        SELECT symbol,
            COALESCE(SUM(CASE WHEN action IN ('buy', 'rsu_vest', 'option_exercise', 'option_buy')
                              THEN quantity ELSE 0 END), 0) as bought,
            COALESCE(SUM(CASE WHEN action IN ('sell', 'rsu_sell', 'option_sell', 'option_expire')
                              THEN quantity ELSE 0 END), 0) as sold
        FROM transactions
        GROUP BY symbol
    """).fetchall()

    for r in rows:
        if r["sold"] > r["bought"]:
            result.add("RC-007", "ERROR",
                       f"{r['symbol']} 累计卖出({r['sold']}) > 买入({r['bought']})，"
                       f"缺口 {r['sold'] - r['bought']}")

    # ── RC-008: 卖出/过期交易汇率为 0（NULL 可在 calc-db 时补填，但 0 一定是问题）
    rows = conn.execute("""
        SELECT id, symbol, action, trade_date, exchange_rate
        FROM transactions
        WHERE action IN ('sell', 'rsu_sell', 'option_sell', 'option_expire')
          AND exchange_rate = 0
    """).fetchall()

    for r in rows:
        result.add("RC-008", "ERROR",
                   f"交易 {r['id']} ({r['symbol']}, {r['action']}, {r['trade_date']}) "
                   f"汇率无效: {r['exchange_rate']}")

    # ── RC-009: 期权行权创建的股票 lot 验证 ──
    # option_exercise 记录应在 FIFO 计算时正确创建 stock lot
    # 此处验证 option_exercise 的 raw_data 中包含 option_premium
    rows = conn.execute("""
        SELECT id, symbol, trade_date, raw_data, price
        FROM transactions
        WHERE action = 'option_exercise'
    """).fetchall()

    for r in rows:
        if not r["raw_data"]:
            result.add("RC-009", "WARNING",
                       f"期权行权 {r['id']} ({r['symbol']}, {r['trade_date']}) "
                       f"缺少 raw_data，无法验证成本 = 行权价 + 权利金")
        else:
            import json
            try:
                rd = json.loads(r["raw_data"])
                if "option_premium" not in rd:
                    result.add("RC-009", "WARNING",
                               f"期权行权 {r['id']} ({r['symbol']}, {r['trade_date']}) "
                               f"raw_data 中缺少 option_premium 字段")
            except (json.JSONDecodeError, TypeError):
                result.add("RC-009", "ERROR",
                           f"期权行权 {r['id']} ({r['symbol']}, {r['trade_date']}) "
                           f"raw_data 不是有效的 JSON")

    # ── RC-010: 分红/利息收入类型与税率一致性 ──
    rows = conn.execute("""
        SELECT id, symbol, action, trade_date, tax_withheld, amount, currency
        FROM transactions
        WHERE action = 'dividend' AND (tax_withheld IS NOT NULL AND tax_withheld > 0)
    """).fetchall()

    for r in rows:
        try:
            withheld = Decimal(str(r["tax_withheld"]))
            amount = Decimal(str(r["amount"]))
            if amount > 0:
                effective_rate = float(withheld / amount * 100)
                # 美股分红预扣税率通常为 30%（非居民）或 10%（税收协定）
                # 港股为 10%
                if r["currency"] == "USD" and effective_rate > 35:
                    result.add("RC-010", "WARNING",
                               f"分红 {r['id']} ({r['symbol']}, {r['trade_date']}) "
                               f"预扣税率 {effective_rate:.1f}% 异常偏高（USD 通常 30%）")
        except Exception:
            pass

    # ── RC-011: 无对应买入的卖出交易（孤立卖出） ──
    # 对每个 symbol 按时间排序，累计买入和卖出，检查是否在任何时间点卖出 > 买入
    rows = conn.execute("""
        SELECT id, symbol, action, trade_date, quantity
        FROM transactions
        WHERE action IN ('buy', 'sell', 'rsu_vest', 'rsu_sell', 'option_exercise')
          AND symbol NOT LIKE '%OPT_%'
        ORDER BY symbol, trade_date
    """).fetchall()

    cumulative: dict[str, int] = {}
    isolated_sells: list[tuple[str, str, int, int]] = []
    for r in rows:
        sym = r["symbol"]
        if sym not in cumulative:
            cumulative[sym] = 0
        if r["action"] in ("buy", "rsu_vest", "option_exercise"):
            cumulative[sym] += r["quantity"]
        elif r["action"] in ("sell", "rsu_sell"):
            cumulative[sym] -= r["quantity"]
            if cumulative[sym] < 0:
                isolated_sells.append((r["symbol"], r["trade_date"], r["quantity"], cumulative[sym]))

    if isolated_sells:
        for sym, dt, qty, neg_balance in isolated_sells[:10]:
            result.add("RC-011", "ERROR",
                       f"孤立卖出: {sym} {dt} 卖出 {qty} 股，累计持仓 {neg_balance} 股，"
                       f"缺少历史买入记录")
        if len(isolated_sells) > 10:
            result.add("RC-011", "WARNING",
                       f"另有 {len(isolated_sells) - 10} 笔孤立卖出（未列出）")

    # ── RC-012: 年度结转批次完整性 ──
    # 税务计算前必须存在 acquisition_type='carryforward' 的 tax_lots
    # 这些批次代表从上一年度结转的持仓，是 2025 年 FIFO 成本追溯的起点
    carryforward_count = conn.execute("""
        SELECT COUNT(*) as cnt FROM tax_lots
        WHERE acquisition_type = 'carryforward' AND remaining > 0
    """).fetchone()["cnt"]

    if carryforward_count == 0:
        result.add("RC-012", "ERROR",
                   "缺少年度结转持仓批次（carryforward tax_lots）"
                   "。在计算 2025 年税务前，必须先运行 carryforward 命令"
                   "或提供上年末持仓数据。"
                   "合规依据：中国个税按年计算，年初持仓 = 上年末持仓快照")
    else:
        # 检查 carryforward 批次是否覆盖了 2025 年有卖出的主要股票
        sold_symbols = conn.execute("""
            SELECT DISTINCT symbol FROM transactions
            WHERE action IN ('sell', 'rsu_sell', 'option_sell')
              AND strftime('%Y', trade_date) = '2025'
        """).fetchall()
        carryforward_symbols = conn.execute("""
            SELECT DISTINCT symbol FROM tax_lots
            WHERE acquisition_type = 'carryforward' AND remaining > 0
        """).fetchall()

        sold_set = {r["symbol"] for r in sold_symbols}
        cf_set = {r["symbol"] for r in carryforward_symbols}
        missing = sold_set - cf_set

        # 只检查非期权股票（期权可能当年买入当年卖出）
        stock_missing = [s for s in missing if "OPT_" not in s]
        if stock_missing:
            result.add("RC-012", "WARNING",
                       f"以下 2025 年有卖出的股票无结转持仓批次: {', '.join(stock_missing[:5])}。"
                       f"如果这些股票在 2024 年末有持仓，应补充 carryforward lot。")

    conn.close()
    return result
