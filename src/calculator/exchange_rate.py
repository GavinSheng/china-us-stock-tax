from __future__ import annotations
from datetime import date
from decimal import Decimal
import os
import pandas as pd
from src.config import DEFAULT_EXCHANGE_RATE, EXCHANGE_RATE_FILE

# 缓存加载的汇率
# {currency_pair: {date: rate}}
# currency_pair = "USD/CNY" | "HKD/CNY"
_rate_cache: dict[str, dict[date, Decimal]] = {}


def load_exchange_rates(file_path: str | None = None) -> dict[str, dict[date, Decimal]]:
    """从 CSV 文件加载多币种汇率

    CSV 格式：date,base_currency,rate（如 2025-01-15,USD,7.2456 或 2025-01-15,HKD,0.9123）
    也可兼容两列格式：date,rate（默认 USD/CNY）
    """
    fp = file_path or EXCHANGE_RATE_FILE
    if not fp or not os.path.exists(fp):
        return _rate_cache

    df = pd.read_csv(fp)
    for _, row in df.iterrows():
        d = pd.to_datetime(row["date"]).date()
        # 三列格式：date,base_currency,rate
        if "base_currency" in row.index and pd.notna(row["base_currency"]):
            base = str(row["base_currency"]).strip().upper()
            pair = f"{base}/CNY"
            rate = Decimal(str(row["rate"]))
        else:
            # 两列格式：date,rate，默认 USD/CNY
            pair = "USD/CNY"
            rate = Decimal(str(row["rate"]))

        if pair not in _rate_cache:
            _rate_cache[pair] = {}
        _rate_cache[pair][d] = rate

    return _rate_cache


def get_exchange_rate(trade_date: date, currency: str = "USD", file_path: str | None = None, year: int | None = None) -> Decimal:
    """获取指定日期的汇率（交易币种 → CNY）

    年度汇算清缴场景（year 已提供）：
        优先使用纳税年度最后一日（12月31日）汇率中间价。
        依据《个人所得税法实施条例》第三十二条：
        "年度终了后办理汇算清缴的，……对应当补缴税款的所得部分，
        按照上一纳税年度最后一日人民币汇率中间价，折合成人民币计算。"
        境外所得（资本利得、分红 gross）属于"应补缴税款部分"，适用年末汇率。

    非年度汇算场景（year 未提供）：
        优先级：精确匹配 → 7 日内最近 → 默认汇率。

    回退链：年末汇率 → 精确日期 → 7 日窗口 → 默认 7.10
    """
    pair = f"{currency.upper()}/CNY"
    if not _rate_cache:
        load_exchange_rates(file_path)

    pair_rates = _rate_cache.get(pair, {})

    # ── 年度汇算优先使用年末汇率（实施条例第三十二条）──
    if year:
        year_end_rate = _find_year_end_rate(pair_rates, year)
        if year_end_rate is not None:
            return year_end_rate

    # ── 非年度场景：精确匹配 ──
    if trade_date in pair_rates:
        return pair_rates[trade_date]

    # 找最近汇率（7 天窗口）
    closest = None
    min_diff = float("inf")
    for d, rate in pair_rates.items():
        diff = abs((d - trade_date).days)
        if diff < min_diff:
            min_diff = diff
            closest = rate

    if closest and min_diff <= 7:
        return closest

    # ── 回退到默认汇率 ──
    default = Decimal(str(DEFAULT_EXCHANGE_RATE))
    if currency.upper() == "HKD":
        # HKD 默认按 USD/CNY / 7.8 近似
        default = (default / Decimal("7.8")).quantize(Decimal("0.0001"))

    return default


def check_year_end_rate(currency: str, year: int) -> tuple[bool, Decimal | None, date | None]:
    """检查指定年度年末汇率是否存在。

    依据《个人所得税法实施条例》第三十二条，年度汇算清缴需使用
    纳税年度最后一日（12月31日）人民币汇率中间价。

    Returns:
        (found, rate, actual_date):
        - found=True  ：在 12月31日或回退10天内找到有效汇率
        - found=False ：未找到，需要用户手动输入
    """
    pair = f"{currency.upper()}/CNY"
    if not _rate_cache:
        load_exchange_rates()

    pair_rates = _rate_cache.get(pair, {})
    year_end = date(year, 12, 31)

    if year_end in pair_rates:
        return True, pair_rates[year_end], year_end

    # 向前查找最近一个有效工作日（最多回退 10 天）
    for day in range(year_end.day - 1, max(year_end.day - 11, 0), -1):
        fallback = date(year, 12, day)
        if fallback in pair_rates:
            return True, pair_rates[fallback], fallback

    return False, None, None


def register_year_end_rate(currency: str, rate: Decimal, year: int):
    """手动注册年末汇率到缓存（用于用户手动输入场景）。"""
    pair = f"{currency.upper()}/CNY"
    if pair not in _rate_cache:
        _rate_cache[pair] = {}
    _rate_cache[pair][date(year, 12, 31)] = rate


def _find_year_end_rate(pair_rates: dict[date, Decimal], year: int) -> Decimal | None:
    """查找纳税年度最后一日的汇率。

    优先 12月31日；若该日为周末/假日无数据，向前查找最近可用日期。
    """
    year_end = date(year, 12, 31)
    if year_end in pair_rates:
        return pair_rates[year_end]
    # 12-31 是周末/假日，向前查找最近一个有效工作日的汇率中间价
    # 依据：实施条例第三十二条，年末汇率取"上一纳税年度最后一日"人民币汇率中间价
    # 央行非交易日不发布数据，应顺延至节前最后一个工作日
    for day in range(year_end.day - 1, max(year_end.day - 11, 0), -1):
        fallback_date = date(year, 12, day)
        if fallback_date in pair_rates:
            return pair_rates[fallback_date]
    return None
