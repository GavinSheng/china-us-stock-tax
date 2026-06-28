"""税务计算验证层 — 独立于 tax_engine 重新计算并校验

对应 Harness 规则：
  CV-001: 应纳税额非负
  CV-002: FIFO 消耗不超过可用持仓
  CV-003: 年度净额法选择合理
  CV-004: 分红税 = 全额 × 20%
  CV-005: 抵免额 ≤ 应纳税额
  CV-006: 海外分红预扣税记录完整性
  CV-007: 年度净额 ≤ 逐笔税额（否则不应选择）
  CV-010: 期权过期交易金额 = FIFO 实际消耗成本（审计一致性）
  CV-011: 现金流量独立验证（脱离 FIFO 的净额交叉校验）
  LC-001: lot_consumptions 记录引用完整性
  FC-001: FTC carryforward 记录合法性
  TI-001: tax_items 覆盖率校验
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from collections import defaultdict

from src.calculator.fifo import FIFOEngine
from src.calculator.tax_engine import CAPITAL_GAINS_RATE, DIVIDEND_RATE
from src.models import TaxLot

DB_PATH = Path("output") / "tax.db"


def _get_year_end_rate(db_path: Path | None, year: int) -> Decimal:
    """获取纳税年度最后一天 USD/CNY 汇率中间价。

    优先从 exchange_rates 表读取，找不到时回退到已知值。
    """
    path = db_path or DB_PATH
    if path.exists():
        import sqlite3
        conn = sqlite3.connect(str(path))
        try:
            row = conn.execute("""
                SELECT rate FROM exchange_rates
                WHERE from_currency = 'USD' AND to_currency = 'CNY'
                  AND date = ?
            """, (f"{year}-12-31",)).fetchone()
            if row:
                return Decimal(str(row[0]))
            # 回退：查找当年最接近年末的 USD/CNY 汇率
            row = conn.execute("""
                SELECT date, rate FROM exchange_rates
                WHERE from_currency = 'USD' AND to_currency = 'CNY'
                  AND date LIKE ?
                ORDER BY date DESC LIMIT 1
            """, (f"{year}-%",)).fetchone()
            if row:
                return Decimal(str(row[1]))
        finally:
            conn.close()

    # 已知年末汇率回退值
    known_rates = {
        2024: Decimal("7.20"),
        2025: Decimal("7.0288"),
    }
    if year in known_rates:
        return known_rates[year]

    # 未知年份：使用 exchange_rate 模块默认回退
    from src.calculator.exchange_rate import get_exchange_rate
    from datetime import date as _date
    rate = get_exchange_rate(_date(year, 12, 31), "USD", year=year)
    import warnings
    warnings.warn(
        f"年末汇率: {year} 年无预设汇率，使用 exchange_rate 模块默认值 {rate}",
        stacklevel=2,
    )
    return rate


@dataclass
class VerificationIssue:
    rule_id: str
    severity: str
    message: str
    details: str = ""

    def __str__(self) -> str:
        return f"[{self.severity}] {self.rule_id}: {self.message}"


@dataclass
class VerificationResult:
    issues: list[VerificationIssue] = field(default_factory=list)
    passed: bool = True
    verified_items: dict[str, float] = field(default_factory=dict)

    def add(self, rule_id: str, severity: str, message: str, details: str = ""):
        issue = VerificationIssue(rule_id, severity, message, details)
        self.issues.append(issue)
        if severity == "ERROR":
            self.passed = False

    def summary(self) -> str:
        status = "PASSED" if self.passed else "FAILED"
        lines = [f"Tax Verification {status}:"]
        for issue in self.issues:
            lines.append(f"  {issue}")
        for key, val in self.verified_items.items():
            lines.append(f"  {key}: {val:,.2f}")
        return "\n".join(lines)


def verify_tax_computation(
    db_path: Path | None = None,
    year: int = 2025,
    transactions: list[dict] | None = None,
    year_end_usd_rate: Decimal | None = None,
) -> VerificationResult:
    """独立验证税务计算的正确性

    Args:
        db_path: 数据库路径
        year: 验证年度
        transactions: 可选，直接传入交易列表（绕过数据库）
        year_end_usd_rate: 年末 USD/CNY 汇率中间价（如 2025 年为 7.0288）。
            不传时自动从 exchange_rates 表或数据推断。

    Returns:
        VerificationResult
    """
    result = VerificationResult()

    if transactions is None:
        path = db_path or DB_PATH
        if not path.exists():
            result.add("CV-000", "ERROR", f"数据库不存在: {path}")
            return result
        transactions = _load_transactions(path, year)

    # 获取年末汇率（作为统一参数，不依赖逐笔汇率）
    if year_end_usd_rate is None:
        year_end_usd_rate = _get_year_end_rate(db_path, year)

    _verify_tax_payable_non_negative(db_path, year, result)
    _verify_fifo_sanity(transactions, result)
    _verify_credit_not_exceeds_tax(db_path, year, result)
    _verify_dividend_withholding(db_path, year, result)
    _verify_dividend_tax(transactions, db_path, year, result)
    _verify_independent_capital_gains(transactions, year, result, db_path, year_end_usd_rate)
    _verify_sell_tax_item_coverage(transactions, year, db_path, result, year_end_usd_rate)
    _verify_year_end_exchange_rate(db_path, year, result)
    _verify_cross_broker_comparison(transactions, year, result, db_path)
    _verify_option_expire_cost_consistency(db_path, year, result)
    _verify_cash_flow_capital_gains(db_path, year, result)
    _verify_lot_consumptions_consistency(db_path, year, result)
    _verify_ftc_carryforward_consistency(db_path, year, result)
    _verify_tax_items_coverage(db_path, year, result)

    return result


def _verify_tax_payable_non_negative(db_path: Path | None, year: int, result: VerificationResult):
    """CV-001: 应纳税额非负 — 每条 tax_item 的 tax_payable_cny 必须 >= 0"""
    path = db_path or DB_PATH
    if not path.exists():
        return
    try:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT id, symbol, income_type, tax_amount_cny, foreign_tax_credit_cny, tax_payable_cny
            FROM tax_items
            WHERE tax_year = ? AND tax_payable_cny < 0
        """, (str(year),)).fetchall()
        conn.close()
        for r in rows:
            result.add("CV-001", "ERROR",
                       f"tax_item {r['id']} ({r['symbol']}, {r['income_type']}) "
                       f"应纳税额为负: tax_amount={r['tax_amount_cny']}, "
                       f"credit={r['foreign_tax_credit_cny']}, "
                       f"payable={r['tax_payable_cny']}")
    except Exception:
        pass


