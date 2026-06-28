from __future__ import annotations
from decimal import Decimal
from typing import List, Optional
from pydantic import BaseModel


class TaxItem(BaseModel):
    """单笔应税事项"""
    date: str
    symbol: str
    income_type: str                     # rsu_vest / capital_gain / dividend
    currency: str = "USD"                # 原始币种，用于判断收入来源国
    gross_income_cny: Decimal            # 应税收入 CNY
    deductible_cny: Decimal = Decimal("0")  # 可扣除 CNY
    taxable_income_cny: Decimal          # 应纳税所得 CNY
    tax_rate: Decimal                    # 税率
    tax_amount_cny: Decimal              # 中国应纳税额 CNY
    tax_withheld_cny: Decimal = Decimal("0")  # 境外已预扣税额 CNY
    foreign_tax_credit_cny: Decimal = Decimal("0")  # 实际抵免的境外税额 CNY（≤ tax_amount）
    excess_withholding_cny: Decimal = Decimal("0")  # 超额未抵免部分 CNY
    domestic_withheld_cny: Decimal = Decimal("0")  # 境内已代扣代缴税额 CNY（如 RSU 公司代扣，不参与境外抵免）
    tax_payable_cny: Decimal             # 应补缴税额 CNY（≥ 0）
    detail: str = ""                     # 备注


class TaxSummary(BaseModel):
    """税务汇总"""
    year: int
    rsu_income: Optional[TaxItem] = None
    capital_gains: List[TaxItem] = []
    dividends: List[TaxItem] = []
    fees: List[TaxItem] = []  # 可抵扣费用（ADR 费、分红手续费等）
    # 卖出计税方法：per_transaction=逐笔计算 / annual_net=年度净额
    computation_method: str = "per_transaction"
    # 年度净额备选方案的信息（仅在两种方法有差异时填充）
    annual_net_comparison: Optional[dict] = None  # {"net_gain_cny", "tax_amount_cny", "per_txn_tax_amount"}

    @property
    def total_taxable_income_cny(self) -> Decimal:
        total = Decimal("0")
        if self.rsu_income:
            total += self.rsu_income.taxable_income_cny
        for item in self.capital_gains:
            total += item.taxable_income_cny
        for item in self.dividends:
            total += item.taxable_income_cny
        return total

    @property
    def total_tax_amount_cny(self) -> Decimal:
        total = Decimal("0")
        if self.rsu_income:
            total += self.rsu_income.tax_amount_cny
        for item in self.capital_gains:
            total += item.tax_amount_cny
        for item in self.dividends:
            total += item.tax_amount_cny
        return total

    @property
    def total_tax_withheld_cny(self) -> Decimal:
        total = Decimal("0")
        if self.rsu_income:
            total += self.rsu_income.tax_withheld_cny
        for item in self.capital_gains:
            total += item.tax_withheld_cny
        for item in self.dividends:
            total += item.tax_withheld_cny
        return total

    @property
    def total_foreign_tax_credit_cny(self) -> Decimal:
        total = Decimal("0")
        if self.rsu_income:
            total += self.rsu_income.foreign_tax_credit_cny
        for item in self.capital_gains:
            total += item.foreign_tax_credit_cny
        for item in self.dividends:
            total += item.foreign_tax_credit_cny
        return total

    @property
    def total_excess_withholding_cny(self) -> Decimal:
        total = Decimal("0")
        if self.rsu_income:
            total += self.rsu_income.excess_withholding_cny
        for item in self.capital_gains:
            total += item.excess_withholding_cny
        for item in self.dividends:
            total += item.excess_withholding_cny
        return total

    @property
    def total_domestic_withheld_cny(self) -> Decimal:
        """境内已代扣代缴税额汇总（如 RSU 公司代扣，不属于境外税收抵免）"""
        total = Decimal("0")
        if self.rsu_income:
            total += self.rsu_income.domestic_withheld_cny
        for item in self.capital_gains:
            total += item.domestic_withheld_cny
        for item in self.dividends:
            total += item.domestic_withheld_cny
        return total

    @property
    def total_tax_payable_cny(self) -> Decimal:
        """应补缴税额汇总 — 逐笔求和，而非全局计算。

        依据：每笔应税事项的 tax_payable_cny 已独立 clamp 到 ≥0。
        若先汇总再 clamp，会因正负相抵导致应补缴税额被低估。
        """
        total = Decimal("0")
        if self.rsu_income:
            total += self.rsu_income.tax_payable_cny
        for item in self.capital_gains:
            total += item.tax_payable_cny
        for item in self.dividends:
            total += item.tax_payable_cny
        return total

    @property
    def total_deductible_fees_cny(self) -> Decimal:
        return sum(f.deductible_cny for f in self.fees)
