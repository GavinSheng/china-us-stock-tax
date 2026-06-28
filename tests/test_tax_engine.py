import pytest
from decimal import Decimal
from datetime import date
from src.calculator.tax_engine import clamp_tax_payable, compute_tax, TaxItem
from src.models import Transaction, Action, TaxLot


# ===== 基础工具测试 =====

def test_tax_payable_not_negative():
    payable = clamp_tax_payable(Decimal("5000"), Decimal("8000"))
    assert payable == Decimal("0")

    payable = clamp_tax_payable(Decimal("8000"), Decimal("5000"))
    assert payable == Decimal("3000")


# ===== 集成测试 =====

def test_rsu_vest_progressive_tax():
    """RSU 归属按 3%~45% 超额累进税率计税（财税〔2018〕164号）"""
    txns = [
        Transaction(
            id="t1", broker="futu", date=date(2025, 4, 1), symbol="AAPL",
            action=Action.RSU_VEST, quantity=100, price=Decimal("200"),
            amount=Decimal("20000"), currency="USD",
            exchange_rate=Decimal("7.1"),
            tax_withheld=Decimal("3000"),
        ),
    ]
    summary, lots, _ = compute_tax(txns, 2025)

    # RSU 收入应产生 TaxItem
    assert summary.rsu_income is not None
    rsu = summary.rsu_income
    # 应税收入 = 20000 * 7.1 = 142000 CNY
    assert rsu.taxable_income_cny == Decimal("142000.00")
    # 适用 10% 税率（36000~144000 档）
    assert rsu.tax_rate == Decimal("0.10")
    # 应纳税额 = 142000 * 10% - 2520 = 11680
    assert rsu.tax_amount_cny == Decimal("11680.00")
    # RSU 代扣属于境内税（公司代扣代缴），不走境外税收抵免（FTC）通道
    assert rsu.foreign_tax_credit_cny == Decimal("0.00")
    # 境内已代扣 = 3000 * 7.1 = 21300 CNY
    assert rsu.domestic_withheld_cny == Decimal("21300.00")
    # tax_withheld_cny 应与 domestic_withheld_cny 一致（防止硬编码为 0）
    assert rsu.tax_withheld_cny == Decimal("21300.00")
    # 应补缴 = max(11680 - 21300, 0) = 0
    assert rsu.tax_payable_cny == Decimal("0.00")

    # FIFO 持仓应正确入队（跨券商合并，key 为 symbol）
    assert len(lots["AAPL"]) == 1
    assert lots["AAPL"][0].remaining == 100