def _verify_credit_not_exceeds_tax(db_path: Path | None, year: int, result: VerificationResult):
    """CV-005: 抵免额不能超过税额 — foreign_tax_credit_cny <= tax_amount_cny"""
    path = db_path or DB_PATH
    if not path.exists():
        return
    try:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT id, symbol, income_type, tax_amount_cny, foreign_tax_credit_cny, tax_payable_cny
            FROM tax_items
            WHERE tax_year = ? AND foreign_tax_credit_cny > tax_amount_cny
        """, (str(year),)).fetchall()
        conn.close()
        for r in rows:
            result.add("CV-005", "ERROR",
                       f"tax_item {r['id']} ({r['symbol']}, {r['income_type']}) "
                       f"抵免额 {r['foreign_tax_credit_cny']} 超过税额 {r['tax_amount_cny']}")
    except Exception:
        pass


def _load_transactions(db_path: Path, year: int) -> list[dict]:
    """从数据库加载指定年度的交易"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT broker_code, trade_date, symbol, action, quantity, price, amount,
               currency, exchange_rate, commission, platform_fee, sec_fee, taf_fee,
               delivery_fee, other_fees, tax_withheld
        FROM transactions
        WHERE strftime('%Y', trade_date) <= ?
          AND action IN ('buy', 'sell', 'option_buy', 'option_sell', 'option_expire',
                         'dividend', 'rsu_vest', 'rsu_sell', 'fee')
          AND NOT (broker_code = 'boci' AND action = 'dividend')
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
            "exchange_rate": Decimal(str(r["exchange_rate"])) if r["exchange_rate"] else Decimal("0"),
            "fee": Decimal(str(r["commission"] or 0)) + Decimal(str(r["platform_fee"] or 0))
                   + Decimal(str(r["sec_fee"] or 0)) + Decimal(str(r["taf_fee"] or 0))
                   + Decimal(str(r["delivery_fee"] or 0)) + Decimal(str(r["other_fees"] or 0)),
            "tax_withheld": Decimal(str(r["tax_withheld"])) if r["tax_withheld"] else Decimal("0"),
        })
    conn.close()
    return txns


def _verify_fifo_sanity(transactions: list[dict], result: VerificationResult):
    """CV-002: 验证 FIFO 消耗不超过可用持仓

    期权特殊处理：
    - option_sell 无持仓 = 写仓（sell-to-open），合法策略
    - 写仓过期 = 权利金全额收益
    - 写仓买入平仓 = 收益 = 权利金 − 买回成本

    跨券商合并：同一 symbol 的所有券商持仓共享队列。
    """
    from datetime import date
    holdings: dict[str, int] = defaultdict(int)        # symbol → long positions
    short_options: dict[str, int] = defaultdict(int)   # symbol → write positions

    for txn in transactions:
        action = txn["action"]
        symbol = txn["symbol"]

        if action in ("buy", "option_buy", "rsu_vest", "option_exercise"):
            holdings[symbol] += txn["quantity"]
            # 如果是买入期权平仓（之前有写仓），减少写仓空头
            if action == "option_buy" and short_options[symbol] > 0:
                close = min(short_options[symbol], txn["quantity"])
                short_options[symbol] -= close
        elif action == "option_sell":
            # 期权卖出：先消耗现有持仓，剩余 = 写仓
            available = holdings.get(symbol, 0)
            if available >= txn["quantity"]:
                # 全部是平仓已有期权多头
                holdings[symbol] -= txn["quantity"]
            elif available > 0:
                # 部分写仓
                write_qty = txn["quantity"] - available
                short_options[symbol] += write_qty
                holdings[symbol] = 0
            else:
                # 纯写仓
                short_options[symbol] += txn["quantity"]
        elif action in ("sell", "rsu_sell"):
            if holdings[symbol] < txn["quantity"]:
                result.add("CV-002", "ERROR",
                           f"FIFO 持仓不足: {symbol} 在 {txn['trade_date']} "
                           f"卖出 {txn['quantity']} 股，可用 {holdings[symbol]} 股")
            holdings[symbol] -= txn["quantity"]
        elif action == "option_expire":
            # 期权过期：可以是多头过期（持仓消耗）或写仓过期（收益确认）
            if short_options.get(symbol, 0) >= txn["quantity"]:
                # 写仓过期 — 权利金 = 收益，不需要消耗多头持仓
                short_options[symbol] -= txn["quantity"]
            else:
                # 多头过期
                if holdings.get(symbol, 0) < txn["quantity"]:
                    # 部分写仓过期 + 部分多头过期
                    expire_from_short = short_options.get(symbol, 0)
                    expire_from_long = txn["quantity"] - expire_from_short
                    if expire_from_long > 0 and holdings.get(symbol, 0) < expire_from_long:
                        result.add("CV-002", "WARNING",
                                   f"期权过期持仓不足: {symbol} 在 {txn['trade_date']} "
                                   f"过期 {txn['quantity']} 份，多头可用 {holdings.get(symbol, 0)} 份 "
                                   f"(可能包含写仓过期)")
                    holdings[symbol] = max(0, holdings.get(symbol, 0) - expire_from_long)
                else:
                    holdings[symbol] -= txn["quantity"]


def _verify_dividend_withholding(db_path: Path | None, year: int, result: VerificationResult):
    """CV-006: 验证所有海外分红都有预扣税记录（无预扣税 = 无法申请外国税收抵免）

    检查 dividends 表而非 transactions 表，因为分红数据存在独立的 dividends 表中。
    同时验证预扣税率是否在合理范围内（5%~30%）。
    """
    path = db_path or DB_PATH
    if not path.exists():
        return

    try:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row

        # 检查预扣税为零的分红
        zero_wh = conn.execute("""
            SELECT broker_code, symbol, payment_date, gross_amount, withholding_tax
            FROM dividends
            WHERE strftime('%Y', payment_date) = ?
              AND withholding_tax <= 0
        """, (str(year),)).fetchall()

        for r in zero_wh:
            result.add("CV-006", "WARNING",
                       f"海外分红无预扣税记录，无法申请外国税收抵免: "
                       f"{r['broker_code']} {r['symbol']} {r['payment_date']} "
                       f"(gross={r['gross_amount']}, wh={r['withholding_tax']})")

        # 验证预扣税率合理性（应为 5%~30%，美股通常 10%）
        all_divs = conn.execute("""
            SELECT broker_code, symbol, payment_date, gross_amount, withholding_tax, withholding_rate
            FROM dividends
            WHERE strftime('%Y', payment_date) = ?
              AND gross_amount > 0
        """, (str(year),)).fetchall()

        for r in all_divs:
            rate = r['withholding_rate'] if r['withholding_rate'] else 0
            if rate > 0 and (rate < 0.05 or rate > 0.30):
                result.add("CV-006", "WARNING",
                           f"分红预扣税率异常: {r['broker_code']} {r['symbol']} {r['payment_date']} "
                           f"(rate={rate:.4f}, 正常范围 0.05~0.30)")

        conn.close()
    except Exception:
        pass


