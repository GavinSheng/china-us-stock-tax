from __future__ import annotations
from datetime import date
from decimal import Decimal
from collections import defaultdict
from src.models import TaxLot
from src.calculator.contract import ContractSpec


class FIFOEngine:
    """FIFO 成本核算引擎（按券商独立队列模式）

    架构设计：按券商独立 FIFO，年度汇总时跨券商对冲
    - FIFO 队列按 (broker_code, symbol) 隔离，各券商各自维护独立成本队列
    - 符合中国税法实务：财产转让所得按账户独立核算成本基础
    - 年度净额法汇总时，跨券商盈亏可合并对冲（财税〔2020〕4号）

    合约乘数（Strategy Pattern）：
    - 通过 ContractSpec 自动识别股票(multiplier=1)和期权(multiplier=100)
    - 所有金额计算统一使用 multiplier：amount = price × quantity × multiplier
    - importer 存入原始价格（PDF 上的每股权利金），无需手动 ×100

    跨年持仓：
    - 支持导入历史持仓（existing_lots），实现跨年度成本追溯
    - existing_lots 格式：dict[str, list[TaxLot]]，key 为 symbol
    - 每个 TaxLot 的 broker_code 决定其归属券商的 FIFO 队列

    期权写仓（sell-to-open）：
    - 无多头持仓时自动记录写仓成本
    - 写仓按 (broker_code, symbol) 独立跟踪
    """

    def __init__(self, existing_lots: dict[str, list[TaxLot]] | None = None):
        # FIFO 队列：按 (broker_code, symbol) 隔离
        # broker_code=None 的持仓归入 ("", symbol) 队列
        self._lots: dict[tuple[str, str], list[TaxLot]] = defaultdict(list)
        # 写仓空头：(broker_code, symbol) → [(premium, quantity)]
        self._short_positions: dict[tuple[str, str], list[tuple[Decimal, int]]] = defaultdict(list)
        if existing_lots:
            for key, lots in existing_lots.items():
                for lot in lots:
                    if lot.remaining > 0:
                        broker = lot.broker_code or ""
                        self._lots[(broker, key)].append(lot)

    @staticmethod
    def _key(broker_code: str | None, symbol: str) -> tuple[str, str]:
        return (broker_code or "", symbol)

    def buy(self, broker_code: str, symbol: str, quantity: int, cost_per_share: Decimal, acquire_date: date, origin: str = "buy"):
        """买入股票/期权，加入该券商的 FIFO 队列

        cost_per_share 为每股成本（期权 = PDF 上的每股权利金，无需 ×100）。
        FIFO 引擎通过 ContractSpec 自动处理合约乘数。
        broker_code 决定归属券商的独立 FIFO 队列。
        """
        spec = ContractSpec.for_symbol(symbol)
        lot = TaxLot(
            symbol=symbol,
            quantity=quantity,
            cost_per_share=cost_per_share,
            acquire_date=acquire_date,
            remaining=quantity,
            origin=origin,
            broker_code=broker_code,
        )
        self._lots[self._key(broker_code, symbol)].append(lot)

    def _calc(self, spec: ContractSpec, price: Decimal, quantity: int) -> Decimal:
        """统一金额计算：price × quantity × multiplier"""
        return price * Decimal(quantity) * Decimal(spec.multiplier)

    def expire(self, broker_code: str, symbol: str, quantity: int, expire_date: date) -> list[dict]:
        """期权过期作废：按 FIFO 顺序消耗该券商的批次，全部成本记为损失

        也支持写仓期权过期：无多头持仓时，视为写仓过期（权利金全额收益）。
        broker_code 决定从哪个券商的独立队列消耗。
        """
        spec = ContractSpec.for_symbol(symbol)
        results = []
        remaining = quantity
        key = self._key(broker_code, symbol)

        # 消耗该券商的多头持仓
        for lot in self._lots[key]:
            if remaining <= 0:
                break
            if lot.remaining <= 0:
                continue

            take = min(lot.remaining, remaining)
            lot.remaining -= take
            remaining -= take

            cost_basis = self._calc(spec, lot.cost_per_share, take)
            results.append({
                "symbol": symbol,
                "broker_code": lot.broker_code or broker_code,
                "quantity": take,
                "cost_per_share": lot.cost_per_share,
                "cost_basis": cost_basis.quantize(Decimal("0.0001")),
                "proceeds": Decimal("0"),
                "gain_loss": (-cost_basis).quantize(Decimal("0.0001")),
                "lot_date": lot.acquire_date,
                "sell_date": expire_date,
                "origin": lot.origin,
            })

        # 写仓空头过期 = 权利金全额收益
        shorts = self._short_positions.get(key, [])
        while remaining > 0 and shorts:
            premium, short_qty = shorts[0]
            take = min(short_qty, remaining)
            shorts[0] = (premium, short_qty - take)
            if shorts[0][1] <= 0:
                shorts.pop(0)
            remaining -= take

            proceeds = self._calc(spec, premium, take)
            results.append({
                "symbol": symbol,
                "broker_code": broker_code,
                "quantity": take,
                "cost_per_share": Decimal("0"),
                "cost_basis": Decimal("0"),
                "proceeds": proceeds.quantize(Decimal("0.0001")),
                "gain_loss": proceeds.quantize(Decimal("0.0001")),
                "lot_date": expire_date,
                "sell_date": expire_date,
                "origin": "option_write_expire",
            })

        # 无历史记录：优雅降级为 $0 成本损失（避免崩溃）
        if remaining > 0 and not shorts and not any(l.remaining > 0 for l in self._lots[key]):
            results.append({
                "symbol": symbol,
                "broker_code": broker_code,
                "quantity": remaining,
                "cost_per_share": Decimal("0"),
                "cost_basis": Decimal("0"),
                "proceeds": Decimal("0"),
                "gain_loss": Decimal("0"),
                "lot_date": expire_date,
                "sell_date": expire_date,
                "origin": "unknown_expire",
            })
            return results

        if remaining > 0:
            available = sum(l.remaining for l in self._lots[key])
            raise ValueError(
                f"期权过期失败：{broker_code}/{symbol} 需要 {quantity} 份，"
                f"可用 {available} 份"
            )

        return results

    def exercise(
        self, broker_code: str, stock_symbol: str, quantity: int, cost_per_share: Decimal, exercise_date: date,
    ):
        """期权行权：在该券商创建股票 lot

        cost_per_share = 行权价 + 原始每股权利金（符合 IRS Pub 550 及中国税法要求）。
        CSV 路径要求 txn.price 已包含 strike + premium。
        数据库导入路径已在 import_statements.py 中正确计算后创建 lot。
        broker_code 决定归属券商的独立 FIFO 队列。
        """
        lot = TaxLot(
            symbol=stock_symbol,
            quantity=quantity,
            cost_per_share=cost_per_share,
            acquire_date=exercise_date,
            remaining=quantity,
            origin="option_exercise",
            broker_code=broker_code,
        )
        self._lots[self._key(broker_code, stock_symbol)].append(lot)

    def sell(
        self, broker_code: str, symbol: str, quantity: int, sell_price: Decimal, sell_date: date,
        fee: Decimal = Decimal("0"), short_allowed: bool = False,
    ) -> list[dict]:
        """卖出股票/期权，按该券商的 FIFO 队列出队计算成本

        按券商独立：只从卖出方券商的队列中按 FIFO 顺序消耗，
        不跨券商匹配成本批次。年度汇总时跨券商盈亏合并对冲。

        无多头持仓时：
        - short_allowed=True：视为写仓（sell-to-open），记录写仓价格
        - short_allowed=False：抛出异常（合规模式，不允许无买入即卖出）

        broker_code 为卖出方券商，决定从哪个券商的独立队列消耗。
        """
        spec = ContractSpec.for_symbol(symbol)
        results = []
        remaining = quantity
        key = self._key(broker_code, symbol)

        # 预检：卖出前检查可用持仓，不足则直接报错（避免部分消耗不回滚）
        if not short_allowed:
            available = sum(l.remaining for l in self._lots[key])
            total_bought = sum(l.quantity for l in self._lots[key])
            if quantity > available:
                raise ValueError(
                    f"卖出数量超出持仓：{broker_code}/{symbol} 卖出 {quantity} 股，"
                    f"累计买入 {total_bought} 股，当前剩余 {available} 股。"
                    f"缺少 {quantity - available} 股的买入记录，请检查月结单数据完整性。"
                )

        # 消耗该券商的多头持仓（已通过预检，不会中途失败）
        for lot in self._lots[key]:
            if remaining <= 0:
                break
            if lot.remaining <= 0:
                continue

            take = min(lot.remaining, remaining)
            lot.remaining -= take
            remaining -= take

            cost_basis = self._calc(spec, lot.cost_per_share, take)
            proceeds = self._calc(spec, sell_price, take)
            fee_alloc = fee * Decimal(take) / Decimal(quantity)
            gain_loss = proceeds - cost_basis - fee_alloc

            results.append({
                "symbol": symbol,
                "broker_code": lot.broker_code or broker_code,
                "sell_broker_code": broker_code,
                "quantity": take,
                "cost_per_share": lot.cost_per_share,
                "cost_basis": cost_basis.quantize(Decimal("0.0001")),
                "sell_price": sell_price,
                "proceeds": proceeds.quantize(Decimal("0.0001")),
                "gain_loss": gain_loss.quantize(Decimal("0.0001")),
                "lot_date": lot.acquire_date,
                "sell_date": sell_date,
                "origin": lot.origin,
            })

        # 剩余 = 写仓（sell-to-open）
        if remaining > 0:
            if not short_allowed:
                # 不应到达这里（预检已通过）
                raise AssertionError(f"预检失败：{broker_code}/{symbol} 持仓不足")

            proceeds = self._calc(spec, sell_price, remaining)
            fee_alloc = fee * Decimal(remaining) / Decimal(quantity)
            gain_loss = proceeds - fee_alloc

            self._short_positions[key].append((sell_price, remaining))

            results.append({
                "symbol": symbol,
                "broker_code": broker_code,
                "sell_broker_code": broker_code,
                "quantity": remaining,
                "cost_per_share": Decimal("0"),
                "cost_basis": Decimal("0"),
                "sell_price": sell_price,
                "proceeds": proceeds.quantize(Decimal("0.0001")),
                "gain_loss": gain_loss.quantize(Decimal("0.0001")),
                "lot_date": sell_date,
                "sell_date": sell_date,
                "origin": "option_write",
            })

        return results

    def get_holdings(self) -> dict[str, int]:
        """获取按 symbol 汇总的持仓数量（跨券商聚合，仅用于展示）"""
        holdings: dict[str, int] = defaultdict(int)
        for (broker, symbol), lots in self._lots.items():
            holdings[symbol] += sum(l.remaining for l in lots if l.remaining > 0)
        return dict(holdings)

    def get_remaining_lots(self) -> dict[str, list[TaxLot]]:
        """获取剩余持仓，key 为 symbol（跨券商聚合）

        每个 TaxLot 上的 broker_code 字段记录该批次归属的券商（审计追踪）。
        返回时按 symbol 聚合，但各 lot 保留独立的 broker_code。
        """
        remaining: dict[str, list[TaxLot]] = defaultdict(list)
        for (broker, symbol), lots in self._lots.items():
            for lot in lots:
                if lot.remaining > 0:
                    remaining[symbol].append(lot)
        return dict(remaining)
