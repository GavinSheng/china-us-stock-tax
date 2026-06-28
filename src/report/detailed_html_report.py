"""生成税务局凭证级别的详细 HTML 税务报告

从数据库读取完整的交易、FIFO lot 消耗、tax_items、tax_summaries
数据，生成包含逐笔明细的 HTML 报告。
"""
from __future__ import annotations
import sqlite3
from decimal import Decimal
from pathlib import Path
from datetime import date


def generate_detailed_html_report(db_path: str, year: int, output_path: str):
    """生成详细的 HTML 税务报告"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # ===== 加载数据 =====
    # Tax items
    tax_items = conn.execute(
        "SELECT * FROM tax_items WHERE tax_year = ? ORDER BY id", (year,)
    ).fetchall()

    # Tax summaries
    tax_summaries = conn.execute(
        "SELECT * FROM tax_summaries WHERE tax_year = ? ORDER BY id", (year,)
    ).fetchall()

    # Lot consumptions (FIFO 审计追踪)
    lot_consumptions = conn.execute("""
        SELECT lc.*, t.symbol as sell_symbol, t.trade_date as sell_date, t.action as sell_action,
               tl.acquisition_date, tl.acquisition_type, tl.broker_code as lot_broker
        FROM lot_consumptions lc
        LEFT JOIN transactions t ON lc.sell_txn_id = t.id
        LEFT JOIN tax_lots tl ON lc.tax_lot_id = tl.id
        ORDER BY lc.id
    """).fetchall()

    # RSU vests
    rsu_vests = conn.execute("""
        SELECT * FROM rsu_vests
        WHERE strftime('%Y', vest_date) = ? AND taxable_income_cny IS NOT NULL
        ORDER BY vest_date
    """, (str(year),)).fetchall()

    # Dividends
    dividends = conn.execute("""
        SELECT * FROM dividends
        WHERE strftime('%Y', payment_date) = ?
        ORDER BY payment_date
    """, (str(year),)).fetchall()

    # Sell transactions (for detail)
    sell_txns = conn.execute("""
        SELECT * FROM transactions
        WHERE strftime('%Y', trade_date) = ? AND action = 'sell'
        ORDER BY trade_date
    """, (str(year),)).fetchall()

    # Option sell transactions
    opt_sell_txns = conn.execute("""
        SELECT * FROM transactions
        WHERE strftime('%Y', trade_date) = ? AND action = 'option_sell'
        ORDER BY trade_date
    """, (str(year),)).fetchall()

    # Option expire transactions
    opt_expire_txns = conn.execute("""
        SELECT * FROM transactions
        WHERE strftime('%Y', trade_date) = ? AND action = 'option_expire'
        ORDER BY trade_date
    """, (str(year),)).fetchall()

    # Interest transactions
    interest_txns = conn.execute("""
        SELECT * FROM transactions
        WHERE strftime('%Y', trade_date) = ? AND action = 'interest'
        ORDER BY trade_date
    """, (str(year),)).fetchall()

    # Fee transactions
    fee_txns = conn.execute("""
        SELECT * FROM transactions
        WHERE strftime('%Y', trade_date) = ? AND action = 'fee'
        ORDER BY trade_date
    """, (str(year),)).fetchall()

    # Exchange rates used
    rates = conn.execute(
        "SELECT * FROM exchange_rates WHERE date LIKE ? ORDER BY date", (f"{year}%",)
    ).fetchall()

    conn.close()

    # ===== 计算汇总 =====
    total_taxable = Decimal("0")
    total_tax = Decimal("0")
    total_withheld = Decimal("0")
    total_foreign_credit = Decimal("0")
    total_excess = Decimal("0")
    total_domestic_withheld = Decimal("0")
    total_payable = Decimal("0")
    total_fees = Decimal("0")

    capital_gain_items = []
    dividend_items = []
    interest_items = []
    rsu_item = None
    fee_items = []

    for item in tax_items:
        itype = item["income_type"]
        def _d(key, default=0):
            val = item[key]
            return default if val is None else val

        gross = Decimal(str(_d("gross_income_cny")))
        taxable = Decimal(str(_d("taxable_income_cny")))
        tax_amt = Decimal(str(_d("tax_amount_cny")))
        withheld = Decimal(str(_d("tax_withheld_cny")))
        credit = Decimal(str(_d("foreign_credit_cny")))
        excess = Decimal(str(_d("excess_withholding_cny")))
        domestic = Decimal("0")
        payable = Decimal(str(_d("tax_payable_cny")))
        deductible = Decimal(str(_d("deductible")))

        total_taxable += taxable
        total_tax += tax_amt
        total_withheld += withheld
        total_foreign_credit += credit
        total_excess += excess
        total_domestic_withheld += domestic
        total_payable += payable
        total_fees += deductible

        if "capital" in itype:
            capital_gain_items.append(item)
        elif itype == "dividend":
            dividend_items.append(item)
        elif "interest" in itype:
            interest_items.append(item)
        elif "rsu" in itype:
            rsu_item = item
        elif "fee" in itype:
            fee_items.append(item)

    # Capital gain method
    cg_summary = None
    for s in tax_summaries:
        if s["income_type"] == "capital_gain":
            cg_summary = s
            break

    # ===== 生成 HTML =====
    html = _build_html(
        year=year,
        tax_summaries=tax_summaries,
        capital_gain_items=capital_gain_items,
        dividend_items=dividend_items,
        interest_items=interest_items,
        rsu_item=rsu_item,
        fee_items=fee_items,
        rsu_vests=rsu_vests,
        dividends=dividends,
        sell_txns=sell_txns,
        opt_sell_txns=opt_sell_txns,
        opt_expire_txns=opt_expire_txns,
        interest_txns=interest_txns,
        fee_txns=fee_txns,
        lot_consumptions=lot_consumptions,
        rates=rates,
        cg_summary=cg_summary,
        total_taxable=total_taxable,
        total_tax=total_tax,
        total_withheld=total_withheld,
        total_foreign_credit=total_foreign_credit,
        total_excess=total_excess,
        total_domestic_withheld=total_domestic_withheld,
        total_payable=total_payable,
        total_fees=total_fees,
    )

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return str(out)


def _fmt(val, is_currency=True):
    """格式化数字"""
    if val is None:
        return "-"
    d = Decimal(str(val))
    if is_currency:
        if abs(d) >= 1:
            return f"¥{d:,.2f}"
        else:
            return f"${d:,.4f}"
    return str(d)


def _pct(val):
    """格式化百分比"""
    d = Decimal(str(val))
    return f"{d:.0%}"


def _build_html(**ctx):
    year = ctx["year"]
    today = date.today().strftime("%Y年%m月%d日")

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{year}年度境外证券个人所得税申报清算报告</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: "PingFang SC", "Microsoft YaHei", "Helvetica Neue", Arial, sans-serif; background: #f5f5f5; color: #333; line-height: 1.6; }}
.container {{ max-width: 1400px; margin: 0 auto; padding: 24px; }}
.header {{ background: linear-gradient(135deg, #1a365d 0%, #2d5a8e 100%); color: #fff; padding: 40px 48px; border-radius: 12px 12px 0 0; }}
.header h1 {{ font-size: 26px; font-weight: 700; margin-bottom: 8px; }}
.header .subtitle {{ font-size: 14px; opacity: 0.85; }}
.header .meta {{ margin-top: 16px; display: flex; gap: 24px; flex-wrap: wrap; font-size: 13px; }}
.summary-cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin: -24px 48px 0; position: relative; z-index: 1; }}
.card {{ background: #fff; border-radius: 10px; padding: 20px; box-shadow: 0 2px 12px rgba(0,0,0,0.08); }}
.card .label {{ font-size: 12px; color: #666; margin-bottom: 6px; }}
.card .value {{ font-size: 24px; font-weight: 700; color: #1a365d; }}
.card .value.red {{ color: #c53030; }}
.card .value.green {{ color: #276749; }}
.section {{ background: #fff; margin-top: 20px; border-radius: 10px; box-shadow: 0 2px 12px rgba(0,0,0,0.06); overflow: hidden; page-break-inside: avoid; }}
.section-header {{ padding: 16px 24px; border-bottom: 1px solid #e8ecf0; }}
.section-header h2 {{ font-size: 17px; font-weight: 600; color: #1a365d; }}
.section-header .count {{ font-size: 12px; padding: 3px 10px; border-radius: 20px; font-weight: 500; background: #ebf5ff; color: #2b6cb0; float: right; margin-top: 2px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
thead {{ background: #f7fafc; }}
th {{ padding: 10px 12px; font-size: 11px; font-weight: 600; color: #718096; text-align: right; border-bottom: 2px solid #e8ecf0; white-space: nowrap; }}
th:first-child {{ text-align: left; }}
td {{ padding: 8px 12px; font-size: 12px; text-align: right; border-bottom: 1px solid #f0f2f5; font-family: "SF Mono", "Roboto Mono", "Consolas", monospace; }}
td:first-child {{ text-align: left; font-family: inherit; }}
tfoot td {{ font-weight: 700; border-top: 2px solid #e8ecf0; background: #f7fafc; }}
.subsection {{ padding: 16px 24px; border-top: 1px solid #e8ecf0; }}
.subsection h3 {{ font-size: 14px; color: #4a5568; margin-bottom: 10px; }}
.subsection p {{ font-size: 13px; color: #718096; line-height: 1.8; }}
.highlight {{ color: #c53030; font-weight: 600; }}
.success {{ color: #276749; font-weight: 600; }}
.badge {{ display: inline-block; font-size: 11px; padding: 2px 8px; border-radius: 12px; }}
.badge-blue {{ background: #ebf5ff; color: #2b6cb0; }}
.badge-green {{ background: #f0fff4; color: #276749; }}
.badge-red {{ background: #fff5f5; color: #c53030; }}
.badge-orange {{ background: #fffaf0; color: #c05621; }}
.law-ref {{ font-size: 11px; color: #a0aec0; font-style: italic; }}
.note {{ background: #fffbeb; border-left: 3px solid #d69e2e; padding: 12px 16px; margin: 12px 24px; font-size: 12px; color: #744210; border-radius: 0 4px 4px 0; }}
.footer {{ text-align: center; padding: 32px; color: #a0aec0; font-size: 12px; margin-top: 24px; }}
@media print {{
    body {{ background: #fff; }}
    .container {{ padding: 0; max-width: 100%; }}
    .section {{ box-shadow: none; border: 1px solid #e2e8f0; margin-top: 12px; page-break-inside: avoid; }}
    .header {{ border-radius: 0; }}
}}
</style>
</head>
<body>
<div class="container">

<!-- ===== HEADER ===== -->
<div class="header">
    <h1>{year}年度境外证券个人所得税申报清算报告</h1>
    <div class="subtitle">境外所得个人所得税综合计算 + 外国税收抵免（分国抵免限额法）</div>
    <div class="meta">
        <span>生成日期：{today}</span>
        <span>计税年度：{year}年1月1日 - {year}年12月31日</span>
        <span>数据来源：Futu / Longbridge / BOCI 月结单</span>
    </div>
</div>

<!-- ===== SUMMARY CARDS ===== -->
<div class="summary-cards">
    <div class="card">
        <div class="label">应税所得总额</div>
        <div class="value">{_fmt(ctx['total_taxable'])}</div>
    </div>
    <div class="card">
        <div class="label">应纳税额合计</div>
        <div class="value red">{_fmt(ctx['total_tax'])}</div>
    </div>
    <div class="card">
        <div class="label">境外已预扣</div>
        <div class="value">{_fmt(ctx['total_withheld'])}</div>
    </div>
    <div class="card">
        <div class="label">国外税收抵免</div>
        <div class="value green">{_fmt(ctx['total_foreign_credit'])}</div>
    </div>
    <div class="card">
        <div class="label">合计应补缴</div>
        <div class="value red">{_fmt(ctx['total_payable'])}</div>
    </div>
</div>

{_section_annual_summary(ctx)}
{_section_capital_gain_detail(ctx)}
{_section_fifo_audit(ctx)}
{_section_rsu_detail(ctx)}
{_section_dividend_detail(ctx)}
{_section_interest_detail(ctx)}
{_section_fee_detail(ctx)}
{_section_tax_credit(ctx)}
{_section_legal_basis(ctx)}

<div class="footer">
    <p>本报告由系统自动生成，所有计算基于 FIFO 成本匹配和中国个人所得税法规。</p>
    <p>数据来源：Futu / Longbridge / BOCI 月结单 PDF 解析 + 国家外汇管理局公布的 {year}年12月31日汇率中间价。</p>
    <p>© {year} 境外证券税务计算系统</p>
</div>

</div>
</body>
</html>"""