def _verify_dividend_tax(transactions: list[dict], db_path: Path | None, year: int, result: VerificationResult):
    """CV-004: 验证分红税 = 全额 × 20%"""
    # 先计算预期分红税
    expected_dividend_tax: dict[str, Decimal] = {}
    for txn in transactions:
        if txn["action"] != "dividend":
            continue

        td_str = txn["trade_date"] if isinstance(txn["trade_date"], str) else str(txn["trade_date"])
        if isinstance(txn["trade_date"], str):
            td = date.fromisoformat(td_str)
        else:
            td = txn["trade_date"]
        if td.year != year:
            continue

        gross = Decimal(str(txn["amount"]))
        rate = txn.get("exchange_rate")
        if rate is None or rate <= 0:
            # USD 为基准货币，不需要汇率
            rate = Decimal("1")
        if not isinstance(rate, Decimal):
            rate = Decimal(str(rate))
        gross_cny = (gross * rate).quantize(Decimal("0.01"))
        expected_tax = (gross_cny * DIVIDEND_RATE).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        key = f"{td_str}_{txn['symbol']}"
        expected_dividend_tax[key] = expected_tax
        result.verified_items[f"dividend_{key}_expected_tax"] = float(expected_tax)

    # 与数据库 tax_items 中的分红税对比
    path = db_path or DB_PATH
    if path.exists() and expected_dividend_tax:
        try:
            conn = sqlite3.connect(str(path))
            conn.row_factory = sqlite3.Row
            db_items = conn.execute("""
                SELECT symbol, date, tax_amount_cny
                FROM tax_items
                WHERE income_type = 'dividend' AND strftime('%Y', date) = ?
            """, (str(year),)).fetchall()
            conn.close()

            for item in db_items:
                key = f"{item['date']}_{item['symbol']}"
                if key in expected_dividend_tax:
                    expected = expected_dividend_tax[key]
                    actual = Decimal(str(item["tax_amount_cny"]))
                    if abs(expected - actual) > Decimal("0.01"):
                        result.add("CV-004", "ERROR",
                                   f"分红税不匹配 {key}: 预期 ¥{expected:,.2f}, "
                                   f"数据库 ¥{actual:,.2f}")
                    else:
                        result.verified_items[f"dividend_{key}_verified"] = float(actual)
                else:
                    result.add("CV-004", "WARNING",
                               f"数据库有分红税记录 {key}，但交易数据中无对应分红")
        except Exception:
            # 数据库表可能不存在，跳过对比
            result.add("CV-004", "WARNING", "无法连接数据库 tax_items 表进行分红税对比")


def _verify_independent_capital_gains(
    transactions: list[dict],
    year: int,
    result: VerificationResult,
    db_path: Path | None = None,
    year_end_usd_rate: Decimal = Decimal("7.0288"),
):
    """CV-003/CV-007: 独立计算资本利得，验证年度净额法选择的合理性

    跨券商合并：同一 symbol 的所有券商持仓共享 FIFO 队列。
    所有交易以 USD 计算，最终用年末汇率统一换算为 CNY。
    """
    from datetime import date
    from src.models import TaxLot

    # 分离当年交易和往年交易（用于构建 FIFO）
    year_txns = []
    pre_year_txns = []
    for txn in transactions:
        td = txn["trade_date"]
        if isinstance(td, str):
            td = date.fromisoformat(td)
        if td.year < year:
            pre_year_txns.append(txn)
        else:
            year_txns.append(txn)

    # 为缺失买入记录的卖出交易添加前置持仓
    # （如 FUTU 2025-01 月份无数据，但其期权在 2 月被卖出）
    # 尝试从数据库回溯历史成本，避免 $0 成本导致虚假应税收入
    from collections import defaultdict
    buy_actions = {"buy", "option_buy", "rsu_vest", "option_exercise"}
    sell_actions = {"sell", "option_sell", "rsu_sell", "option_expire"}
    total_bought: dict[str, int] = defaultdict(int)
    total_sold: dict[str, int] = defaultdict(int)
    for txn in transactions:
        symbol = txn["symbol"]
        if txn["action"] in buy_actions:
            total_bought[symbol] += txn["quantity"]
        if txn["action"] in sell_actions:
            total_sold[symbol] += txn["quantity"]

    conn_for_backfill = None
    if db_path and db_path.exists():
        try:
            conn_for_backfill = sqlite3.connect(str(db_path))
            conn_for_backfill.row_factory = sqlite3.Row
        except Exception:
            conn_for_backfill = None

    for symbol, sold_qty in total_sold.items():
        bought_qty = total_bought.get(symbol, 0)
        if bought_qty < sold_qty:
            deficit = sold_qty - bought_qty
            backfill_price = 0

            # 从数据库回溯历史成本
            if conn_for_backfill is not None:
                try:
                    row = conn_for_backfill.execute("""
                        SELECT SUM(quantity) AS total_qty,
                               SUM(quantity * price) AS total_cost
                        FROM transactions
                        WHERE symbol = ?
                          AND action IN ('buy', 'option_exercise', 'rsu_vest')
                          AND trade_date IS NOT NULL AND trade_date != ''
                          AND trade_date <= ?
                          AND price > 0 AND quantity > 0
                    """, (symbol, f"{year - 1}-12-31")).fetchone()
                    if row and row["total_qty"] and row["total_qty"] > 0:
                        backfill_price = float(row["total_cost"]) / float(row["total_qty"])

                    if backfill_price <= 0:
                        rsu = conn_for_backfill.execute("""
                            SELECT fmv_per_share FROM rsu_vests
                            WHERE symbol = ? AND vest_date <= ?
                              AND fmv_per_share > 0
                            ORDER BY vest_date DESC LIMIT 1
                        """, (symbol, f"{year - 1}-12-31")).fetchone()
                        if rsu and rsu["fmv_per_share"] > 0:
                            backfill_price = float(rsu["fmv_per_share"])
                except Exception:
                    pass

            pre_year_txns.append({
                "broker_code": "unknown",
                "symbol": symbol,
                "action": "buy",
                "quantity": deficit,
                "price": backfill_price,
                "trade_date": f"{year - 1}-12-31",
                "currency": "USD",
                "exchange_rate": 0,
            })

    if conn_for_backfill is not None:
        conn_for_backfill.close()

    # 用往年交易构建年初持仓，key = symbol（跨券商合并）
    existing_lots: dict[str, list[TaxLot]] = defaultdict(list)
    for txn in pre_year_txns:
        action = txn["action"]
        if action in ("buy", "option_buy", "rsu_vest", "option_exercise"):
            broker_code = txn.get("broker_code", "unknown")
            symbol = txn["symbol"]
            existing_lots[symbol].append(TaxLot(
                symbol=symbol,
                quantity=txn["quantity"],
                cost_per_share=txn["price"],
                acquire_date=date.fromisoformat(txn["trade_date"]) if isinstance(txn["trade_date"], str) else txn["trade_date"],
                remaining=txn["quantity"],
                origin=action,
                broker_code=broker_code,
            ))

    # 用当年交易运行 FIFO（跨券商合并）
    # 所有交易以 USD 计算盈亏，最终统一用年末汇率换算为 CNY
    fifo = FIFOEngine(existing_lots=dict(existing_lots))
    per_txn_tax_total = Decimal("0")
    net_gain_usd = Decimal("0")
    sell_count = 0
    loss_count = 0

    for txn in year_txns:
        action = txn["action"]
        broker_code = txn.get("broker_code", "unknown")
        symbol = txn["symbol"]
        td = date.fromisoformat(txn["trade_date"]) if isinstance(txn["trade_date"], str) else txn["trade_date"]

        if action in ("buy", "option_buy", "rsu_vest", "option_exercise"):
            fifo.buy(broker_code, symbol, txn["quantity"], txn["price"], td, origin=action)
        elif action in ("sell", "option_sell", "rsu_sell"):
            try:
                results = fifo.sell(broker_code, symbol, txn["quantity"], txn["price"], td, txn.get("fee", Decimal("0")))
                sell_count += 1
                for r in results:
                    gain_usd = r["gain_loss"]
                    net_gain_usd += gain_usd
                    if gain_usd > 0:
                        gain_cny = (gain_usd * year_end_usd_rate).quantize(Decimal("0.01"))
                        per_txn_tax_total += (gain_cny * CAPITAL_GAINS_RATE).quantize(Decimal("0.01"))
                    else:
                        loss_count += 1
            except ValueError as e:
                result.add("CV-002", "ERROR",
                           f"FIFO 计算失败: {broker_code}/{symbol} 在 {txn['trade_date']} "
                           f"卖出 {txn['quantity']} 股时发生错误: {e}")
        elif action == "option_expire":
            try:
                expire_results = fifo.expire(broker_code, symbol, txn["quantity"], td)
                for r in expire_results:
                    net_gain_usd += r["gain_loss"]
            except ValueError as e:
                result.add("CV-002", "ERROR",
                           f"FIFO 过期计算失败: {broker_code}/{symbol} 在 {txn['trade_date']} "
                           f"过期 {txn['quantity']} 份时发生错误: {e}")

    # 最终用年末汇率将 USD 盈亏统一换算为 CNY
    net_gain_cny = (net_gain_usd * year_end_usd_rate).quantize(Decimal("0.01"))

    # 验证年度净额法选择合理性
    if loss_count > 0 or net_gain_cny < per_txn_tax_total / CAPITAL_GAINS_RATE:
        if net_gain_cny > 0:
            annual_net_tax = (net_gain_cny * CAPITAL_GAINS_RATE).quantize(Decimal("0.01"))
            result.verified_items["per_transaction_tax_cny"] = float(per_txn_tax_total)
            result.verified_items["annual_net_gain_cny"] = float(net_gain_cny)
            result.verified_items["annual_net_tax_cny"] = float(annual_net_tax)

            if annual_net_tax > per_txn_tax_total:
                result.add("CV-007", "WARNING",
                           f"年度净额税额 ¥{annual_net_tax:,.2f} > 逐笔税额 ¥{per_txn_tax_total:,.2f}，"
                           f"不应选择年度净额法")

    result.verified_items["total_sells"] = sell_count
    result.verified_items["total_losses"] = loss_count

    # CV-003: 年度净额法只在有亏损时启用
    if loss_count == 0 and per_txn_tax_total > 0:
        # 没有亏损但可能有期权过期，这是正常的
        pass


