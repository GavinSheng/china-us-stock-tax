"""数据库模块

SQLite 数据存储层，用于：
- 完整记录三家券商月结单交易数据
- RSU 授予与归属记录
- 分红事件与预扣税追踪
- FIFO 持仓批次管理
- 税务计算结果存储与审计
"""
from src.database.connection import init_db, get_connection, get_db
from src.database.schema import get_schema_sql
from src.database.repositories import (
    BrokerRepository,
    ExchangeRateRepository,
    StatementFileRepository,
    TransactionRepository,
    DividendRepository,
    RSUGrantRepository,
    RSUVestRepository,
    CashRewardRepository,
    TaxLotRepository,
    PositionRepository,
    TaxItemRepository,
    TaxSummaryRepository,
    ForeignTaxCreditCarryforwardRepository,
)

__all__ = [
    "init_db",
    "get_connection",
    "get_db",
    "get_schema_sql",
    "BrokerRepository",
    "ExchangeRateRepository",
    "StatementFileRepository",
    "TransactionRepository",
    "DividendRepository",
    "RSUGrantRepository",
    "RSUVestRepository",
    "CashRewardRepository",
    "TaxLotRepository",
    "PositionRepository",
    "TaxItemRepository",
    "TaxSummaryRepository",
    "ForeignTaxCreditCarryforwardRepository",
]