def _section_annual_summary(ctx):
    summaries = ctx["tax_summaries"]
    rows = ""
    for s in summaries:
        itype = s["income_type"]
        label = {
            "capital_gain": "资本利得（财产转让）",
            "dividend": "股息红利",
            "rsu_income": "RSU 股权激励",
            "fee_expense": "可抵扣费用",
        }.get(itype, itype)

        method_badge = ""
        if itype == "capital_gain" and s["computation_method"]:
            method = s["computation_method"]
            if method == "annual_net":
                method_badge = ' <span class="badge badge-blue">年度净额法</span>'
            else:
                method_badge = ' <span class="badge badge-orange">逐笔计算法</span>'

        rows += f"""<tr>
    <td>{label}{method_badge}</td>
    <td class="num">{_fmt(s['total_taxable_cny'] or 0)}</td>
    <td class="num">{_fmt(s['total_deductible_cny'] or 0)}</td>
    <td class="num">{_fmt(s['total_taxable_cny'] or 0)}</td>
    <td class="num">{_fmt(s['total_tax_cny'] or 0)}</td>
    <td class="num">{_fmt(s['total_withheld_cny'] or 0)}</td>
    <td class="num">{_fmt(s['total_credit_cny'] or 0)}</td>
    <td class="num">{_fmt(s['total_excess_cny'] or 0)}</td>
    <td class="num {'highlight' if (s['total_payable_cny'] or 0) > 0 else ''}">{_fmt(s['total_payable_cny'] or 0)}</td>
</tr>"""

    return f"""
<!-- ===== SECTION: 年度汇总 ===== -->
<div class="section">
    <div class="section-header">
        <h2>一、年度税务汇总</h2>
    </div>
    <table>
        <thead>
            <tr>
                <th>所得项目</th>
                <th>应税收入(CNY)</th>
                <th>可扣除(CNY)</th>
                <th>应税所得(CNY)</th>
                <th>应纳税额(CNY)</th>
                <th>已预扣(CNY)</th>
                <th>抵免额(CNY)</th>
                <th>超额未抵免(CNY)</th>
                <th>应补缴(CNY)</th>
            </tr>
        </thead>
        <tbody>{rows}</tbody>
        <tfoot>
            <tr>
                <td>合计</td>
                <td class="num">{_fmt(ctx['total_taxable'])}</td>
                <td class="num">{_fmt(ctx['total_fees'])}</td>
                <td class="num">{_fmt(ctx['total_taxable'])}</td>
                <td class="num">{_fmt(ctx['total_tax'])}</td>
                <td class="num">{_fmt(ctx['total_withheld'])}</td>
                <td class="num">{_fmt(ctx['total_foreign_credit'])}</td>
                <td class="num">{_fmt(ctx['total_excess'])}</td>
                <td class="num highlight">{_fmt(ctx['total_payable'])}</td>
            </tr>
        </tfoot>
    </table>
</div>"""