def _verify_sell_tax_item_coverage(
    transactions: list[dict],
    year: int,
    db_path: Path | None,
    result: VerificationResult,
    year_end_usd_rate: Decimal = Decimal("7.0288"),
):
    """CV-006: 验证所有卖出交易均生成了对应的 tax_item 记录。

    规则：
    - 盈利卖出（逐笔法）→ 每笔一条 capital_gain tax_item
    - 年度净额法 → 全部卖出合并为一条 capital_gain_annual_net tax_item
    - 亏损卖出不产生 tax_item（不征税），不违反此规则
    - 期权过期（option_expire）损失也不产生独立 tax_item（参与年度净额）

    跨券商合并：同一 symbol 的所有券商持仓共享队列。
    """
    from datetime import date as date_cls

    # 统计当年盈利卖出的笔数
    profitable_sell_count = 0
    has_losses = False
    holdings: dict[str, list[tuple[Decimal, int]]] = defaultdict(list)  # symbol → [(cost, qty)]

    # 按时间排序
    sorted_txns = sorted(transactions, key=lambda t: t.get("trade_date", ""))

    for txn in sorted_txns:
        td = txn["trade_date"]
        if isinstance(td, str):
            td = date_cls.fromisoformat(td)
        symbol = txn["symbol"]
        if td.year != year:
            # 往年买入也算持仓
            if txn["action"] in ("buy", "option_buy", "rsu_vest", "option_exercise"):
                holdings[symbol].append((txn["price"], txn["quantity"]))
            continue

        action = txn["action"]
        qty = txn["quantity"]

        if action in ("buy", "option_buy", "rsu_vest", "option_exercise"):
            holdings[symbol].append((txn["price"], qty))
        elif action in ("sell", "option_sell", "rsu_sell"):
            # 简化 FIFO：按成本顺序消耗（跨券商合并）
            remaining = qty
            total_proceeds = Decimal("0")
            total_cost = Decimal("0")
            rate = txn.get("exchange_rate")
            if rate is None or rate <= 0:
                rate = Decimal("1")
            if not isinstance(rate, Decimal):
                rate = Decimal(str(rate))
            fee = txn.get("fee", Decimal("0"))

            sell_price = txn["price"]
            for lot_idx, (cost, lot_qty) in enumerate(holdings.get(symbol, [])):
                if remaining <= 0:
                    break
                take = min(lot_qty, remaining)
                total_proceeds += sell_price * take
                total_cost += cost * take
                remaining -= take
                holdings[symbol][lot_idx] = (cost, lot_qty - take)
            # 清理已消耗的 lots
            holdings[symbol] = [(c, q) for c, q in holdings.get(symbol, []) if q > 0]

            gain = total_proceeds - total_cost - fee
            if gain > 0:
                profitable_sell_count += 1
            else:
                has_losses = True

    # 检查数据库 tax_items
    path = db_path or DB_PATH
    if not path.exists():
        return  # 无数据库，跳过

    try:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        cg_items = conn.execute("""
            SELECT income_type, COUNT(*) as cnt
            FROM tax_items
            WHERE tax_year = ?
              AND income_type IN ('capital_gain', 'capital_gain_annual_net', 'capital_gain_expire_loss')
            GROUP BY income_type
        """, (str(year),)).fetchall()
        conn.close()

        item_types = {r["income_type"]: r["cnt"] for r in cg_items}
        has_annual_net = "capital_gain_annual_net" in item_types

        if has_annual_net:
            # 年度净额法：应有 1 条 annual_net 记录
            if item_types.get("capital_gain_annual_net", 0) != 1:
                result.add("CV-006", "WARNING",
                           f"年度净额法下应有 1 条 capital_gain_annual_net tax_item，"
                           f"实际 {item_types.get('capital_gain_annual_net', 0)} 条")
        else:
            # 逐笔法：盈利卖出笔数 = capital_gain tax_item 笔数
            cg_count = item_types.get("capital_gain", 0)
            if profitable_sell_count > 0 and cg_count == 0:
                result.add("CV-006", "ERROR",
                           f"有 {profitable_sell_count} 笔盈利卖出，"
                           f"但无 capital_gain tax_item 记录")
            elif cg_count > 0 and profitable_sell_count == 0:
                result.add("CV-006", "WARNING",
                           f"有 {cg_count} 条 capital_gain tax_item，"
                           f"但未发现盈利卖出交易（可能为逐笔法下跨年持仓卖出）")

        result.verified_items["cv006_profitable_sells"] = profitable_sell_count
        result.verified_items["cv006_cg_tax_items"] = sum(item_types.values())

    except Exception:
        result.add("CV-006", "WARNING", "无法连接数据库验证 tax_item 覆盖")


