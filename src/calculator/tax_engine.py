from __future__ import annotations
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from src.models import Transaction, TaxItem, TaxSummary, TaxLot
from src.calculator.fifo import FIFOEngine
from src.calculator.exchange_rate import get_exchange_rate
from collections import defaultdict

CAPITAL_GAINS_RATE = Decimal("0.20")
DIVIDEND_RATE = Decimal("0.20")

# RSU 股权激励所得 7 档超额累进税率（财税〔2018〕164号）
# (上限, 税率, 速算扣除数)
RSU_TAX_BRACKETS = [
    (36000, Decimal("0.03"), Decimal("0")),
    (144000, Decimal("0.10"), Decimal("2520")),
    (300000, Decimal("0.20"), Decimal("16920")),
    (420000, Decimal("0.25"), Decimal("31920")),
    (660000, Decimal("0.30"), Decimal("52920")),
    (960000, Decimal("0.35"), Decimal("85920")),
    (float("inf"), Decimal("0.45"), Decimal("181920")),
]


def compute_rsu_progressive_tax(taxable_income_cny: Decimal) -> tuple[Decimal, Decimal]:
    """计算 RSU 股权激励所得应纳税额（3%~45% 超额累进）

    Returns: (应纳税额, 适用税率)
    """
    for limit, rate, deduction in RSU_TAX_BRACKETS:
        if taxable_income_cny <= Decimal(str(limit)):
            tax = (taxable_income_cny * rate - deduction).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            return max(tax, Decimal("0")), rate
    # Should not reach here
    return Decimal("0"), Decimal("0")


def detect_country(item: TaxItem) -> str:
    """根据币种判断收入来源国（用于分国抵免限额法）

    依据财税〔2020〕3号，抵免限额分国计算：
    - USD → 美国（美股或美股 ADR）
    - HKD → 香港（港股）
    - CNY → 中国境内（如 RSU 代扣，不参与境外抵免）

    注意：若出现其他币种（GBP/EUR/JPY 等），将触发 WARNING 并默认归入 US 池。
    分国抵免限额法下，错误归类会导致抵免限额计算错误。
    扩展时应在此函数中添加对应分支，并同步更新 group_taxes 逻辑。
    """
    if item.currency == "HKD":
        return "HK"
    if item.currency == "CNY":
        return "CN"  # 境内收入，不参与境外抵免
    if item.currency == "USD":
        return "US"
    # 未知币种：返回 UNKNOWN，后续 FTC 处理中跳过抵免
    return "UNKNOWN"