def _section_capital_gain_detail(ctx):
    items = ctx["capital_gain_items"]
    if not items:
        return ""

    rows = ""
    for item in items:
        itype = item["income_type"]
        if "annual_net" in itype:
            label = '<span class="badge badge-blue">年度净额</span>'
        elif "expire" in itype:
            label = '<span class="badge badge-red">期权过期</span>'
        else:
            label = '<span class="badge badge-orange">逐笔</span>'

        rows += f"""<tr>
    <td>{item['symbol']}</td>
    <td>{label}</td>
    <td class="num">{_fmt(item['gross_income_cny'] or 0)}</td>
    <td class="num">{_fmt(item['taxable_income_cny'] or 0)}</td>
    <td class="num">{_pct(item['tax_rate'] or 0)}</td>
    <td class="num">{_fmt(item['tax_amount_cny'] or 0)}</td>
    <td class="num">{_fmt(item['tax_withheld_cny'] or 0)}</td>
    <td class="num">{_fmt(item['tax_payable_cny'] or 0)}</td>
    <td class="detail">{item['detail'] or ''}</td>
</tr>"""

    # Annual net comparison
    annual_net_note = ""
    cg = ctx.get("cg_summary")
    if cg and cg["computation_method"] == "annual_net":
        annual_net_note = f"""
    <div class="note">
        <strong>计税方法说明：</strong>本年度选择<strong>年度净额法</strong>计算资本利得。
        依据《个人所得税法实施条例》第十九条，财产转让所得按次计征。
        系统自动比较“逐笔计算”与“年度净额”两种方法，选择税额较低者。
    </div>"""

    return f"""
<!-- ===== SECTION: 资本利得明细 ===== -->
<div class="section">
    <div class="section-header">
        <h2>二、资本利得明细（财产转让所得 20%）</h2>
        <span class="count">{len(items)} 项</span>
    </div>
    <table>
        <thead>
            <tr>
                <th>标的</th>
                <th>类型</th>
                <th>毛收入(CNY)</th>
                <th>应税所得(CNY)</th>
                <th>税率</th>
                <th>应纳税额(CNY)</th>
                <th>已预扣(CNY)</th>
                <th>应补缴(CNY)</th>
                <th>备注</th>
            </tr>
        </thead>
        <tbody>{rows}</tbody>
    </table>
    {annual_net_note}
</div>"""


