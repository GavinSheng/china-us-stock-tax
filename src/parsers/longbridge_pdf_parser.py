from __future__ import annotations
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import pdfplumber
from src.models import Transaction, Action
from src.parsers.base import BaseParser
from src.parsers.futu_pdf_parser import _detect_currency, _parse_date


class LongbridgePDFParser(BaseParser):
    """长桥月结单 PDF 解析器"""

    def parse(self, file_path: str | Path) -> list[Transaction]:
        txns = []
        path = Path(file_path)

        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                tables = page.extract_tables()
                for table in tables:
                    txns.extend(self._extract_transactions(table, path.stem))

        return txns

    def _extract_transactions(self, table: list, source: str) -> list[Transaction]:
        txns = []
        headers = table[0] if table else []
        col_map = self._map_columns(headers)
        if not col_map:
            return txns

        for row in table[1:]:
            try:
                txn = self._parse_row(row, col_map, source)
                if txn:
                    txns.append(txn)
            except (ValueError, IndexError):
                continue

        return txns

    def _map_columns(self, headers: list) -> dict | None:
        if not headers:
            return None

        col_map = {}
        for i, h in enumerate(headers):
            if not h:
                continue
            h_lower = str(h).lower().strip()

            if "date" in h_lower or "日期" in h_lower or "trade date" in h_lower:
                col_map["date"] = i
            elif "symbol" in h_lower or "代码" in h_lower or "ticker" in h_lower or "stock" in h_lower:
                col_map["symbol"] = i
            elif "quantity" in h_lower or "数量" in h_lower or "share" in h_lower or "qty" in h_lower:
                col_map["quantity"] = i
            elif "price" in h_lower or "价格" in h_lower or "avg price" in h_lower:
                col_map["price"] = i
            elif "amount" in h_lower or "金额" in h_lower or "total" in h_lower or "net" in h_lower:
                col_map["amount"] = i
            elif "action" in h_lower or "type" in h_lower or "类型" in h_lower or "方向" in h_lower or "side" in h_lower:
                col_map["action"] = i
            elif "fee" in h_lower or "手续费" in h_lower or "commission" in h_lower or "charge" in h_lower:
                col_map["fee"] = i
            elif "tax" in h_lower or "税" in h_lower or "withhold" in h_lower or " withholding" in h_lower:
                col_map["tax"] = i

        return col_map if "date" in col_map else None

    def _parse_row(self, row: list, col_map: dict, source: str) -> Transaction | None:
        def get_val(key):
            idx = col_map.get(key)
            if idx is None or idx >= len(row):
                return ""
            return str(row[idx]).strip() if row[idx] is not None else ""

        date_str = get_val("date")
        symbol = get_val("symbol")
        qty_str = get_val("quantity")
        price_str = get_val("price")
        amount_str = get_val("amount")
        action_str = get_val("action")
        fee_str = get_val("fee")
        tax_str = get_val("tax")

        if not date_str or not symbol:
            return None

        dt = _parse_date(date_str)
        if not dt:
            return None

        quantity = int(float(qty_str)) if qty_str else 0
        price = Decimal(price_str) if price_str else Decimal("0")
        amount = Decimal(amount_str) if amount_str else Decimal("0")
        fee = Decimal(fee_str) if fee_str else Decimal("0")
        tax = Decimal(tax_str) if tax_str else Decimal("0")

        action = self._classify_action(action_str, amount)
        if action is None:
            return None

        currency = _detect_currency(symbol)

        return Transaction(
            id=f"{source}_{dt.strftime('%Y-%m-%d')}_{symbol}_{action.value}",
            broker="longbridge",
            date=dt.date(),
            symbol=symbol,
            action=action,
            quantity=quantity,
            price=abs(price),
            amount=abs(amount),
            fee=abs(fee),
            tax_withheld=abs(tax),
            currency=currency,
        )

    def _classify_action(self, action_str: str, amount: Decimal) -> Action | None:
        s = action_str.lower().strip()

        if "vest" in s or "归属" in s or "行权" in s:
            return Action.RSU_VEST
        if "dividend" in s or "分红" in s or "股息" in s:
            return Action.DIVIDEND
        if "buy" in s or "买入" in s or "购买" in s:
            return Action.BUY
        if "sell" in s or "卖出" in s or "出售" in s:
            return Action.SELL

        if amount < 0:
            return Action.BUY
        elif amount > 0:
            return Action.SELL

        return None
