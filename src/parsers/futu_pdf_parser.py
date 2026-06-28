from __future__ import annotations
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import pdfplumber
from src.models import Transaction, Action
from src.parsers.base import BaseParser


# ============================================================
# 共享工具函数（被 LongbridgePDFParser 导入）
# ============================================================

def _detect_currency(symbol: str) -> str:
    """从股票代码判断币种"""
    symbol_upper = symbol.upper()
    if ".HK" in symbol_upper or symbol_upper.startswith("0") and len(symbol_upper) == 5:
        return "HKD"
    if ".US" in symbol_upper or ".O" in symbol_upper:
        return "USD"
    return "USD"


def _parse_date(date_str: str) -> datetime | None:
    """尝试多种日期格式解析"""
    s = date_str.strip()[:10]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


# 常见美股交易所代码
US_EXCHANGES = {
    'XNAS', 'XNYS', 'ARCX', 'EDGX', 'IEXG', 'BATS', 'MEMX',
    'EDGA', 'BYSS', 'LTSE', 'MIAX', 'PEARL', 'EMERALD',
    'JPMX', 'JNST', 'BARX', 'VFMI', 'CDED', 'LEVL',
    'UBSA', 'INCR', 'SGMT', 'KNEM', 'EPRL', 'XISX',
}

# 非交易表格模式（持仓摘要等）
NON_TXN_PATTERNS = [
    r'^綜合賬戶月結單',
    r'^保證金',
    r'^維維持持',
    r'^初初始始',
    r'^可再開倉',
    r'^資産組合',
    r'^現金結餘',
    r'^資産淨値',
    r'^期初',
    r'^期末',
    r'^參考匯率',
    r'^製備日期',
]

DATE_RE = re.compile(r'(\d{4}/\d{2}/\d{2})')


def _clean_number(s: str) -> str:
    """移除千分位逗号，供 Decimal 解析"""
    return s.replace(',', '').strip()


def _parse_single_cell_txn(cell: str, source: str) -> list[Transaction]:
    """解析单单元格交易数据（支持股票和期权）

    股票格式：交易所 币种 交易日 交收日 数量 价格 金额 净额\n时间
    期权格式：期权代码 交易所 币种 交易日 交收日 数量 价格 金额 净额\n...
    """
    txns = []
    for line in str(cell).split('\n'):
        line = line.strip()
        if not line:
            continue
        m = DATE_RE.search(line)
        if not m:
            continue

        parts = line.split()

        # 判断格式：parts[2] 是日期 → 股票(8字段)；parts[3] 是日期 → 期权(9+字段)
        date_idx = None
        if len(parts) >= 8 and re.match(r'^\d{4}/\d{2}/\d{2}$', parts[2]):
            date_idx = 2
        elif len(parts) >= 9 and re.match(r'^\d{4}/\d{2}/\d{2}$', parts[3]):
            date_idx = 3
        else:
            continue

        exchange = parts[date_idx - 2]
        currency = parts[date_idx - 1]
        trade_date_str = parts[date_idx]
        # parts[date_idx + 1] = settlement date
        qty_str = parts[date_idx + 2]
        price_str = parts[date_idx + 3]
        amount_str = parts[date_idx + 4]
        net_str = parts[date_idx + 5]

        # 解析日期
        try:
            trade_date = datetime.strptime(trade_date_str, '%Y/%m/%d').date()
        except ValueError:
            continue

        # 解析数值
        try:
            quantity = int(_clean_number(qty_str))
            price = Decimal(_clean_number(price_str))
            amount = Decimal(_clean_number(amount_str))
            net_amount = Decimal(_clean_number(net_str))
        except (ValueError, Exception):
            continue

        # 买卖方向：净额为负 = 买入，为正 = 卖出
        if net_amount < 0:
            action = Action.BUY
        else:
            action = Action.SELL

        # 手续费
        if action == Action.BUY:
            fee = abs(net_amount) - abs(amount)
        else:
            fee = abs(amount) - abs(net_amount)
        fee = abs(fee)

        # 币种
        if currency == 'CNH':
            currency = 'CNY'
        elif currency not in ('USD', 'HKD', 'CNY', 'EUR', 'GBP', 'JPY', 'SGD'):
            currency = 'USD'

        txns.append(Transaction(
            id=f"{source}_{trade_date}_{exchange}_{quantity}_{amount}",
            broker="futu",
            date=trade_date,
            symbol=exchange,
            action=action,
            quantity=quantity,
            price=abs(price),
            amount=abs(amount),
            fee=fee,
            currency=currency,
        ))

    return txns