def _section_fifo_audit(ctx):
    """FIFO lot 消耗审计明细 —— 税务局最看重的成本追溯"""
    lots = ctx["lot_consumptions"]
    if not lots:
        return ""

    # 按 sell_date 分组
    from collections import defaultdict
    by_sell = defaultdict(list)
    for lc in lots:
        sell_id = lc["sell_txn_id"]
        by_sell[sell_id].append(lc)

    rows = ""
    total_cost = Decimal("0")
    total_gain = Decimal("0")
    total_qty = 0

    for sell_id, consumptions in sorted(by_sell.items()):
        sell_sym = consumptions[0]["sell_symbol"] or "-"
        sell_date = consumptions[0]["sell_date"] or "-"
        sell_action = consumptions[0]["sell_action"] or "-"
        action_label = {
            "sell": "卖出",
            "option_sell": "期权卖出",
            "option_expire": "期权过期",
        }.get(sell_action, sell_action)

        for lc in consumptions:
            cost = Decimal(str(lc["cost_basis"] or 0))
            gain = Decimal(str(lc["realized_gain"] or 0))
            qty = lc["consumed_qty"] or 0
            total_cost += cost
            total_gain += gain
            total_qty += qty

            acquire_date = lc["acquisition_date"] or "-"
            acq_type = {
                "buy": "买入",
                "rsu_vest": "RSU",
                "exercise": "行权",
                "carryforward": "结转",
                "gap_fill": "补录",
            }.get(lc["acquisition_type"] or "", lc["acquisition_type"] or "-")

            gain_tag = ""
            if gain > 0:
                gain_tag = f'<span class="success">+{_fmt(gain)}</span>'
            elif gain < 0:
                gain_tag = f'<span class="highlight">{_fmt(gain)}</span>'
            else:
                gain_tag = _fmt(gain)

            rows += f"""<tr>
    <td>{sell_sym}</td>
    <td>{action_label}</td>
    <td class="date">{sell_date}</td>
    <td class="num">{qty}</td>
    <td>{acq_type}</td>
    <td class="date">{acquire_date}</td>
    <td class="num">{_fmt(Decimal(str(lc['cost_per_share'] or 0)), True)}</td>
    <td class="num">{_fmt(cost)}</td>
    <td>{gain_tag}</td>
    <td>{lc['lot_broker'] or '-'}</td>
</tr>"""

    return f"""
<!-- ===== SECTION: FIFO 成本消耗审计 ===== -->
<div class="section">
    <div class="section-header">
        <h2>三、FIFO 成本消耗审计明细</h2>
        <span class="count">{len(lots)} 笔</span>
    </div>
    <div class="note">
        <strong>审计说明：</strong>每笔卖出/过期消耗的 FIFO 成本批次。
        成本匹配严格遵循先进先出（FIFO）原则，按券商独立队列。
        本表为税务审计核查所需的成本追溯凭证。
    </div>
    <table>
        <thead>
            <tr>
                <th>标的</th>
                <th>交易类型</th>
                <th>交易日期</th>
                <th>消耗数量</th>
                <th>获取方式</th>
                <th>获取日期</th>
                <th>单位成本(USD)</th>
                <th>成本基础(CNY)</th>
                <th>已实现盈亏(CNY)</th>
                <th>券商</th>
            </tr>
        </thead>
        <tbody>{rows}</tbody>
        <tfoot>
            <tr>
                <td>合计</td>
                <td></td>
                <td></td>
                <td class="num">{total_qty}</td>
                <td></td>
                <td></td>
                <td></td>
                <td class="num">{_fmt(total_cost)}</td>
                <td class="num">{_fmt(total_gain)}</td>
                <td></td>
            </tr>
        </tfoot>
    </table>
</div>"""


