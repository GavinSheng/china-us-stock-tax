from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pydantic import BaseModel, Field

# 支持的币种
CURRENCY_MAP = {
    "USD": ("USD/CNY", 1),       # 直接查 USD/CNY
    "HKD": ("HKD/CNY", 1),       # 直接查 HKD/CNY
}


class Action(str, Enum):
    BUY = "buy"
    SELL = "sell"
    RSU_VEST = "rsu_vest"
    RSU_SELL = "rsu_sell"
    DIVIDEND = "dividend"
    FEE = "fee"
    OPTION_BUY = "option_buy"
    OPTION_SELL = "option_sell"
    OPTION_EXPIRE = "option_expire"
    OPTION_EXERCISE = "option_exercise"  # 期权行权：strike + 原始权利金 = 股票成本
    INTEREST = "interest"          # 债券利息、股票借贷收益等利息所得
    YIELD_INCOME = "yield_income"  # 投资收益/分红收益（BOCI 特有）


class Transaction(BaseModel):
    """单笔交易记录"""
    id: str
    broker: str                          # futu / longbridge
    date: date
    symbol: str
    action: Action
    quantity: int = Field(ge=0)
    price: Decimal = Field(ge=0)         # 单价（交易币种）
    amount: Decimal = Field(ge=0)        # 总金额（交易币种）
    fee: Decimal = Field(default=0)      # 手续费（交易币种）
    tax_withheld: Decimal = Field(default=0)  # 预扣税（交易币种）
    currency: str = Field(default="USD")  # 交易币种：USD / HKD
    exchange_rate: Decimal = Field(default=0)  # 交易币种 → CNY 汇率
    origin: str = Field(default="")      # 股份来源：buy / rsu_vest（用于审计追溯）

    @property
    def amount_cny(self) -> Decimal:
        rate = self.exchange_rate if self.exchange_rate > 0 else Decimal("0")
        return (self.amount * rate).quantize(Decimal("0.01"))

    @property
    def fee_cny(self) -> Decimal:
        rate = self.exchange_rate if self.exchange_rate > 0 else Decimal("0")
        return (self.fee * rate).quantize(Decimal("0.01"))

    @property
    def tax_withheld_cny(self) -> Decimal:
        rate = self.exchange_rate if self.exchange_rate > 0 else Decimal("0")
        return (self.tax_withheld * rate).quantize(Decimal("0.01"))
