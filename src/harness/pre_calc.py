"""预计算就绪检查 — 确保一键算税所需的前置条件全部满足

对应 Harness 规则：
  PC-001: 年末汇率存在性
  PC-002: 月结单完整性
  PC-003: FIFO 缺口检测
  PC-004: Phantom lot 风险
  PC-005: 期权生命周期完整性
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from collections import defaultdict

from src.calculator.exchange_rate import get_exchange_rate
from datetime import date

DB_PATH = Path("output") / "tax.db"


@dataclass
class PreCalcIssue:
    rule_id: str
    severity: str  # "ERROR" | "WARNING"
    message: str
    details: str = ""

    def __str__(self) -> str:
        return f"[{self.severity}] {self.rule_id}: {self.message}"


@dataclass
class PreCalcReport:
    issues: list[PreCalcIssue] = field(default_factory=list)
    passed: bool = True
    stats: dict[str, str] = field(default_factory=dict)

    def add(self, rule_id: str, severity: str, message: str, details: str = ""):
        self.issues.append(PreCalcIssue(rule_id, severity, message, details))
        if severity == "ERROR":
            self.passed = False

    def summary(self) -> str:
        lines = ["=" * 60, "  预计算就绪检查报告", "=" * 60]
        if self.stats:
            lines.append("")
            for k, v in self.stats.items():
                lines.append(f"  {k}: {v}")
        lines.append("")
        for issue in self.issues:
            lines.append(f"  {issue}")
        status = "✅ 就绪" if self.passed else "❌ 存在阻断项"
        lines.append(f"\n  就绪状态: {status}")
        lines.append("=" * 60)
        return "\n".join(lines)


def check_pre_calc_readiness(
    db_path: Path | None = None,
    year: int = 2025,
    usd_cny: float | None = None,
) -> PreCalcReport:
    """运行预计算就绪检查

    Args:
        db_path: 数据库路径
        year: 计税年度
        usd_cny: 用户指定的 USD/CNY 汇率（如提供则跳过 PC-001 检查）

    Returns:
        PreCalcReport
    """
    report = PreCalcReport()
    path = db_path or DB_PATH

    if not path.exists():
        report.add("PC-000", "ERROR", f"数据库不存在: {path}")
        return report

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row

    _check_year_end_rate(conn, report, year, usd_cny)
    _check_statement_completeness(conn, report, year)
    _check_fifo_gaps(conn, report, year)
    _check_phantom_lots(conn, report, year)
    _check_option_lifecycle(conn, report, year)

    conn.close()
    return report


def _check_year_end_rate(conn, report: PreCalcReport, year: int, usd_cny: float | None):
    """PC-001: 年末汇率存在性

    检查 exchange_rates 表中是否有该年 12-31 的 USD/CNY 汇率，
    或用户已通过 --usd-cny 提供。
    """
    if usd_cny is not None:
        report.stats["年末汇率"] = f"{usd_cny}（用户提供）"
        return

    row = conn.execute("""
        SELECT rate FROM exchange_rates
        WHERE from_currency = 'USD' AND to_currency = 'CNY'
          AND date = ?
    """, (f"{year}-12-31",)).fetchone()

    if row:
        report.stats["年末汇率"] = f"{row['rate']}（来自 exchange_rates 表）"
    else:
        # 检查当年是否有任意汇率数据
        any_rate = conn.execute("""
            SELECT date, rate FROM exchange_rates
            WHERE from_currency = 'USD' AND to_currency = 'CNY'
              AND date LIKE ?
            ORDER BY date DESC LIMIT 1
        """, (f"{year}-%",)).fetchone()

        if any_rate:
            report.add(
                "PC-001", "WARNING",
                f"缺少 {year}-12-31 年末汇率，使用当年最新汇率 {any_rate['date']} = {any_rate['rate']}",
            )
            report.stats["年末汇率"] = f"{any_rate['rate']}（{any_rate['date']} 回退）"
        else:
            # 尝试通过 exchange_rate 模块获取（可能从 CSV 读取）
            try:
                rate = get_exchange_rate(date(year, 12, 31), "USD", year=year)
                if rate > 0:
                    report.add(
                        "PC-001", "WARNING",
                        f"exchange_rates 表无 {year}-12-31 汇率，使用 CSV 默认值 {rate}",
                    )
                    report.stats["年末汇率"] = f"{rate}（CSV 回退）"
                else:
                    report.add(
                        "PC-001", "ERROR",
                        f"无法获取 {year} 年末汇率，请提供 --usd-cny 参数或更新 exchange_rates 表",
                    )
            except Exception:
                report.add(
                    "PC-001", "ERROR",
                    f"无法获取 {year} 年末汇率，请提供 --usd-cny 参数或更新 exchange_rates 表",
                )


def _check_statement_completeness(conn, report: PreCalcReport, year: int):
    """PC-002: 月结单完整性

    检查目标年度各券商的月结单导入情况，记录缺失月份。
    """
    rows = conn.execute("""
        SELECT broker_code, statement_month, status,
               (SELECT COUNT(*) FROM transactions t WHERE t.statement_file_id = sf.id) as txn_count
        FROM statement_files sf
        WHERE statement_month LIKE ?
        ORDER BY broker_code, statement_month
    """, (f"{year}-%",)).fetchall()

    if not rows:
        report.add(
            "PC-002", "ERROR",
            f"{year} 年无任何月结单导入记录",
        )
        return

    # 按券商统计
    broker_months: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        broker_months[r["broker_code"]].append(r["statement_month"])

    for broker, months in sorted(broker_months.items()):
        month_nums = sorted(set(int(m.split("-")[1]) for m in months))
        missing = [m for m in range(1, 13) if m not in month_nums]
        status = f"{broker}: {len(months)} 个月 ({', '.join(str(m) for m in month_nums)})"
        if missing:
            status += f"，缺失: {', '.join(str(m) for m in missing)}月"
            report.add(
                "PC-002", "WARNING",
                f"{broker} 月结单缺失 {len(missing)} 个月: {', '.join(str(m) for m in missing)}月",
            )
        report.stats[f"{broker} 月结单"] = status

    # 总体统计
    total_files = len(rows)
    total_txns = sum(r["txn_count"] for r in rows)
    report.stats["月结单总计"] = f"{total_files} 个文件, {total_txns} 笔交易"


def _check_fifo_gaps(conn, report: PreCalcReport, year: int):
    """PC-003: FIFO 缺口检测

    按 symbol 扫描买入 vs 卖出（含上年结转），标记 sell > buy + carryforward 的缺口。
    """
    buy_actions = ("buy", "option_buy", "option_exercise", "rsu_vest")
    sell_actions = ("sell", "option_sell", "rsu_sell", "option_expire")

    rows = conn.execute("""
        SELECT symbol, action,
               COALESCE(SUM(quantity), 0) as total_qty
        FROM transactions
        WHERE strftime('%Y', trade_date) = ?
          AND action IN (?, ?, ?, ?, ?, ?, ?, ?)
        GROUP BY symbol, action
    """, (str(year), *buy_actions, *sell_actions)).fetchall()

    by_symbol: dict[str, dict[str, int]] = defaultdict(lambda: {"bought": 0, "sold": 0})
    for r in rows:
        if r["action"] in buy_actions:
            by_symbol[r["symbol"]]["bought"] += r["total_qty"]
        elif r["action"] in sell_actions:
            by_symbol[r["symbol"]]["sold"] += r["total_qty"]

    # 获取上年结转持仓
    carryforward: dict[str, int] = defaultdict(int)
    cf_rows = conn.execute("""
        SELECT symbol, SUM(remaining) as total_remaining
        FROM tax_lots
        WHERE acquisition_type = 'carryforward'
          AND remaining > 0
        GROUP BY symbol
    """).fetchall()
    for r in cf_rows:
        carryforward[r["symbol"]] = r["total_remaining"]

    gaps = []
    for sym, counts in sorted(by_symbol.items()):
        bought = counts["bought"]
        sold = counts["sold"]
        carry = carryforward.get(sym, 0)
        if sold > bought + carry:
            deficit = sold - bought - carry
            gaps.append((sym, bought, sold, carry, deficit))

    if gaps:
        for sym, bought, sold, carry, deficit in gaps:
            report.add(
                "PC-003", "WARNING",
                f"{sym} FIFO 缺口: 卖出 {sold} > 买入 {bought} + 结转 {carry}，缺口 {deficit} 股",
            )
        report.stats["FIFO 缺口"] = f"{len(gaps)} 个 symbol 存在缺口"
    else:
        report.stats["FIFO 缺口"] = "无"


def _check_phantom_lots(conn, report: PreCalcReport, year: int):
    """PC-004: Phantom lot 风险

    检测 $0 成本或 gap_fill 类型的 tax_lot，这些会导致卖出全额征税。
    """
    rows = conn.execute("""
        SELECT symbol, acquisition_type, quantity, remaining, cost_per_share
        FROM tax_lots
        WHERE (acquisition_type = 'carryforward' OR cost_per_share = 0)
          AND remaining > 0
        ORDER BY symbol, acquisition_date
    """).fetchall()

    zero_cost = [r for r in rows if float(r["cost_per_share"]) == 0]
    gap_fill = [r for r in rows if r["acquisition_type"] == "gap_fill"]

    if zero_cost:
        for r in zero_cost:
            report.add(
                "PC-004", "WARNING",
                f"{r['symbol']} 存在 $0 成本批次: {r['quantity']} 股（{r['acquisition_type']}）",
            )

    if gap_fill:
        report.add(
            "PC-004", "WARNING",
            f"共 {len(gap_fill)} 个 gap_fill 类型批次（时序缺口自动补充）",
        )

    if not zero_cost and not gap_fill:
        report.stats["Phantom lot 风险"] = "无"
    else:
        report.stats["Phantom lot 风险"] = (
            f"{len(zero_cost)} 个 $0 成本批次, {len(gap_fill)} 个 gap_fill"
        )


def _check_option_lifecycle(conn, report: PreCalcReport, year: int):
    """PC-005: 期权生命周期完整性

    检查每个期权 symbol 的买入 vs 卖出/过期/行权数量是否匹配。
    """
    rows = conn.execute("""
        SELECT symbol,
               COALESCE(SUM(CASE WHEN action IN ('option_buy', 'buy') THEN quantity ELSE 0 END), 0) as bought,
               COALESCE(SUM(CASE WHEN action IN ('sell', 'option_sell') THEN quantity ELSE 0 END), 0) as sold,
               COALESCE(SUM(CASE WHEN action = 'option_expire' THEN quantity ELSE 0 END), 0) as expired,
               COALESCE(SUM(CASE WHEN action = 'option_exercise' THEN quantity ELSE 0 END), 0) as exercised
        FROM transactions
        WHERE strftime('%Y', trade_date) = ?
          AND symbol LIKE '%OPT%'
        GROUP BY symbol
        HAVING bought > 0 OR sold > 0 OR expired > 0 OR exercised > 0
    """, (str(year),)).fetchall()

    issues = 0
    for r in rows:
        bought = r["bought"]
        disposed = r["sold"] + r["expired"] + r["exercised"]
        if disposed > bought:
            issues += 1
            report.add(
                "PC-005", "WARNING",
                f"{r['symbol']} 期权处置 {disposed} > 买入 {bought}（卖出 {r['sold']} + 过期 {r['expired']} + 行权 {r['exercised']}）",
            )
        elif bought > disposed:
            remaining = bought - disposed
            report.stats[f"{r['symbol']} 期权"] = (
                f"买入 {bought}, 处置 {disposed}, 剩余 {remaining}（可能跨年持仓）"
            )
        else:
            report.stats[f"{r['symbol']} 期权"] = (
                f"买入 {bought} = 处置 {disposed}（生命周期完整）"
            )

    if issues == 0:
        report.stats["期权生命周期"] = "完整" if rows else "无期权交易"
    else:
        report.stats["期权生命周期"] = f"{issues} 个 symbol 存在缺口"