def _section_rsu_detail(ctx):
    vests = ctx["rsu_vests"]
    if not vests:
        return ""

    rows = ""
    total_income = Decimal("0")
    total_tax = Decimal("0")
    for v in vests:
        income = Decimal(str(v["taxable_income_cny"] or 0))
        tax = Decimal(str(v["tax_amount_cny"] or 0))
        total_income += income
        total_tax += tax
        rows += f"""<tr>
    <td>{v['vest_date']}</td>
    <td>{v['symbol']}</td>
    <td class="num">{v['vested_quantity']}</td>
    <td class="num">${v['fmv_per_share']:.4f}</td>
    <td class="num">{_fmt(income)}</td>
    <td class="num">{_fmt(tax)}</td>
    <td class="num">{_fmt(Decimal(str(v['tax_paid'] or 0) == '1' and v['tax_amount_cny'] or 0))}</td>
    <td>{v['custody_broker'] or '-'}</td>
    <td>{v['source_image'] or '-'}</td>
</tr>"""

    rsu_tax_item = ctx.get("rsu_item")
    rsu_tax_amt = Decimal("0")
    rsu_rate = Decimal("0")
    rsu_payable = Decimal("0")
    rsu_domestic = Decimal("0")
    if rsu_tax_item:
        rsu_tax_amt = Decimal(str(rsu_tax_item["tax_amount_cny"] or 0))
        rsu_rate = Decimal(str(rsu_tax_item["tax_rate"] or 0))
        rsu_payable = Decimal(str(rsu_tax_item["tax_payable_cny"] or 0))
    # RSU 境内代扣从 rsu_vests 表汇总
    rsu_domestic = sum(Decimal(str(v["tax_amount_cny"] or 0)) for v in vests)

    # RSU 税率表
    bracket_rows = ""
    brackets = [
        ("¥0 ~ 36,000", "3%", "0"),
        ("¥36,001 ~ 144,000", "10%", "2,520"),
        ("¥144,001 ~ 300,000", "20%", "16,920"),
        ("¥300,001 ~ 420,000", "25%", "31,920"),
        ("¥420,001 ~ 660,000", "30%", "52,920"),
        ("¥660,001 ~ 960,000", "35%", "85,920"),
        ("¥960,001 以上", "45%", "181,920"),
    ]
    for rng, rate, deduct in brackets:
        is_active = f' class="highlight"' if f"{rsu_rate:.0%}" == rate else ""
        bracket_rows += f"""<tr{is_active}>
    <td>{rng}</td>
    <td>{rate}</td>
    <td>{deduct}</td>
</tr>"""

    return f"""
<!-- ===== SECTION: RSU ===== -->
<div class="section">
    <div class="section-header">
        <h2>四、RSU 股权激励明细（工资薪金所得 3%~45% 累进）</h2>
        <span class="count">{len(vests)} 笔</span>
    </div>
    <div class="subsection">
        <h3>归属明细</h3>
        <table>
            <thead>
                <tr>
                    <th>归属日期</th>
                    <th>标的</th>
                    <th>归属数量(股)</th>
                    <th>公允价值(USD)</th>
                    <th>应税所得(CNY)</th>
                    <th>应纳税额(CNY)</th>
                    <th>已缴纳(CNY)</th>
                    <th>托管券商</th>
                    <th>来源凭证</th>
                </tr>
            </thead>
            <tbody>{rows}</tbody>
            <tfoot>
                <tr>
                    <td>合计</td>
                    <td></td>
                    <td class="num">{sum(v['vested_quantity'] for v in vests)}</td>
                    <td></td>
                    <td class="num">{_fmt(total_income)}</td>
                    <td class="num">{_fmt(total_tax)}</td>
                    <td class="num">{_fmt(rsu_domestic)}</td>
                    <td></td>
                    <td></td>
                </tr>
            </tfoot>
        </table>
    </div>
    <div class="subsection">
        <h3>税率计算说明</h3>
        <p>依据《财税〔2018〕164号》文，股权激励所得不并入综合所得，
        全额单独计税，适用下表累进税率：</p>
        <p>适用税率：<span class="highlight">{rsu_rate:.0%}</span>
        ｜ 应纳税额：<span class="highlight">{_fmt(rsu_tax_amt)}</span>
        ｜ 境内已代扣：<span class="success">{_fmt(rsu_domestic)}</span>
        ｜ 应补缴：<span class="highlight">{_fmt(rsu_payable)}</span></p>
    </div>
    <div class="subsection">
        <h3>税率表</h3>
        <table>
            <thead>
                <tr>
                    <th>级距</th>
                    <th>税率</th>
                    <th>速算扣除数</th>
                </tr>
            </thead>
            <tbody>{bracket_rows}</tbody>
        </table>
    </div>
</div>"""


