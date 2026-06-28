from __future__ import annotations
import csv
from pathlib import Path
from src.models import TaxSummary, TaxItem


def write_tax_report(summary: TaxSummary, output_dir: str | Path):
    """生成税务报表

    输出：
    - tax_summary.csv：汇总
    - tax_detail_capital_gain.csv：资本利得明细
    - tax_detail_dividend.csv：分红明细
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # 汇总
    rows = []
    if summary.rsu_income:
        rows.append(_tax_item_to_dict(summary.rsu_income))
    for item in summary.capital_gains:
        rows.append(_tax_item_to_dict(item))
    for item in summary.dividends:
        rows.append(_tax_item_to_dict(item))
    for item in summary.fees:
        rows.append(_tax_item_to_dict(item))

    # 添加汇总行
    rows.append({
        "日期": "",
        "股票代码": "",
        "收入类型": "合计",
        "应税收入_CNY": float(summary.total_taxable_income_cny),
        "可扣除_CNY": float(sum(f.deductible_cny for f in summary.fees)),
        "应纳税所得_CNY": float(summary.total_taxable_income_cny),
        "税率": "",
        "应纳税额_CNY": float(summary.total_tax_amount_cny),
        "已预扣_CNY": float(summary.total_tax_withheld_cny),
        "境外抵免_CNY": float(summary.total_foreign_tax_credit_cny),
        "超额未抵免_CNY": float(summary.total_excess_withholding_cny),
        "应补缴_CNY": float(summary.total_tax_payable_cny),
        "备注": f"{summary.year} 年度（含费用抵扣 ¥{sum(f.deductible_cny for f in summary.fees):,.2f}）",
    })

    with open(out / "tax_summary.csv", "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    # 明细
    if summary.capital_gains:
        with open(out / "tax_detail_capital_gain.csv", "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=_tax_item_to_dict(summary.capital_gains[0]).keys())
            writer.writeheader()
            writer.writerows(_tax_item_to_dict(i) for i in summary.capital_gains)

    if summary.dividends:
        with open(out / "tax_detail_dividend.csv", "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=_tax_item_to_dict(summary.dividends[0]).keys())
            writer.writeheader()
            writer.writerows(_tax_item_to_dict(i) for i in summary.dividends)

    if summary.fees:
        with open(out / "tax_detail_fees.csv", "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=_tax_item_to_dict(summary.fees[0]).keys())
            writer.writeheader()
            writer.writerows(_tax_item_to_dict(i) for i in summary.fees)


def _tax_item_to_dict(item: TaxItem) -> dict:
    return {
        "日期": item.date,
        "股票代码": item.symbol,
        "收入类型": item.income_type.replace("capital_gain_annual_net", "capital_gain(年度净额)") if item.income_type == "capital_gain_annual_net" else item.income_type,
        "应税收入_CNY": float(item.gross_income_cny),
        "可扣除_CNY": float(item.deductible_cny),
        "应纳税所得_CNY": float(item.taxable_income_cny),
        "税率": str(item.tax_rate),
        "应纳税额_CNY": float(item.tax_amount_cny),
        "已预扣_CNY": float(item.tax_withheld_cny),
        "境外抵免_CNY": float(item.foreign_tax_credit_cny),
        "超额未抵免_CNY": float(item.excess_withholding_cny),
        "应补缴_CNY": float(item.tax_payable_cny),
        "备注": item.detail,
    }
