#!/usr/bin/env python3
"""2025年度分账户税务核算报告

以中国个人所得税法为基础：
- 财产转让所得（股票/期权买卖）：20%
- 股息红利所得：20%
- RSU归属所得（工资薪金）：已预扣，不纳入本工具补缴计算

复用主引擎的 FIFOEngine 和汇率加载器，每个 broker 独立 FIFO 队列。
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from collections import defaultdict

from src.calculator.exchange_rate import load_exchange_rates, get_exchange_rate
from src.calculator.fifo import FIFOEngine
from src.calculator.tax_engine import CAPITAL_GAINS_RATE, DIVIDEND_RATE
from src.models.tax_lot import TaxLot


def clamp_tax(tax: Decimal, withheld: Decimal) -> Decimal:
    payable = tax - withheld
    if payable < 0:
        return Decimal("0")
    return payable.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)




def load_2024_year_end_lots(db_path: str = "output/output/tax.db") -> dict[str, list[TaxLot]]:
    """通过2024年交易构建年末持仓（2025年初起点）"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT symbol, action, quantity, price, trade_date
        FROM transactions
        WHERE strftime('%Y', trade_date) = '2024'
          AND action IN ('buy', 'sell', 'option_buy', 'option_sell', 'option_expire')
        ORDER BY trade_date
    """).fetchall()
    conn.close()

    buy_actions = {"buy", "option_buy", "option_exercise"}
    sell_actions = {"sell", "option_sell", "option_expire"}

    lots: dict[str, list[TaxLot]] = defaultdict(list)
    for r in rows:
        sym = r["symbol"]
        qty = r["quantity"]
        td = date.fromisoformat(r["trade_date"])
        if r["action"] in buy_actions:
            lots[sym].append(TaxLot(
                symbol=sym, quantity=qty, cost_per_share=Decimal(str(r["price"])),
                acquire_date=td, remaining=qty, origin=r["action"]))
        elif r["action"] in sell_actions:
            remaining = qty
            for lot in lots.get(sym, []):
                if remaining <= 0 or lot.remaining <= 0:
                    continue
                take = min(lot.remaining, remaining)
                lot.remaining -= take
                remaining -= take

    # 返回有剩余的持仓
    result = {}
    for sym, sym_lots in lots.items():
        remaining_lots = [l for l in sym_lots if l.remaining > 0]
        if remaining_lots:
            result[sym] = remaining_lots
    return result


def load_2025_data(db_path: str = "output/output/tax.db"):
    """加载2025年交易、分红、RSU归属，以及2025年前的交易（用于缺失买入补头）"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Load all 2025 transactions
    txn_rows = conn.execute("""
        SELECT id, broker_code, trade_date, symbol, action, quantity, price, amount,
               currency, exchange_rate,
               COALESCE(commission,0) + COALESCE(platform_fee,0) + COALESCE(sec_fee,0)
               + COALESCE(taf_fee,0) + COALESCE(delivery_fee,0) + COALESCE(other_fees,0) as fee_total,
               tax_withheld, raw_data
        FROM transactions
        WHERE strftime('%Y', trade_date) = '2025'
        ORDER BY broker_code, trade_date
    """).fetchall()

    transactions = []
    for r in txn_rows:
        td = date.fromisoformat(r["trade_date"])
        rate = Decimal(str(r["exchange_rate"])) if r["exchange_rate"] else Decimal("0")
        transactions.append({
            "id": r["id"],
            "broker_code": r["broker_code"],
            "trade_date": td,
            "symbol": r["symbol"],
            "action": r["action"],
            "quantity": r["quantity"] or 0,
            "price": Decimal(str(r["price"])) if r["price"] else Decimal("0"),
            "amount": Decimal(str(r["amount"])) if r["amount"] else Decimal("0"),
            "fee": Decimal(str(r["fee_total"])),
            "tax_withheld": Decimal(str(r["tax_withheld"])) if r["tax_withheld"] else Decimal("0"),
            "currency": r["currency"],
            "exchange_rate": rate,
        })

    # Load 2025 dividends
    div_rows = conn.execute("""
        SELECT broker_code, symbol, payment_date, per_share_amount, share_quantity,
               gross_amount, withholding_tax, withholding_refund, currency
        FROM dividends
        WHERE strftime('%Y', payment_date) = '2025'
    """).fetchall()

    dividends = []
    for r in div_rows:
        dividends.append({
            "broker_code": r["broker_code"],
            "symbol": r["symbol"],
            "payment_date": date.fromisoformat(r["payment_date"]),
            "per_share_amount": Decimal(str(r["per_share_amount"])),
            "share_quantity": r["share_quantity"],
            "gross_amount": Decimal(str(r["gross_amount"])),
            "withholding_tax": Decimal(str(r["withholding_tax"])),
            "withholding_refund": Decimal(str(r["withholding_refund"] or 0)),
            "currency": r["currency"],
        })

    # Load 2025 RSU vests
    rsu_rows = conn.execute("""
        SELECT grant_number, symbol, vest_date, vested_quantity, fmv_per_share,
               taxable_income, tax_amount, currency
        FROM rsu_vests
        WHERE strftime('%Y', vest_date) = '2025'
    """).fetchall()

    rsu_vests = []
    for r in rsu_rows:
        taxable = Decimal(str(r["taxable_income"])) if r["taxable_income"] else Decimal("0")
        tax_amt = Decimal(str(r["tax_amount"])) if r["tax_amount"] else Decimal("0")
        rsu_vests.append({
            "grant_number": r["grant_number"],
            "symbol": r["symbol"],
            "vest_date": date.fromisoformat(r["vest_date"]),
            "vested_quantity": r["vested_quantity"],
            "fmv_per_share": Decimal(str(r["fmv_per_share"])),
            "taxable_income": taxable,
            "tax_amount": tax_amt,
            "currency": r["currency"],
        })

    # Load pre-2025 transactions for missing buy lot detection
    pre2025_rows = conn.execute("""
        SELECT broker_code, symbol, action, quantity, price, trade_date
        FROM transactions
        WHERE strftime('%Y', trade_date) < '2025'
          AND action IN ('buy', 'sell', 'option_buy', 'option_sell', 'option_expire')
        ORDER BY trade_date
    """).fetchall()

    conn.close()
    return transactions, dividends, rsu_vests, pre2025_rows