def _section_dividend_detail(ctx):
    divs = ctx["dividends"]
    if not divs:
        return ""

    # 逐笔明细
    rows = ""
    total_gross = Decimal("0")
    total_withholding = Decimal("0")
    total_net = Decimal("0")

    for d in divs:
        gross = Decimal(str(d["gross_amount"] or 0))
        withholding = Decimal(str(d["withholding_tax"] or 0)) - Decimal(str(d["withholding_refund"] or 0))
        net = Decimal(str(d["net_amount"] or 0))
        gross_cny = Decimal(str(d["gross_amount_cny"] or 0))
        if gross_cny == 0 and d["exchange_rate"]:
            gross_cny = gross * Decimal(str(d["exchange_rate"]))
        withholding_cny = Decimal(str(d["withholding_tax_cny"] or 0))
        if withholding_cny == 0 and d["exchange_rate"]:
            withholding_cny = withholding * Decimal(str(d["exchange_rate"]))
        net_cny = Decimal(str(d["net_amount_cny"] or 0))
        if net_cny == 0 and d["exchange_rate"]:
            net_cny = net * Decimal(str(d["exchange_rate"]))

        total_gross += gross_cny
        total_withholding += withholding_cny
        total_net += net_cny

        rate_tag = ""
        if d["withholding_rate"]:
            rate_tag = f"{d['withholding_rate']:.0%}"

        rows += f"""<tr>
    <td>{d['payment_date']}</td>
    <td>{d['symbol']}</td>
    <td>{d['broker_code']}</td>
    <td class="num">{d['share_quantity']}</td>
    <td class="num">${d['per_share_amount']:.6f}</td>
    <td class="num">${gross:,.2f}</td>
    <td class="num">{_fmt(gross_cny)}</td>
    <td class="num">${withholding:,.2f}</td>
    <td class="num">{_fmt(withholding_cny)}</td>
    <td>{rate_tag or '-'}</td>
    <td class="num">{_fmt(Decimal(str(d['china_tax_amount'] or 0)))}</td>
</tr>"""

    # 按股票汇总
    from collections import defaultdict
    by_symbol = defaultdict(lambda: {"count": 0, "gross_cny": Decimal("0"), "withholding_cny": Decimal("0"), "china_tax": Decimal("0")})
    for d in divs:
        sym = d["symbol"]
        gross = Decimal(str(d["gross_amount"] or 0))
        gross_cny = Decimal(str(d["gross_amount_cny"] or 0))
        if gross_cny == 0 and d["exchange_rate"]:
            gross_cny = gross * Decimal(str(d["exchange_rate"]))
        withholding_cny = Decimal(str(d["withholding_tax_cny"] or 0))
        china_tax = Decimal(str(d["china_tax_amount"] or 0))
        by_symbol[sym]["count"] += 1
        by_symbol[sym]["gross_cny"] += gross_cny
        by_symbol[sym]["withholding_cny"] += withholding_cny
        by_symbol[sym]["china_tax"] += china_tax

    summary_rows = ""
    for sym, data in sorted(by_symbol.items()):
        summary_rows += f"""<tr>
    <td>{sym}</td>
    <td class="num">{data['count']}</td>
    <td class="num">{_fmt(data['gross_cny'])}</td>
    <td class="num">{_fmt(data['withholding_cny'])}</td>
    <td class="num">{_fmt(data['china_tax'])}</td>
</tr>"""

    return f"""
<!-- ===== SECTION: 分红 ===== -->
<div class="section">
    <div class="section-header">
        <h2>五、分红明细（股息红利所得 20%）</h2>
        <span class="count">{len(divs)} 笔</span>
    </div>
    <div class="subsection">
        <h3>逐笔分红明细</h3>
        <table>
            <thead>
                <tr>
                    <th>支付日期</th>
                    <th>标的</th>
                    <th>券商</th>
                    <th>股数</th>
                    <th>每股分红(USD)</th>
                    <th>毛额(USD)</th>
                    <th>毛额(CNY)</th>
                    <th>预扣税(USD)</th>
                    <th>预扣税(CNY)</th>
                    <th>预扣率</th>
                    <th>中国应纳税(CNY)</th>
                </tr>
            </thead>
            <tbody>{rows}</tbody>
            <tfoot>
                <tr>
                    <td>合计</td>
                    <td></td>
                    <td></td>
                    <td class="num">{sum(d['share_quantity'] for d in divs)}</td>
                    <td></td>
                    <td></td>
                    <td class="num">{_fmt(total_gross)}</td>
                    <td></td>
                    <td class="num">{_fmt(total_withholding)}</td>
                    <td></td>
                    <td class="num">{_fmt(sum(Decimal(str(d['china_tax_amount'] or 0)) for d in divs))}</td>
                </tr>
            </tfoot>
        </table>
    </div>
    <div class="subsection">
        <h3>按股票汇总</h3>
        <table>
            <thead>
                <tr>
                    <th>标的</th>
                    <th>笔数</th>
                    <th>分红总额(CNY)</th>
                    <th>境外预扣(CNY)</th>
                    <th>中国应纳税(CNY)</th>
                </tr>
            </thead>
            <tbody>{summary_rows}</tbody>
        </table>
    </div>
</div>"""


