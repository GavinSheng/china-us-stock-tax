import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).parent.parent

DEFAULT_EXCHANGE_RATE = float(os.getenv("DEFAULT_EXCHANGE_RATE", "7.10"))

# 默认汇率文件路径（PBOC 每日中间价）
_default_rate_file = PROJECT_ROOT / "output" / "exchange_rates.csv"
EXCHANGE_RATE_FILE = os.getenv("EXCHANGE_RATE_FILE", str(_default_rate_file))

# ============================================================
# PDF 月结单密码（从环境变量或 .env 文件读取，不硬编码）
# ============================================================

# 长桥 PDF 密码
LONGBRIDGE_PASSWORD = os.getenv("LONGBRIDGE_PASSWORD", "")

# 富途 PDF 密码（2024 年及以前的月结单需要密码）
FUTU_2024_PASSWORD = os.getenv("FUTU_2024_PASSWORD", "")