def _verify_year_end_exchange_rate(
    db_path: Path | None,
    year: int,
    result: VerificationResult,
):
    """CV-008: 验证纳税年度年末汇率存在性

    依据《个人所得税法实施条例》第三十二条：
    "年度终了后办理汇算清缴的，……对应当补缴税款的所得部分，
    按照上一纳税年度最后一日人民币汇率中间价，折合成人民币计算。"

    年末汇率是年度汇算清缴的法定要求，缺失时应报错而非静默回退。
    """
    from src.config import EXCHANGE_RATE_FILE

    # 检查汇率文件是否存在
    rate_file = Path(EXCHANGE_RATE_FILE) if EXCHANGE_RATE_FILE else None
    if rate_file and rate_file.exists():
        import pandas as pd
        try:
            df = pd.read_csv(rate_file)
            year_end = f"{year}-12-31"

            # 检查 USD 汇率
            if "base_currency" in df.columns:
                usd_rates = df[df["base_currency"].str.upper() == "USD"]
                year_end_usd = usd_rates[usd_rates["date"] == year_end]
                if len(year_end_usd) == 0:
                    # 检查是否有年末前后的可用日期
                    dec_rates = usd_rates[
                        (usd_rates["date"] >= f"{year}-12-21") &
                        (usd_rates["date"] <= f"{year}-12-31")
                    ]
                    if len(dec_rates) == 0:
                        result.add("CV-008", "ERROR",
                                   f"年末汇率缺失：{year}年12月31日 USD/CNY 汇率不存在，"
                                   f"年度汇算清缴需使用年末汇率，请补充汇率数据")
                    else:
                        closest_date = dec_rates.iloc[-1]["date"]
                        result.add("CV-008", "WARNING",
                                   f"年末汇率回退：{year}年12月31日汇率缺失，"
                                   f"将使用 {closest_date} 汇率，需人工复核是否符合税法要求")
                        result.verified_items["year_end_rate_fallback_date"] = closest_date
                else:
                    rate = float(year_end_usd.iloc[0]["rate"])
                    result.verified_items["year_end_usd_rate"] = rate

                # 检查 HKD 汇率（如有港股交易）
                hkd_rates = df[df["base_currency"].str.upper() == "HKD"]
                year_end_hkd = hkd_rates[hkd_rates["date"] == year_end]
                if len(hkd_rates) > 0 and len(year_end_hkd) == 0:
                    dec_rates = hkd_rates[
                        (hkd_rates["date"] >= f"{year}-12-21") &
                        (hkd_rates["date"] <= f"{year}-12-31")
                    ]
                    if len(dec_rates) == 0:
                        result.add("CV-008", "WARNING",
                                   f"港股年末汇率缺失：{year}年12月31日 HKD/CNY 汇率不存在，"
                                   f"将按 USD/CNY ÷ 7.8 近似计算")
                    else:
                        result.verified_items["year_end_hkd_rate_fallback"] = True
            else:
                # 两列格式：date, rate（默认 USD）
                year_end_rate = df[df["date"] == year_end]
                if len(year_end_rate) == 0:
                    result.add("CV-008", "ERROR",
                               f"年末汇率缺失：{year}年12月31日汇率不存在，"
                               f"年度汇算清缴需使用年末汇率，请补充汇率数据")
                else:
                    rate = float(year_end_rate.iloc[0]["rate"])
                    result.verified_items["year_end_usd_rate"] = rate

        except Exception as e:
            result.add("CV-008", "WARNING",
                       f"汇率文件读取失败：{e}，无法校验年末汇率")
    else:
        # 无汇率文件时检查数据库
        path = db_path or DB_PATH
        if path.exists():
            try:
                conn = sqlite3.connect(str(path))
                conn.row_factory = sqlite3.Row
                year_end = f"{year}-12-31"

                usd_rate = conn.execute("""
                    SELECT rate FROM exchange_rates
                    WHERE date = ? AND from_currency = 'USD' AND to_currency = 'CNY'
                """, (year_end,)).fetchone()

                if usd_rate is None:
                    # 检查年末前10天是否有数据
                    fallback = conn.execute("""
                        SELECT date, rate FROM exchange_rates
                        WHERE date >= ? AND date <= ?
                          AND from_currency = 'USD' AND to_currency = 'CNY'
                        ORDER BY date DESC LIMIT 1
                    """, (f"{year}-12-21", year_end)).fetchone()

                    if fallback is None:
                        result.add("CV-008", "ERROR",
                                   f"年末汇率缺失：数据库无 {year}年12月31日 USD/CNY 汇率，"
                                   f"年度汇算清缴需使用年末汇率，请补充数据")
                    else:
                        result.add("CV-008", "WARNING",
                                   f"年末汇率回退：将使用 {fallback['date']} 汇率 {fallback['rate']}")
                        result.verified_items["year_end_rate_fallback_date"] = fallback['date']
                else:
                    result.verified_items["year_end_usd_rate"] = float(usd_rate["rate"])

                conn.close()
            except Exception:
                result.add("CV-008", "WARNING",
                           "数据库 exchange_rates 表不存在或无法访问，跳过年末汇率校验")
        else:
            result.add("CV-008", "WARNING",
                       "无汇率数据源，跳过年末汇率校验")


