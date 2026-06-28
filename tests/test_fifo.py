from datetime import date
from decimal import Decimal
from src.calculator.fifo import FIFOEngine
from src.models import TaxLot


def test_basic_fifo():
    engine = FIFOEngine()
    engine.buy("lb", "AAPL", 100, Decimal("50"), date(2025, 1, 1))
    engine.buy("lb", "AAPL", 50, Decimal("60"), date(2025, 2, 1))

    results = engine.sell("lb", "AAPL", 120, Decimal("70"), date(2025, 3, 1))

    assert results[0]["quantity"] == 100
    assert results[0]["cost_per_share"] == Decimal("50")
    assert results[0]["gain_loss"] == Decimal("2000")

    assert results[1]["quantity"] == 20
    assert results[1]["cost_per_share"] == Decimal("60")
    assert results[1]["gain_loss"] == Decimal("200")


def test_fifo_insufficient_shares():
    engine = FIFOEngine()
    engine.buy("lb", "AAPL", 50, Decimal("50"), date(2025, 1, 1))

    try:
        engine.sell("lb", "AAPL", 100, Decimal("60"), date(2025, 2, 1))
        assert False, "应抛出异常"
    except ValueError:
        pass


def test_get_holdings():
    engine = FIFOEngine()
    engine.buy("lb", "AAPL", 100, Decimal("50"), date(2025, 1, 1))
    engine.buy("lb", "MSFT", 50, Decimal("300"), date(2025, 1, 1))
    engine.sell("lb", "AAPL", 30, Decimal("55"), date(2025, 2, 1))

    holdings = engine.get_holdings()
    assert holdings["AAPL"] == 70
    assert holdings["MSFT"] == 50


def test_fifo_with_existing_lots_cross_year():
    """测试跨年持仓：2024 年买入，2025 年卖出"""
    existing = {
        "AAPL": [
            TaxLot(symbol="AAPL", quantity=100, cost_per_share=Decimal("50"),
                   acquire_date=date(2024, 6, 15), remaining=100, origin="buy",
                   broker_code="lb"),
        ]
    }
    engine = FIFOEngine(existing_lots=existing)
    engine.buy("lb", "AAPL", 50, Decimal("60"), date(2025, 3, 1))

    results = engine.sell("lb", "AAPL", 120, Decimal("70"), date(2025, 4, 1))

    assert results[0]["quantity"] == 100
    assert results[0]["cost_per_share"] == Decimal("50")
    assert results[1]["quantity"] == 20
    assert results[1]["cost_per_share"] == Decimal("60")


def test_capital_loss_recorded():
    """测试亏损卖出被正确记录"""
    engine = FIFOEngine()
    engine.buy("lb", "AAPL", 100, Decimal("50"), date(2025, 1, 1))

    results = engine.sell("lb", "AAPL", 50, Decimal("30"), date(2025, 2, 1))

    assert results[0]["gain_loss"] < 0
    assert results[0]["gain_loss"] == Decimal("-1000")


def test_fifo_origin_tracking():
    """测试股份来源追溯"""
    engine = FIFOEngine()
    engine.buy("lb", "AAPL", 100, Decimal("50"), date(2025, 1, 1), origin="buy")
    engine.buy("lb", "AAPL", 50, Decimal("0"), date(2025, 2, 1), origin="rsu_vest")

    results = engine.sell("lb", "AAPL", 120, Decimal("70"), date(2025, 3, 1))

    assert results[0]["origin"] == "buy"
    assert results[1]["origin"] == "rsu_vest"


def test_profit_and_loss():
    engine = FIFOEngine()
    engine.buy("lb", "AAPL", 100, Decimal("50"), date(2025, 1, 1))

    # 盈利卖出
    results = engine.sell("lb", "AAPL", 50, Decimal("80"), date(2025, 2, 1))
    assert results[0]["gain_loss"] == Decimal("1500")

    # 亏损卖出
    engine.buy("lb", "AAPL", 50, Decimal("50"), date(2025, 3, 1))
    results = engine.sell("lb", "AAPL", 50, Decimal("30"), date(2025, 4, 1))
    assert results[0]["gain_loss"] == Decimal("-1000")


