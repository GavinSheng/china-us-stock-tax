"""汇率逻辑测试"""

from datetime import date
from decimal import Decimal
from src.calculator.exchange_rate import get_exchange_rate, _rate_cache, load_exchange_rates
import io
import csv
import os


def _setup_test_rates():
    """注入测试汇率到缓存，避免依赖外部 CSV 文件"""
    from src.calculator import exchange_rate
    exchange_rate._rate_cache = {
        "USD/CNY": {
            date(2025, 3, 15): Decimal("7.20"),   # 交易日精确匹配
            date(2025, 3, 16): Decimal("7.21"),   # 7 日窗口
            date(2025, 12, 31): Decimal("7.30"),  # 年末汇率
        },
        "HKD/CNY": {
            date(2025, 12, 31): Decimal("0.93"),  # 年末汇率
        },
    }


def test_year_end_rate_prioritized_for_annual():
    """年度汇算清缴场景：年末汇率优先于交易日汇率。

    依据：《实施条例》第三十二条，应补缴税款部分按年末汇率折合。
    """
    _setup_test_rates()
    # 3月15日有精确汇率 7.20，但年度汇算应优先使用年末 7.30
    rate = get_exchange_rate(date(2025, 3, 15), "USD", year=2025)
    assert rate == Decimal("7.30"), f"年末汇率应优先，实际: {rate}"


def test_no_year_falls_back_to_exact_match():
    """非年度汇算场景：无 year 参数时使用交易日精确汇率"""
    _setup_test_rates()
    rate = get_exchange_rate(date(2025, 3, 15), "USD", year=None)
    assert rate == Decimal("7.20"), f"应精确匹配，实际: {rate}"


def test_year_end_rate_weekend_fallback():
    """12月31日为周末时，向前查找最近可用汇率"""
    from src.calculator import exchange_rate
    # 2025-12-31 是周三（有数据），模拟无数据场景
    exchange_rate._rate_cache = {
        "USD/CNY": {
            date(2025, 12, 29): Decimal("7.29"),  # 周一，最近的可用日期
            # 12-30, 12-31 无数据
        },
    }
    rate = get_exchange_rate(date(2025, 6, 15), "USD", year=2025)
    assert rate == Decimal("7.29"), f"应回退到 12-29 汇率，实际: {rate}"


def test_default_rate_when_no_data():
    """无任何汇率数据时使用默认汇率"""
    from src.calculator import exchange_rate
    from unittest.mock import patch
    with patch.object(exchange_rate, 'load_exchange_rates', return_value={}):
        exchange_rate._rate_cache = {}
        rate = get_exchange_rate(date(2025, 6, 15), "USD", year=2025)
        assert rate == Decimal("7.10"), f"应使用默认 7.10，实际: {rate}"


def test_hkd_default_rate():
    """HKD 无数据时按 USD/7.8 近似"""
    from src.calculator import exchange_rate
    from unittest.mock import patch
    with patch.object(exchange_rate, 'load_exchange_rates', return_value={}):
        exchange_rate._rate_cache = {}
        rate = get_exchange_rate(date(2025, 6, 15), "HKD", year=2025)
        expected = (Decimal("7.10") / Decimal("7.8")).quantize(Decimal("0.0001"))
        assert rate == expected, f"HKD 默认汇率不正确，实际: {rate}"
