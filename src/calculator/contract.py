from __future__ import annotations


class ContractSpec:
    """合约规格：封装合约乘数（策略模式）

    美股期权合约乘数为 100（1 份合约 = 100 股）
    股票合约乘数为 1

    FIFO 引擎根据 symbol 自动获取对应 ContractSpec，
    调用方无需关心乘数差异。
    """
    _registry: dict[str, "ContractSpec"] = {}
    _defaults_initialized: bool = False

    STOCK: "ContractSpec"  # type: ignore
    US_OPTION: "ContractSpec"  # type: ignore

    def __init__(self, multiplier: int):
        self.multiplier = multiplier

    @classmethod
    def _init_defaults(cls) -> None:
        if not cls._defaults_initialized:
            cls.STOCK = cls.__new__(cls)
            cls.STOCK.multiplier = 1
            cls.US_OPTION = cls.__new__(cls)
            cls.US_OPTION.multiplier = 100
            cls._defaults_initialized = True

    @classmethod
    def for_symbol(cls, symbol: str) -> "ContractSpec":
        cls._init_defaults()
        if symbol in cls._registry:
            return cls._registry[symbol]
        if "_OPT_" in symbol.upper():
            return cls.US_OPTION
        return cls.STOCK

    @classmethod
    def register(cls, symbol: str, spec: "ContractSpec") -> None:
        cls._registry[symbol] = spec