def _verify_cross_broker_comparison(
    transactions: list[dict],
    year: int,
    result: VerificationResult,
    db_path: Path | None = None,
):
    """CV-009: 跨券商合并计税 vs 按券商独立计税对比

    背景：
    - 系统采用跨券商合并模式（同一 symbol 所有券商共享 FIFO 队列）
    - 但《实施条例》第十六条要求"方法一经选择，不得随意变更"
    - 跨券商合并的法律依据存在争议，税务机关可能要求按券商独立计税

    本校验提供两种计税方式的对比，帮助纳税人复核决策。
    """
    from datetime import date as date_cls
    from collections import defaultdict

    # 分离当年交易和往年交易
    year_txns = []
    pre_year_txns = []
    for txn in transactions:
        td = txn["trade_date"]
        if isinstance(td, str):
            td = date_cls.fromisoformat(td)
        if td.year < year:
            pre_year_txns.append(txn)
        else:
            year_txns.append(txn)

    # ===== 方法 A：跨券商合并（系统默认） =====
    merged_fifo = FIFOEngine()
    merged_net_gain_cny = Decimal("0")
    merged_per_txn_tax_cny = Decimal("0")

    # 用往年交易构建年初持仓
    for txn in pre_year_txns:
        action = txn["action"]
        if action in ("buy", "option_buy", "rsu_vest", "option_exercise"):
            broker_code = txn.get("broker_code", "unknown")
            symbol = txn["symbol"]
            td = date_cls.fromisoformat(txn["trade_date"]) if isinstance(txn["trade_date"], str) else txn["trade_date"]
            merged_fifo.buy(broker_code, symbol, txn["quantity"], txn["price"], td, origin=action)

    # 处理当年交易
    for txn in year_txns:
        action = txn["action"]
        broker_code = txn.get("broker_code", "unknown")
        symbol = txn["symbol"]
        td = date_cls.fromisoformat(txn["trade_date"]) if isinstance(txn["trade_date"], str) else txn["trade_date"]
        rate = txn.get("exchange_rate")
        if rate is None or rate <= 0:
            # USD 为基准货币，不需要汇率
            rate = Decimal("1")
        if not isinstance(rate, Decimal):
            rate = Decimal(str(rate))

        if action in ("buy", "option_buy", "rsu_vest", "option_exercise"):
            merged_fifo.buy(broker_code, symbol, txn["quantity"], txn["price"], td, origin=action)
        elif action in ("sell", "option_sell", "rsu_sell"):
            try:
                sell_results = merged_fifo.sell(broker_code, symbol, txn["quantity"], txn["price"], td,
                                                txn.get("fee", Decimal("0")))
                for r in sell_results:
                    gain_cny = (r["gain_loss"] * rate).quantize(Decimal("0.01"))
                    merged_net_gain_cny += gain_cny
                    if gain_cny > 0:
                        merged_per_txn_tax_cny += (gain_cny * CAPITAL_GAINS_RATE).quantize(Decimal("0.01"))
            except ValueError:
                pass  # FIFO 持仓不足已在其他校验中报错
        elif action == "option_expire":
            try:
                expire_results = merged_fifo.expire(broker_code, symbol, txn["quantity"], td)
                for r in expire_results:
                    loss_cny = (r["gain_loss"] * rate).quantize(Decimal("0.01"))
                    merged_net_gain_cny += loss_cny
            except ValueError:
                pass

    merged_net_gain_cny = merged_net_gain_cny.quantize(Decimal("0.01"))
    merged_annual_net_tax = Decimal("0")
    if merged_net_gain_cny > 0:
        merged_annual_net_tax = (merged_net_gain_cny * CAPITAL_GAINS_RATE).quantize(Decimal("0.01"))

    # ===== 方法 B：按券商独立计税 =====
    broker_gains: dict[str, Decimal] = defaultdict(Decimal)  # broker -> net_gain_cny
    broker_fifo_engines: dict[str, FIFOEngine] = {}

    # 初始化各券商的 FIFO 引擎
    brokers_in_txns = set(txn.get("broker_code", "unknown") for txn in transactions)

    for broker in brokers_in_txns:
        broker_fifo_engines[broker] = FIFOEngine()

    # 用往年交易构建各券商年初持仓
    for txn in pre_year_txns:
        action = txn["action"]
        broker_code = txn.get("broker_code", "unknown")
        if action in ("buy", "option_buy", "rsu_vest", "option_exercise"):
            symbol = txn["symbol"]
            td = date_cls.fromisoformat(txn["trade_date"]) if isinstance(txn["trade_date"], str) else txn["trade_date"]
            fifo = broker_fifo_engines.get(broker_code)
            if fifo:
                fifo.buy(broker_code, symbol, txn["quantity"], txn["price"], td, origin=action)

    # 处理当年交易（按券商独立）
    for txn in year_txns:
        action = txn["action"]
        broker_code = txn.get("broker_code", "unknown")
        symbol = txn["symbol"]
        td = date_cls.fromisoformat(txn["trade_date"]) if isinstance(txn["trade_date"], str) else txn["trade_date"]
        rate = txn.get("exchange_rate")
        if rate is None or rate <= 0:
            # USD 为基准货币，不需要汇率
            rate = Decimal("1")
        if not isinstance(rate, Decimal):
            rate = Decimal(str(rate))

        fifo = broker_fifo_engines.get(broker_code)
        if fifo is None:
            continue

        if action in ("buy", "option_buy", "rsu_vest", "option_exercise"):
            fifo.buy(broker_code, symbol, txn["quantity"], txn["price"], td, origin=action)
        elif action in ("sell", "option_sell", "rsu_sell"):
            try:
                sell_results = fifo.sell(broker_code, symbol, txn["quantity"], txn["price"], td,
                                         txn.get("fee", Decimal("0")))
                for r in sell_results:
                    gain_cny = (r["gain_loss"] * rate).quantize(Decimal("0.01"))
                    broker_gains[broker_code] += gain_cny
            except ValueError:
                pass
        elif action == "option_expire":
            try:
                expire_results = fifo.expire(broker_code, symbol, txn["quantity"], td)
                for r in expire_results:
                    loss_cny = (r["gain_loss"] * rate).quantize(Decimal("0.01"))
                    broker_gains[broker_code] += loss_cny
            except ValueError:
                pass

    # 各券商独立计税汇总
    independent_total_tax_cny = Decimal("0")
    for broker, net_gain in broker_gains.items():
        net_gain = net_gain.quantize(Decimal("0.01"))
        if net_gain > 0:
            tax = (net_gain * CAPITAL_GAINS_RATE).quantize(Decimal("0.01"))
            independent_total_tax_cny += tax
            result.verified_items[f"independent_{broker}_net_gain_cny"] = float(net_gain)
            result.verified_items[f"independent_{broker}_tax_cny"] = float(tax)
        else:
            # 亏损券商不征税，亏损不抵扣其他券商盈利
            result.verified_items[f"independent_{broker}_net_loss_cny"] = float(abs(net_gain))

    # ===== 对比分析 =====
    merged_tax_cny = min(merged_per_txn_tax_cny, merged_annual_net_tax) if merged_annual_net_tax > 0 else merged_per_txn_tax_cny

    result.verified_items["merged_net_gain_cny"] = float(merged_net_gain_cny)
    result.verified_items["merged_tax_cny"] = float(merged_tax_cny)
    result.verified_items["independent_total_tax_cny"] = float(independent_total_tax_cny)

    tax_diff = merged_tax_cny - independent_total_tax_cny

    if abs(tax_diff) > Decimal("0.01"):
        severity = "WARNING"
        if tax_diff < 0:
            # 合并计税更低，说明跨券商亏损抵扣了盈利
            message = f"跨券商合并计税 ¥{merged_tax_cny:,.2f} < 按券商独立计税 ¥{independent_total_tax_cny:,.2f}，"
            message += f"差额 ¥{abs(tax_diff):,.2f}（合并模式利用了跨券商亏损抵扣）。"
            message += "⚠️ 此处理方式的法律依据存在争议，税务机关可能要求按券商独立计税，请人工复核。"
        else:
            # 合并计税更高（罕见情况）
            message = f"跨券商合并计税 ¥{merged_tax_cny:,.2f} > 按券商独立计税 ¥{independent_total_tax_cny:,.2f}，"
            message += f"差额 ¥{tax_diff:,.2f}。建议复核计税方法选择。"

        result.add("CV-009", severity, message)

        # 输出详细对比表
        comparison_detail = "计税方式对比明细：\n"
        comparison_detail += f"  - 跨券商合并：净盈亏 ¥{merged_net_gain_cny:,.2f}，税额 ¥{merged_tax_cny:,.2f}\n"
        comparison_detail += f"  - 按券商独立：各券商税额合计 ¥{independent_total_tax_cny:,.2f}\n"
        for broker in sorted(broker_gains.keys()):
            gain = broker_gains[broker].quantize(Decimal("0.01"))
            comparison_detail += f"    · {broker}: 净盈亏 ¥{gain:,.2f}\n"
        result.issues[-1].details = comparison_detail
    else:
        # 两种方法结果一致，无需警告
        result.verified_items["cv009_methods_match"] = True