def clamp_tax_payable(tax_amount: Decimal, foreign_credit: Decimal) -> Decimal:
    """计算应补缴税额，保证不为负"""
    payable = tax_amount - foreign_credit
    if payable < 0:
        return Decimal("0")
    return payable.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def compute_tax(
    transactions: list[Transaction],
    year: int,
    existing_lots: dict[str, list[TaxLot]] | None = None,
    carryforwards: dict[tuple[str, str], list[dict]] | None = None,
) -> tuple[TaxSummary, dict[str, list[TaxLot]], list[dict]]:
    """计算指定年度个人所得税

    范围：
    - 卖出盈利 → 财产转让所得 20%
    - 分红 → 股息红利所得 20%
    - RSU 归属 → 工资薪金所得 3%~45% 累进（单独计税）

    卖出计税方式：自动选择税额较低者
    - 方法A：逐笔计算（每笔盈利独立计税，亏损不抵扣）
    - 方法B：年度净额（全年盈亏相抵后按净额 × 20%）
    - 同一纳税年度内方法一致，不同年度独立选择

    Args:
        transactions: 全部交易记录（跨年度）
        year: 计税年度
        existing_lots: 上年末剩余持仓（跨年持仓）
    """
    fifo = FIFOEngine(existing_lots=existing_lots)
    capital_gain_items: list[TaxItem] = []
    dividend_items: list[TaxItem] = []
    fee_items: list[TaxItem] = []
    rsu_vest_txns: list[Transaction] = []  # 收集 RSU_VEST 用于年末累进税计算
    all_sell_results: list[dict] = []
    all_expire_results: list[dict] = []
    lot_consumptions: list[dict] = []  # 记录每个 lot 的消耗详情（用于审计）
    net_loss_excess_withholding = Decimal("0")  # 净亏损年度 excess_withholding 审计留痕

    # 初始化结转抵免（直接引用调用方的字典，H-2/H-1 修复需要 caller 看到新 key 和突变）
    if carryforwards is None:
        carryforwards = {}

    # 排序：先按日期，再按 action 优先级（买入→卖出→分红/利息→过期→费用），最后按 ID
    ACTION_PRIORITY = {
        "BUY": 0, "RSU_VEST": 0, "OPTION_BUY": 0, "OPTION_EXERCISE": 0,
        "SELL": 1, "RSU_SELL": 1, "OPTION_SELL": 1,
        "DIVIDEND": 2, "INTEREST": 2, "YIELD_INCOME": 2,
        "OPTION_EXPIRE": 3,
        "FEE": 4,
    }
    sorted_txns = sorted(transactions, key=lambda t: (
        t.date,
        ACTION_PRIORITY.get(t.action.name, 5),
        int(t.id) if t.id and str(t.id).isdigit() else 0,
    ))

    for txn in sorted_txns:
        if txn.exchange_rate == 0:
            txn.exchange_rate = get_exchange_rate(txn.date, txn.currency, year=year)

        if txn.action.name in ("BUY", "OPTION_BUY"):
            origin = txn.origin or (
                "option_buy" if txn.action.name == "OPTION_BUY"
                else "buy"
            )
            fifo.buy(txn.broker, txn.symbol, txn.quantity, txn.price, txn.date, origin=origin)

        elif txn.action.name == "RSU_VEST":
            # RSU 归属：入队 FIFO（成本 = 归属日 FMV），同时收集用于累进税计算
            fifo.buy(txn.broker, txn.symbol, txn.quantity, txn.price, txn.date, origin="rsu_vest")
            rsu_vest_txns.append(txn)

        elif txn.action.name == "OPTION_EXERCISE":
            # 期权行权：创建股票 lot，成本 = 行权价 + 原始权利金
            # CSV 路径要求 txn.price 已包含 strike + premium
            fifo.exercise(txn.broker, txn.symbol, txn.quantity, txn.price, txn.date)

        elif txn.action.name == "OPTION_EXPIRE":
            # 期权过期作废：全部成本作为已实现损失
            expire_results = fifo.expire(txn.broker, txn.symbol, txn.quantity, txn.date)
            all_expire_results.extend(expire_results)

            # 为每个过期生成 TaxItem 留痕（即使不参与征税）
            for r in expire_results:
                loss_cny = (r["gain_loss"] * txn.exchange_rate).quantize(Decimal("0.01"))
                cost_cny = (r["cost_basis"] * txn.exchange_rate).quantize(Decimal("0.01"))
                capital_gain_items.append(TaxItem(
                    date=str(txn.date),
                    symbol=txn.symbol,
                    income_type="capital_gain_expire_loss",
                    currency=txn.currency,
                    gross_income_cny=Decimal("0"),
                    deductible_cny=cost_cny,
                    taxable_income_cny=Decimal("0"),  # 损失不征税
                    tax_rate=CAPITAL_GAINS_RATE,
                    tax_amount_cny=Decimal("0"),
                    tax_withheld_cny=Decimal("0"),
                    foreign_tax_credit_cny=Decimal("0"),
                    excess_withholding_cny=Decimal("0"),
                    tax_payable_cny=Decimal("0"),
                    detail=f"期权过期 {r['quantity']} 份, 损失 {abs(loss_cny)} CNY",
                ))

                # 记录 lot 消耗详情（用于审计追踪）
                lot_consumptions.append({
                    "sell_txn_id": txn.id,
                    "sell_date": str(txn.date),
                    "sell_action": "OPTION_EXPIRE",
                    "sell_broker_code": txn.broker,
                    "symbol": r["symbol"],
                    "lot_broker_code": r.get("broker_code", txn.broker),
                    "consumed_qty": r["quantity"],
                    "cost_per_share": str(r["cost_per_share"]),
                    "cost_basis": str(r["cost_basis"]),
                    "sell_price": str(r.get("sell_price", "0")),
                    "proceeds": str(r["proceeds"]),
                    "gain_loss": str(r["gain_loss"]),
                    "lot_date": str(r["lot_date"]),
                    "lot_origin": r["origin"],
                    "currency": txn.currency,
                })

        elif txn.action.name in ("SELL", "RSU_SELL", "OPTION_SELL"):
            # 期权卖出允许 short_allowed（无买入记录时视为写仓），股票/RSU 严格匹配
            is_option_sell = "_OPT_" in txn.symbol.upper()
            sell_results = fifo.sell(
                txn.broker, txn.symbol, txn.quantity, txn.price, txn.date, txn.fee,
                short_allowed=is_option_sell,
            )
            all_sell_results.extend(sell_results)

            # 记录 lot 消耗详情（用于审计追踪）
            for r in sell_results:
                lot_consumptions.append({
                    "sell_txn_id": txn.id,
                    "sell_date": str(txn.date),
                    "sell_action": txn.action.name,
                    "sell_broker_code": txn.broker,
                    "symbol": r["symbol"],
                    "lot_broker_code": r.get("broker_code", txn.broker),
                    "consumed_qty": r["quantity"],
                    "cost_per_share": str(r["cost_per_share"]),
                    "cost_basis": str(r["cost_basis"]),
                    "sell_price": str(r["sell_price"]),
                    "proceeds": str(r["proceeds"]),
                    "gain_loss": str(r["gain_loss"]),
                    "lot_date": str(r["lot_date"]),
                    "lot_origin": r["origin"],
                    "currency": txn.currency,
                })

            # 预扣税按 proceeds 比例分配到各 lot 消耗（C5 修复）
            # 单笔卖出可能消耗多个 FIFO lot，若每个 lot 都附加全额 tax_withheld，
            # 会导致预扣税重复计算，虚增 FTC。
            total_proceeds = sum(r["proceeds"] for r in sell_results)
            withholding_alloc: dict[int, Decimal] = {}
            if total_proceeds > 0 and txn.tax_withheld_cny > 0:
                for idx, r in enumerate(sell_results):
                    ratio = r["proceeds"] / total_proceeds
                    withholding_alloc[idx] = (txn.tax_withheld_cny * ratio).quantize(Decimal("0.01"))

            for idx, r in enumerate(sell_results):
                gain_cny = (r["gain_loss"] * txn.exchange_rate).quantize(Decimal("0.01"))
                proceeds_cny = (r["proceeds"] * txn.exchange_rate).quantize(Decimal("0.01"))
                cost_cny = (r["cost_basis"] * txn.exchange_rate).quantize(Decimal("0.01"))

                if gain_cny > 0:
                    capital_gain_items.append(TaxItem(
                        date=str(txn.date),
                        symbol=txn.symbol,
                        income_type="capital_gain",
                        currency=txn.currency,
                        gross_income_cny=proceeds_cny,
                        deductible_cny=cost_cny,
                        taxable_income_cny=gain_cny,
                        tax_rate=CAPITAL_GAINS_RATE,
                        tax_amount_cny=(gain_cny * CAPITAL_GAINS_RATE).quantize(Decimal("0.01")),
                        tax_withheld_cny=withholding_alloc.get(idx, Decimal("0")),
                        foreign_tax_credit_cny=Decimal("0"),
                        excess_withholding_cny=Decimal("0"),
                        tax_payable_cny=Decimal("0"),
                        detail=f"卖出 {r['quantity']} 股, 成本 {r['cost_per_share']} {txn.currency}, "
                               f"收益 {gain_cny} CNY",
                    ))

        elif txn.action.name == "DIVIDEND":
            gross_cny = txn.amount_cny
            taxable_cny = gross_cny  # 股息红利按"每次收入额"计税，无费用扣除（个税法第六条）
            tax_amount = (taxable_cny * DIVIDEND_RATE).quantize(Decimal("0.01"))

            dividend_items.append(TaxItem(
                date=str(txn.date),
                symbol=txn.symbol,
                income_type="dividend",
                currency=txn.currency,
                gross_income_cny=gross_cny,
                taxable_income_cny=taxable_cny,
                tax_rate=DIVIDEND_RATE,
                tax_amount_cny=tax_amount,
                tax_withheld_cny=txn.tax_withheld_cny,
                foreign_tax_credit_cny=Decimal("0"),
                excess_withholding_cny=Decimal("0"),
                tax_payable_cny=Decimal("0"),
                detail=f"分红 {txn.quantity} 股 @ {txn.price} {txn.currency}"
                       + (f"，费用 {(txn.fee * txn.exchange_rate).quantize(Decimal('0.01'))} CNY" if txn.fee else ""),
            ))

        elif txn.action.name == "INTEREST":
            # 利息所得（债券利息、股票借贷收益等），按 20% 税率计税
            gross_cny = txn.amount_cny
            tax_amount = (gross_cny * DIVIDEND_RATE).quantize(Decimal("0.01"))

            dividend_items.append(TaxItem(
                date=str(txn.date),
                symbol=txn.symbol,
                income_type="interest_income",
                currency=txn.currency,
                gross_income_cny=gross_cny,
                taxable_income_cny=gross_cny,
                tax_rate=DIVIDEND_RATE,
                tax_amount_cny=tax_amount,
                tax_withheld_cny=txn.tax_withheld_cny,
                foreign_tax_credit_cny=Decimal("0"),
                excess_withholding_cny=Decimal("0"),
                tax_payable_cny=Decimal("0"),
                detail=f"利息收入 {txn.amount} {txn.currency}",
            ))

        elif txn.action.name == "YIELD_INCOME":
            # 投资收益/分红收益（BOCI 特有），按 20% 税率计税
            gross_cny = txn.amount_cny
            tax_amount = (gross_cny * DIVIDEND_RATE).quantize(Decimal("0.01"))

            dividend_items.append(TaxItem(
                date=str(txn.date),
                symbol=txn.symbol,
                income_type="yield_income",
                currency=txn.currency,
                gross_income_cny=gross_cny,
                taxable_income_cny=gross_cny,
                tax_rate=DIVIDEND_RATE,
                tax_amount_cny=tax_amount,
                tax_withheld_cny=txn.tax_withheld_cny,
                foreign_tax_credit_cny=Decimal("0"),
                excess_withholding_cny=Decimal("0"),
                tax_payable_cny=Decimal("0"),
                detail=f"投资收益 {txn.amount} {txn.currency}",
            ))

        elif txn.action.name == "FEE":
            # 费用交易：记录为可抵扣费用，减少应纳税额
            fee_cny = (txn.amount * txn.exchange_rate).quantize(Decimal("0.01"))
            fee_items.append(TaxItem(
                date=str(txn.date),
                symbol=txn.symbol,
                income_type="fee_expense",
                currency=txn.currency,
                gross_income_cny=fee_cny,
                deductible_cny=fee_cny,
                taxable_income_cny=Decimal("0"),
                tax_rate=Decimal("0"),
                tax_amount_cny=Decimal("0"),
                tax_withheld_cny=Decimal("0"),
                foreign_tax_credit_cny=Decimal("0"),
                excess_withholding_cny=Decimal("0"),
                tax_payable_cny=Decimal("0"),
                detail=f"费用: {txn.symbol} {txn.amount} {txn.currency}",
            ))

    # ===== RSU 归属收入：3%~45% 超额累进税（财税〔2018〕164号）=====
    # RSU 属"股权激励所得"，单独计税不并入综合所得，适用综合所得税率表
    rsu_income_item: TaxItem | None = None
    if rsu_vest_txns:
        total_rsu_income_cny = sum(t.amount_cny for t in rsu_vest_txns)
        total_rsu_withheld_cny = sum(t.tax_withheld_cny for t in rsu_vest_txns)
        tax_amount_cny, effective_rate = compute_rsu_progressive_tax(total_rsu_income_cny)

        # RSU 代扣税性质认定：
        # 依据 tax_policy.md 第八节，RSU 归属时的中国境内代扣个税属于国内税，
        # 不参与境外税收抵免（FTC）。已代扣税额直接冲减应补缴税额。
        # 这与境外分红预扣税（W-8BEN 10%）走 FTC 通道不同。
        domestic_withheld = total_rsu_withheld_cny
        tax_payable = clamp_tax_payable(tax_amount_cny, domestic_withheld)

        rsu_income_item = TaxItem(
            date=f"{year}-12-31",
            symbol="RSU汇总",
            income_type="rsu_income",
            currency="CNY",
            gross_income_cny=total_rsu_income_cny,
            deductible_cny=Decimal("0"),
            taxable_income_cny=total_rsu_income_cny,
            tax_rate=effective_rate,
            tax_amount_cny=tax_amount_cny,
            tax_withheld_cny=domestic_withheld,  # 境内代扣
            foreign_tax_credit_cny=Decimal("0"),  # 不走 FTC 通道
            excess_withholding_cny=Decimal("0"),
            domestic_withheld_cny=domestic_withheld,  # 境内代扣代缴
            tax_payable_cny=tax_payable,
            detail=f"RSU 归属 {len(rsu_vest_txns)} 笔，"
                   f"收入 {total_rsu_income_cny} CNY，"
                   f"适用税率 {effective_rate}，"
                   f"境内已代扣 {domestic_withheld} CNY",
        )

    # ===== 资本利得：自动择优（逐笔 vs 年度净额）=====
    # 依据《个人所得税法实施条例》第十九条，两种方法均有法律依据。
    # 系统自动选择税额较低者，纳税人有利。
    # 同一纳税年度内方法保持一致，不同年度可独立选择。
    # 期权过期损失也计入年度净盈亏

    # 统计是否有亏损/过期（触发年度净额法的条件）
    has_losses = (any(r["gain_loss"] < 0 for r in all_sell_results)
                  or len(all_expire_results) > 0)
    method_used = "per_transaction"
    annual_net_info: dict | None = None

    if has_losses and (all_sell_results or all_expire_results):
        # 构建卖出交易的汇率映射 (symbol, date) -> exchange_rate
        sell_rate_map: dict[tuple[str, str], Decimal] = {}
        for txn in sorted_txns:
            if txn.action.name in ("SELL", "RSU_SELL", "OPTION_SELL"):
                sell_rate_map[(txn.symbol, str(txn.date))] = txn.exchange_rate
            elif txn.action.name == "OPTION_EXPIRE":
                sell_rate_map[(txn.symbol, str(txn.date))] = txn.exchange_rate

        # 计算年度净盈亏（CNY）— 包含卖出和过期损失
        net_gain_cny = Decimal("0")
        for r in all_sell_results:
            rate = sell_rate_map.get((r["symbol"], str(r["sell_date"])), Decimal("1"))
            gain_cny = (r["gain_loss"] * rate).quantize(Decimal("0.01"))
            net_gain_cny += gain_cny
        for r in all_expire_results:
            rate = sell_rate_map.get((r["symbol"], str(r["sell_date"])), Decimal("1"))
            loss_cny = (r["gain_loss"] * rate).quantize(Decimal("0.01"))
            net_gain_cny += loss_cny

        # 汇总所有卖出交易的预扣税
        total_cg_withheld_cny = Decimal("0")
        for txn in sorted_txns:
            if txn.action.name in ("SELL", "RSU_SELL", "OPTION_SELL", "OPTION_EXPIRE"):
                total_cg_withheld_cny += txn.tax_withheld_cny

        net_gain_cny = net_gain_cny.quantize(Decimal("0.01"))

        per_txn_total_tax = sum(i.tax_amount_cny for i in capital_gain_items)

        if net_gain_cny > 0:
            annual_net_tax_cny = (net_gain_cny * CAPITAL_GAINS_RATE).quantize(Decimal("0.01"))

            if annual_net_tax_cny < per_txn_total_tax:
                method_used = "annual_net"
                # 保留 expire 审计留痕记录
                expire_items_for_preserve = [
                    i for i in capital_gain_items
                    if i.income_type == "capital_gain_expire_loss"
                ]
                annual_net_info = {
                    "net_gain_cny": float(net_gain_cny),
                    "tax_amount_cny": float(annual_net_tax_cny),
                    "per_txn_tax_amount": float(per_txn_total_tax),
                }
                loss_count = sum(1 for r in all_sell_results if r["gain_loss"] <= 0)
                expire_count = len(all_expire_results)
                expire_detail = f"，{expire_count} 笔期权过期损失" if expire_count > 0 else ""
                capital_gain_items = [TaxItem(
                    date=f"{year}-01-01",
                    symbol="多只股票",
                    income_type="capital_gain_annual_net",
                    currency="USD",
                    gross_income_cny=Decimal("0"),
                    deductible_cny=Decimal("0"),
                    taxable_income_cny=net_gain_cny,
                    tax_rate=CAPITAL_GAINS_RATE,
                    tax_amount_cny=annual_net_tax_cny,
                    tax_withheld_cny=total_cg_withheld_cny,
                    foreign_tax_credit_cny=Decimal("0"),
                    excess_withholding_cny=Decimal("0"),
                    tax_payable_cny=Decimal("0"),
                    detail=f"年度净额法：{len(all_sell_results)} 笔卖出，"
                           f"{loss_count} 笔亏损已抵扣{expire_detail}，"
                           f"（逐笔计算应纳税 {per_txn_total_tax} CNY）",
                )]
                # 恢复 expire 审计留痕记录
                capital_gain_items.extend(expire_items_for_preserve)
        else:
            # 年度净亏损：选择年度净额法，逐笔计算的税项清零（亏损不征税）
            # 保留 expire 审计留痕记录
            expire_items_for_preserve = [
                i for i in capital_gain_items
                if i.income_type == "capital_gain_expire_loss"
            ]
            method_used = "annual_net"
            annual_net_info = {
                "net_gain_cny": float(net_gain_cny),
                "tax_amount_cny": 0.0,
                "per_txn_tax_amount": float(per_txn_total_tax),
            }
            loss_count = sum(1 for r in all_sell_results if r["gain_loss"] <= 0)
            expire_count = len(all_expire_results)
            expire_detail = f"，{expire_count} 笔期权过期损失" if expire_count > 0 else ""

            # 年度净亏损：境外预扣税不生成结转（财税〔2020〕3号：
            # 仅当抵免限额 > 实际已缴税额时，剩余限额可结转；亏损年度限额为 0，
            # 不存在可结转额度。已扣税款仅做审计留痕，不参与后续抵免）
            profitable_withheld = sum((item.tax_withheld_cny for item in capital_gain_items), Decimal("0"))
            net_loss_excess_withholding = profitable_withheld.quantize(Decimal("0.01"))

            capital_gain_items = [TaxItem(
                date=f"{year}-01-01",
                symbol="多只股票",
                income_type="capital_gain_annual_net",
                currency="USD",
                gross_income_cny=Decimal("0"),
                deductible_cny=Decimal("0"),
                taxable_income_cny=Decimal("0"),
                tax_rate=CAPITAL_GAINS_RATE,
                tax_amount_cny=Decimal("0"),
                tax_withheld_cny=total_cg_withheld_cny,
                foreign_tax_credit_cny=Decimal("0"),
                excess_withholding_cny=Decimal("0"),  # FTC 循环后会被重置，下面再恢复
                tax_payable_cny=Decimal("0"),
                detail=f"年度净额法：{len(all_sell_results)} 笔卖出，"
                       f"{loss_count} 笔亏损已抵扣{expire_detail}，"
                       f"年度净亏损 {abs(net_gain_cny)} CNY（不征税）"
                       + (f"，外国已扣税 {profitable_withheld} CNY 当年作废" if profitable_withheld > 0 else ""),
            )]
            # 恢复 expire 审计留痕记录
            capital_gain_items.extend(expire_items_for_preserve)

            # FTC 循环会重置 excess_withholding_cny，净亏损年份需在之后恢复审计留痕值
            _net_loss_item_idx = 0

    # ===== 境外税收抵免 — 分国+分项限额法 =====
    # 依据：财税〔2020〕3号，境外税收抵免应分国且分项计算
    # 即：美国资本利得、美国分红、香港资本利得、香港分红分别计算抵免限额
    # 不同收入类别之间的抵免限额不得互相调剂

    # 按 (country, income_category) 分组
    # 依据财税〔2020〕3号第七条，不同收入类别分别计算抵免限额
    # income_category: "capital_gain" / "dividend" / "interest"
    group_taxes: dict[tuple[str, str], dict[str, Decimal]] = defaultdict(
        lambda: {"tax_amount": Decimal("0"), "tax_withheld": Decimal("0")}
    )

    for item in capital_gain_items:
        country = detect_country(item)
        key = (country, "capital_gain")
        group_taxes[key]["tax_amount"] += item.tax_amount_cny
        group_taxes[key]["tax_withheld"] += item.tax_withheld_cny

    for item in dividend_items:
        country = detect_country(item)
        if item.income_type.startswith("interest"):
            key = (country, "interest")
        elif item.income_type.startswith("yield"):
            key = (country, "yield")
        else:
            key = (country, "dividend")
        group_taxes[key]["tax_amount"] += item.tax_amount_cny
        group_taxes[key]["tax_withheld"] += item.tax_withheld_cny

    # 计算每个组的抵免额和超额
    for key, taxes in group_taxes.items():
        if taxes["tax_withheld"] > 0:
            credit = min(taxes["tax_withheld"], taxes["tax_amount"])
            excess = taxes["tax_withheld"] - credit
            taxes["credit"] = credit.quantize(Decimal("0.01"))
            taxes["excess"] = excess.quantize(Decimal("0.01"))
        else:
            taxes["credit"] = Decimal("0")
            taxes["excess"] = Decimal("0")

    # 按组内比例分配抵免额到每个 item
    all_items = capital_gain_items + dividend_items
    for item in all_items:
        item.foreign_tax_credit_cny = Decimal("0")
        item.excess_withholding_cny = Decimal("0")
        item.tax_payable_cny = clamp_tax_payable(item.tax_amount_cny, Decimal("0"))

    for item in all_items:
        country = detect_country(item)
        if item.income_type.startswith("interest"):
            category = "interest"
        elif item.income_type.startswith("yield"):
            category = "yield"
        elif item.income_type.startswith("dividend"):
            category = "dividend"
        else:
            category = "capital_gain"
        key = (country, category)
        taxes = group_taxes[key]

        if taxes["credit"] > 0:
            # 组内按 tax_amount 比例分配
            group_total_tax = taxes["tax_amount"]
            if group_total_tax > 0:
                ratio = item.tax_amount_cny / group_total_tax
                item.foreign_tax_credit_cny = (taxes["credit"] * ratio).quantize(Decimal("0.01"))
                item.excess_withholding_cny = (taxes["excess"] * ratio).quantize(Decimal("0.01"))
                item.tax_payable_cny = clamp_tax_payable(item.tax_amount_cny, item.foreign_tax_credit_cny)

    # ===== 境外税收抵免结转（财税〔2020〕3号：超额可向后结转 5 年）=====
    # H-1 修复：按 source_year ASC 顺序（最早先到期）消耗结转，避免按比例分摊
    # carryforwards 格式：dict[tuple, list[dict]]，每个 list 按 source_year ASC 排序
    if carryforwards:
        for item in capital_gain_items + dividend_items:
            if item.tax_payable_cny > 0:
                country = detect_country(item)
                if item.income_type.startswith("interest"):
                    category = "interest"
                elif item.income_type.startswith("yield"):
                    category = "yield"
                elif item.income_type.startswith("dividend"):
                    category = "dividend"
                else:
                    category = "capital_gain"
                key = (country, category)
                records = carryforwards.get(key, [])
                remaining_payable = item.tax_payable_cny
                for record in records:
                    if remaining_payable <= 0:
                        break
                    # 跳过已过期的结转记录（财税〔2020〕3号：5年有效期）
                    if record.get("expires_year", year) < year:
                        continue
                    rec_remaining = Decimal(str(record.get("remaining_amount", 0)))
                    if rec_remaining <= 0:
                        continue
                    use_amount = min(rec_remaining, remaining_payable)
                    item.foreign_tax_credit_cny += use_amount
                    remaining_payable -= use_amount
                    record["remaining_amount"] = float((rec_remaining - use_amount).quantize(Decimal("0.01")))
                item.excess_withholding_cny = max(
                    item.excess_withholding_cny - (item.tax_payable_cny - remaining_payable), Decimal("0")
                )
                item.tax_payable_cny = clamp_tax_payable(
                    item.tax_amount_cny, item.foreign_tax_credit_cny
                )

    # 净亏损年度：恢复 excess_withholding_cny 审计留痕值（FTC 循环会重置为 0）
    if net_loss_excess_withholding > 0:
        for item in capital_gain_items:
            if item.income_type == "capital_gain_annual_net":
                item.excess_withholding_cny = net_loss_excess_withholding
                break

    # 费用处理说明：
    # - 卖出交易的费用已在 FIFO.sell() 中直接从 gain_loss 扣减（fifo.py:141）
    # - 独立 FEE 交易（如 ADR 费、分红手续费）已记录为 fee_items，
    #   但中国税法对财产转让所得无单独的"费用抵税额"规定，
    #   费用应作为成本一部分在计算 gain 时扣除，而非在税额层面再抵免
    # - 因此此处不再对 fee_items 做税额抵免，避免双重享受

    remaining_lots = fifo.get_remaining_lots()

    return TaxSummary(
        year=year,
        rsu_income=rsu_income_item,
        capital_gains=capital_gain_items,
        dividends=dividend_items,
        fees=fee_items,
        computation_method=method_used,
        annual_net_comparison=annual_net_info,
    ), remaining_lots, lot_consumptions
