from datetime import date
from decimal import Decimal
from typing import Optional
from pydantic import BaseModel


class TaxLot(BaseModel):
    """持仓批次，用于 FIFO 成本核算"""
    symbol: str
    quantity: int                        # 原始股数
    cost_per_share: Decimal              # 每股成本（交易币种）
    acquire_date: date                   # 取得日期
    remaining: Optional[int] = None      # 当前剩余股数（mutable，加载时保留原值）
    origin: str = "buy"                  # 来源：buy / rsu_vest / carryforward
    broker_code: Optional[str] = None    # 所属券商（longbridge / futu / boci），用于按券商隔离 FIFO

    def model_post_init(self, __context):
        # 只有未显式指定 remaining 时，才初始化为 quantity
        if self.remaining is None:
            self.remaining = self.quantity

    @property
    def total_cost(self) -> Decimal:
        return (self.cost_per_share * self.remaining).quantize(Decimal("0.0001"))