def test_rsu_sell_capital_gains_20():
    """RSU 归属后卖出，按 20% 计税，成本 = 归属日 FMV"""
    txns = [
        Transaction(
            id="t1", broker="futu", date=date(2025, 1, 1), symbol="AAPL",
            action=Action.RSU_VEST, quantity=100, price=Decimal("100"),
            amount=Decimal("10000"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
        Transaction(
            id="t2", broker="futu", date=date(2025, 6, 1), symbol="AAPL",
            action=Action.SELL, quantity=100, price=Decimal("150"),
            amount=Decimal("15000"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
    ]
    summary, lots, _ = compute_tax(txns, 2025)

    gain_cny = (Decimal("50") * Decimal("100") * Decimal("7.1")).quantize(Decimal("0.01"))
    expected_tax = (gain_cny * Decimal("0.20")).quantize(Decimal("0.01"))

    assert len(summary.capital_gains) == 1
    assert summary.capital_gains[0].taxable_income_cny == gain_cny
    assert summary.capital_gains[0].tax_amount_cny == expected_tax


def test_capital_loss_not_taxed():
    """亏损卖出不产生应税义务"""
    txns = [
        Transaction(
            id="t1", broker="futu", date=date(2025, 1, 1), symbol="AAPL",
            action=Action.BUY, quantity=100, price=Decimal("100"),
            amount=Decimal("10000"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
        Transaction(
            id="t2", broker="futu", date=date(2025, 6, 1), symbol="AAPL",
            action=Action.SELL, quantity=100, price=Decimal("60"),
            amount=Decimal("6000"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
    ]
    summary, lots, _ = compute_tax(txns, 2025)

    # 年度净亏损时生成一条审计留痕记录（capital_gain_annual_net），税额为 0
    assert len(summary.capital_gains) == 1
    assert summary.capital_gains[0].income_type == "capital_gain_annual_net"
    assert summary.capital_gains[0].tax_amount_cny == 0
    assert summary.capital_gains[0].tax_payable_cny == 0
    assert summary.total_tax_payable_cny == 0


def test_cross_year_holdings():
    """跨年持仓：2024 年 RSU 归属，2025 年卖出"""
    existing = {
        "AAPL": [
            TaxLot(symbol="AAPL", quantity=100, cost_per_share=Decimal("150"),
                   acquire_date=date(2024, 4, 1), remaining=100, origin="rsu_vest",
                   broker_code="futu"),
        ]
    }
    txns = [
        Transaction(
            id="t1", broker="futu", date=date(2025, 6, 1), symbol="AAPL",
            action=Action.SELL, quantity=100, price=Decimal("200"),
            amount=Decimal("20000"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
    ]
    summary, lots, _ = compute_tax(txns, 2025, existing_lots=existing)

    assert len(summary.capital_gains) == 1
    assert summary.capital_gains[0].taxable_income_cny > 0


def test_dividend_20_percent():
    """分红按 20% 计税"""
    txns = [
        Transaction(
            id="t1", broker="futu", date=date(2025, 3, 1), symbol="AAPL",
            action=Action.DIVIDEND, quantity=100, price=Decimal("2"),
            amount=Decimal("200"), currency="USD", exchange_rate=Decimal("7.1"),
            tax_withheld=Decimal("20"),  # W-8BEN 10%
        ),
    ]
    summary, lots, _ = compute_tax(txns, 2025)

    assert len(summary.dividends) == 1
    gross_cny = (Decimal("200") * Decimal("7.1")).quantize(Decimal("0.01"))
    assert summary.dividends[0].tax_amount_cny == (gross_cny * Decimal("0.20")).quantize(Decimal("0.01"))


def test_dividend_foreign_tax_credit():
    """分红境外预扣可抵免"""
    txns = [
        Transaction(
            id="t1", broker="futu", date=date(2025, 3, 1), symbol="AAPL",
            action=Action.DIVIDEND, quantity=100, price=Decimal("10"),
            amount=Decimal("1000"), currency="USD", exchange_rate=Decimal("7.1"),
            tax_withheld=Decimal("100"),  # W-8BEN 10%
        ),
    ]
    summary, lots, _ = compute_tax(txns, 2025)

    assert summary.dividends[0].foreign_tax_credit_cny > 0
    assert summary.dividends[0].tax_payable_cny < summary.dividends[0].tax_amount_cny


def test_sell_loss_withholding():
    """卖出亏损 + 盈利混合同一年，仅盈利交税"""
    txns = [
        Transaction(
            id="t1", broker="futu", date=date(2025, 1, 1), symbol="AAPL",
            action=Action.BUY, quantity=100, price=Decimal("50"),
            amount=Decimal("5000"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
        # 盈利
        Transaction(
            id="t2", broker="futu", date=date(2025, 3, 1), symbol="AAPL",
            action=Action.SELL, quantity=50, price=Decimal("80"),
            amount=Decimal("4000"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
        Transaction(
            id="t3", broker="futu", date=date(2025, 4, 1), symbol="AAPL",
            action=Action.BUY, quantity=100, price=Decimal("50"),
            amount=Decimal("5000"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
        # 亏损
        Transaction(
            id="t4", broker="futu", date=date(2025, 5, 1), symbol="AAPL",
            action=Action.SELL, quantity=50, price=Decimal("30"),
            amount=Decimal("1500"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
    ]
    summary, lots, _ = compute_tax(txns, 2025)

    # 仅盈利产生税
    assert len(summary.capital_gains) == 1
    assert summary.capital_gains[0].taxable_income_cny > 0


def test_taxlot_remaining_preserved_on_load():
    lot = TaxLot(
        symbol="AAPL", quantity=100, cost_per_share=Decimal("50"),
        acquire_date=date(2024, 6, 15), remaining=70, origin="buy",
    )
    assert lot.remaining == 70


def test_taxlot_remaining_defaults_to_quantity():
    lot = TaxLot(
        symbol="AAPL", quantity=100, cost_per_share=Decimal("50"),
        acquire_date=date(2025, 1, 1),
    )
    assert lot.remaining == 100


def test_fifo_basic():
    from src.calculator.fifo import FIFOEngine
    engine = FIFOEngine()
    engine.buy("lb", "AAPL", 100, Decimal("50"), date(2025, 1, 1))
    engine.buy("lb", "AAPL", 50, Decimal("60"), date(2025, 2, 1))

    results = engine.sell("lb", "AAPL", 120, Decimal("70"), date(2025, 3, 1))

    assert results[0]["quantity"] == 100
    assert results[0]["cost_per_share"] == Decimal("50")
    assert results[1]["quantity"] == 20
    assert results[1]["cost_per_share"] == Decimal("60")


# ===== 年度净额法测试 =====

def test_annual_net_selected_over_per_transaction():
    """混合盈利+亏损：年度净额法税额更低时应被选中"""
    txns = [
        Transaction(
            id="t1", broker="futu", date=date(2025, 1, 1), symbol="AAPL",
            action=Action.BUY, quantity=100, price=Decimal("50"),
            amount=Decimal("5000"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
        Transaction(
            id="t2", broker="futu", date=date(2025, 3, 1), symbol="AAPL",
            action=Action.SELL, quantity=50, price=Decimal("80"),
            amount=Decimal("4000"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
        Transaction(
            id="t3", broker="futu", date=date(2025, 4, 1), symbol="AAPL",
            action=Action.BUY, quantity=100, price=Decimal("50"),
            amount=Decimal("5000"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
        Transaction(
            id="t4", broker="futu", date=date(2025, 5, 1), symbol="AAPL",
            action=Action.SELL, quantity=50, price=Decimal("30"),
            amount=Decimal("1500"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
    ]
    summary, lots, _ = compute_tax(txns, 2025)

    # 应使用年度净额法
    assert summary.computation_method == "annual_net"
    assert summary.annual_net_comparison is not None

    # 验证年度净额税额 < 逐笔计算税额
    info = summary.annual_net_comparison
    assert info["tax_amount_cny"] < info["per_txn_tax_amount"]

    # 只有一笔年度净额的 TaxItem
    assert len(summary.capital_gains) == 1
    assert summary.capital_gains[0].income_type == "capital_gain_annual_net"


def test_per_transaction_when_no_losses():
    """全部盈利卖出：逐笔计算（无亏损可抵扣，两种方法相同）"""
    txns = [
        Transaction(
            id="t1", broker="futu", date=date(2025, 1, 1), symbol="AAPL",
            action=Action.BUY, quantity=100, price=Decimal("50"),
            amount=Decimal("5000"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
        Transaction(
            id="t2", broker="futu", date=date(2025, 3, 1), symbol="AAPL",
            action=Action.SELL, quantity=50, price=Decimal("80"),
            amount=Decimal("4000"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
        Transaction(
            id="t3", broker="futu", date=date(2025, 4, 1), symbol="AAPL",
            action=Action.SELL, quantity=50, price=Decimal("70"),
            amount=Decimal("3500"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
    ]
    summary, lots, _ = compute_tax(txns, 2025)

    # 应使用逐笔计算（无亏损）
    assert summary.computation_method == "per_transaction"
    assert summary.annual_net_comparison is None
    assert len(summary.capital_gains) == 2


def test_annual_net_all_losses_no_tax():
    """全部亏损卖出：不产生任何税"""
    txns = [
        Transaction(
            id="t1", broker="futu", date=date(2025, 1, 1), symbol="AAPL",
            action=Action.BUY, quantity=100, price=Decimal("100"),
            amount=Decimal("10000"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
        Transaction(
            id="t2", broker="futu", date=date(2025, 6, 1), symbol="AAPL",
            action=Action.SELL, quantity=100, price=Decimal("60"),
            amount=Decimal("6000"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
    ]
    summary, lots, _ = compute_tax(txns, 2025)

    # 年度净亏损时生成一条审计留痕记录，税额为 0
    assert len(summary.capital_gains) == 1
    assert summary.capital_gains[0].tax_amount_cny == 0
    assert summary.total_tax_payable_cny == 0


def test_annual_net_across_symbols():
    """不同股票间的盈亏可以互相抵扣"""
    txns = [
        # AAPL 盈利
        Transaction(
            id="t1", broker="futu", date=date(2025, 1, 1), symbol="AAPL",
            action=Action.BUY, quantity=100, price=Decimal("50"),
            amount=Decimal("5000"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
        Transaction(
            id="t2", broker="futu", date=date(2025, 3, 1), symbol="AAPL",
            action=Action.SELL, quantity=100, price=Decimal("80"),
            amount=Decimal("8000"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
        # GOOGL 亏损
        Transaction(
            id="t3", broker="futu", date=date(2025, 1, 15), symbol="GOOGL",
            action=Action.BUY, quantity=100, price=Decimal("150"),
            amount=Decimal("15000"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
        Transaction(
            id="t4", broker="futu", date=date(2025, 5, 1), symbol="GOOGL",
            action=Action.SELL, quantity=100, price=Decimal("130"),
            amount=Decimal("13000"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
    ]
    summary, lots, _ = compute_tax(txns, 2025)

    # AAPL 盈利: 30 * 100 = 3000 USD, GOOGL 亏损: 20 * 100 = -2000 USD
    # 净盈利: 1000 USD * 7.1 = 7100 CNY
    # 年度净额税: 7100 * 0.2 = 1420 CNY
    # 逐笔计算: 3000 * 7.1 * 0.2 = 4260 CNY

    assert summary.computation_method == "annual_net"
    assert len(summary.capital_gains) == 1
    assert summary.capital_gains[0].income_type == "capital_gain_annual_net"

    # 验证净盈利金额
    assert summary.capital_gains[0].taxable_income_cny == Decimal("7100.00")


def test_carryforwards_mutation_is_expected():
    """carryforwards 设计为 in-place 突变：H-1 消耗 + H-2 新增均需调用方看到结果。

    compute_tax 注释（tax_engine.py:102）：
    "直接引用调用方的字典，H-2/H-1 修复需要 caller 看到新 key 和突变"

    本测试验证：调用方传入的 carryforwards 字典会被正确修改
    （消耗 remaining_amount、新增 source_year key）。
    """
    carryforwards: dict[tuple, list] = {
        ("US", "dividend"): [
            {"id": 1, "source_year": 2022, "country": "US", "income_category": "dividend",
             "remaining_amount": 500.0, "expires_year": 2027},
        ],
    }
    txns = [
        # 分红 → 产生应税义务，消耗结转额度
        Transaction(
            id="t1", broker="futu", date=date(2025, 3, 1), symbol="AAPL",
            action=Action.DIVIDEND, quantity=100, price=Decimal("20"),
            amount=Decimal("2000"), currency="USD", exchange_rate=Decimal("7.1"),
            tax_withheld=Decimal("0"),
        ),
    ]
    compute_tax(txns, 2025, carryforwards=carryforwards)

    # H-1: 原有结转被部分消耗
    # gross=14200, tax=2840, withheld=0, payable=2840
    # 应从结转消耗 min(500, 2840) = 500
    assert carryforwards[("US", "dividend")][0]["remaining_amount"] == 0.0


def test_total_payable_is_per_item_sum():
    """total_tax_payable_cny 应逐笔求和，而非全局计算 max(total - credit, 0)"""
    txns = [
        # 分红 A（US）：超额预扣 → 组内信用额度
        Transaction(
            id="t1", broker="futu", date=date(2025, 3, 1), symbol="AAPL",
            action=Action.DIVIDEND, quantity=100, price=Decimal("10"),
            amount=Decimal("1000"), currency="USD", exchange_rate=Decimal("7.1"),
            tax_withheld=Decimal("300"),  # 预扣 300 USD = 2130 CNY，超过税额 1420
        ),
        # 分红 B（HK）：独立抵免池，无预扣
        Transaction(
            id="t2", broker="futu", date=date(2025, 4, 1), symbol="9988.HK",
            action=Action.DIVIDEND, quantity=1000, price=Decimal("1"),
            amount=Decimal("1000"), currency="HKD", exchange_rate=Decimal("0.91"),
            tax_withheld=Decimal("0"),
        ),
    ]
    summary, _, _ = compute_tax(txns, 2025)

    # US 分红: gross=7100, tax=1420, withheld=2130, credit=min(2130,1420)=1420, payable=0
    # HK 分红: gross=910, tax=182, withheld=0, credit=0, payable=182
    # 逐笔合计 = 0 + 182 = 182
    # 若全局计算 max((1420+182) - 1420, 0) = 182 ← 此例相同
    # 但逻辑本质不同：逐笔 clamp 确保每笔 ≥0 后求和
    assert summary.total_tax_payable_cny == Decimal("182.00")


# ===== G5 修复: 净亏损年份 FTC 不生成结转 =====

def test_net_loss_year_ftc_no_carryforward():
    """G5 修复：年度净亏损时，境外预扣税不生成结转额度

    依据财税〔2020〕3号，仅当抵免限额 > 实际已缴税额时，剩余限额可结转。
    亏损年度抵免限额为 0，不存在可结转额度，已扣税款当年作废（仅审计留痕）。
    """
    txns = [
        # 买入 AAPL
        Transaction(
            id="t1", broker="futu", date=date(2025, 1, 1), symbol="AAPL",
            action=Action.BUY, quantity=100, price=Decimal("100"),
            amount=Decimal("10000"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
        # 买入 GOOGL（高价）
        Transaction(
            id="t2", broker="futu", date=date(2025, 1, 1), symbol="GOOGL",
            action=Action.BUY, quantity=100, price=Decimal("200"),
            amount=Decimal("20000"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
        # 卖出 AAPL 盈利（含外国已扣税）
        Transaction(
            id="t3", broker="futu", date=date(2025, 3, 1), symbol="AAPL",
            action=Action.SELL, quantity=100, price=Decimal("150"),
            amount=Decimal("15000"), currency="USD", exchange_rate=Decimal("7.1"),
            tax_withheld=Decimal("100"),  # 外国已扣税 100 USD
        ),
        # 卖出 GOOGL 大幅亏损
        Transaction(
            id="t4", broker="futu", date=date(2025, 6, 1), symbol="GOOGL",
            action=Action.SELL, quantity=100, price=Decimal("50"),
            amount=Decimal("5000"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
    ]
    # AAPL 盈利: 50*100*7.1 = 35500 CNY, GOOGL 亏损: 150*100*7.1 = 106500 CNY
    # 年度净亏损: 35500 - 106500 = -71000 CNY
    carryforwards: dict[tuple, list] = {}
    summary, lots, _ = compute_tax(txns, 2025, carryforwards=carryforwards)

    # 年度净亏损，不应有应缴税
    assert summary.total_tax_payable_cny == 0

    # 亏损年度不应生成任何结转额度（财税〔2020〕3号：限额为 0，无可结转）
    us_cg = carryforwards.get(("US", "capital_gain"), [])
    assert len(us_cg) == 0

    # 但 excess_withholding_cny 应记录作废金额用于审计留痕
    net_item = [i for i in summary.capital_gains
                if i.income_type == "capital_gain_annual_net"][0]
    # 盈利交易已扣税: 100 USD * 7.1 = 710 CNY
    from decimal import Decimal as D
    assert net_item.excess_withholding_cny == D("710.00")
    assert "作废" in net_item.detail


# ===== H-1: FTC 结转 FIFO 消耗 =====

def test_carryforward_fifo_consumption():
    """H-1: 结转消耗应按 source_year ASC 顺序（最早先到期）"""
    # 传入两笔结转：2020 年（即将到期）和 2022 年
    carryforwards: dict[tuple, list] = {
        ("US", "dividend"): [
            {"id": 1, "source_year": 2022, "country": "US", "income_category": "dividend",
             "remaining_amount": 50.0, "expires_year": 2027},
            {"id": 2, "source_year": 2020, "country": "US", "income_category": "dividend",
             "remaining_amount": 100.0, "expires_year": 2025},
        ],
    }
    txns = [
        Transaction(
            id="t1", broker="futu", date=date(2025, 3, 1), symbol="AAPL",
            action=Action.DIVIDEND, quantity=100, price=Decimal("10"),
            amount=Decimal("1000"), currency="USD", exchange_rate=Decimal("7.1"),
            tax_withheld=Decimal("50"),  # 预扣 50 USD = 355 CNY
        ),
    ]
    # gross=7100, tax=1420, withheld=355, 应缴=1420-355=1065
    # 应先从 2020 年结转消耗 100，再从 2022 年结转消耗 65（但只有 50）
    compute_tax(txns, 2025, carryforwards=carryforwards)

    # 2020 年的结转应被完全消耗（最早先到期）
    rec_2020 = [r for r in carryforwards[("US", "dividend")] if r["source_year"] == 2020]
    assert all(r["remaining_amount"] == 0.0 for r in rec_2020)


def test_carryforward_expired_skipped():
    """H-C: 超过 5 年有效期的结转记录应被跳过（财税〔2020〕3号）"""
    # 2019 年结转（2024 年到期）在 2025 年已过期
    carryforwards: dict[tuple, list] = {
        ("US", "dividend"): [
            {"id": 1, "source_year": 2019, "country": "US", "income_category": "dividend",
             "remaining_amount": 200.0, "expires_year": 2024},
        ],
    }
    txns = [
        Transaction(
            id="t1", broker="futu", date=date(2025, 3, 1), symbol="AAPL",
            action=Action.DIVIDEND, quantity=100, price=Decimal("10"),
            amount=Decimal("1000"), currency="USD", exchange_rate=Decimal("7.1"),
            tax_withheld=Decimal("0"),
        ),
    ]
    # 应缴=1420，但结转已过期，不能用
    compute_tax(txns, 2025, carryforwards=carryforwards)

    # 2019 年的结转不应被消耗
    rec_2019 = [r for r in carryforwards[("US", "dividend")] if r["source_year"] == 2019]
    assert all(r["remaining_amount"] == 200.0 for r in rec_2019)


# ===== M-2: 分红不扣除费用 =====

def test_dividend_no_fee_deduction():
    """M-2: 分红按 gross 全额计税，不扣除费用（个税法第六条）"""
    txns = [
        Transaction(
            id="t1", broker="longbridge", date=date(2025, 3, 1), symbol="AAPL",
            action=Action.DIVIDEND, quantity=100, price=Decimal("10"),
            amount=Decimal("1000"), currency="USD", exchange_rate=Decimal("7.1"),
            tax_withheld=Decimal("200"), fee=Decimal("5"),  # ADR fee 5 USD
        ),
    ]
    summary, _, _ = compute_tax(txns, 2025)

    # gross = 1000 * 7.1 = 7100 CNY
    # 应税所得应等于 gross（不扣 fee）
    div_item = summary.dividends[0]
    assert div_item.taxable_income_cny == Decimal("7100.00")
    # 税额 = 7100 * 0.2 = 1420 CNY
    assert div_item.tax_amount_cny == Decimal("1420.00")


# ===== M-4: 未知币种 =====

def test_unknown_currency_no_ftc():
    """M-4: 未知币种（如 GBP）不应被归入 US 抵免组"""
    from src.calculator.tax_engine import detect_country, TaxItem

    item = TaxItem(
        date="2025-03-01", symbol="VOD.L", income_type="dividend",
        currency="GBP", gross_income_cny=Decimal("1000"),
        deductible_cny=Decimal("0"), taxable_income_cny=Decimal("1000"),
        tax_rate=Decimal("0.20"), tax_amount_cny=Decimal("200"),
        tax_withheld_cny=Decimal("100"), foreign_tax_credit_cny=Decimal("0"),
        excess_withholding_cny=Decimal("0"), tax_payable_cny=Decimal("0"),
    )
    country = detect_country(item)
    # 未知币种应返回 UNKNOWN，而非 US
    assert country == "UNKNOWN"
    assert country != "US"


# ===== 补充缺失路径测试 =====

def test_rsu_sell_to_cover_zero_gain():
    """RSU sell-to-cover: 雇主在归属日自动卖出部分股票用于缴税，零资本利得"""
    txns = [
        Transaction(
            id="t1", broker="boci", date=date(2025, 3, 1), symbol="BABA",
            action=Action.RSU_VEST, quantity=100, price=Decimal("100"),
            amount=Decimal("10000"), currency="USD",
            exchange_rate=Decimal("7.1"), tax_withheld=Decimal("3000"),
        ),
        Transaction(
            id="t2", broker="boci", date=date(2025, 3, 1), symbol="BABA",
            action=Action.SELL, quantity=30, price=Decimal("100"),
            amount=Decimal("3000"), currency="USD",
            exchange_rate=Decimal("7.1"),
        ),
    ]
    summary, lots, _ = compute_tax(txns, 2025)
    # sell-to-cover 以 FMV 卖出，成本 = FMV，资本利得为 0
    cg_items = [
        i for i in summary.capital_gains
        if i.income_type == "capital_gain" and i.taxable_income_cny > 0
    ]
    assert len(cg_items) == 0


def test_option_exercise_cost_includes_premium():
    """期权行权: 股票 lot 成本 = 行权价 + 权利金"""
    txns = [
        Transaction(
            id="t1", broker="lb", date=date(2025, 1, 1), symbol="AAPL250221C150",
            action=Action.OPTION_BUY, quantity=1, price=Decimal("5"),
            amount=Decimal("500"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
        Transaction(
            id="t2", broker="lb", date=date(2025, 2, 21), symbol="AAPL",
            action=Action.OPTION_EXERCISE, quantity=100, price=Decimal("155"),
            amount=Decimal("15500"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
        Transaction(
            id="t3", broker="lb", date=date(2025, 3, 1), symbol="AAPL",
            action=Action.SELL, quantity=100, price=Decimal("200"),
            amount=Decimal("20000"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
    ]
    summary, _, _ = compute_tax(txns, 2025)
    # gain = (200 - 155) * 100 = 4500 USD
    gain_cny = (Decimal("4500") * Decimal("7.1")).quantize(Decimal("0.01"))
    assert len(summary.capital_gains) == 1
    assert summary.capital_gains[0].taxable_income_cny == gain_cny


def test_option_exercise_cross_year():
    """跨年期权行权：2024 年买入期权，2025 年行权"""
    existing = {
        "AAPL250221C150": [
            TaxLot(symbol="AAPL250221C150", quantity=1, cost_per_share=Decimal("5"),
                   acquire_date=date(2024, 11, 1), remaining=1, origin="option_buy",
                   broker_code="lb"),
        ]
    }
    txns = [
        Transaction(
            id="t1", broker="lb", date=date(2025, 2, 21), symbol="AAPL",
            action=Action.OPTION_EXERCISE, quantity=100, price=Decimal("155"),
            amount=Decimal("15500"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
        Transaction(
            id="t2", broker="lb", date=date(2025, 3, 1), symbol="AAPL",
            action=Action.SELL, quantity=100, price=Decimal("180"),
            amount=Decimal("18000"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
    ]
    summary, _, _ = compute_tax(txns, 2025, existing_lots=existing)
    # gain = (180 - 155) * 100 = 2500 USD
    gain_cny = (Decimal("2500") * Decimal("7.1")).quantize(Decimal("0.01"))
    assert len(summary.capital_gains) == 1
    assert summary.capital_gains[0].taxable_income_cny == gain_cny


def test_option_exercise_sell_to_cover():
    """行权后立即卖出缴税（sell-to-cover）：行权 FMV 卖出，零资本利得"""
    txns = [
        Transaction(
            id="t1", broker="lb", date=date(2025, 1, 1), symbol="AAPL250221C150",
            action=Action.OPTION_BUY, quantity=1, price=Decimal("5"),
            amount=Decimal("500"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
        Transaction(
            id="t2", broker="lb", date=date(2025, 2, 21), symbol="AAPL",
            action=Action.OPTION_EXERCISE, quantity=100, price=Decimal("155"),
            amount=Decimal("15500"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
        # 以行权日 FMV 卖出 30 股用于缴税
        Transaction(
            id="t3", broker="lb", date=date(2025, 2, 21), symbol="AAPL",
            action=Action.SELL, quantity=30, price=Decimal("155"),
            amount=Decimal("4650"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
    ]
    summary, _, _ = compute_tax(txns, 2025)
    # sell-to-cover 以 FMV 卖出，零资本利得
    cg_items = [
        i for i in summary.capital_gains
        if i.income_type == "capital_gain" and i.taxable_income_cny > 0
    ]
    assert len(cg_items) == 0


def test_interest_income_taxed_at_20():
    """利息收入（如富途股票收益计划）按 20% 税率征税"""
    txns = [
        Transaction(
            id="t1", broker="futu", date=date(2025, 6, 1), symbol="STOCK_YIELD",
            action=Action.INTEREST, quantity=0, price=Decimal("0"),
            amount=Decimal("500"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
    ]
    summary, _, _ = compute_tax(txns, 2025)
    assert len(summary.dividends) == 1
    item = summary.dividends[0]
    assert item.income_type == "interest_income"
    expected_tax = (Decimal("500") * Decimal("7.1") * Decimal("0.20")).quantize(Decimal("0.01"))
    assert item.tax_amount_cny == expected_tax


def test_yield_income_taxed_at_20():
    """投资收益（BOCI 专有）按 20% 税率征税"""
    txns = [
        Transaction(
            id="t1", broker="boci", date=date(2025, 6, 1), symbol="BABA",
            action=Action.YIELD_INCOME, quantity=0, price=Decimal("0"),
            amount=Decimal("300"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
    ]
    summary, _, _ = compute_tax(txns, 2025)
    assert len(summary.dividends) == 1
    item = summary.dividends[0]
    assert item.income_type == "yield_income"
    expected_tax = (Decimal("300") * Decimal("7.1") * Decimal("0.20")).quantize(Decimal("0.01"))
    assert item.tax_amount_cny == expected_tax


def test_rsu_bracket_boundary_36000():
    """RSU 收入恰好在 36000 CNY 边界"""
    # 36000 / 7.1 ≈ 5070.42 USD
    income_usd = Decimal("5070.42")
    txns = [
        Transaction(
            id="t1", broker="boci", date=date(2025, 3, 1), symbol="BABA",
            action=Action.RSU_VEST, quantity=5070, price=Decimal("1"),
            amount=income_usd, currency="USD", exchange_rate=Decimal("7.1"),
            tax_withheld=Decimal("0"),
        ),
    ]
    summary, _, _ = compute_tax(txns, 2025)
    assert summary.rsu_income is not None
    # 36000 * 3% - 0 = 1080
    assert summary.rsu_income.tax_amount_cny == Decimal("1080.00")
    assert summary.rsu_income.tax_rate == Decimal("0.03")


def test_rsu_bracket_boundary_144000():
    """RSU 收入恰好在 144000 CNY 边界"""
    # 144000 / 7.1 ≈ 20281.69 USD
    income_usd = Decimal("20281.69")
    txns = [
        Transaction(
            id="t1", broker="boci", date=date(2025, 3, 1), symbol="BABA",
            action=Action.RSU_VEST, quantity=20282, price=Decimal("1"),
            amount=income_usd, currency="USD", exchange_rate=Decimal("7.1"),
            tax_withheld=Decimal("0"),
        ),
    ]
    summary, _, _ = compute_tax(txns, 2025)
    assert summary.rsu_income is not None
    # 144000 * 10% - 2520 = 11880
    assert summary.rsu_income.tax_amount_cny == Decimal("11880.00")
    assert summary.rsu_income.tax_rate == Decimal("0.10")


def test_ftc_proportional_allocation_within_group():
    """FTC 同组内多笔按税额比例分配抵免"""
    txns = [
        Transaction(
            id="t1", broker="futu", date=date(2025, 3, 1), symbol="AAPL",
            action=Action.DIVIDEND, quantity=100, price=Decimal("10"),
            amount=Decimal("1000"), currency="USD", exchange_rate=Decimal("7.1"),
            tax_withheld=Decimal("100"),
        ),
        Transaction(
            id="t2", broker="futu", date=date(2025, 4, 1), symbol="MSFT",
            action=Action.DIVIDEND, quantity=100, price=Decimal("10"),
            amount=Decimal("1000"), currency="USD", exchange_rate=Decimal("7.1"),
            tax_withheld=Decimal("100"),
        ),
    ]
    summary, _, _ = compute_tax(txns, 2025)
    # 两笔税额相同，FTC 应均分
    assert len(summary.dividends) == 2
    for item in summary.dividends:
        assert item.foreign_tax_credit_cny > 0


def test_withholding_allocation_across_multiple_lot_consumptions():
    """一笔卖出消耗多个 lot 时，预扣税应正确分配到每个 TaxItem"""
    txns = [
        Transaction(
            id="t1", broker="futu", date=date(2025, 1, 1), symbol="AAPL",
            action=Action.BUY, quantity=50, price=Decimal("50"),
            amount=Decimal("2500"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
        Transaction(
            id="t2", broker="futu", date=date(2025, 1, 15), symbol="AAPL",
            action=Action.BUY, quantity=50, price=Decimal("60"),
            amount=Decimal("3000"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
        Transaction(
            id="t3", broker="futu", date=date(2025, 3, 1), symbol="AAPL",
            action=Action.SELL, quantity=100, price=Decimal("80"),
            amount=Decimal("8000"), currency="USD", exchange_rate=Decimal("7.1"),
            tax_withheld=Decimal("50"),
        ),
    ]
    summary, _, _ = compute_tax(txns, 2025)
    # 一笔卖出消耗两个 lot，产生两条 capital_gain TaxItem
    cg_items = [i for i in summary.capital_gains if i.income_type == "capital_gain"]
    assert len(cg_items) == 2
    # 每条 TaxItem 都有预扣税分配
    total_withheld = sum(i.tax_withheld_cny for i in cg_items)
    assert total_withheld > 0


def test_option_expire_generates_tax_item():
    """期权过期应生成 TaxItem（zero tax，审计留痕）"""
    txns = [
        # 买入期权
        Transaction(
            id="t1", broker="lb", date=date(2025, 1, 1), symbol="AAPL250221C150",
            action=Action.OPTION_BUY, quantity=1, price=Decimal("3"),
            amount=Decimal("300"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
        # 另一笔盈利交易，确保使用 per_transaction 模式
        Transaction(
            id="t2", broker="lb", date=date(2025, 1, 15), symbol="MSFT",
            action=Action.BUY, quantity=100, price=Decimal("50"),
            amount=Decimal("5000"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
        Transaction(
            id="t3", broker="lb", date=date(2025, 3, 1), symbol="MSFT",
            action=Action.SELL, quantity=100, price=Decimal("80"),
            amount=Decimal("8000"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
        # 期权过期
        Transaction(
            id="t4", broker="lb", date=date(2025, 2, 21), symbol="AAPL250221C150",
            action=Action.OPTION_EXPIRE, quantity=1, price=Decimal("0"),
            amount=Decimal("300"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
    ]
    summary, _, _ = compute_tax(txns, 2025)
    # 期权过期应产生一条 capital_gain_expire_loss 类型的 TaxItem
    expire_items = [
        i for i in summary.capital_gains
        if "expire" in i.income_type
    ]
    assert len(expire_items) == 1
    assert expire_items[0].tax_amount_cny == 0


def test_hkd_dividend_ftc_classified_as_hk():
    """HKD 股息应被分类为 HK 国家用于 FTC"""
    from src.calculator.tax_engine import detect_country, TaxItem

    item = TaxItem(
        date="2025-06-01", symbol="9988.HK", income_type="dividend",
        currency="HKD", gross_income_cny=Decimal("910"),
        deductible_cny=Decimal("0"), taxable_income_cny=Decimal("910"),
        tax_rate=Decimal("0.20"), tax_amount_cny=Decimal("182"),
        tax_withheld_cny=Decimal("0"), foreign_tax_credit_cny=Decimal("0"),
        excess_withholding_cny=Decimal("0"), tax_payable_cny=Decimal("182"),
    )
    country = detect_country(item)
    assert country == "HK"


def test_empty_transactions():
    """空交易列表应返回零税额"""
    summary, lots, consumptions = compute_tax([], 2025)
    assert summary.total_tax_payable_cny == 0
    assert summary.rsu_income is None
    assert len(summary.capital_gains) == 0
    assert len(summary.dividends) == 0


def test_rsu_multiple_vests_aggregation():
    """多次 RSU vest 应合并后计算累进税"""
    txns = [
        Transaction(
            id="t1", broker="boci", date=date(2025, 3, 1), symbol="BABA",
            action=Action.RSU_VEST, quantity=100, price=Decimal("100"),
            amount=Decimal("10000"), currency="USD",
            exchange_rate=Decimal("7.1"), tax_withheld=Decimal("3000"),
        ),
        Transaction(
            id="t2", broker="boci", date=date(2025, 6, 1), symbol="BABA",
            action=Action.RSU_VEST, quantity=50, price=Decimal("120"),
            amount=Decimal("6000"), currency="USD",
            exchange_rate=Decimal("7.1"), tax_withheld=Decimal("1800"),
        ),
    ]
    summary, _, _ = compute_tax(txns, 2025)
    # 合并应税收入: (10000 + 6000) * 7.1 = 113600 CNY
    assert summary.rsu_income is not None
    assert summary.rsu_income.taxable_income_cny == Decimal("113600.00")


def test_carryforward_multiple_year_consumption():
    """3+ 年结转额度的 FIFO 消耗顺序"""
    carryforwards: dict[tuple, list] = {
        ("US", "dividend"): [
            {"id": 1, "source_year": 2023, "country": "US", "income_category": "dividend",
             "remaining_amount": 200.0, "expires_year": 2028},
            {"id": 2, "source_year": 2021, "country": "US", "income_category": "dividend",
             "remaining_amount": 300.0, "expires_year": 2026},
            {"id": 3, "source_year": 2020, "country": "US", "income_category": "dividend",
             "remaining_amount": 100.0, "expires_year": 2025},
        ],
    }
    txns = [
        Transaction(
            id="t1", broker="futu", date=date(2025, 3, 1), symbol="AAPL",
            action=Action.DIVIDEND, quantity=100, price=Decimal("20"),
            amount=Decimal("2000"), currency="USD", exchange_rate=Decimal("7.1"),
            tax_withheld=Decimal("0"),
        ),
    ]
    # gross=14200, tax=2840, 应从结转消耗 2840
    # FIFO: 先 2020 (100), 再 2021 (300), 再 2023 (200) = 600 全部消耗
    compute_tax(txns, 2025, carryforwards=carryforwards)

    # 2020 年应被完全消耗
    rec_2020 = [r for r in carryforwards[("US", "dividend")] if r["source_year"] == 2020]
    assert all(r["remaining_amount"] == 0.0 for r in rec_2020)
    # 2021 年应被完全消耗
    rec_2021 = [r for r in carryforwards[("US", "dividend")] if r["source_year"] == 2021]
    assert all(r["remaining_amount"] == 0.0 for r in rec_2021)
    # 2023 年应被完全消耗
    rec_2023 = [r for r in carryforwards[("US", "dividend")] if r["source_year"] == 2023]
    assert all(r["remaining_amount"] == 0.0 for r in rec_2023)


def test_yield_income_ftc_category_is_yield_not_capital_gain():
    """P0 修复：yield_income 的 FTC 结转应归入 'yield' 类别，而非错误落入 'capital_gain'"""
    from src.calculator.tax_engine import detect_country, compute_tax

    # 模拟 2024 年有 yield_income 的 FTC 结转
    carryforwards = {
        ("US", "yield"): [
            {"id": 1, "source_year": 2024, "country": "US", "income_category": "yield",
             "remaining_amount": 100.0, "expires_year": 2029},
        ],
    }
    txns = [
        # 2025 年 yield_income 产生应纳税额
        Transaction(
            id="t1", broker="boci", date=date(2025, 3, 1), symbol="BABA",
            action=Action.YIELD_INCOME, quantity=0, price=Decimal("0"),
            amount=Decimal("1000"), currency="USD", exchange_rate=Decimal("7.1"),
            tax_withheld=Decimal("0"),
        ),
    ]
    summary, _, _ = compute_tax(txns, 2025, carryforwards=carryforwards)

    # yield_income 应产生 TaxItem
    yield_items = [i for i in summary.dividends if i.income_type == "yield_income"]
    assert len(yield_items) == 1
    item = yield_items[0]
    # 应纳税额 = 1000 * 7.1 * 0.20 = 142 CNY
    assert item.tax_amount_cny > 0
    # 应使用结转额度抵免
    assert item.foreign_tax_credit_cny > 0
    # 结转记录应被消耗
    assert carryforwards[("US", "yield")][0]["remaining_amount"] < 100.0


def test_leveraged_etf_dividend_taxed_as_normal_dividend():
    """合规修复：杠杆 ETF 分红统一按 20% 股息红利申报，不做 ROC 递延

    中国税法不认可美国 ROC（资本返还）概念，境外现金分红
    统一归为股息红利所得，适用 20% 税率（个税法第三条）。
    """
    txns = [
        # 买入杠杆 ETF
        Transaction(
            id="t1", broker="futu", date=date(2025, 1, 1), symbol="TSLL",
            action=Action.BUY, quantity=100, price=Decimal("10"),
            amount=Decimal("1000"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
        # 收到分红
        Transaction(
            id="t2", broker="futu", date=date(2025, 2, 1), symbol="TSLL",
            action=Action.DIVIDEND, quantity=100, price=Decimal("0.5"),
            amount=Decimal("50"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
        # 卖出（成本不做冲减，按原始买入价）
        Transaction(
            id="t3", broker="futu", date=date(2025, 3, 1), symbol="TSLL",
            action=Action.SELL, quantity=100, price=Decimal("10"),
            amount=Decimal("1000"), currency="USD", exchange_rate=Decimal("7.1"),
        ),
    ]
    summary, _, _ = compute_tax(txns, 2025)

    # 分红应作为股息红利所得计税：50 * 7.1 = 355 CNY，税额 = 355 * 0.20 = 71 CNY
    div_items = [i for i in summary.dividends if i.symbol == "TSLL"]
    assert len(div_items) == 1
    assert div_items[0].taxable_income_cny == Decimal("355.00")
    assert div_items[0].tax_amount_cny == Decimal("71.00")

    # 卖出无盈亏，不应产生资本利得税
    cg_items = [i for i in summary.capital_gains if i.income_type == "capital_gain"]
    assert len(cg_items) == 0


def test_rsu_tax_withheld_equals_domestic_withheld():
    """回归测试：RSU 的 tax_withheld_cny 必须等于 domestic_withheld_cny

    历史 Bug：tax_engine.py 曾将 tax_withheld_cny 硬编码为 Decimal("0")，
    导致结构化字段不反映实际代扣金额，tax_items 表中 tax_withheld_cny 为 0。

    RSU 代扣属于境内代扣代缴，tax_withheld_cny 和 domestic_withheld_cny
    应保持一致，以便数据库和报表正确记录。
    """
    txns = [
        Transaction(
            id="t1", broker="boci", date=date(2025, 4, 1), symbol="BABA",
            action=Action.RSU_VEST, quantity=200, price=Decimal("132.23"),
            amount=Decimal("26446"), currency="USD",
            exchange_rate=Decimal("7.1782"),
            tax_withheld=Decimal("3138.70"),
        ),
    ]
    summary, _, _ = compute_tax(txns, 2025)

    assert summary.rsu_income is not None
    rsu = summary.rsu_income
    # 两个 withheld 字段必须一致
    assert rsu.tax_withheld_cny == rsu.domestic_withheld_cny
    # 且不能为 0
    assert rsu.tax_withheld_cny > 0


def test_rsu_multi_currency_usd_and_hkd():
    """模拟 CLI 从 rsu_vests 表加载的真实场景：同年有 USD 和 HKD 两种货币的 RSU

    验证：
    1. 不同币种/汇率的 RSU 正确汇总
    2. 境内已代扣金额正确累加
    3. tax_withheld_cny 正确反映总代扣
    """
    txns = [
        # BABA USD RSU（模拟从 rsu_vests 加载，exchange_rate 为凭证上的汇率）
        Transaction(
            id="rsu_vest_1", broker="boci", date=date(2025, 4, 1), symbol="BABA",
            action=Action.RSU_VEST, quantity=200, price=Decimal("132.23"),
            amount=Decimal("26446"), currency="USD",
            exchange_rate=Decimal("7.1782"),
            tax_withheld=Decimal("3138.70"),
        ),
        # 9988.HK HKD RSU
        Transaction(
            id="rsu_vest_2", broker="boci", date=date(2025, 7, 1), symbol="9988.HK",
            action=Action.RSU_VEST, quantity=56, price=Decimal("109.80"),
            amount=Decimal("6148.80"), currency="HKD",
            exchange_rate=Decimal("0.91126"),
            tax_withheld=Decimal("1229.76"),
        ),
        # 9988.HK HKD RSU（第二次归属）
        Transaction(
            id="rsu_vest_3", broker="boci", date=date(2025, 10, 1), symbol="9988.HK",
            action=Action.RSU_VEST, quantity=56, price=Decimal("177"),
            amount=Decimal("9912"), currency="HKD",
            exchange_rate=Decimal("0.91298"),
            tax_withheld=Decimal("1982.40"),
        ),
    ]
    summary, _, _ = compute_tax(txns, 2025)

    assert summary.rsu_income is not None
    rsu = summary.rsu_income

    # 总收入应为三笔之和
    income_usd = (Decimal("26446") * Decimal("7.1782")).quantize(Decimal("0.01"))
    income_hkd1 = (Decimal("6148.80") * Decimal("0.91126")).quantize(Decimal("0.01"))
    income_hkd2 = (Decimal("9912") * Decimal("0.91298")).quantize(Decimal("0.01"))
    expected_total = (income_usd + income_hkd1 + income_hkd2).quantize(Decimal("0.01"))
    assert rsu.taxable_income_cny == expected_total

    # 总代扣 = 三笔代扣之和
    withheld_usd = (Decimal("3138.70") * Decimal("7.1782")).quantize(Decimal("0.01"))
    withheld_hkd1 = (Decimal("1229.76") * Decimal("0.91126")).quantize(Decimal("0.01"))
    withheld_hkd2 = (Decimal("1982.40") * Decimal("0.91298")).quantize(Decimal("0.01"))
    expected_withheld = (withheld_usd + withheld_hkd1 + withheld_hkd2).quantize(Decimal("0.01"))
    assert rsu.domestic_withheld_cny == expected_withheld
    assert rsu.tax_withheld_cny == expected_withheld
    # 代扣 > 应纳税额时，应补缴为 0
    assert rsu.tax_payable_cny == Decimal("0.00")
