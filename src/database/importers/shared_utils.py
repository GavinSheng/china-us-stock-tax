"""共享工具函数和常量

从原 import_statements.py 提取，供所有券商 importer 使用。
函数行为与原版本完全一致，仅文件路径计算有调整。
"""
from __future__ import annotations

import re
from datetime import datetime, date
from decimal import Decimal
from pathlib import Path

# ============================================================
# 路径常量
# ============================================================

# 注意：此文件位于 src/database/importers/，比原 src/database/ 深一级
INPUT_DIR = Path(__file__).parent.parent.parent.parent / "input"
DECRYPTED_DIR = INPUT_DIR / "decrypted"
DEFAULT_CURRENCY = "USD"

# ============================================================
# 密码常量（从 src.config 读取，支持 .env 文件配置）
# ============================================================

from src.config import LONGBRIDGE_PASSWORD, FUTU_2024_PASSWORD  # noqa: F401

# ============================================================
# 股票代码映射
# ============================================================

SYMBOL_NAME_MAP = {
    "BABA": "阿里巴巴",
    "AVGO": "博通",
    "GOOGL": "谷歌-C",
    "NVDA": "英伟达",
    "Oklo": "Oklo",
    "PDD": "拼多多",
    "TSM": "台积电",
    "SOXL": "半导体ETF-SOXL",
    "XPEV": "小鹏汽车",
    "YINN": "富时中国3倍做多ETF",
    "REDACTED_RSU_GRANT": "BABA RSU",
    "RKLB": "Rocket Lab",
    "TSLL": "2倍做多特斯拉ETF",
}

SYMBOL_EXCHANGE = {
    "AAPL": "NASDAQ", "GOOGL": "NASDAQ", "NVDA": "NASDAQ", "Oklo": "NASDAQ",
    "PDD": "NASDAQ", "RKLB": "NASDAQ", "TSLA": "NASDAQ", "META": "NASDAQ",
    "MU": "NASDAQ", "AMZN": "NASDAQ", "MSFT": "NASDAQ", "BILI": "NASDAQ",
    "OKLO": "NASDAQ",
    "BABA": "NYSE", "AVGO": "NYSE", "UNH": "NYSE", "TSM": "NYSE",
    "YINN": "NYSE",
    "SOXL": "NYSE",
    "TSLL": "NYSE",
    "COIN": "NASDAQ",
}

# 杠杆 ETF 列表（ROC 分红性质：先预扣 10%，后全额返还，净预扣 = $0）
LEVERAGED_ETFS = frozenset({"TSLL", "SOXL", "YINN"})


def is_leveraged_etf(symbol: str) -> bool:
    """判断是否为杠杆 ETF（ROC 分红性质）"""
    return symbol.upper() in LEVERAGED_ETFS


def _futu_div_rate(date_str: str) -> float:
    """获取 Futu 分红/利息的 USD/CNY 汇率"""
    try:
        d = date.fromisoformat(date_str)
        from src.calculator.exchange_rate import get_exchange_rate
        rate = get_exchange_rate(d, "USD")
        return float(rate)
    except Exception:
        from src.config import DEFAULT_EXCHANGE_RATE
        return float(DEFAULT_EXCHANGE_RATE)


def _normalize_option_underlying(underlying: str) -> str:
    """规范化期权标的符号：BABA1 → BABA 等。"""
    mapping = {
        "BABA1": "BABA",
    }
    return mapping.get(underlying, underlying)


def _infer_exchange(symbol: str) -> str:
    """自动推断交易所，未知默认 NYSE"""
    base = symbol.split("_OPT_")[0] if "_OPT_" in symbol else symbol
    return SYMBOL_EXCHANGE.get(base, "NYSE")


def _clean_text(text: str) -> str:
    """清理PDF解析后的控制字符（如 SOH 0x01）。"""
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', ' ', text)


def _normalize_strike(strike: str) -> str:
    """标准化期权行权价：15.00 → 15.0, 103.0 → 103.0。"""
    try:
        return str(float(strike))
    except (ValueError, TypeError):
        return strike


def _clean_num(s: str) -> str:
    """清理数字字符串: 去掉逗号、空格、括号负号"""
    if not s:
        return "0"
    s = s.strip().replace(",", "").replace(" ", "")
    m = re.match(r"\((.+?)\)", s)
    if m:
        return "-" + m.group(1)
    return s


def _dec(s: str) -> Decimal:
    return Decimal(_clean_num(s))


def _parse_date(s: str) -> date | None:
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _normalize_lb_symbol(raw_symbol: str) -> str:
    """规范化 Longbridge 股票符号：中文 → 英文代码。"""
    first_word = raw_symbol.split()[0]

    cn_to_en = {
        "拼多多": "PDD",
        "英伟达": "NVDA",
        "博通": "AVGO",
        "谷歌": "GOOGL",
        "⾕歌": "GOOGL",
        "台积电": "TSM",
        "富时中国": "YINN",
        "哔哩哔哩": "BILI",
        "阿里巴巴": "BABA",
        "阿⾥巴巴": "BABA",
        "美光科技": "MU",
        "美光": "MU",
        "特斯拉": "TSLA",
        "苹果": "AAPL",
        "康西哥": "CNC",
        "半导体": "SOXL",
        " Rocket": "RKLB",
        "联合健康": "UNH",
    }
    for cn, en in cn_to_en.items():
        if cn in raw_symbol:
            return en
    if first_word and first_word[0].isascii() and first_word[0].isalpha():
        if first_word.lower() == "oklo":
            return "OKLO"
        return first_word
    return first_word if first_word else raw_symbol


def _company_name(symbol: str) -> str:
    return SYMBOL_NAME_MAP.get(symbol, symbol)


def _dedup_text(text: str) -> str:
    """富途 PDF 中文字符重复问题：每个中文字符出现两次。"""
    result = []
    i = 0
    while i < len(text):
        if (
            i + 1 < len(text)
            and text[i] == text[i + 1]
            and 0x4E00 <= ord(text[i]) <= 0x9FFF
        ):
            result.append(text[i])
            i += 2
        else:
            result.append(text[i])
            i += 1
    return "".join(result)