def test_get_remaining_lots():
    """测试获取剩余持仓批次"""
    engine = FIFOEngine()
    engine.buy("lb", "AAPL", 100, Decimal("50"), date(2025, 1, 1), origin="buy")
    engine.sell("lb", "AAPL", 30, Decimal("60"), date(2025, 2, 1))

    lots = engine.get_remaining_lots()
    assert len(lots["AAPL"]) == 1
    assert lots["AAPL"][0].remaining == 70


def test_taxlot_remaining_preserved_on_load():
    """测试 TaxLot 从 JSON 加载时 remaining 不被覆盖"""
    # 模拟从 JSON 加载的剩余持仓（原 remaining = 70）
    lot = TaxLot(
        symbol="AAPL",
        quantity=100,
        cost_per_share=Decimal("50"),
        acquire_date=date(2024, 6, 15),
        remaining=70,  # 已卖出 30 股
        origin="buy",
    )
    assert lot.remaining == 70  # 不应被重置为 100


def test_taxlot_remaining_defaults_to_quantity():
    """测试新建 TaxLot 时 remaining 默认等于 quantity"""
    lot = TaxLot(
        symbol="AAPL",
        quantity=100,
        cost_per_share=Decimal("50"),
        acquire_date=date(2025, 1, 1),
    )
    assert lot.remaining == 100


def test_fifo_per_broker_independent():
    """按券商独立 FIFO：不同券商的持仓各自独立，不跨券商消耗

    各券商独立维护成本队列，卖出只能消耗本券商的持仓。
    Longbridge 买 100 股 @ $50，Futu 买 50 股 @ $80；
    Longbridge 卖出 120 股时，只能消耗 Longbridge 的 100 股（不够会报错）。
    """
    engine = FIFOEngine()
    # Longbridge 买入 AAPL 100 股 @ $50
    engine.buy("lb", "AAPL", 100, Decimal("50"), date(2025, 1, 1))
    # Futu 买入 AAPL 50 股 @ $80
    engine.buy("futu", "AAPL", 50, Decimal("80"), date(2025, 2, 1))

    # Longbridge 卖出 100 股 — 只消耗 Longbridge 的 100 股 @ $50
    results = engine.sell("lb", "AAPL", 100, Decimal("70"), date(2025, 3, 1))
    assert len(results) == 1
    assert results[0]["quantity"] == 100
    assert results[0]["cost_per_share"] == Decimal("50")
    assert results[0]["broker_code"] == "lb"
    assert results[0]["gain_loss"] == Decimal("2000")  # (70-50)*100

    # Longbridge 再卖 20 股 — 持仓不足，报错
    import pytest
    with pytest.raises(ValueError, match="超出持仓"):
        engine.sell("lb", "AAPL", 20, Decimal("70"), date(2025, 3, 2))

    # Futu 的 50 股未被动用
    holdings = engine.get_holdings()
    assert holdings["AAPL"] == 50  # 只剩 Futu 50 股

    lots = engine.get_remaining_lots()
    assert len(lots["AAPL"]) == 1
    assert lots["AAPL"][0].remaining == 50
    assert lots["AAPL"][0].broker_code == "futu"