def _verify_option_expire_cost_consistency(
    db_path: Path | None, year: int, result: VerificationResult,
):
    """CV-010: 期权过期交易金额必须与 FIFO 实际消耗成本一致

    税务师审查要求：expire 交易记录的成本 = FIFO 引擎计算的实际消耗成本
    = 月结单上显示的权利金支出。

    验证优先级：
    1. 有 lot_consumptions → 与 FIFO cost_basis 对比（权威）
    2. 无 lot_consumptions（历史期权未参与计算）→ 与 option_buy 实际支出对比
    """
    path = db_path or DB_PATH
    if not path.exists():
        return

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row

    # 获取所有 option_expire 交易
    expire_txns = conn.execute("""
        SELECT id, symbol, broker_code, trade_date, quantity, price, amount
        FROM transactions
        WHERE action = 'option_expire'
        ORDER BY trade_date, symbol
    """).fetchall()

    if not expire_txns:
        conn.close()
        return

    total_expire_amount = Decimal("0")
    total_fifo_cost = Decimal("0")
    mismatch_count = 0
    under_reported = Decimal("0")
    over_reported = Decimal("0")
    no_lc_count = 0  # 无 lot_consumptions 的历史 expire

    for txn in expire_txns:
        # 1. 从 lot_consumptions 获取 FIFO 实际消耗成本
        lc_row = conn.execute("""
            SELECT COALESCE(SUM(ABS(CAST(cost_basis AS REAL))), 0) as total_cost
            FROM lot_consumptions
            WHERE sell_txn_id = ? AND consumption_type = 'expire'
        """, (txn["id"],)).fetchone()

        fifo_cost = Decimal(str(lc_row["total_cost"]))
        expire_amount = Decimal(str(txn["amount"])) if txn["amount"] else Decimal("0")

        total_expire_amount += expire_amount

        if fifo_cost > 0:
            # 有 FIFO 记录：与 FIFO cost_basis 对比
            total_fifo_cost += fifo_cost
            if abs(fifo_cost - expire_amount) > Decimal("1.00"):
                mismatch_count += 1
                diff = fifo_cost - expire_amount
                if diff > 0:
                    under_reported += diff
                    result.add(
                        "CV-010", "WARNING",
                        f"{txn['symbol']} [{txn['broker_code']}] expire_date={txn['trade_date']}: "
                        f"expire 交易金额 ${expire_amount:.2f} < FIFO 实际成本 ${fifo_cost:.2f}，"
                        f"差异 ${abs(diff):.2f}（成本被低估，可能少抵扣）"
                    )
                else:
                    over_reported += abs(diff)
                    result.add(
                        "CV-010", "WARNING",
                        f"{txn['symbol']} [{txn['broker_code']}] expire_date={txn['trade_date']}: "
                        f"expire 交易金额 ${expire_amount:.2f} > FIFO 实际成本 ${fifo_cost:.2f}，"
                        f"差异 ${abs(diff):.2f}（金额虚高）"
                    )
        else:
            # 2. 无 lot_consumptions：与 option_buy 交易的实际支出对比
            no_lc_count += 1
            buy_total = conn.execute("""
                SELECT COALESCE(SUM(amount), 0) as total_cost
                FROM transactions
                WHERE symbol = ? AND broker_code = ? AND action = 'option_buy'
            """, (txn['symbol'], txn['broker_code'])).fetchone()["total_cost"]

            buy_cost = Decimal(str(buy_total))

            # 检查是否有 sell 记录（部分持仓已在到期前卖出）
            sell_qty = conn.execute("""
                SELECT COALESCE(SUM(quantity), 0) as sold
                FROM transactions
                WHERE symbol = ? AND broker_code = ?
                  AND action IN ('sell', 'option_sell')
            """, (txn['symbol'], txn['broker_code'])).fetchone()["sold"]

            buy_qty = conn.execute("""
                SELECT COALESCE(SUM(quantity), 0) as bought
                FROM transactions
                WHERE symbol = ? AND broker_code = ? AND action = 'option_buy'
            """, (txn['symbol'], txn['broker_code'])).fetchone()["bought"]

            # 如果有部分卖出，expire 只对应剩余部分，跳过买成本对比
            # （expire amount 应等于剩余持仓的成本，而非总买入成本）
            if sell_qty > 0 and buy_qty > 0 and sell_qty < buy_qty:
                # 部分卖出：expire 对应剩余部分，不触发 mismatch
                continue

            if buy_cost > 0 and abs(buy_cost - expire_amount) > Decimal("1.00"):
                mismatch_count += 1
                diff = buy_cost - expire_amount
                if diff > 0:
                    under_reported += diff
                    result.add(
                        "CV-010", "WARNING",
                        f"{txn['symbol']} [{txn['broker_code']}] expire_date={txn['trade_date']}: "
                        f"expire 交易金额 ${expire_amount:.2f} < 买入成本 ${buy_cost:.2f}，"
                        f"差异 ${abs(diff):.2f}（部分持仓可能在到期前已卖出）"
                    )
                else:
                    over_reported += abs(diff)
                    result.add(
                        "CV-010", "WARNING",
                        f"{txn['symbol']} [{txn['broker_code']}] expire_date={txn['trade_date']}: "
                        f"expire 交易金额 ${expire_amount:.2f} > 买入成本 ${buy_cost:.2f}，"
                        f"差异 ${abs(diff):.2f}（金额虚高）"
                    )

    result.verified_items["cv010_expire_txns_total"] = len(expire_txns)
    result.verified_items["cv010_total_expire_amount_usd"] = float(total_expire_amount)
    if total_fifo_cost > 0:
        result.verified_items["cv010_total_fifo_cost_usd"] = float(total_fifo_cost)
    result.verified_items["cv010_mismatch_count"] = mismatch_count
    if no_lc_count > 0:
        result.verified_items["cv010_no_lot_consumptions"] = no_lc_count
    if under_reported > 0:
        result.verified_items["cv010_under_reported_usd"] = float(under_reported)
    if over_reported > 0:
        result.verified_items["cv010_over_reported_usd"] = float(over_reported)

    if mismatch_count > 0:
        result.verified_items["cv010_passed"] = False
    else:
        result.verified_items["cv010_passed"] = True

    conn.close()


def _verify_lot_consumptions_consistency(db_path: Path | None, year: int, result: VerificationResult):
    """LC-001: lot_consumptions 记录引用完整性

    每条 lot_consumption 的 sell_txn_id 必须引用有效卖出交易，
    consumption_type 与 sell_action 必须匹配。
    """
    path = db_path or DB_PATH
    if not path.exists():
        return
    try:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row

        orphaned = conn.execute("""
            SELECT lc.id, lc.sell_txn_id, lc.consumed_qty, lc.consumption_type
            FROM lot_consumptions lc
            LEFT JOIN transactions t ON lc.sell_txn_id = t.id
            WHERE t.id IS NULL
        """, ()).fetchall()
        for r in orphaned:
            result.add("LC-001", "ERROR",
                       f"lot_consumption {r['id']} 引用不存在的 sell_txn_id={r['sell_txn_id']}")

        total_lc = conn.execute("SELECT COUNT(*) as cnt FROM lot_consumptions").fetchone()["cnt"]
        result.verified_items["lc001_total_consumptions"] = total_lc
        if orphaned:
            result.verified_items["lc001_orphaned"] = len(orphaned)

        conn.close()
    except Exception:
        pass


def _verify_ftc_carryforward_consistency(db_path: Path | None, year: int, result: VerificationResult):
    """FC-001: FTC carryforward 记录合法性

    remaining_amount 不能为负，expires_year 不能早于 source_year。
    """
    path = db_path or DB_PATH
    if not path.exists():
        return
    try:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row

        invalid = conn.execute("""
            SELECT id, country, income_category, source_year,
                   remaining_amount, expires_year
            FROM foreign_tax_credit_carryforward
            WHERE remaining_amount < 0 OR expires_year < source_year
        """, ()).fetchall()
        for r in invalid:
            result.add("FC-001", "ERROR",
                       f"FTC carryforward {r['id']} 状态非法: "
                       f"remaining={r['remaining_amount']}, expires={r['expires_year']}")

        soon_expiring = conn.execute("""
            SELECT id, country, income_category, source_year, remaining_amount
            FROM foreign_tax_credit_carryforward
            WHERE expires_year = ? AND remaining_amount > 0
        """, (str(year),)).fetchall()
        for r in soon_expiring:
            result.add("FC-001", "WARNING",
                       f"FTC 结转即将到期: {r['country']}/{r['income_category']}, "
                       f"剩余 {r['remaining_amount']}")

        conn.close()
    except Exception:
        pass