def _load_all_history_transactions(db_path: str = "output/tax.db") -> list[dict]:
    """加载全部历史交易（所有年份），供 FIFO 自然推进"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, broker_code, trade_date, symbol, action, quantity, price, amount,
               currency, exchange_rate,
               COALESCE(commission,0) + COALESCE(platform_fee,0) + COALESCE(sec_fee,0)
               + COALESCE(taf_fee,0) + COALESCE(delivery_fee,0) + COALESCE(other_fees,0) as fee_total,
               tax_withheld
        FROM transactions
        WHERE action IN ('buy', 'sell', 'option_buy', 'option_sell', 'option_expire',
                         'option_exercise', 'rsu_vest', 'rsu_sell')
        ORDER BY trade_date
    """).fetchall()

    txns = []
    for r in rows:
        td = date.fromisoformat(r["trade_date"])
        rate = Decimal(str(r["exchange_rate"])) if r["exchange_rate"] else Decimal("0")
        txns.append({
            "id": r["id"],
            "broker_code": r["broker_code"],
            "trade_date": td,
            "symbol": r["symbol"],
            "action": r["action"],
            "quantity": r["quantity"] or 0,
            "price": Decimal(str(r["price"])) if r["price"] else Decimal("0"),
            "amount": Decimal(str(r["amount"])) if r["amount"] else Decimal("0"),
            "fee": Decimal(str(r["fee_total"])),
            "tax_withheld": Decimal(str(r["tax_withheld"])) if r["tax_withheld"] else Decimal("0"),
            "currency": r["currency"],
            "exchange_rate": rate,
        })
    conn.close()
    return txns