def _parse_interest_table(row: list, source: str) -> Transaction | None:
    """解析利息收入表格（6列多列表格，每行是一条利息记录）

    格式：[日期, 币种, 金额, 利率%, 利息, 累计利息]
    示例：['2025/11/02', 'USD', '14,689.03', '4.80%', '1.96', '3.92']
    """
    if len(row) < 6:
        return None

    date_str = str(row[0]).strip()
    currency = str(row[1]).strip()
    interest_str = str(row[4]).strip().replace(',', '')

    try:
        dt = datetime.strptime(date_str, '%Y/%m/%d').date()
        interest = Decimal(interest_str)
    except (ValueError, Exception):
        return None

    if currency == 'CNH':
        currency = 'CNY'

    return Transaction(
        id=f"{source}_{dt}_interest_{interest}",
        broker="futu",
        date=dt,
        symbol="INTEREST",
        action=Action.INTEREST,
        quantity=1,
        price=interest,
        amount=interest,
        fee=Decimal("0"),
        currency=currency,
    )


def _parse_distribution_table(row: list, source: str) -> Transaction | None:
    """解析 ETF 分配表格（12列多列表格）

    格式：[日期, 证券名, 市场, 币种, '分配', 数量, 分配金额, 税前总额, ...]
    示例：['2025/11/01', 'TSLL(...)', 'US', 'USD', '分配', '70.00', '1,507.80', '1,470.00', ...]
    """
    if len(row) < 8:
        return None

    date_str = str(row[0]).strip()
    # Clean date - handle wrapped text like "2025/11/27 T\nD"
    date_str = re.split(r'\s', date_str)[0].strip()

    symbol = str(row[1]).strip().replace('\n', '')
    currency = str(row[3]).strip()
    action_type = str(row[4]).strip()
    amount_str = str(row[6]).strip().replace(',', '')

    if action_type != '分配':
        return None

    try:
        dt = datetime.strptime(date_str, '%Y/%m/%d').date()
        amount = Decimal(amount_str)
    except (ValueError, Exception):
        return None

    if currency == 'CNH':
        currency = 'CNY'

    return Transaction(
        id=f"{source}_{dt}_{symbol}_dist_{amount}",
        broker="futu",
        date=dt,
        symbol=symbol[:20],  # Truncate long names
        action=Action.DIVIDEND,
        quantity=0,
        price=amount,
        amount=amount,
        fee=Decimal("0"),
        currency=currency,
    )


def _is_interest_table(headers: list) -> bool:
    """判断是否是利息收入表格"""
    if len(headers) != 6:
        return False
    # 利息表第3列包含利率% (如 "4.80%")
    h3 = str(headers[3]).strip() if len(headers) > 3 else ''
    return '%' in h3


def _is_distribution_table(headers: list) -> bool:
    """判断是否是 ETF 分配表格"""
    if len(headers) < 8:
        return False
    # 分配表第5列包含"分配"
    h5 = str(headers[4]).strip() if len(headers) > 4 else ''
    return '分配' in h5


def _is_single_cell_txn_table(headers: list) -> bool:
    """判断是否是单单元格交易明细表"""
    if len(headers) != 1 or not headers[0]:
        return False
    h = str(headers[0]).strip()
    # 排除非交易内容
    for pattern in NON_TXN_PATTERNS:
        if re.search(pattern, h):
            return False
    # 包含日期格式的视为交易表
    return bool(DATE_RE.search(h))


class FutuPDFParser(BaseParser):
    """富途月结单 PDF 解析器

    处理四种表格格式：
    1. 单单元格交易表：股票/期权买卖
    2. 6列利息表：利息收入
    3. 12列分配表：ETF 分配（作为股息处理）
    4. 其他：跳过
    """

    def parse(self, file_path: str | Path) -> list[Transaction]:
        txns = []
        path = Path(file_path)

        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                tables = page.extract_tables()
                for table in tables:
                    if not table or not table[0]:
                        continue

                    headers = table[0]

                    # 1) 单单元格交易表
                    if _is_single_cell_txn_table(headers):
                        for row in table:
                            if not row or not row[0]:
                                continue
                            txns.extend(_parse_single_cell_txn(row[0], path.stem))

                    # 2) 利息收入表（6列）
                    elif _is_interest_table(headers):
                        for row in table:
                            txn = _parse_interest_table(row, path.stem)
                            if txn:
                                txns.append(txn)

                    # 3) ETF 分配表（12列）
                    elif _is_distribution_table(headers):
                        for row in table:
                            txn = _parse_distribution_table(row, path.stem)
                            if txn:
                                txns.append(txn)

        return txns