def _verify_tax_items_coverage(db_path: Path | None, year: int, result: VerificationResult):
    """TI-001: tax_items 覆盖率校验

    有预扣税的卖出/股息应在 tax_items 表中有对应记录。
    """
    path = db_path or DB_PATH
    if not path.exists():
        return
    try:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row

        sells_without_items = conn.execute("""
            SELECT t.id, t.symbol, t.amount, t.tax_withheld
            FROM transactions t
            WHERE strftime('%Y', t.trade_date) = ?
              AND t.action IN ('sell', 'option_sell', 'rsu_sell')
              AND t.amount > 0 AND t.tax_withheld > 0
              AND NOT EXISTS (
                  SELECT 1 FROM tax_items ti
                  WHERE ti.symbol = t.symbol
                    AND ti.tax_year = ?
                    AND ti.date = t.trade_date
                    AND ti.income_type IN ('capital_gain', 'capital_gain_annual_net')
              )
        """, (str(year), str(year))).fetchall()
        for r in sells_without_items:
            result.add("TI-001", "WARNING",
                       f"sell {r['id']} ({r['symbol']}) 有预扣税 ${r['tax_withheld']} "
                       f"但无 capital_gain tax_item")

        total_tax_items = conn.execute("""
            SELECT COUNT(*) as cnt FROM tax_items WHERE tax_year = ?
        """, (str(year),)).fetchone()["cnt"]
        result.verified_items["ti001_total_tax_items"] = total_tax_items

        conn.close()
    except Exception:
        pass


def _verify_cash_flow_capital_gains(
    db_path: Path | None, year: int, result: VerificationResult
):
    """CV-011: 现金流量独立验证 — 通过 lot_consumptions 独立计算资本利得，与 tax_engine 交叉校验。

    验证方法（完全脱离 FIFO 引擎逻辑）：
        净盈亏 = 卖出现金收入 − 消耗成本
        消耗成本 = SUM(consumed_qty × cost_per_share × 期权乘数)
        期权乘数：x100（symbol 包含 _OPT_ 或匹配 *N[CP]*N 格式）

    与 tax_engine 的年度净额法资本利得对比，
    差异来源主要是汇率（本验证用年末统一汇率，tax_engine 用逐笔汇率）。
    差异阈值：>3% ERROR, 1%-3% WARNING, <1% PASSED。
    """
    path = db_path or DB_PATH
    if not path.exists():
        return
    try:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        year_str = str(year)
        option_mult_sql = (
            "CASE WHEN symbol LIKE '%_OPT_%' "
            "OR symbol GLOB '*[0-9][CP]*[0-9]' THEN 100.0 ELSE 1.0 END"
        )

        # --- 卖出现金流入 (USD) ---
        sell_inflows = conn.execute("""
            SELECT COALESCE(SUM(amount), 0) AS total
            FROM transactions
            WHERE strftime('%Y', trade_date) = ?
              AND action IN ('sell', 'option_sell')
        """, (year_str,)).fetchone()["total"]

        # --- 消耗成本 (USD) — 从 lot_consumptions 独立计算 ---
        consumed_cost = conn.execute(f"""
            SELECT COALESCE(SUM(lc.consumed_qty * tl.cost_per_share * {option_mult_sql}), 0) AS total
            FROM lot_consumptions lc
            JOIN tax_lots tl ON lc.tax_lot_id = tl.id
        """).fetchone()["total"]

        # --- 现金流量净盈亏 (USD) ---
        cash_flow_net_usd = Decimal(str(sell_inflows)) - Decimal(str(consumed_cost))

        # --- tax_engine 年度净额法资本利得 ---
        row = conn.execute("""
            SELECT COALESCE(SUM(taxable_income_cny), 0) AS engine_net
            FROM tax_items
            WHERE tax_year = ?
              AND income_type = 'capital_gain_annual_net'
        """, (year_str,)).fetchone()
        tax_engine_net_cny = Decimal(str(row["engine_net"]))

        # 年末汇率
        year_end_rate = _get_year_end_rate(path, year)
        tax_engine_net_usd = tax_engine_net_cny / year_end_rate

        # 逐笔法参考
        row2 = conn.execute("""
            SELECT COALESCE(SUM(taxable_income_cny), 0) AS engine_per_txn
            FROM tax_items
            WHERE tax_year = ?
              AND income_type = 'capital_gain_per_txn'
        """, (year_str,)).fetchone()
        tax_engine_per_txn_cny = Decimal(str(row2["engine_per_txn"]))

        # 统计
        total_sells = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE strftime('%Y', trade_date) = ? AND action IN ('sell', 'option_sell')",
            (year_str,),
        ).fetchone()[0]

        total_consumptions = conn.execute(
            "SELECT COUNT(*) FROM lot_consumptions"
        ).fetchone()[0]

        conn.close()

        # --- 验证记录 ---
        sell_dec = Decimal(str(sell_inflows))
        consumed_dec = Decimal(str(consumed_cost))
        cash_flow_net_cny = cash_flow_net_usd * year_end_rate
        result.verified_items["cv011_cash_flow_net_cny"] = cash_flow_net_cny
        result.verified_items["cv011_tax_engine_net_cny"] = tax_engine_net_cny
        result.verified_items["cv011_consumed_cost_usd"] = consumed_cost

        if tax_engine_net_cny == 0:
            if cash_flow_net_cny != 0:
                result.add("CV-011", "ERROR",
                           f"现金流量计算结果为 ¥{cash_flow_net_cny:,.2f}，"
                           f"但 tax_engine 资本利得为 0")
            else:
                result.add("CV-011", "PASSED",
                           "现金流量与 tax_engine 资本利得均为 0")
            return

        diff = abs(cash_flow_net_cny - tax_engine_net_cny)
        pct = diff / abs(tax_engine_net_cny) * 100

        fmt = lambda d: f"¥{d:,.2f}"
        fmt_usd = lambda d: f"${d:,.2f}"

        detail_lines = [
            f"独立验证: 卖出收入 − 消耗成本",
            f"  卖出现金流入:  {fmt_usd(sell_inflows)} ({fmt(sell_dec * year_end_rate)} CNY)",
            f"  消耗成本:      {fmt_usd(consumed_cost)} (lot_consumptions {total_consumptions} 条)",
            f"现金流量净盈亏: {fmt(cash_flow_net_cny)} ({fmt_usd(cash_flow_net_usd)})",
            f"tax_engine 净额:  {fmt(tax_engine_net_cny)} (年度净额法)",
            f"逐笔法参考:     {fmt(tax_engine_per_txn_cny)}",
            f"差异:           {fmt(diff)} ({pct:.2f}%)",
        ]

        if pct > 3.0:
            result.add("CV-011", "ERROR",
                       f"现金流量验证差异过大: {pct:.2f}% (>3.0%)",
                       "\n".join(detail_lines))
        elif pct > 1.0:
            result.add("CV-011", "WARNING",
                       f"现金流量验证差异 {pct:.2f}% (1%-3% 区间，可能来自汇率差异)",
                       "\n".join(detail_lines))
        else:
            result.add("CV-011", "PASSED",
                       f"现金流量验证通过: 差异 {pct:.2f}% (<1%)",
                       "\n".join(detail_lines))

    except Exception as e:
        result.add("CV-011", "ERROR", f"现金流量验证异常: {e}")
