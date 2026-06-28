#!/usr/bin/env python3
"""检测期权交易数据缺口：有卖出但无对应买入。

这通常意味着：
1. 买入在更早的月结单中，未被解析/导入
2. 买入在 2024 年末持仓中，未正确结转
3. PDF 解析遗漏了某些买入交易

对于有数据缺口的期权，不能正确计算成本基础，税额可能偏高或偏低。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

DB_PATH = Path("output") / "tax.db"


@dataclass
class OptionDataGap:
    symbol: str
    bought: int
    sold: int
    expired: int
    missing_buys: int       # sold - bought - expired
    sell_amount: float      # 卖出总金额
    buy_amount: float       # 买入总金额
    net_pnl: float          # 卖出金额 - 买入金额（不完整）


@dataclass
class OptionGapReport:
    gaps: list[OptionDataGap] = field(default_factory=list)
    has_gaps: bool = False

    def summary(self) -> str:
        if not self.gaps:
            return "✅ 期权交易数据完整，无缺失买入记录。"

        lines = [f"⚠️  发现 {len(self.gaps)} 个期权合约存在数据缺口："]
        for g in self.gaps:
            lines.append(f"\n  {g.symbol}:")
            lines.append(f"    买入: {g.bought} 份 (${g.buy_amount:,.2f})")
            lines.append(f"    卖出: {g.sold} 份 (${g.sell_amount:,.2f})")
            lines.append(f"    过期: {g.expired} 份")
            lines.append(f"    缺失买入: {g.missing_buys} 份")
            lines.append(f"    卖出金额 - 买入金额 = ${g.net_pnl:,.2f} (不完整，仅供参考)")
            lines.append(f"    ⚠️  缺少买入成本，无法正确计算盈亏。请检查早期月结单。")
        return "\n".join(lines)


def check_option_data_gaps(db_path: Path | None = None) -> OptionGapReport:
    """检查期权交易数据缺口"""
    path = db_path or DB_PATH
    report = OptionGapReport()

    if not path.exists():
        return report

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row

    # 找出所有 sold > bought + expired 的期权
    rows = conn.execute("""
        SELECT symbol,
               SUM(CASE WHEN action = 'option_buy' THEN quantity ELSE 0 END) as bought,
               SUM(CASE WHEN action = 'option_sell' THEN quantity ELSE 0 END) as sold,
               SUM(CASE WHEN action = 'option_expire' THEN quantity ELSE 0 END) as expired
        FROM transactions
        WHERE symbol LIKE '%OPT%'
        GROUP BY symbol
        HAVING sold > bought + expired
        ORDER BY symbol
    """).fetchall()

    for r in rows:
        missing = r['sold'] - r['bought'] - r['expired']
        sell_amount = conn.execute("""
            SELECT COALESCE(SUM(amount), 0) FROM transactions
            WHERE symbol = ? AND action = 'option_sell'
        """, (r['symbol'],)).fetchone()[0]
        buy_amount = conn.execute("""
            SELECT COALESCE(SUM(amount), 0) FROM transactions
            WHERE symbol = ? AND action = 'option_buy'
        """, (r['symbol'],)).fetchone()[0]

        gap = OptionDataGap(
            symbol=r['symbol'],
            bought=r['bought'],
            sold=r['sold'],
            expired=r['expired'],
            missing_buys=missing,
            sell_amount=float(sell_amount),
            buy_amount=float(buy_amount),
            net_pnl=float(sell_amount) - float(buy_amount),
        )
        report.gaps.append(gap)
        report.has_gaps = True

    conn.close()
    return report


def main():
    report = check_option_data_gaps()
    print(report.summary())
    raise SystemExit(1 if report.has_gaps else 0)


if __name__ == "__main__":
    main()