def test_fifo_per_broker_with_existing_lots():
    """按券商独立：一个券商的卖出只能消耗本券商的 carryforward 持仓"""
    existing = {
        "BABA": [
            TaxLot(symbol="BABA", quantity=602, cost_per_share=Decimal("124.66"),
                   acquire_date=date(2024, 12, 31), remaining=602, origin="carryforward",
                   broker_code="boci"),
        ]
    }
    engine = FIFOEngine(existing_lots=existing)
    # Longbridge 2025 年买入 251 股 BABA @ $86.66
    engine.buy("lb", "BABA", 251, Decimal("86.66"), date(2025, 1, 15))

    # Longbridge 卖出 200 股 — 只能消耗 Longbridge 的 251 股（不消耗 BOCI）
    results = engine.sell("lb", "BABA", 200, Decimal("100"), date(2025, 3, 1))
    assert len(results) == 1
    assert results[0]["quantity"] == 200
    assert results[0]["cost_per_share"] == Decimal("86.66")
    assert results[0]["broker_code"] == "lb"

    # BOCI 的 602 股未被动用
    lots = engine.get_remaining_lots()
    assert len(lots["BABA"]) == 2
    boci_lots = [l for l in lots["BABA"] if l.broker_code == "boci"]
    lb_lots = [l for l in lots["BABA"] if l.broker_code == "lb"]
    assert len(boci_lots) == 1
    assert boci_lots[0].remaining == 602  # BOCI 未消耗
    assert len(lb_lots) == 1
    assert lb_lots[0].remaining == 51  # LB: 251-200=51

    # BOCI 卖出 400 股 — 消耗 BOCI carryforward
    results2 = engine.sell("boci", "BABA", 400, Decimal("100"), date(2025, 4, 1))
    assert len(results2) == 1
    assert results2[0]["quantity"] == 400
    assert results2[0]["broker_code"] == "boci"


def test_fifo_per_broker_audit_trail():
    """审计追踪：sell result 记录 lot 归属券商和卖出方"""
    engine = FIFOEngine()
    engine.buy("futu", "AAPL", 100, Decimal("50"), date(2025, 1, 1))
    engine.buy("futu", "AAPL", 50, Decimal("60"), date(2025, 2, 1))

    # futu 卖出 120 股：消耗 futu 的 100 股 + 20 股
    results = engine.sell("futu", "AAPL", 120, Decimal("70"), date(2025, 3, 1))

    # 两笔消耗都来自 futu
    assert results[0]["broker_code"] == "futu"         # lot 原始归属
    assert results[0]["sell_broker_code"] == "futu"    # 卖出方
    assert results[0]["quantity"] == 100
    # 第二笔消耗：futu 的 lot
    assert results[1]["broker_code"] == "futu"
    assert results[1]["sell_broker_code"] == "futu"
    assert results[1]["quantity"] == 20


# ===== 补充缺失路径测试 =====

def test_contract_multiplier_stock():
    """股票交易使用乘数 1"""
    from src.calculator.contract import ContractSpec
    spec = ContractSpec.for_symbol("AAPL")
    assert spec.multiplier == 1


def test_contract_multiplier_option():
    """期权交易使用乘数 100"""
    from src.calculator.contract import ContractSpec
    spec = ContractSpec.for_symbol("AAPL_OPT_250221_150.0_C")
    assert spec.multiplier == 100


def test_fifo_with_contract_multiplier():
    """FIFO 计算应通过 ContractSpec 使用正确的乘数"""
    engine = FIFOEngine()
    # 期权：1 份合约 = 100 股等效
    engine.buy("lb", "AAPL_OPT_250221C150", 1, Decimal("5"), date(2025, 1, 1))
    # 卖出 0.5 份合约（实际消耗 50 股等效）
    results = engine.sell("lb", "AAPL_OPT_250221C150", 1, Decimal("8"), date(2025, 2, 1))
    # gain = (8 - 5) * 100 = 300
    assert len(results) == 1
    assert results[0]["gain_loss"] == Decimal("300")


def test_option_write_sell_to_open():
    """期权卖出开仓（write）：无买入记录时允许卖出创建空头"""
    engine = FIFOEngine()
    results = engine.sell(
        "lb", "AAPL_OPT_250221C150", 1, Decimal("5"),
        date(2025, 1, 1), short_allowed=True,
    )
    assert len(results) == 1
    assert results[0]["origin"] == "option_write"
    assert results[0]["cost_basis"] == Decimal("0")