def compute_per_broker_tax(broker: str, all_txns: list[dict],
                           all_divs: list[dict], year: int,
                           lots_2024: dict[str, list[TaxLot]]):
    """计算单一券商的税务（不含RSU），复用 FIFOEngine

    以2024年末持仓为起点，仅处理2025年交易。
    """
    # 该 broker 的 2025 交易
    txns_2025 = [t for t in all_txns if t["broker_code"] == broker]
    divs = [d for d in all_divs if d["broker_code"] == broker]

    # 确保汇率已加载
    load_exchange_rates()

    # 为 2025 交易填充汇率
    for txn in txns_2025:
        if txn["exchange_rate"] == 0:
            txn["exchange_rate"] = get_exchange_rate(txn["trade_date"], txn["currency"])

    # 按日期排序
    txns_2025.sort(key=lambda t: t["trade_date"])

    # 统计总买入/卖出量，为总差额添加 $0 成本 lot
    buy_actions = {"buy", "option_buy", "option_exercise", "rsu_vest"}
    sell_actions = {"sell", "option_sell", "rsu_sell"}
    expire_actions = {"option_expire"}

    total_bought: dict[str, int] = defaultdict(int)
    total_sold: dict[str, int] = defaultdict(int)
    for txn in txns_2025:
        if txn["action"] in buy_actions:
            total_bought[txn["symbol"]] += txn["quantity"]
        if txn["action"] in sell_actions or txn["action"] in expire_actions:
            total_sold[txn["symbol"]] += txn["quantity"]

    # 2024年末持仓作为2025年初起点
    initial_lots: dict[str, list[TaxLot]] = defaultdict(list)
    for sym, sym_lots in lots_2024.items():
        for lot in sym_lots:
            initial_lots[sym].append(lot)

    # 为总差额（sold > bought + 2024剩余）添加 $0 成本 lot
    for symbol, sold in total_sold.items():
        bought = total_bought.get(symbol, 0)
        remaining_2024 = sum(l.remaining for l in initial_lots.get(symbol, []))
        if sold > bought + remaining_2024:
            deficit = sold - bought - remaining_2024
            initial_lots[symbol].append(TaxLot(
                symbol=symbol,
                quantity=deficit,
                cost_per_share=Decimal("0"),
                acquire_date=date(year - 1, 12, 31),
                remaining=deficit,
                origin="carry_forward_missing",
            ))

    fifo = FIFOEngine(existing_lots=dict(initial_lots))

    # === 处理 2025 交易 ===
    sell_results: list[dict] = []
    expire_results: list[dict] = []
    capital_gain_items: list[dict] = []
    fifo_error_count = 0

    for txn in txns_2025:
        rate = txn["exchange_rate"]
        action = txn["action"]
        symbol = txn["symbol"]
        td = txn["trade_date"]

        if action in ("buy", "option_buy", "option_exercise", "rsu_vest"):
            fifo.buy(symbol, txn["quantity"], txn["price"], td, origin=action)

        elif action == "option_expire":
            try:
                results = fifo.expire(symbol, txn["quantity"], td)
                expire_results.extend(results)
            except ValueError:
                fifo_error_count += 1

        elif action in ("sell", "option_sell", "rsu_sell"):
            try:
                results = fifo.sell(symbol, txn["quantity"], txn["price"], td, txn["fee"])
                sell_results.extend(results)
                for r in results:
                    gain_cny = (r["gain_loss"] * rate).quantize(Decimal("0.01"))
                    if gain_cny > 0:
                        proceeds_cny = (r["proceeds"] * rate).quantize(Decimal("0.01"))
                        cost_cny = (r["cost_basis"] * rate).quantize(Decimal("0.01"))
                        tax_amt = (gain_cny * CAPITAL_GAINS_RATE).quantize(Decimal("0.01"))
                        withheld_cny = (txn["tax_withheld"] * rate).quantize(Decimal("0.01"))
                        capital_gain_items.append({
                            "date": str(td),
                            "symbol": symbol,
                            "income_type": "option_gain" if "option" in action else "capital_gain",
                            "gross_income_cny": proceeds_cny,
                            "deductible_cny": cost_cny,
                            "taxable_income_cny": gain_cny,
                            "tax_rate": CAPITAL_GAINS_RATE,
                            "tax_amount_cny": tax_amt,
                            "tax_withheld_cny": withheld_cny,
                            "foreign_tax_credit_cny": Decimal("0"),
                            "excess_withholding_cny": Decimal("0"),
                            "tax_payable_cny": Decimal("0"),
                            "detail": f"卖出 {r['quantity']} 股, 成本 {r['cost_per_share']} {txn['currency']}",
                        })
            except ValueError:
                fifo_error_count += 1

    # === 资本利得：比较两种计税方法 ===
    has_losses = (any(r["gain_loss"] < 0 for r in sell_results)
                  or len(expire_results) > 0)
    method_used = "per_transaction"
    annual_net_info = None

    if has_losses and (sell_results or expire_results):
        # 构建卖出/过期的汇率映射
        sell_rate_map = {}
        for txn in txns_2025:
            if txn["action"] in ("sell", "option_sell", "rsu_sell", "option_expire"):
                sell_rate_map[(txn["symbol"], str(txn["trade_date"]))] = txn["exchange_rate"]

        # 计算年度净盈亏
        net_gain_cny = Decimal("0")
        for r in sell_results:
            rate = sell_rate_map.get((r["symbol"], str(r["sell_date"])), Decimal("7.10"))
            gain_cny = (r["gain_loss"] * rate).quantize(Decimal("0.01"))
            net_gain_cny += gain_cny
        for r in expire_results:
            rate = sell_rate_map.get((r["symbol"], str(r["sell_date"])), Decimal("7.10"))
            loss_cny = (r["gain_loss"] * rate).quantize(Decimal("0.01"))
            net_gain_cny += loss_cny

        # 汇总预扣税
        total_cg_withheld = Decimal("0")
        for txn in txns_2025:
            if txn["action"] in ("sell", "option_sell", "rsu_sell", "option_expire"):
                total_cg_withheld += (txn["tax_withheld"] * txn["exchange_rate"]).quantize(Decimal("0.01"))

        net_gain_cny = net_gain_cny.quantize(Decimal("0.01"))

        if net_gain_cny > 0:
            annual_net_tax = (net_gain_cny * CAPITAL_GAINS_RATE).quantize(Decimal("0.01"))
            per_txn_total_tax = sum(i["tax_amount_cny"] for i in capital_gain_items)

            if annual_net_tax < per_txn_total_tax:
                method_used = "annual_net"
                annual_net_info = {
                    "net_gain_cny": float(net_gain_cny),
                    "tax_amount_cny": float(annual_net_tax),
                    "per_txn_tax_amount": float(per_txn_total_tax),
                }
                loss_count = sum(1 for r in sell_results if r["gain_loss"] <= 0)
                expire_count = len(expire_results)
                expire_detail = f"，{expire_count} 笔期权过期损失" if expire_count > 0 else ""
                capital_gain_items = [{
                    "date": f"{year}-01-01",
                    "symbol": "多只股票/期权",
                    "income_type": "capital_gain_annual_net",
                    "gross_income_cny": Decimal("0"),
                    "deductible_cny": Decimal("0"),
                    "taxable_income_cny": net_gain_cny,
                    "tax_rate": CAPITAL_GAINS_RATE,
                    "tax_amount_cny": annual_net_tax,
                    "tax_withheld_cny": total_cg_withheld,
                    "foreign_tax_credit_cny": Decimal("0"),
                    "excess_withholding_cny": Decimal("0"),
                    "tax_payable_cny": Decimal("0"),
                    "detail": f"年度净额法：{len(sell_results)} 笔卖出/过期，"
                             f"{loss_count} 笔亏损已抵扣{expire_detail}，"
                             f"（逐笔计算应纳税 {per_txn_total_tax} CNY）",
                }]

    # === 分红 ===
    dividend_items: list[dict] = []
    for d in divs:
        rate = get_exchange_rate(d["payment_date"], d["currency"])
        gross_cny = (d["gross_amount"] * rate).quantize(Decimal("0.01"))
        tax_amt = (gross_cny * DIVIDEND_RATE).quantize(Decimal("0.01"))
        # 净预扣 = 初始预扣 - ROC 返还（杠杆 ETF 净预扣 = $0）
        refund = d.get("withholding_refund", 0)
        net_withholding = d["withholding_tax"] - refund
        withheld_cny = (net_withholding * rate).quantize(Decimal("0.01"))
        refund_cny = (refund * rate).quantize(Decimal("0.01"))
        dividend_items.append({
            "date": str(d["payment_date"]),
            "symbol": d["symbol"],
            "income_type": "dividend",
            "gross_income_cny": gross_cny,
            "deductible_cny": Decimal("0"),
            "taxable_income_cny": gross_cny,
            "tax_rate": DIVIDEND_RATE,
            "tax_amount_cny": tax_amt,
            "tax_withheld_cny": withheld_cny,
            "withholding_refund_cny": refund_cny,
            "foreign_tax_credit_cny": Decimal("0"),
            "excess_withholding_cny": Decimal("0"),
            "tax_payable_cny": Decimal("0"),
            "detail": f"分红 {d['share_quantity']} 股 @ {d['per_share_amount']} {d['currency']}",
        })

    # === 利息所得（债券利息、股票借贷收益等） ===
    interest_items: list[dict] = []
    for txn in txns_2025:
        if txn["action"] == "interest" and txn["broker_code"] == broker:
            rate = txn["exchange_rate"]
            gross_cny = (txn["amount"] * rate).quantize(Decimal("0.01"))
            tax_amt = (gross_cny * DIVIDEND_RATE).quantize(Decimal("0.01"))
            withheld_cny = (txn["tax_withheld"] * rate).quantize(Decimal("0.01"))
            interest_items.append({
                "date": str(txn["trade_date"]),
                "symbol": txn["symbol"],
                "income_type": "interest_income",
                "gross_income_cny": gross_cny,
                "deductible_cny": Decimal("0"),
                "taxable_income_cny": gross_cny,
                "tax_rate": DIVIDEND_RATE,
                "tax_amount_cny": tax_amt,
                "tax_withheld_cny": withheld_cny,
                "foreign_tax_credit_cny": Decimal("0"),
                "excess_withholding_cny": Decimal("0"),
                "tax_payable_cny": Decimal("0"),
                "detail": f"利息收入 {txn['amount']} {txn['currency']}",
            })

    # === 境外税收抵免（分国+分项限额法） ===
    from collections import defaultdict as dd
    group_taxes: dict[tuple, dict] = dd(lambda: {"tax_amount": Decimal("0"), "tax_withheld": Decimal("0")})

    def _pcountry(item: dict) -> str:
        if item.get("currency", "USD") == "HKD":
            return "HK"
        return "US"

    def _pcategory(item: dict) -> str:
        it = item["income_type"]
        if it.startswith("interest"):
            return "interest"
        elif it.startswith("dividend"):
            return "dividend"
        else:
            return "capital_gain"

    for item in capital_gain_items:
        key = (_pcountry(item), _pcategory(item))
        group_taxes[key]["tax_amount"] += item["tax_amount_cny"]
        group_taxes[key]["tax_withheld"] += item["tax_withheld_cny"]

    for item in dividend_items + interest_items:
        key = (_pcountry(item), _pcategory(item))
        group_taxes[key]["tax_amount"] += item["tax_amount_cny"]
        group_taxes[key]["tax_withheld"] += item["tax_withheld_cny"]

    for key, taxes in group_taxes.items():
        if taxes["tax_withheld"] > 0:
            credit = min(taxes["tax_withheld"], taxes["tax_amount"])
            excess = taxes["tax_withheld"] - credit
            taxes["credit"] = credit.quantize(Decimal("0.01"))
            taxes["excess"] = excess.quantize(Decimal("0.01"))
        else:
            taxes["credit"] = Decimal("0")
            taxes["excess"] = Decimal("0")

    all_items = capital_gain_items + dividend_items + interest_items
    for item in all_items:
        item["foreign_tax_credit_cny"] = Decimal("0")
        item["excess_withholding_cny"] = Decimal("0")
        item["tax_payable_cny"] = item["tax_amount_cny"]

    for item in all_items:
        key = (_pcountry(item), _pcategory(item))
        taxes = group_taxes[key]
        if taxes["credit"] > 0:
            group_total_tax = taxes["tax_amount"]
            if group_total_tax > 0:
                ratio = item["tax_amount_cny"] / group_total_tax
                item["foreign_tax_credit_cny"] = (taxes["credit"] * ratio).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                item["excess_withholding_cny"] = (taxes["excess"] * ratio).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                payable = item["tax_amount_cny"] - item["foreign_tax_credit_cny"]
                item["tax_payable_cny"] = max(payable, Decimal("0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    # === RSU — 不在券商维度计算 ===
    rsu_items: list[dict] = []

    return {
        "capital_gain_items": capital_gain_items,
        "dividend_items": dividend_items,
        "interest_items": interest_items,
        "rsu_items": rsu_items,
        "sell_results": sell_results,
        "expire_results": expire_results,
        "method_used": method_used,
        "annual_net_info": annual_net_info,
        "fifo_error_count": fifo_error_count,
    }


def format_decimal(d: Decimal) -> str:
    return f"¥{d:,.2f}"


def print_report(broker_name: str, broker_results: dict, broker_label: str):
    cg_items = broker_results["capital_gain_items"]
    div_items = broker_results["dividend_items"]
    int_items = broker_results.get("interest_items", [])
    rsu_items = broker_results["rsu_items"]
    method = broker_results["method_used"]
    annual_net = broker_results["annual_net_info"]

    print(f"\n{'='*70}")
    print(f"  {broker_label}")
    print(f"{'='*70}")

    # --- RSU ---
    if rsu_items:
        print(f"\n【一】RSU 归属所得（工资薪金所得）")
        print(f"  注：RSU 归属按工资薪金计税，公司预扣代缴，本工具不计算补缴")
        for item in rsu_items:
            print(f"  {item['date']}  {item['symbol']:10s} {item['detail']}")
            print(f"    归属收入: {format_decimal(item['gross_income_cny'])} | "
                  f"参考税额: {format_decimal(item['tax_amount_cny'])} | "
                  f"已预扣: {format_decimal(item['tax_withheld_cny'])} | "
                  f"应补缴: {format_decimal(item['tax_payable_cny'])}")
        total_rsu = sum(i['gross_income_cny'] for i in rsu_items)
        total_rsu_tax = sum(i['tax_amount_cny'] for i in rsu_items)
        print(f"  RSU归属合计: 收入 {format_decimal(total_rsu)} | 参考税额 {format_decimal(total_rsu_tax)}")
    else:
        print(f"\n【一】RSU 归属所得: 无")

    # --- Capital Gains ---
    print(f"\n【二】财产转让所得（股票/期权买卖）")
    if annual_net:
        print(f"  计税方法: 年度净额法（亏损已抵扣，税额更优）")
        print(f"    逐笔计算应纳税: {format_decimal(Decimal(str(annual_net['per_txn_tax_amount'])))}")
        print(f"    年度净额应纳税: {format_decimal(Decimal(str(annual_net['tax_amount_cny'])))}")
        print(f"    节省: {format_decimal(Decimal(str(annual_net['per_txn_tax_amount'] - annual_net['tax_amount_cny'])))}")
        for item in cg_items:
            print(f"  {item['date']}  {item['symbol']:20s}")
            print(f"    应税盈利: {format_decimal(item['taxable_income_cny'])} | "
                  f"税额: {format_decimal(item['tax_amount_cny'])} | "
                  f"预扣: {format_decimal(item['tax_withheld_cny'])} | "
                  f"抵免: {format_decimal(item['foreign_tax_credit_cny'])} | "
                  f"补缴: {format_decimal(item['tax_payable_cny'])}")
            print(f"    {item['detail']}")
    else:
        print(f"  计税方法: 逐笔计算（无亏损或年度净额法不优）")
        # Show summary stats
        total_proceeds = sum(i['gross_income_cny'] for i in cg_items)
        total_cost = sum(i['deductible_cny'] for i in cg_items)
        total_gain = sum(i['taxable_income_cny'] for i in cg_items)
        total_tax = sum(i['tax_amount_cny'] for i in cg_items)
        total_withheld = sum(i['tax_withheld_cny'] for i in cg_items)
        total_credit = sum(i['foreign_tax_credit_cny'] for i in cg_items)
        total_payable = sum(i['tax_payable_cny'] for i in cg_items)
        gain_count = len(cg_items)
        loss_count = sum(1 for r in broker_results["sell_results"] if r["gain_loss"] <= 0)
        expire_count = len(broker_results.get("expire_results", []))
        total_sell_count = len(broker_results["sell_results"])

        print(f"    卖出总笔数: {total_sell_count}（盈利 {gain_count} 笔，亏损 {loss_count} 笔，过期 {expire_count} 笔）")
        print(f"    总收入: {format_decimal(total_proceeds)} | "
              f"总成本: {format_decimal(total_cost)} | "
              f"总盈利: {format_decimal(total_gain)}")
        print(f"    应纳税: {format_decimal(total_tax)} | "
              f"已预扣: {format_decimal(total_withheld)} | "
              f"抵免: {format_decimal(total_credit)} | "
              f"补缴: {format_decimal(total_payable)}")

        # Show top 10 largest gains
        if cg_items:
            print(f"  盈利交易明细（Top 15 by tax amount）:")
            sorted_cg = sorted(cg_items, key=lambda x: x['tax_amount_cny'], reverse=True)
            for item in sorted_cg[:15]:
                print(f"    {item['date']}  {item['symbol']:20s} 盈利 {format_decimal(item['taxable_income_cny'])} | "
                      f"税 {format_decimal(item['tax_amount_cny'])} | 预扣 {format_decimal(item['tax_withheld_cny'])} | "
                      f"补缴 {format_decimal(item['tax_payable_cny'])}")
            if len(cg_items) > 15:
                print(f"    ... 及其他 {len(cg_items) - 15} 笔")

    cg_tax_payable = sum(i['tax_payable_cny'] for i in cg_items)
    cg_tax = sum(i['tax_amount_cny'] for i in cg_items)
    cg_credit = sum(i['foreign_tax_credit_cny'] for i in cg_items)

    # --- Dividends ---
    print(f"\n【三】股息红利所得（分红）")
    if div_items:
        for item in div_items:
            print(f"  {item['date']}  {item['symbol']:10s} {item['detail']}")
            refund_str = ""
            if item.get("withholding_refund_cny", 0) > 0:
                refund_str = f" | ROC返还 {format_decimal(item['withholding_refund_cny'])}"
            print(f"    分红: {format_decimal(item['gross_income_cny'])} | "
                  f"税: {format_decimal(item['tax_amount_cny'])} | "
                  f"净预扣: {format_decimal(item['tax_withheld_cny'])}{refund_str} | "
                  f"抵免: {format_decimal(item['foreign_tax_credit_cny'])} | "
                  f"补缴: {format_decimal(item['tax_payable_cny'])}")
    else:
        print(f"  无分红记录")
    div_tax_payable = sum(i['tax_payable_cny'] for i in div_items)
    div_tax = sum(i['tax_amount_cny'] for i in div_items)
    div_credit = sum(i['foreign_tax_credit_cny'] for i in div_items)

    # --- Interest Income ---
    print(f"\n【四】利息所得（债券利息、股票借贷收益）")
    if int_items:
        for item in int_items:
            print(f"  {item['date']}  {item['symbol']:10s} {item['detail']}")
            print(f"    利息收入: {format_decimal(item['gross_income_cny'])} | "
                  f"税: {format_decimal(item['tax_amount_cny'])} | "
                  f"预扣: {format_decimal(item['tax_withheld_cny'])} | "
                  f"抵免: {format_decimal(item['foreign_tax_credit_cny'])} | "
                  f"补缴: {format_decimal(item['tax_payable_cny'])}")
    else:
        print(f"  无利息收入记录")
    int_tax_payable = sum(i['tax_payable_cny'] for i in int_items)
    int_tax = sum(i['tax_amount_cny'] for i in int_items)
    int_credit = sum(i['foreign_tax_credit_cny'] for i in int_items)

    # --- Summary for this broker ---
    total_payable = cg_tax_payable + div_tax_payable + int_tax_payable
    total_tax = cg_tax + div_tax + int_tax
    total_credit = cg_credit + div_credit + int_credit
    total_excess = sum(i['excess_withholding_cny'] for i in cg_items + div_items + int_items)

    print(f"\n{'─'*60}")
    print(f"  {broker_label} 税务小结")
    print(f"{'─'*60}")
    if rsu_items:
        print(f"  RSU归属所得:     收入 {format_decimal(sum(i['gross_income_cny'] for i in rsu_items))}  "
              f"(税已预扣缴清)")
    print(f"  财产转让所得:     应纳税 {format_decimal(cg_tax)}  | 抵免 {format_decimal(cg_credit)}  | 补缴 {format_decimal(cg_tax_payable)}")
    print(f"  股息红利所得:     应纳税 {format_decimal(div_tax)}  | 抵免 {format_decimal(div_credit)}  | 补缴 {format_decimal(div_tax_payable)}")
    print(f"  利息所得:         应纳税 {format_decimal(int_tax)}  | 抵免 {format_decimal(int_credit)}  | 补缴 {format_decimal(int_tax_payable)}")
    print(f"  ─────────────────────────────────────────────────────────")
    print(f"  合计应补缴:        {format_decimal(total_payable)}")
    if total_excess > 0:
        print(f"  超额未抵免:        {format_decimal(total_excess)}")

    per_txn_tax = annual_net['per_txn_tax_amount'] if annual_net else cg_tax

    return {
        "rsu_income": sum(i['gross_income_cny'] for i in rsu_items),
        "rsu_tax": sum(i['tax_amount_cny'] for i in rsu_items),
        "cg_tax": cg_tax,
        "cg_credit": cg_credit,
        "cg_payable": cg_tax_payable,
        "div_tax": div_tax,
        "div_credit": div_credit,
        "div_payable": div_tax_payable,
        "int_tax": int_tax,
        "int_credit": int_credit,
        "int_payable": int_tax_payable,
        "total_payable": total_payable,
        "total_excess": total_excess,
        "method": method,
        "per_txn_tax": Decimal(str(per_txn_tax)),
    }


def compute_rsu_tax(all_rsu: list[dict]) -> list[dict]:
    """计算RSU归属税额（单独处理，不按券商分配）"""
    load_exchange_rates()
    rsu_items = []
    for r in all_rsu:
        rate = get_exchange_rate(r["vest_date"], r["currency"])
        taxable_cny = (r["taxable_income"] * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        tax_amt_cny = (r["tax_amount"] * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        rsu_items.append({
            "date": str(r["vest_date"]),
            "symbol": r["symbol"],
            "income_type": "rsu_vest",
            "gross_income_cny": taxable_cny,
            "deductible_cny": Decimal("0"),
            "taxable_income_cny": taxable_cny,
            "tax_rate": Decimal("0"),  # RSU 属工资薪金所得，适用 3%-45% 超额累进税率
                              # 单独计税（财税〔2018〕164号），此处仅作记录，不计算补缴
            "tax_amount_cny": tax_amt_cny,
            "tax_withheld_cny": tax_amt_cny,
            "foreign_tax_credit_cny": Decimal("0"),  # RSU 属工资薪金，不适用境外税收抵免
            "tax_payable_cny": Decimal("0"),
            "detail": f"RSU归属 {r['vested_quantity']} 股 @ {r['fmv_per_share']} {r['currency']}",
        })
    return rsu_items


def main():
    print("=" * 70)
    print("  2025年度个人所得税核算报告 — 分账户参考版")
    print("  ⚠  本报告按账户分别计算，仅供参考，不可用于正式申报")
    print("  正式申报请使用主引擎 compute_tax() 的聚合结果（所有账户合并 FIFO）")
    print("=" * 70)
    print(f"  税率: 财产转让 20% | 股息红利 20%")
    print(f"  抵免: 分国限额法（美国/香港）")
    print(f"  汇率: 从数据库加载历史汇率")

    transactions, dividends, rsu_vests, _ = load_2025_data()

    # 通过2024年交易构建年末持仓（2025年初起点）
    lots_2024 = load_2024_year_end_lots()

    # 按broker过滤2024年末持仓
    broker_lots_2024 = {
        "boci": {},
        "futu": {},
        "longbridge": lots_2024,
    }

    brokers = {
        "boci": "BOCI 中银国际",
        "futu": "Futu 富途",
        "longbridge": "Longbridge 长桥",
    }

    broker_results = {}
    broker_summaries = {}

    for broker_code, broker_label in brokers.items():
        results = compute_per_broker_tax(
            broker_code, transactions, dividends,
            year=2025, lots_2024=broker_lots_2024[broker_code],
        )
        broker_results[broker_code] = results
        broker_summaries[broker_code] = print_report(broker_code, results, broker_label)

    # === Grand Total ===
    print(f"\n\n{'='*70}")
    print(f"  2025年度 汇总总览")
    print(f"{'='*70}")

    # RSU summary
    rsu_tax_items = compute_rsu_tax(rsu_vests)
    total_rsu_income = sum(i['gross_income_cny'] for i in rsu_tax_items)
    total_rsu_tax = sum(i['tax_amount_cny'] for i in rsu_tax_items)

    print(f"\n【一】RSU 归属所得（工资薪金）")
    print(f"  归属收入合计: {format_decimal(total_rsu_income)}")
    print(f"  参考税额合计: {format_decimal(total_rsu_tax)} （已由公司预扣代缴）")
    print(f"  应补缴: ¥0.00（RSU作为工资薪金，已在归属时完税）")
    print(f"  明细:")
    for item in rsu_tax_items:
        print(f"    {item['date']}  {item['symbol']:10s} {item['detail']}")
        print(f"      归属收入: {format_decimal(item['gross_income_cny'])} | 参考税额: {format_decimal(item['tax_amount_cny'])}")

    # Capital gains comparison
    print(f"\n【二】财产转让所得（股票 + 期权）")
    total_cg_tax = sum(s["cg_tax"] for s in broker_summaries.values())
    total_cg_credit = sum(s["cg_credit"] for s in broker_summaries.values())
    total_cg_payable = sum(s["cg_payable"] for s in broker_summaries.values())

    annual_net_used = any(s["method"] == "annual_net" for s in broker_summaries.values())
    print(f"  计税方法: {'年度净额法（部分账户）' if annual_net_used else '逐笔计算'}")
    print(f"  应纳税合计: {format_decimal(total_cg_tax)}")
    print(f"  境外抵免:   {format_decimal(total_cg_credit)}")
    print(f"  应补缴:     {format_decimal(total_cg_payable)}")

    # Dividends
    print(f"\n【三】股息红利所得（分红）")
    total_div_tax = sum(s["div_tax"] for s in broker_summaries.values())
    total_div_credit = sum(s["div_credit"] for s in broker_summaries.values())
    total_div_payable = sum(s["div_payable"] for s in broker_summaries.values())
    print(f"  应纳税合计: {format_decimal(total_div_tax)}")
    print(f"  境外抵免:   {format_decimal(total_div_credit)}")
    print(f"  应补缴:     {format_decimal(total_div_payable)}")

    # Final
    grand_total_payable = total_cg_payable + total_div_payable
    grand_total_excess = sum(s["total_excess"] for s in broker_summaries.values())

    print(f"\n{'='*70}")
    print(f"  最终结果")
    print(f"{'='*70}")
    print(f"  RSU归属所得:      收入 {format_decimal(total_rsu_income)}  (税已缴)")
    print(f"  财产转让所得:     补缴 {format_decimal(total_cg_payable)}")
    print(f"  股息红利所得:     补缴 {format_decimal(total_div_payable)}")
    print(f"  {'─'*50}")
    print(f"  合计应补缴税额:    {format_decimal(grand_total_payable)}")
    if grand_total_excess > 0:
        print(f"  超额未抵免(可结转): {format_decimal(grand_total_excess)}")
    print(f"\n{'='*70}")
    print(f"  分账户对比：逐笔计算 vs 年度净额")
    print(f"{'='*70}")
    print(f"  {'账户':<20} {'逐笔计算(元)':>18} {'年度净额(元)':>18} {'节省(元)':>15}")
    print(f"  {'─'*20} {'─'*18} {'─'*18} {'─'*15}")
    total_per_txn = Decimal("0")
    total_annual_net_tax = Decimal("0")
    for code, label in brokers.items():
        s = broker_summaries[code]
        per_txn = s['per_txn_tax']
        annual = s['cg_tax']
        saving = per_txn - annual
        total_per_txn += per_txn
        total_annual_net_tax += annual
        print(f"  {label:<20} {format_decimal(per_txn):>18} {format_decimal(annual):>18} {format_decimal(saving):>15}")
    print(f"  {'─'*20} {'─'*18} {'─'*18} {'─'*15}")
    print(f"  {'合计':<20} {format_decimal(total_per_txn):>18} {format_decimal(total_annual_net_tax):>18} {format_decimal(total_per_txn - total_annual_net_tax):>15}")

    # Final payable comparison
    print(f"\n{'='*70}")
    print(f"  最终结果对比")
    print(f"{'='*70}")
    print(f"  {'':<20} {'逐笔计算':>15} {'年度净额':>15} {'差异':>15}")
    print(f"  {'─'*20} {'─'*15} {'─'*15} {'─'*15}")
    per_txn_total_payable = total_per_txn + total_div_payable
    annual_net_total_payable = total_annual_net_tax + total_div_payable
    print(f"  {'财产转让+分红':<18} {format_decimal(per_txn_total_payable):>15} {format_decimal(annual_net_total_payable):>15} {format_decimal(per_txn_total_payable - annual_net_total_payable):>15}")
    print(f"  {'其中: 分红':<18} {format_decimal(total_div_payable):>15} {format_decimal(total_div_payable):>15} {'¥0.00':>15}")
    print(f"\n  实际应申报: {format_decimal(annual_net_total_payable)}（年度净额法，亏损已抵扣）")
    print(f"  若按逐笔: {format_decimal(per_txn_total_payable)}（亏损不抵扣，税额更高）")

    # === Tax optimization notes ===
    print(f"\n{'='*70}")
    print(f"  税务优化建议")
    print(f"{'='*70}")
    if annual_net_used:
        print(f"  1. 年度净额法已自动适用：亏损抵扣盈利，降低应纳税额")
    else:
        print(f"  1. 当前无亏损交易，逐笔计算即最优方案")
    print(f"  2. 超额未抵免税额 {format_decimal(grand_total_excess)} 可在以后5个纳税年度内结转抵免")
    print(f"  3. RSU归属作为工资薪金所得，需并入综合所得年度汇算清缴")
    print(f"  4. 境外所得需自行申报，年度终了后3月1日至6月30日办理汇算清缴")
    print(f"\n  注：本核算仅供参考，具体申报请以主管税务机关要求为准")
    print(f"  汇率依据：exchange_rates.csv 历史汇率")


if __name__ == "__main__":
    main()