def _section_interest_detail(ctx):
    txns = ctx["interest_txns"]
    if not txns:
        return ""

    rows = ""
    total_amount = Decimal("0")
    total_cny = Decimal("0")
    total_tax = Decimal("0")

    for t in txns:
        amount = Decimal(str(t["amount"] or 0))
        amount_cny = Decimal(str(t["amount_cny"] or 0))
        tax = amount_cny * Decimal("0.20")
        total_amount += amount
        total_cny += amount_cny
        total_tax += tax

        rows += f"""<tr>
    <td>{t['trade_date']}</td>
    <td>{t['symbol']}</td>
    <td>{t['broker_code']}</td>
    <td class="num">${amount:,.2f}</td>
    <td class="num">{_fmt(amount_cny)}</td>
    <td class="num">20%</td>
    <td class="num">{_fmt(tax)}</td>
</tr>"""

    return f"""
<!-- ===== SECTION: 利息 ===== -->
<div class="section">
    <div class="section-header">
        <h2>六、利息所得明细（20%）</h2>
        <span class="count">{len(txns)} 笔</span>
    </div>
    <table>
        <thead>
            <tr>
                <th>日期</th>
                <th>标的</th>
                <th>券商</th>
                <th>金额(USD)</th>
                <th>金额(CNY)</th>
                <th>税率</th>
                <th>应纳税额(CNY)</th>
            </tr>
        </thead>
        <tbody>{rows}</tbody>
        <tfoot>
            <tr>
                <td>合计</td>
                <td></td>
                <td></td>
                <td class="num">${total_amount:,.2f}</td>
                <td class="num">{_fmt(total_cny)}</td>
                <td></td>
                <td class="num">{_fmt(total_tax)}</td>
            </tr>
        </tfoot>
    </table>
</div>"""


def _section_fee_detail(ctx):
    txns = ctx["fee_txns"]
    if not txns:
        return ""

    rows = ""
    total_fee = Decimal("0")

    for t in txns:
        fee_cny = Decimal(str(t["fee_total_cny"] or 0))
        total_fee += fee_cny
        fee_breakdown = ""
        if t["fee_breakdown"]:
            fee_breakdown = t["fee_breakdown"]

        rows += f"""<tr>
    <td>{t['trade_date']}</td>
    <td>{t['symbol']}</td>
    <td>{t['broker_code']}</td>
    <td class="num">{t['action']}</td>
    <td class="num">${t['commission'] or 0:,.2f}</td>
    <td class="num">{_fmt(fee_cny)}</td>
    <td>{fee_breakdown or '-'}</td>
</tr>"""

    return f"""
<!-- ===== SECTION: 费用 ===== -->
<div class="section">
    <div class="section-header">
        <h2>七、可抵扣费用明细</h2>
        <span class="count">{len(txns)} 笔</span>
    </div>
    <div class="note">
        <strong>说明：</strong>分红手续费、ADR 费用等与取得所得直接相关的费用，
        可在计算应纳税所得额时扣除。
    </div>
    <table>
        <thead>
            <tr>
                <th>日期</th>
                <th>标的</th>
                <th>券商</th>
                <th>类型</th>
                <th>金额(USD)</th>
                <th>金额(CNY)</th>
                <th>备注</th>
            </tr>
        </thead>
        <tbody>{rows}</tbody>
        <tfoot>
            <tr>
                <td>合计</td>
                <td></td>
                <td></td>
                <td></td>
                <td></td>
                <td class="num">{_fmt(total_fee)}</td>
                <td></td>
            </tr>
        </tfoot>
    </table>
</div>"""