def test_option_expire_write_position():
    """写仓期权过期：权利金全额确认为收益"""
    engine = FIFOEngine()
    # 卖出开仓 1 份期权，收到权利金 $5
    engine.sell(
        "lb", "AAPL_OPT_250221C150", 1, Decimal("5"),
        date(2025, 1, 1), short_allowed=True,
    )
    # 期权过期
    results = engine.expire("lb", "AAPL_OPT_250221C150", 1, date(2025, 2, 21))
    assert len(results) == 1
    # 写仓过期：gain = 权利金 = 5 * 100 = 500
    assert results[0]["gain_loss"] > 0
    assert results[0]["origin"] == "option_write_expire"


def test_option_expire_long_position():
    """多头期权过期：全部成本作为损失"""
    engine = FIFOEngine()
    engine.buy("lb", "AAPL_OPT_250221C150", 1, Decimal("3"), date(2025, 1, 1))
    results = engine.expire("lb", "AAPL_OPT_250221C150", 1, date(2025, 2, 21))
    assert len(results) == 1
    # 过期损失 = 3 * 100 = 300
    assert results[0]["gain_loss"] < 0
    assert results[0]["gain_loss"] == Decimal("-300")


def test_option_exercise_creates_stock_lot():
    """期权行权创建股票 lot，成本 = 行权价 + 权利金"""
    engine = FIFOEngine()
    engine.buy("lb", "AAPL_OPT_250221C150", 1, Decimal("5"), date(2025, 1, 1))
    # 行权：strike $150 + premium $5 = $155
    engine.exercise("lb", "AAPL", 100, Decimal("155"), date(2025, 2, 21))
    lots = engine.get_remaining_lots()
    aapl_lots = lots.get("AAPL", [])
    assert len(aapl_lots) == 1
    assert aapl_lots[0].quantity == 100
    assert aapl_lots[0].cost_per_share == Decimal("155")
    assert aapl_lots[0].origin == "option_exercise"


def test_option_exercise_with_existing_stock_lot():
    """行权创建的股票 lot 可与已有股票 lot 共存（不合并）"""
    engine = FIFOEngine()
    # 先买入股票
    engine.buy("lb", "AAPL", 50, Decimal("100"), date(2025, 1, 1))
    # 再行权期权
    engine.buy("lb", "AAPL_OPT_250221C150", 1, Decimal("5"), date(2025, 1, 15))
    engine.exercise("lb", "AAPL", 100, Decimal("155"), date(2025, 2, 21))
    # 应有两个独立的 AAPL lot
    lots = engine.get_remaining_lots()
    aapl_lots = lots.get("AAPL", [])
    assert len(aapl_lots) == 2
    # FIFO 顺序：先买入的在前
    assert aapl_lots[0].quantity == 50
    assert aapl_lots[0].cost_per_share == Decimal("100")
    assert aapl_lots[0].origin == "buy"
    assert aapl_lots[1].quantity == 100
    assert aapl_lots[1].cost_per_share == Decimal("155")
    assert aapl_lots[1].origin == "option_exercise"


def test_option_exercise_partial_sell():
    """行权后部分卖出：FIFO 先消耗早的 lot"""
    engine = FIFOEngine()
    engine.buy("lb", "AAPL", 100, Decimal("100"), date(2025, 1, 1))
    engine.buy("lb", "AAPL_OPT_250221C150", 1, Decimal("5"), date(2025, 1, 15))
    engine.exercise("lb", "AAPL", 100, Decimal("155"), date(2025, 2, 21))
    # 卖出 120 股：先消耗 buy lot 100 股，再消耗 exercise lot 20 股
    results = engine.sell("lb", "AAPL", 120, Decimal("200"), date(2025, 3, 1))
    assert len(results) == 2
    assert results[0]["quantity"] == 100
    assert results[0]["cost_per_share"] == Decimal("100")
    assert results[0]["origin"] == "buy"
    assert results[1]["quantity"] == 20
    assert results[1]["cost_per_share"] == Decimal("155")
    assert results[1]["origin"] == "option_exercise"


