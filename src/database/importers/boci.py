"""BOCI 中银国际月结单导入"""
from __future__ import annotations

import re
import warnings
from datetime import date
from decimal import Decimal
from pathlib import Path

from .shared_utils import (
    DECRYPTED_DIR, _dec, _company_name, SYMBOL_NAME_MAP,
)
from .base import BaseImporter


class BOCIImporter(BaseImporter):
    """BOCI 月结单解析

    结构特点:
    - 全文本提取（非表格）
    - Page 1: 交易总览（款项进支）
    - Page 2: RSU 摘要 + 利息 + 户口总览
    - Page 3+: 备注
    """

    broker_code = "boci"
    PDF_GLOB = "boci_*.pdf"

    _GENERIC_WORDS = frozenset({
        "ADR", "US", "USD", "HKD", "CNY", "ISSUANCE", "FEE", "COLLECTION",
        "CHARGE", "CHARGES", "DIV", "GROUP", "HOLDING", "LTD", "CORP", "INC",
        "SHARE", "SHR", "P/D", "R/D",
    })

    def _display_name(self) -> str:
        return "BOCI 中银国际"

    def _extract_month(self, pdf_file: Path) -> str:
        # 文件名格式: boci_YYYYMMDD_... 或 YYYYMMDD_...
        parts = pdf_file.stem.split("_")
        fname = parts[1] if parts[0] == "boci" and len(parts) > 1 else parts[0]
        return f"{fname[:4]}-{fname[4:6]}"

    def _pdf_password(self, month_str: str) -> str | None:
        return None

    def _preprocess_text(self, text: str) -> str:
        return text

    def _import_notes(self) -> str:
        return "BOCI - RSU托管 + BABA持仓"

    def _parse_all(self, full_text: str, file_id: int, month_str: str) -> None:
        self._parse_transactions(full_text, file_id, month_str)
        self._parse_dividends(full_text, file_id, month_str)
        self._parse_rsu_transfers(full_text, file_id, month_str)
        self._parse_positions(full_text, file_id, month_str)

    def _count_positions(self, file_id: int) -> int:
        from src.database.connection import get_connection
        conn = get_connection(self.db_path)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM positions WHERE statement_file_id = ?",
                (file_id,)
            ).fetchone()
            return row[0]
        finally:
            conn.close()

    def _parse_transactions(self, text: str, file_id: int, month_str: str):
        lines = text.split("\n")
        year = month_str.split("-")[0]

        i = 0
        while i < len(lines):
            line = lines[i]
            m = re.match(
                r"(\d{2}/\d{2})\s+(\d{2}/\d{2})\s+(\d{6,})\s+进\s*/\s*支\s*:\s*(.*)",
                line
            )
            if m:
                trade_date_str = m.group(1)
                settle_date_str = m.group(2)
                ref_no = m.group(3)
                detail = m.group(4)

                full_detail = detail
                continuation_lines = []
                j = i + 1
                while j < len(lines):
                    next_line = lines[j].strip()
                    if re.match(r"\d{2}/\d{2}\s+\d{2}/\d{2}\s+\d{6,}", next_line):
                        break
                    if re.search(r"转后结余|承前结余", next_line):
                        break
                    continuation_lines.append(next_line)
                    j += 1

                if continuation_lines:
                    full_detail = detail + "\n" + "\n".join(continuation_lines)

                amount = Decimal("0")
                clean_detail = detail

                balance_match = re.search(r"\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s*$", detail)
                has_balance = bool(balance_match)

                if has_balance:
                    middle = detail[:balance_match.start()]
                    fee_paren = re.search(r"\(([\d.]+)\)\s*$", middle)
                    if fee_paren:
                        amount = _dec(fee_paren.group(1))
                        clean_detail = re.sub(r"\s*\([\d.]+\)\s*$", "", middle)
                    else:
                        gross_match = re.search(r"R/D\s+([\d,]+\.?\d*)", middle)
                        if gross_match:
                            amount = _dec(gross_match.group(1))
                        else:
                            last_amt = re.findall(r"([\d,]+\.\d{2})", middle)
                            if last_amt:
                                amount = _dec(last_amt[-1])
                        clean_detail = middle
                else:
                    fee_paren = re.search(r"\(([\d.]+)\)", detail)
                    if fee_paren:
                        amount = _dec(fee_paren.group(1))
                        clean_detail = re.sub(r"\s*\([\d.]+\)", "", detail)

                trade_date = self._resolve_date(trade_date_str, month_str)
                settle_date = self._resolve_date(settle_date_str, month_str)

                classify_text = clean_detail
                if continuation_lines:
                    classify_text = clean_detail + "\n" + "\n".join(continuation_lines)
                    if "股息" in classify_text or "dividend" in classify_text.lower():
                        for cl in continuation_lines:
                            solo_amt = re.match(r"^\s*([\d,]+\.\d{2})\s*$", cl)
                            if solo_amt:
                                candidate = _dec(solo_amt.group(1))
                                if candidate > amount:
                                    amount = candidate
                action, symbol, company = self._classify_boci_detail(classify_text, text)

                if action == "dividend":
                    i += 1
                    continue

                if action and amount != 0:
                    dividend_qty = None
                    dividend_price = None
                    if action == "dividend":
                        per_share_match = re.search(r"美金([\d.]+)/SHR", classify_text)
                        if per_share_match:
                            dividend_price = float(per_share_match.group(1))
                            if dividend_price > 0:
                                dividend_qty = int(round(float(abs(amount)) / dividend_price))

                    self.txn_repo.insert(
                        broker_code="boci",
                        trade_date=trade_date.isoformat() if trade_date else "",
                        settlement_date=settle_date.isoformat() if settle_date else None,
                        reference_no=ref_no,
                        symbol=symbol,
                        company_name=company,
                        exchange="NYSE" if symbol == "BABA" else ("HKEX" if "9988" in symbol else None),
                        action=action,
                        quantity=dividend_qty,
                        price=dividend_price,
                        amount=float(abs(amount)),
                        currency="USD",
                        statement_file_id=file_id,
                    )

                    if action == "buy" and trade_date:
                        buy_qty = None
                        buy_price = None
                        qty_match = re.search(r"(\d+)\s*股", classify_text)
                        price_match = re.search(r"@\s*([\d.]+)", classify_text)
                        if qty_match:
                            buy_qty = int(qty_match.group(1))
                        if price_match:
                            buy_price = float(price_match.group(1))

                        if buy_qty and buy_price and buy_price > 0:
                            self.tax_lot_repo.add_lot(
                                symbol=symbol,
                                acquisition_date=trade_date.isoformat(),
                                acquisition_type="buy",
                                quantity=buy_qty,
                                cost_per_share=round(buy_price, 6),
                                currency="USD",
                                broker_code="boci",
                            )
                        elif buy_qty and amount > 0:
                            cost_per_share = float(amount) / buy_qty
                            self.tax_lot_repo.add_lot(
                                symbol=symbol,
                                acquisition_date=trade_date.isoformat(),
                                acquisition_type="buy",
                                quantity=buy_qty,
                                cost_per_share=round(cost_per_share, 6),
                                currency="USD",
                                broker_code="boci",
                            )
                        else:
                            warnings.warn(
                                f"BOCI 买入 {symbol} {trade_date}: 无法提取数量/价格，"
                                f"未创建 tax_lot。后续卖出将触发 FIFO 缺口",
                                stacklevel=2,
                            )

            i += 1

    def _parse_dividends(self, text: str, file_id: int, month_str: str):
        lines = text.split("\n")

        for i, line in enumerate(lines):
            m = re.match(r"\d{2}/\d{2}\s+\d{2}/\d{2}\s+(\d{6,})\s+进\s*/\s*支\s*:\s*(.*)", line)
            if not m:
                continue
            ref_no = m.group(1)
            header_detail = m.group(2)

            # 收集延续行
            detail_lines = []
            next_txn_line = None  # 记录下一个交易行（可能是 ADR fee 等独立交易）
            j = i + 1
            while j < len(lines):
                nl = lines[j].strip()
                if re.match(r"\d{2}/\d{2}\s+\d{2}/\d{2}\s+\d{6,}", nl):
                    # 下一个交易行：如果是 ADR fee 或 collection charge，关联到当前分红
                    if "ADR ISSUANCE FEE" in nl or "ADR FEE" in nl or \
                       "DIV COLLECTION CHARGES" in nl or "DIVIDEND COLLECTION CHARGE" in nl or \
                       "COLLECTION CHARGE" in nl:
                        next_txn_line = nl
                    break
                if re.search(r"转后结余|承前结余", nl):
                    break
                detail_lines.append(nl)
                j += 1

            full_text = header_detail + "\n" + "\n".join(detail_lines)
            if "股息" not in full_text and "dividend" not in full_text.lower():
                continue

            per_share_match = re.search(r"美金([\d.]+)/SHR", full_text)
            per_share = float(per_share_match.group(1)) if per_share_match else 0

            gross = 0.0
            rd_match = re.search(r"R/D\s+(\S+)", full_text)
            if rd_match:
                rd_val = rd_match.group(1)
                if not re.match(r"\d{2}-\d{2}-\d{4}", rd_val):
                    amt_match = re.match(r"([\d,]+\.\d{2})$", rd_val)
                    if amt_match:
                        gross = float(_dec(amt_match.group(1)))

            if gross == 0:
                for dl in detail_lines:
                    solo = re.match(r"^\s*([\d,]+\.\d{2})\s*$", dl)
                    if solo:
                        gross = float(_dec(solo.group(1)))
                        break

            symbol_match = re.search(r"FOR\s+(\w+)", full_text)
            if not symbol_match:
                sym_match = re.search(r"\b(BABA|9988\.HK)\b", full_text)
                symbol = sym_match.group(1) if sym_match else "BABA"
            else:
                symbol = symbol_match.group(1)

            shares = int(gross / per_share) if per_share > 0 and gross > 0 else 0

            collection_fee = 0.0
            adr_fee = 0.0
            # 从延续行中提取费用
            fee_lines = detail_lines + ([next_txn_line] if next_txn_line else [])
            for dl in fee_lines:
                if "ADR ISSUANCE FEE" in dl or "ADR FEE" in dl:
                    fee_match = re.search(r"\(([\d.]+)\)", dl)
                    if fee_match:
                        adr_fee += float(_dec(fee_match.group(1)))
                if "DIV COLLECTION CHARGES" in dl or "DIVIDEND COLLECTION CHARGE" in dl or \
                   "COLLECTION CHARGE" in dl:
                    fee_match = re.search(r"\(([\d.]+)\)", dl)
                    if fee_match:
                        collection_fee += float(_dec(fee_match.group(1)))

            pd_match = re.search(r"P/D\s+(\d{1,2})-([A-Z]+)-(\d{4})", full_text)
            if pd_match:
                day = int(pd_match.group(1))
                month_str2 = pd_match.group(2).upper()
                yr = int(pd_match.group(3))
                months = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
                          "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
                mon = months.get(month_str2, int(month_str.split("-")[1]))
                try:
                    payment_date = date(yr, mon, day)
                except ValueError:
                    payment_date = None
            else:
                rd_num_match = re.search(r"R/D\s+(\d{1,2})-(\d{1,2})-(\d{4})", full_text)
                if rd_num_match:
                    day = int(rd_num_match.group(1))
                    mon = int(rd_num_match.group(2))
                    yr = int(rd_num_match.group(3))
                    try:
                        payment_date = date(yr, mon, day)
                    except ValueError:
                        payment_date = None
                else:
                    rd_match2 = re.search(r"R/D\s+(\d{1,2})-([A-Z]+)-(\d{4})", full_text)
                    if rd_match2:
                        day = int(rd_match2.group(1))
                        month_str2 = rd_match2.group(2).upper()
                        yr = int(rd_match2.group(3))
                        months = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
                                  "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
                        mon = months.get(month_str2, int(month_str.split("-")[1]))
                        try:
                            payment_date = date(yr, mon, day)
                        except ValueError:
                            payment_date = None
                    else:
                        try:
                            y, m_num = map(int, month_str.split("-"))
                            import calendar
                            last_day = calendar.monthrange(y, m_num)[1]
                            payment_date = date(y, m_num, last_day)
                        except (ValueError, IndexError):
                            payment_date = None

            withholding_rate = 0.10
            withholding_tax = gross * withholding_rate

            warnings.warn(
                f"BOCI 分红 {symbol}: 预扣税按 10% 估算，请核实 W-8BEN 状态",
                stacklevel=2,
            )

            self.div_repo.insert(
                broker_code="boci",
                payment_date=payment_date.isoformat() if payment_date else "",
                symbol=symbol,
                company_name=_company_name(symbol),
                per_share_amount=per_share,
                share_quantity=shares,
                gross_amount=gross,
                withholding_tax=withholding_tax,
                withholding_rate=withholding_rate,
                withholding_country="US",
                collection_fee=collection_fee,
                adr_fee=adr_fee,
                net_amount=gross - withholding_tax - collection_fee - adr_fee,
                currency="USD",
                statement_file_id=file_id,
            )
            print(f"    分红: {symbol} 每股${per_share} × {shares}股 = ${gross:.2f} "
                  f"(预扣: ${withholding_tax:.2f}, ADR费: ${adr_fee:.2f}, 收款费: ${collection_fee:.2f})")

    def _parse_rsu_transfers(self, text: str, file_id: int, month_str: str):
        lines = text.split("\n")

        in_rsu_section = False
        i = 0
        while i < len(lines):
            line = lines[i]
            if "证券提存" in line:
                in_rsu_section = True
                i += 1
                continue
            if in_rsu_section:
                if "证券存仓摘要" in line or "交易总览" in line or "交易總覽" in line:
                    in_rsu_section = False
                    i += 1
                    continue

                # Single-line format: "10/04 10/04 441243263 提 货 REDACTED_RSU_GRANT ..."
                rsu_line = re.match(
                    r"^\s*(\d{2}/\d{2})\s+(\d{2}/\d{2})\s+(\d{6,})\s+(.*)",
                    line.strip()
                )
                if rsu_line:
                    trade_date_str = rsu_line.group(1)
                    settle_date_str = rsu_line.group(2)
                    ref_no = rsu_line.group(3)
                    detail = rsu_line.group(4).strip()

                    # Extract quantity from end of detail
                    quantity = 0
                    qty_neg = re.search(r"\((\d+)\)\s*$", detail)
                    qty_pos = re.search(r"(\d+)\s*$", detail)
                    if qty_neg:
                        quantity = -int(qty_neg.group(1))
                    elif qty_pos:
                        quantity = int(qty_pos.group(1))

                    if quantity != 0 and ref_no and detail:
                        trade_date = self._resolve_date(trade_date_str, month_str)
                        settle_date = self._resolve_date(settle_date_str, month_str)

                        # pdfplumber 在中文间插入空格："存 货" → "存货"
                        detail_nospace = re.sub(r"\s+", "", detail)

                        if "存货" in detail_nospace and quantity > 0:
                            sym_match = re.search(r"存\s*货\s*([A-Z]+)\s*(.*)", detail)
                            symbol = sym_match.group(1) if sym_match else ""
                            company = re.sub(r"\s+", "", sym_match.group(2)) if sym_match else ""
                            if symbol:
                                self.txn_repo.insert(
                                    broker_code="boci",
                                    trade_date=trade_date.isoformat() if trade_date else "",
                                    settlement_date=settle_date.isoformat() if settle_date else None,
                                    reference_no=ref_no,
                                    symbol=symbol,
                                    company_name=company,
                                    exchange="NYSE" if symbol == "BABA" else None,
                                    action="rsu_vest",
                                    quantity=quantity,
                                    price=None,
                                    amount=0.0,
                                    currency="USD",
                                    statement_file_id=file_id,
                                )
                                print(f"    RSU归属: {symbol} × {quantity} ({trade_date})")

                        elif "提货" in detail_nospace and quantity < 0:
                            # 提货行是 RSU 取消记录，仅作内部跟踪，不创建应税交易
                            if "REDACTED_RSU_GRANT" in detail_nospace:
                                symbol = "BABA"
                                company = "阿里巴巴"
                                print(f"    RSU取消: {symbol} × {abs(quantity)} 股（税扣）({trade_date})")
                            else:
                                sym_match2 = re.match(r"提货\s+([A-Z]+)\s+(.*)", detail)
                                symbol = sym_match2.group(1) if sym_match2 else ""
                                company = sym_match2.group(2).strip() if sym_match2 else ""
                                if "REDACTED_RSU_GRANT" in symbol:
                                    symbol = "BABA"
                                if symbol:
                                    self.txn_repo.insert(
                                        broker_code="boci",
                                        trade_date=trade_date.isoformat() if trade_date else "",
                                        settlement_date=settle_date.isoformat() if settle_date else None,
                                        reference_no=ref_no,
                                        symbol=symbol,
                                        company_name=company,
                                        exchange="NYSE" if symbol == "BABA" else None,
                                        action="rsu_cancel",
                                        quantity=quantity,
                                        price=None,
                                        amount=0.0,
                                        currency="USD",
                                        statement_file_id=file_id,
                                    )
                                    print(f"    RSU取消: {symbol} × {quantity} ({trade_date})")
                    i += 1
                    continue

                # Fallback: multi-line format (old style)
                date_match = re.match(r"^\s*(\d{2}/\d{2})\s*$", line.strip())
                if date_match:
                    trade_date_str = date_match.group(1)
                    settle_date_str = ""
                    ref_no = ""
                    detail = ""
                    quantity = 0

                    if i + 1 < len(lines):
                        sd_match = re.match(r"^\s*(\d{2}/\d{2})\s*$", lines[i + 1].strip())
                        if sd_match:
                            settle_date_str = sd_match.group(1)
                            i += 1

                    if i + 1 < len(lines):
                        ref_match = re.match(r"^\s*(\d{6,})\s*$", lines[i + 1].strip())
                        if ref_match:
                            ref_no = ref_match.group(1)
                            i += 1

                    if i + 1 < len(lines):
                        i += 1
                        detail = lines[i].strip()

                    if i + 1 < len(lines):
                        qty_line = lines[i + 1].strip()
                        qty_match_neg = re.match(r"^\((\d+)\)\s*$", qty_line)
                        qty_match_pos = re.match(r"^(\d+)\s*$", qty_line)
                        if qty_match_neg:
                            quantity = -int(qty_match_neg.group(1))
                            i += 1
                        elif qty_match_pos:
                            quantity = int(qty_match_pos.group(1))
                            i += 1

                    trade_date = self._resolve_date(trade_date_str, month_str) if trade_date_str else None
                    settle_date = self._resolve_date(settle_date_str, month_str) if settle_date_str else None

                    if quantity != 0 and ref_no and detail:
                        if "存货" in detail:
                            sym_match = re.match(r"存货\s+([A-Z]+)\s+(.*)", detail)
                            symbol = sym_match.group(1) if sym_match else ""
                            company = sym_match.group(2).strip() if sym_match else ""
                            if symbol and quantity > 0:
                                self.txn_repo.insert(
                                    broker_code="boci",
                                    trade_date=trade_date.isoformat() if trade_date else "",
                                    settlement_date=settle_date.isoformat() if settle_date else None,
                                    reference_no=ref_no,
                                    symbol=symbol,
                                    company_name=company,
                                    exchange="NYSE" if symbol == "BABA" else None,
                                    action="rsu_vest",
                                    quantity=quantity,
                                    price=None,
                                    amount=0.0,
                                    currency="USD",
                                    statement_file_id=file_id,
                                )
                                print(f"    RSU转入: {symbol} × {quantity} ({trade_date})")

                        elif "提货" in detail:
                            sym_match = re.match(r"提货\s+(ABR\d+[A-Z]?)\s+(.*)", detail)
                            symbol = sym_match.group(1) if sym_match else ""
                            company = sym_match.group(2).strip() if sym_match else ""
                            if "REDACTED_RSU_GRANT" in symbol:
                                symbol = "BABA"
                            if symbol and quantity < 0:
                                self.txn_repo.insert(
                                    broker_code="boci",
                                    trade_date=trade_date.isoformat() if trade_date else "",
                                    settlement_date=settle_date.isoformat() if settle_date else None,
                                    reference_no=ref_no,
                                    symbol=symbol,
                                    company_name=company,
                                    exchange="NYSE" if symbol == "BABA" else None,
                                    action="rsu_cancel",
                                    quantity=quantity,
                                    price=None,
                                    amount=0.0,
                                    currency="USD",
                                    statement_file_id=file_id,
                                )
                                print(f"    RSU提货: {symbol} × {quantity} ({trade_date})")

            i += 1

    def _parse_positions(self, text: str, file_id: int, month_str: str):
        month_num = int(month_str.split("-")[1])
        year_num = int(month_str.split("-")[0])

        if month_num == 12:
            end_date = f"{year_num}-12-31"
        else:
            import calendar
            last_day = calendar.monthrange(year_num, month_num)[1]
            end_date = f"{year_num}-{month_num:02d}-{last_day}"

        lines = text.split("\n")

        in_boci_section = False
        for i, line in enumerate(lines):
            if "证券存仓摘要" in line or "證券存倉摘要" in line:
                in_boci_section = True
                continue
            if in_boci_section:
                if line.strip().startswith(("交易总览", "交易總覽", "重要提示", "製備日期")):
                    in_boci_section = False
                    continue
                sym_match = re.match(r"^([A-Z][A-Z0-9]+)\s+", line.strip())
                if sym_match:
                    symbol = sym_match.group(1)
                    if symbol == "SFC":
                        continue
                    if i + 1 < len(lines):
                        nums_line = lines[i + 1].strip()
                        nums = re.findall(r"([\d,]+\.?\d*)", nums_line)
                        if len(nums) >= 5:
                            quantity = int(float(_dec(nums[4])))
                            price = float(_dec(nums[5])) if len(nums) > 5 else 0
                            if quantity > 0:
                                self.pos_repo.upsert(
                                    broker_code="boci",
                                    as_of_date=end_date,
                                    symbol=symbol,
                                    quantity=quantity,
                                    currency="USD",
                                    closing_price=price,
                                    statement_file_id=file_id,
                                )
        if self._count_positions(file_id) > 0:
            return

        from .shared_utils import _normalize_option_underlying, _normalize_strike
        in_position_section = False
        position_count = 0

        pos_pattern = re.compile(
            r"^([A-Z][A-Z0-9_]+)(?:\([^)]+\))?\s+"
            r"(\w+)\s+"
            r"(USD|HKD|CNY)\s+"
            r"([\d,]+\.?\d*)\s+"
            r"([\d,]+\.?\d*)\s+"
            r"(-|[\d,]+\.?\d*)\s+"
            r"([\d,]+\.?\d*)"
        )

        for i, line in enumerate(lines):
            if any(s in line for s in (
                "期末概覽-股票和股票期權",
                "期末概览-股票和股票期权",
                "期末概覽--股票和股票期權",
                "期末概览--股票和股票期权",
                "期末概覽——股票和股票期權",
                "期末概览——股票和股票期权",
            )):
                in_position_section = True
                continue

            if in_position_section:
                if line.strip().startswith(("重要提示", "交易明細", "分紅明細", "製備日期")):
                    in_position_section = False
                    continue

            if not in_position_section:
                continue

            if "代碼名稱" in line or "代码名称" in line:
                continue

            m = pos_pattern.match(line.strip())
            if not m:
                continue

            raw_symbol = m.group(1)
            exchange = m.group(2)
            currency = m.group(3)
            quantity = int(float(_dec(m.group(4))))
            price = float(_dec(m.group(5)))
            multiplier_str = m.group(6)
            market_value = float(_dec(m.group(7)))

            if quantity <= 0:
                continue

            symbol = raw_symbol
            company_name = ""

            if "_OPT_" in symbol:
                parts = symbol.split("_OPT_")
                if len(parts) == 2:
                    underlying = parts[0]
                    company_name = f"{underlying} Option"
            else:
                option_match = re.match(r"([A-Za-z]+\d?)(\d{6})([CP])(\d*)", symbol)
                if option_match:
                    underlying = _normalize_option_underlying(option_match.group(1))
                    expiry = option_match.group(2)
                    cp_type = option_match.group(3)
                    strike_digits = option_match.group(4)

                    if strike_digits:
                        if len(strike_digits) >= 2:
                            strike = str(int(strike_digits) / 1000.0)
                        else:
                            strike = strike_digits
                    else:
                        strike = "0"

                    symbol = f"{underlying}_OPT_{expiry}_{_normalize_strike(strike)}_{cp_type}"
                    company_name = f"{underlying} Option"
                else:
                    company_name = _company_name(symbol)

            avg_cost = market_value / quantity if quantity > 0 else 0

            self.pos_repo.upsert(
                broker_code="boci",
                as_of_date=end_date,
                symbol=symbol,
                company_name=company_name,
                quantity=quantity,
                avg_cost=avg_cost,
                closing_price=price,
                market_value=market_value,
                unrealized_pnl=0,
                currency=currency,
                statement_file_id=file_id,
            )
            position_count += 1

        if position_count > 0:
            print(f"    持仓: {position_count} 个品种")

    def _resolve_date(self, date_str: str, month_str: str) -> date | None:
        parts = date_str.split("/")
        if len(parts) != 2:
            return None
        first, second = int(parts[0]), int(parts[1])
        year = int(month_str.split("-")[0])
        stmt_month = int(month_str.split("-")[1])
        day, month = first, second
        if month > stmt_month:
            year -= 1
        try:
            return date(year, month, day)
        except ValueError:
            try:
                return date(year, first, second)
            except ValueError:
                return None

    def _classify_boci_detail(self, detail: str, full_text: str):
        detail_lower = detail.lower()

        if "股息" in detail or "dividend" in detail_lower:
            if "adr issuance fee" in detail_lower or "adr fee" in detail_lower:
                return "fee", "BABA", "阿里巴巴"
            if "collection charge" in detail_lower or "collection fee" in detail_lower:
                if "股息" in detail or "cash dividend" in detail_lower:
                    return "dividend", "BABA", "阿里巴巴"
                return "fee", "BABA", "阿里巴巴"
            return "dividend", "BABA", "阿里巴巴"

        if "adr" in detail_lower or "fee" in detail_lower or "charge" in detail_lower:
            return "fee", "BABA", "阿里巴巴"

        if "buy" in detail_lower or "买入" in detail:
            sym = self._extract_symbol(detail)
            return "buy", sym, _company_name(sym)

        if "sell" in detail_lower or "卖出" in detail:
            sym = self._extract_symbol(detail)
            return "sell", sym, _company_name(sym)

        if "承前结余" in detail or "转后结余" in detail:
            return None, None, None

        symbol = self._extract_symbol(detail)
        if symbol:
            return "fee", symbol, _company_name(symbol)

        return None, None, None

    def _extract_symbol(self, detail: str) -> str:
        for sym in SYMBOL_NAME_MAP:
            if sym in detail:
                return sym
        m = re.search(r"\b([A-Z]{2,6})\b", detail)
        if m:
            word = m.group(1)
            if word not in self._GENERIC_WORDS:
                return word
        return ""