def _section_tax_credit(ctx):
    """境外税收抵免明细"""
    items = ctx.get("capital_gain_items", []) + ctx.get("dividend_items", []) + ctx.get("interest_items", [])
    credit_items = [i for i in items if Decimal(str(i["tax_withheld_cny"] or 0)) > 0 or Decimal(str(i["foreign_credit_cny"] or 0)) > 0]
    if not credit_items:
        return ""

    rows = ""
    total_withheld = Decimal("0")
    total_credit = Decimal("0")
    total_excess = Decimal("0")

    for item in credit_items:
        withheld = Decimal(str(item["tax_withheld_cny"] or 0))
        credit = Decimal(str(item["foreign_credit_cny"] or 0))
        excess = Decimal(str(item["excess_withholding_cny"] or 0))
        total_withheld += withheld
        total_credit += credit
        total_excess += excess

        # 判断收入来源国
        currency = item["currency"]
        country = "美国" if currency == "USD" else "香港" if currency == "HKD" else currency

        rows += f"""<tr>
    <td>{item['symbol']}</td>
    <td>{item['income_type']}</td>
    <td>{country}</td>
    <td>{currency}</td>
    <td class="num">{_fmt(withheld)}</td>
    <td class="num">{_fmt(Decimal(str(item['tax_amount_cny'] or 0)))}</td>
    <td class="num">{_fmt(credit)}</td>
    <td class="num">{_fmt(excess)}</td>
</tr>"""

    return f"""
<!-- ===== SECTION: 境外税收抵免 ===== -->
<div class="section">
    <div class="section-header">
        <h2>八、境外税收抵免明细（分国抵免限额法）</h2>
        <span class="count">{len(credit_items)} 项</span>
    </div>
    <div class="note">
        <strong>法规依据：</strong>《财税〔2020〕3号》《关于境外所得外国税收抵免有关问题的通知》。
        抵免限额分国计算，超额部分可在后续 5 个纳税年度结转抵免。
    </div>
    <table>
        <thead>
            <tr>
                <th>标的</th>
                <th>所得类型</th>
                <th>来源国</th>
                <th>币种</th>
                <th>境外已缴(CNY)</th>
                <th>中国应纳税(CNY)</th>
                <th>实际抵免(CNY)</th>
                <th>超额未抵免(CNY)</th>
            </tr>
        </thead>
        <tbody>{rows}</tbody>
        <tfoot>
            <tr>
                <td>合计</td>
                <td></td>
                <td></td>
                <td></td>
                <td class="num">{_fmt(total_withheld)}</td>
                <td></td>
                <td class="num">{_fmt(total_credit)}</td>
                <td class="num">{_fmt(total_excess)}</td>
            </tr>
        </tfoot>
    </table>
</div>"""


def _section_legal_basis(ctx):
    return """
<!-- ===== SECTION: 法规依据 ===== -->
<div class="section">
    <div class="section-header">
        <h2>九、法规依据与计税原则</h2>
    </div>
    <div class="subsection">
        <table>
            <thead>
                <tr>
                    <th style="width:40%">税目</th>
                    <th>法规依据</th>
                </tr>
            </thead>
            <tbody>
                <tr>
                    <td>财产转让所得（资本利得）20%</td>
                    <td>《个人所得税法》第三条、第六条；《实施条例》第六条、第十九条</td>
                </tr>
                <tr>
                    <td>股息红利所得 20%</td>
                    <td>《个人所得税法》第三条、第六条；《实施条例》第六条</td>
                </tr>
                <tr>
                    <td>股权激励所得 3%~45% 累进</td>
                    <td>《财税〔2018〕164号》《关于个人所得税法修改后有关优惠政策衔接问题的通知》</td>
                </tr>
                <tr>
                    <td>境外税收抵免（分国抵免限额法）</td>
                    <td>《财税〔2020〕3号》《关于境外所得外国税收抵免有关问题的通知》</td>
                </tr>
                <tr>
                    <td>汇率换算（年末中间价）</td>
                    <td>《个人所得税法实施条例》第三十二条：所得为外币的，折合人民币缴纳税款，按纳税年度最后一日的汇率中间价折算</td>
                </tr>
                <tr>
                    <td>成本匹配方法（FIFO）</td>
                    <td>《个人所得税法实施条例》第十六条：财产转让所得的原值按「购入价格」确定</td>
                </tr>
            </tbody>
        </table>
    </div>
</div>"""