def test_broker_code_none_defaults_to_empty():
    """broker_code=None 时应默认为空字符串 key"""
    engine = FIFOEngine()
    engine.buy(None, "AAPL", 100, Decimal("50"), date(2025, 1, 1))
    results = engine.sell(None, "AAPL", 50, Decimal("60"), date(2025, 2, 1))
    assert len(results) == 1
    assert results[0]["gain_loss"] == Decimal("500")


def test_same_date_multiple_lots_fifo_order():
    """同日多批次买入：按 FIFO 顺序消耗（先买入的先消耗）"""
    engine = FIFOEngine()
    engine.buy("lb", "AAPL", 50, Decimal("40"), date(2025, 1, 1))
    engine.buy("lb", "AAPL", 50, Decimal("60"), date(2025, 1, 1))
    # 卖出 60 股，应先消耗第一批 50 股，再消耗第二批 10 股
    results = engine.sell("lb", "AAPL", 60, Decimal("55"), date(2025, 2, 1))
    assert len(results) == 2
    assert results[0]["quantity"] == 50
    assert results[0]["cost_per_share"] == Decimal("40")
    assert results[1]["quantity"] == 10
    assert results[1]["cost_per_share"] == Decimal("60")


def test_sell_exact_remaining_quantity():
    """卖出数量恰好等于剩余持仓"""
    engine = FIFOEngine()
    engine.buy("lb", "AAPL", 100, Decimal("50"), date(2025, 1, 1))
    results = engine.sell("lb", "AAPL", 100, Decimal("70"), date(2025, 2, 1))
    assert len(results) == 1
    assert results[0]["quantity"] == 100
    # 持仓应被完全消耗
    holdings = engine.get_holdings()
    assert holdings.get("AAPL", 0) == 0


def test_option_write_then_expire_without_close():
    """写仓后不做买回：过期时权利金全额收益"""
    engine = FIFOEngine()
    # 卖出开仓 1 份，收到权利金 $5
    engine.sell(
        "lb", "AAPL_OPT_250221C150", 1, Decimal("5"),
        date(2025, 1, 1), short_allowed=True,
    )
    # 直接过期（不买回）
    results = engine.expire("lb", "AAPL_OPT_250221C150", 1, date(2025, 2, 21))
    assert len(results) == 1
    assert results[0]["gain_loss"] > 0
    assert results[0]["origin"] == "option_write_expire"


def test_option_buy_to_close_write_position():
    """买入平仓写仓期权：应实现权利金差价 = 收益/损失

    注意：当前 FIFO 引擎的 buy() 不减少 _short_positions，
    买回被视为新的多头建仓。此测试记录当前行为，
    实际平仓逻辑应在 importer 层或 tax_engine 层处理。
    """
    engine = FIFOEngine()
    # 卖出开仓 1 份 @ $5（write）
    engine.sell(
        "lb", "AAPL_OPT_250221C150", 1, Decimal("5"),
        date(2025, 1, 1), short_allowed=True,
    )
    # 买入平仓 1 份 @ $3（buy-to-close）
    engine.buy("lb", "AAPL_OPT_250221C150", 1, Decimal("3"), date(2025, 1, 15))

    # 当前行为：buy 创建新 lot，short position 未被减少
    # 买入后被当作多头持仓，后续过期时会消耗该 lot
    results = engine.expire("lb", "AAPL_OPT_250221C150", 1, date(2025, 2, 21))
    # 消耗的是买入的 lot（成本 $3），而非写仓过期
    assert len(results) == 1
    assert results[0]["cost_per_share"] == Decimal("3")
    assert results[0]["gain_loss"] < 0  # 买入后过期 = 损失
