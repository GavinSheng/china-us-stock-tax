"""Futu 富途月结单导入"""
from __future__ import annotations

import json
import re
import warnings
from pathlib import Path

from .shared_utils import (
    DECRYPTED_DIR, FUTU_2024_PASSWORD, _dedup_text, _clean_num,
    _normalize_strike, _normalize_option_underlying, _company_name, _futu_div_rate,
)
from .base import BaseImporter


class FutuImporter(BaseImporter):
    """富途月结单解析

    结构特点:
    - 交易数据在文本中，非表格形式
    - 每笔交易占 3-5 行:
      L1: 买卖方向 代码(名称) 货币 数量 价格 成交额 变动额
      L2: 交易所代码 币种 日期 交收日期 数量 价格 成交额 变动额
      L3: 时间
      L4: 佣金: X 平台使用費: Y 交收費: Z 小計: N
    """

    broker_code = "futu"
    PDF_GLOB = "futu_*.pdf"

    def _display_name(self) -> str:
        return "Futu 富途"

    def _extract_month(self, pdf_file: Path) -> str:
        m = re.search(r"(\d{8})-\d{13,}", pdf_file.stem)
        if m:
            date_raw = m.group(1)
            return f"{date_raw[:4]}-{date_raw[4:6]}"
        m2 = re.search(r"(\d{6})_", pdf_file.stem)
        if m2:
            date_raw = m2.group(1)
            return f"{date_raw[:4]}-{date_raw[4:6]}"
        return "unknown"

    def _pdf_password(self, month_str: str) -> str | None:
        year = int(month_str[:4]) if month_str != "unknown" and month_str[:4].isdigit() else 2025
        return FUTU_2024_PASSWORD if year <= 2024 else None

    def _preprocess_text(self, text: str) -> str:
        return _dedup_text(text)

    def _import_notes(self) -> str:
        return "Futu - 主动交易账户"

    def _parse_all(self, full_text: str, file_id: int, month_str: str) -> None:
        self._parse_transactions(full_text, file_id, month_str)
        self._parse_dividends(full_text, file_id, month_str)
        self._parse_positions(full_text, file_id, month_str)

    def _extract_futu_option_context(self, lines: list[str], trade_line_idx: int, raw_symbol: str) -> tuple[str, str, str, str]:
        """从 Futu 期权交易行的上下文中提取合约详情。

        返回 (underlying, expiry_yymmdd, cp_type, strike)
        """
        underlying = ""
        expiry = ""
        cp = ""
        strike = ""

        sym_match = re.match(r"([A-Za-z]+\d?)(\d{6})([CP])(\d*)", raw_symbol)
        if sym_match:
            underlying = sym_match.group(1)
            expiry = sym_match.group(2)
            cp = sym_match.group(3)
            strike_digits = sym_match.group(4)
            if strike_digits:
                strike = str(int(strike_digits) / 1000.0)

        if trade_line_idx > 0:
            prev = lines[trade_line_idx - 1].strip()
            m = re.search(r"\(([A-Za-z]+)", prev)
            if m:
                underlying = m.group(1)

        for j in range(trade_line_idx + 1, min(trade_line_idx + 8, len(lines))):
            ctx = lines[j].strip()

            m = re.match(r"^(\d{6})$", ctx)
            if m:
                expiry = m.group(1)

            m = re.search(r"([\d.]+)([CP])\)", ctx)
            if m:
                strike = m.group(1)
                if not cp:
                    cp = m.group(2)

            if re.match(r"\w+\s+(USD|HKD|CNY)\s+\d{4}/\d{2}/\d{2}", ctx):
                break

        return _normalize_option_underlying(underlying), expiry, cp, strike

    def _parse_transactions(self, text: str, file_id: int, month_str: str):
        lines = text.split("\n")
        total_trades = 0
        option_count = 0
        txn_counter = 0

        i = 0
        while i < len(lines):
            line = lines[i].strip()

            trade_match = re.match(
                r"(買入開倉|賣出平倉|買入|賣出)\s+(\S+)\s+(USD|HKD|CNY)\s+([\d,]+\.?\d*)\s+([\d,]+\.?\d+)\s+(-?[\d,]+\.?\d+)\s+(-?[\d,]+\.?\d+)",
                line
            )
            if not trade_match:
                # 期权到期检测: "減少 期權行權"
                # 格式A(单行): 2025/02/28 減少 期權行權 NVDA250228C140000(NVDA USD -16 0.00 Opt EXP-NA-
                # 格式B(完整): 2025/02/28 減少 期權行權 YINN250228C46000(YINN 250228 USD -8 0.00 Opt EXP-NA-
                #   (下一行) 250228 140.00C) NVDA250228C140000-20250228
                expire_match = re.match(
                    r"(\d{4}/\d{2}/\d{2})\s+減少\s+期權行權\s+"
                    r"([A-Za-z]+\d{6}[CP]\d*)\s*"
                    r"\(([A-Za-z]+)\s*(?:\d{6})?\s*USD\s+"
                    r"(-[\d,]+\.?\d*)\s+([\d.]+)",
                    line
                )
                if expire_match:
                    date_str = expire_match.group(1).replace("/", "-")
                    raw_symbol = expire_match.group(2)
                    underlying = expire_match.group(3)
                    quantity = abs(int(float(expire_match.group(4).replace(",", ""))))
                    amount_val = float(expire_match.group(5))

                    if amount_val == 0.0:
                        # 解析期权符号
                        opt_m = re.match(r"([A-Za-z]+)(\d{6})([CP])(\d+)", raw_symbol)
                        expiry = ""
                        cp = ""
                        strike = ""
                        if opt_m:
                            expiry = opt_m.group(2)
                            cp = opt_m.group(3)
                            strike_digits = opt_m.group(4)
                            # 富途 strike 编码规则: strike × 1000 编码为整数
                            # "140000" → 140.0, "46000" → 46.0
                            strike = str(int(strike_digits) / 1000.0)

                        # 如果 expiry 为空，尝试从下一行补充
                        if not expiry or expiry == "000000":
                            if i + 1 < len(lines):
                                next_line = lines[i + 1].strip()
                                ctx_m = re.match(r"(\d{6})\s+([\d.]+)([CP])", next_line)
                                if ctx_m:
                                    expiry = ctx_m.group(1)
                                    strike_digits_next = ctx_m.group(2)
                                    strike = str(int(float(strike_digits_next)) / 1000.0)
                                    cp = ctx_m.group(3)

                        if expiry and cp and strike:
                            option_symbol = f"{underlying}_OPT_{expiry}_{strike}_{cp}"
                            txn_counter += 1
                            ref_no = f"F{file_id}-EXP-{txn_counter:04d}"
                            self.txn_repo.insert(
                                broker_code="futu",
                                trade_date=date_str,
                                settlement_date=date_str,
                                reference_no=ref_no,
                                symbol=option_symbol,
                                company_name=_company_name(underlying),
                                exchange=None,
                                action="option_expire",
                                quantity=quantity,
                                price=0,
                                amount=0,
                                commission=0,
                                platform_fee=0,
                                delivery_fee=0,
                                sec_fee=0,
                                taf_fee=0,
                                currency="USD",
                                statement_file_id=file_id,
                                raw_data=json.dumps({
                                    "source": "option_expire",
                                    "original_symbol": raw_symbol,
                                    "expiry": f"20{expiry[:2]}-{expiry[2:4]}-{expiry[4:]}",
                                }),
                            )
                            total_trades += 1
                            i += 1
                            continue

                trade_match_b = re.match(
                    r"(買入開倉|賣出平倉|買入|賣出)\s+(USD|HKD|CNY)\s+([\d,]+\.?\d*)\s+([\d,]+\.?\d+)\s+(-?[\d,]+\.?\d+)\s+(-?[\d,]+\.?\d+)",
                    line
                )
                if trade_match_b and i + 1 < len(lines):
                    next_line = lines[i + 1].strip()
                    if next_line.startswith('製備日期') or not next_line:
                        # 下一行是页脚，symbol 可能在下一页开头。
                        # 扫描后续行，跳过页脚和表头，寻找 symbol 行。
                        direction = trade_match_b.group(1)
                        currency = trade_match_b.group(2)
                        quantity_str = trade_match_b.group(3)
                        price_str = trade_match_b.group(4)
                        amount_str = trade_match_b.group(5)

                        quantity = int(float(_clean_num(quantity_str)))
                        price = float(_clean_num(price_str))
                        amount = float(_clean_num(amount_str))

                        # 扫描后续行寻找 symbol（最多跨 5 行）
                        raw_symbol = ""
                        found_j = None
                        for j in range(i + 2, min(i + 8, len(lines))):
                            ctx = lines[j].strip()
                            # 跳过页脚和表头
                            if ctx.startswith('製備日期') or '證券月結單' in ctx or '買賣方向' in ctx:
                                continue
                            # 匹配 symbol 行（如 BABA251114C1 GMNI USD ...）
                            sym_cross = re.match(
                                r"([A-Za-z]+\d{6}[CP]\d*)\s+\S+\s+(USD|HKD|CNY)",
                                ctx
                            )
                            if sym_cross:
                                raw_symbol = sym_cross.group(1)
                                found_j = j
                                break
                            # 如果是佣金行或下一笔交易，说明没有 symbol 行
                            if "佣金" in ctx or re.match(r"(買入|賣出)", ctx):
                                break

                        if not raw_symbol:
                            i += 1
                            continue

                        # 从 symbol 行之后找 exchange 日期和佣金
                        trade_date = ""
                        exchange = ""

                        # 跨页场景下，symbol 行本身可能包含日期：
                        # BABA251114C1 GMNI USD 2025/11/04 2025/11/05 2 1.2800 ...
                        sym_line = lines[found_j].strip()
                        date_in_sym = re.search(
                            r"\d{4}/\d{1,2}/\d{1,2}\s+(\d{4}/\d{1,2}/\d{1,2})",
                            sym_line
                        )
                        if date_in_sym:
                            trade_date = date_in_sym.group(1)  # 取第二个日期作为结算日
                            trade_date_for_trade = date_in_sym.group(1).replace("/", "-")
                        else:
                            trade_date_for_trade = ""

                        for j in range(found_j + 1, min(found_j + 10, len(lines))):
                            ctx = lines[j].strip()
                            em = re.match(
                                r"(\w+)\s+(USD|HKD|CNY)\s+(\d{4}/\d{1,2}/\d{1,2})\s+(\d{4}/\d{1,2}/\d{1,2})",
                                ctx
                            )
                            if em:
                                exchange = em.group(1)
                                if not trade_date:
                                    trade_date = em.group(3)
                                break
                            if "佣金" in ctx:
                                break

                        option_match = re.match(r"([A-Za-z]+\d?)(\d{6})([CP])(\d*)", raw_symbol)
                        if option_match:
                            underlying, expiry, cp_type, strike = self._extract_futu_option_context(
                                lines, found_j, raw_symbol
                            )
                            if not underlying or not expiry or not strike:
                                raise ValueError(
                                    f"Futu 期权跨页上下文提取失败：raw_symbol={raw_symbol}"
                                )
                            option_symbol = f"{underlying}_OPT_{expiry}_{_normalize_strike(strike)}_{cp_type}"

                            fees = {}
                            for j in range(found_j + 1, min(found_j + 12, len(lines))):
                                next_l = lines[j].strip()
                                if "佣金" in next_l:
                                    fee_items = re.findall(r"([一-鿿]+):\s*([\d.]+)", next_l)
                                    for fname, fval in fee_items:
                                        if fname == "小計":
                                            continue
                                        fees[fname] = float(fval)
                                    break

                            commission = fees.get("佣金", 0)
                            platform_fee = fees.get("平台使用費", 0)
                            delivery_fee = fees.get("交收費", 0)
                            sec_fee = fees.get("證監會規費", 0)
                            taf_fee = fees.get("交易活動費", 0)
                            trade_date_str = trade_date_for_trade if trade_date_for_trade else (trade_date.replace("/", "-") if trade_date else "")

                            # UTC 时区校正
                            if trade_date_str and expiry and len(expiry) == 6:
                                expiry_str = f"20{expiry[:2]}-{expiry[2:4]}-{expiry[4:]}"
                                if trade_date_str > expiry_str:
                                    trade_date_str = expiry_str

                            action = "option_buy" if "買入" in direction else "option_sell"
                            raw_data = {
                                "option_type": "call" if cp_type == "C" else "put",
                                "underlying": underlying,
                                "expiry": f"20{expiry[:2]}-{expiry[2:4]}-{expiry[4:]}",
                                "strike": float(strike) if strike else 0.0,
                                "original_symbol": raw_symbol,
                                "contract_symbol": option_symbol,
                            }

                            txn_counter += 1
                            ref_no = f"F{file_id}-{txn_counter:04d}"
                            self.txn_repo.insert(
                                broker_code="futu",
                                trade_date=trade_date_str,
                                settlement_date=trade_date_str,
                                reference_no=ref_no,
                                symbol=option_symbol,
                                company_name=_company_name(underlying),
                                exchange=exchange,
                                action=action,
                                quantity=quantity,
                                price=price,
                                amount=abs(amount),
                                commission=commission,
                                platform_fee=platform_fee,
                                delivery_fee=delivery_fee,
                                sec_fee=sec_fee,
                                taf_fee=taf_fee,
                                fee_breakdown=fees if fees else None,
                                currency=currency,
                                statement_file_id=file_id,
                                raw_data=json.dumps(raw_data),
                            )
                            total_trades += 1
                            option_count += 1

                            if action == "option_buy":
                                self.tax_lot_repo.add_lot(
                                    symbol=option_symbol,
                                    acquisition_date=trade_date_str,
                                    acquisition_type="buy",
                                    quantity=quantity,
                                    cost_per_share=price,
                                    currency=currency,
                                    broker_code="futu",
                                )

                            i += 1
                            continue
                        else:
                            i += 1
                            continue

                    direction = trade_match_b.group(1)
                    currency = trade_match_b.group(2)
                    quantity_str = trade_match_b.group(3)
                    price_str = trade_match_b.group(4)
                    amount_str = trade_match_b.group(5)

                    quantity = int(float(_clean_num(quantity_str)))
                    price = float(_clean_num(price_str))
                    amount = float(_clean_num(amount_str))

                    is_2024_fmt = bool(re.match(r"^[\d.]+[CP]\)$", next_line))
                    has_symbol_above = i > 0 and bool(re.match(r"[A-Za-z0-9]+\(", lines[i - 1].strip()))

                    raw_symbol = ""
                    exchange = ""
                    trade_date = ""

                    if is_2024_fmt or has_symbol_above:
                        if i > 0:
                            prev_line = lines[i - 1].strip()
                            opt_prev = re.match(r"([A-Za-z]+\d?)(\d{6})([CP])(\d*)", prev_line)
                            if opt_prev:
                                raw_symbol = opt_prev.group(0)
                            else:
                                raw_symbol = prev_line.split('(')[0].split()[0] if prev_line else ""
                        trade_date = ""
                        for j in range(i + 1, min(i + 10, len(lines))):
                            ctx = lines[j].strip()
                            # 优先匹配双日期行（成交日 + 结算日）
                            em = re.match(
                                r"(\w+)\s+(USD|HKD|CNY)\s+(\d{4}/\d{1,2}/\d{1,2})\s+(\d{4}/\d{1,2}/\d{1,2})",
                                ctx
                            )
                            if em:
                                exchange = em.group(1)
                                trade_date = em.group(3)        # 成交日（第一个日期）
                                break
                            # 回退到单日期行
                            em2 = re.match(
                                r"(\w+)\s+(USD|HKD|CNY)\s+(\d{4}/\d{1,2}/\d{1,2})",
                                ctx
                            )
                            if em2:
                                exchange = em2.group(1)
                                trade_date = em2.group(3)
                                break
                            if "佣金" in ctx:
                                break
                    else:
                        raw_symbol = next_line.split()[0] if next_line else ""
                        raw_symbol = raw_symbol.split('(')[0]
                        parts = next_line.split()
                        if len(parts) >= 5:
                            exchange = parts[1]
                            trade_date = parts[4]

                    option_match = re.match(r"([A-Za-z]+\d?)(\d{6})([CP])(\d*)", raw_symbol)
                    if option_match:
                        underlying, expiry, cp_type, strike = self._extract_futu_option_context(
                            lines, i + 1, raw_symbol
                        )

                        if not underlying or not expiry or not strike:
                            raise ValueError(
                                f"Futu 期权上下文提取失败：raw_symbol={raw_symbol}, "
                                f"underlying={underlying!r}, expiry={expiry!r}, strike={strike!r}"
                            )

                        option_symbol = f"{underlying}_OPT_{expiry}_{_normalize_strike(strike)}_{cp_type}"

                        fees = {}
                        j = i + 1
                        while j < len(lines) and j < i + 12:
                            next_l = lines[j].strip()
                            if "佣金" in next_l:
                                fee_items = re.findall(r"([一-鿿]+):\s*([\d.]+)", next_l)
                                for fname, fval in fee_items:
                                    if fname == "小計":
                                        continue
                                    fees[fname] = float(fval)
                                break
                            j += 1

                        commission = fees.get("佣金", 0)
                        platform_fee = fees.get("平台使用費", 0)
                        delivery_fee = fees.get("交收費", 0)
                        sec_fee = fees.get("證監會規費", 0)
                        taf_fee = fees.get("交易活動費", 0)
                        trade_date_str = trade_date.replace("/", "-") if trade_date else ""

                        # UTC 时区校正：trade_date > 期权到期日时，锁定为到期日
                        if trade_date_str and expiry and len(expiry) == 6:
                            expiry_str = f"20{expiry[:2]}-{expiry[2:4]}-{expiry[4:]}"
                            if trade_date_str > expiry_str:
                                trade_date_str = expiry_str

                        action = "option_buy" if "買入" in direction else "option_sell"

                        raw_data = {
                            "option_type": "call" if cp_type == "C" else "put",
                            "underlying": underlying,
                            "expiry": f"20{expiry[:2]}-{expiry[2:4]}-{expiry[4:]}",
                            "strike": float(strike) if strike else 0.0,
                            "original_symbol": raw_symbol,
                            "contract_symbol": option_symbol,
                        }

                        txn_counter += 1
                        ref_no = f"F{file_id}-{txn_counter:04d}"
                        self.txn_repo.insert(
                            broker_code="futu",
                            trade_date=trade_date_str,
                            settlement_date=trade_date_str,
                            reference_no=ref_no,
                            symbol=option_symbol,
                            company_name=_company_name(underlying),
                            exchange=exchange,
                            action=action,
                            quantity=quantity,
                            price=price,
                            amount=abs(amount),
                            commission=commission,
                            platform_fee=platform_fee,
                            delivery_fee=delivery_fee,
                            sec_fee=sec_fee,
                            taf_fee=taf_fee,
                            fee_breakdown=fees if fees else None,
                            currency=currency,
                            statement_file_id=file_id,
                            raw_data=json.dumps(raw_data),
                        )
                        total_trades += 1
                        option_count += 1

                        if action == "option_buy":
                            self.tax_lot_repo.add_lot(
                                symbol=option_symbol,
                                acquisition_date=trade_date_str,
                                acquisition_type="buy",
                                quantity=quantity,
                                cost_per_share=price,
                                currency=currency,
                                broker_code="futu",
                            )

                        i += 1
                        continue
                    else:
                        symbol_match = re.match(r"([A-Z]+)", raw_symbol)
                        symbol = symbol_match.group(1) if symbol_match else raw_symbol
                        action = "buy" if "買入" in direction else "sell"

                        fees = {}
                        j = i + 1
                        while j < len(lines) and j < i + 12:
                            next_l = lines[j].strip()
                            if "佣金" in next_l:
                                fee_items = re.findall(r"([一-鿿]+):\s*([\d.]+)", next_l)
                                for fname, fval in fee_items:
                                    if fname == "小計":
                                        continue
                                    fees[fname] = float(fval)
                                break
                            j += 1

                        commission = fees.get("佣金", 0)
                        platform_fee = fees.get("平台使用費", 0)
                        delivery_fee = fees.get("交收費", 0)
                        sec_fee = fees.get("證監會規費", 0)
                        taf_fee = fees.get("交易活動費", 0)
                        # stock 路径：trade_date 已从上文 option 解析块获取
                        trade_date_str = trade_date.replace("/", "-") if trade_date else ""

                        txn_counter += 1
                        ref_no = f"F{file_id}-{txn_counter:04d}"
                        self.txn_repo.insert(
                            broker_code="futu",
                            trade_date=trade_date_str,
                            settlement_date=trade_date_str,
                            reference_no=ref_no,
                            symbol=symbol,
                            company_name=_company_name(symbol),
                            exchange=exchange,
                            action=action,
                            quantity=quantity,
                            price=price,
                            amount=abs(amount),
                            commission=commission,
                            platform_fee=platform_fee,
                            delivery_fee=delivery_fee,
                            sec_fee=sec_fee,
                            taf_fee=taf_fee,
                            fee_breakdown=fees if fees else None,
                            currency=currency,
                            statement_file_id=file_id,
                        )
                        total_trades += 1
                        i += 1
                        continue
                else:
                    i += 1
                    continue
            if not trade_match:
                i += 1
                continue

            direction = trade_match.group(1)
            raw_symbol = trade_match.group(2)
            currency = trade_match.group(3)
            quantity_str = trade_match.group(4)
            price_str = trade_match.group(5)
            amount_str = trade_match.group(6)

            if re.match(r"^[\d.]+[CP]\)$", raw_symbol):
                if i > 0:
                    prev = lines[i - 1].strip()
                    opt_prev = re.match(r"([A-Za-z]+\d?)(\d{6})([CP])(\d*)", prev)
                    if opt_prev:
                        raw_symbol = opt_prev.group(0)
                    else:
                        i += 1
                        continue
                else:
                    i += 1
                    continue

            quantity = int(float(_clean_num(quantity_str)))
            price = float(_clean_num(price_str))
            amount = float(_clean_num(amount_str))

            symbol_match = re.match(r"([A-Z]+)", raw_symbol)
            symbol = symbol_match.group(1) if symbol_match else raw_symbol

            option_match = re.match(r"([A-Za-z]+\d?)(\d{6})([CP])(\d*)", raw_symbol)
            is_option = bool(option_match)
            if is_option:
                underlying, expiry, cp_type, strike = self._extract_futu_option_context(
                    lines, i, raw_symbol
                )
                option_symbol = f"{underlying}_OPT_{expiry}_{_normalize_strike(strike)}_{cp_type}"

                trade_date = ""
                settlement_date = ""
                exchange = ""
                fees = {}
                j = i + 1
                while j < len(lines) and j < i + 14:
                    next_line = lines[j].strip()
                    exch_match = re.match(
                        r"(\w+)\s+(USD|HKD|CNY)\s+(\d{4}/\d{1,2}/\d{1,2})(?:\s+\S+)?\s+(\d{4}/\d{1,2}/\d{1,2})",
                        next_line
                    )
                    if exch_match:
                        exchange = exch_match.group(1)
                        trade_date = exch_match.group(3)        # 成交日（第一个日期）
                        settlement_date = exch_match.group(4)  # 结算日（第二个日期）
                    if "佣金" in next_line:
                        fee_items = re.findall(r"([一-鿿]+):\s*([\d.]+)", next_line)
                        for fname, fval in fee_items:
                            if fname == "小計":
                                continue
                            fees[fname] = float(fval)
                        break
                    j += 1

                commission = fees.get("佣金", 0)
                platform_fee = fees.get("平台使用費", 0)
                delivery_fee = fees.get("交收費", 0)
                sec_fee = fees.get("證監會規費", 0)
                taf_fee = fees.get("交易活動費", 0)

                trade_date_str = trade_date.replace("/", "-") if trade_date else ""
                settlement_date_str = settlement_date.replace("/", "-") if settlement_date else trade_date_str

                # UTC 时区校正：Futu 账单采用 UTC 时区，美东盘中成交对应 UTC 已是次日凌晨。
                # 若 trade_date > 期权到期日，说明是 UTC 入账日延后，实际交易发生在到期日前，
                # 将 trade_date 锁定为到期日，避免「到期后买入」的逻辑矛盾。
                if trade_date_str and expiry and len(expiry) == 6:
                    expiry_str = f"20{expiry[:2]}-{expiry[2:4]}-{expiry[4:]}"
                    if trade_date_str > expiry_str:
                        trade_date_str = expiry_str

                action = "option_buy" if "買入" in direction else "option_sell"

                raw_data = {
                    "option_type": "call" if cp_type == "C" else "put",
                    "underlying": underlying,
                    "expiry": f"20{expiry[:2]}-{expiry[2:4]}-{expiry[4:]}",
                    "strike": float(strike) if strike else 0.0,
                    "original_symbol": raw_symbol,
                    "contract_symbol": option_symbol,
                }

                txn_counter += 1
                ref_no = f"F{file_id}-{txn_counter:04d}"
                self.txn_repo.insert(
                    broker_code="futu",
                    trade_date=trade_date_str,
                    settlement_date=settlement_date_str,
                    reference_no=ref_no,
                    symbol=option_symbol,
                    company_name=_company_name(underlying),
                    exchange=exchange,
                    action=action,
                    quantity=quantity,
                    price=price,
                    amount=abs(amount),
                    commission=commission,
                    platform_fee=platform_fee,
                    delivery_fee=delivery_fee,
                    sec_fee=sec_fee,
                    taf_fee=taf_fee,
                    fee_breakdown=fees if fees else None,
                    currency=currency,
                    statement_file_id=file_id,
                    raw_data=json.dumps(raw_data),
                )
                total_trades += 1
                option_count += 1

                if action == "option_buy":
                    self.tax_lot_repo.add_lot(
                        symbol=option_symbol,
                        acquisition_date=trade_date_str,
                        acquisition_type="buy",
                        quantity=quantity,
                        cost_per_share=price,
                        currency=currency,
                        broker_code="futu",
                    )

                i += 1
                continue

            action = "buy" if "買入" in direction else "sell"

            trade_date = ""
            settlement_date = ""
            exchange = ""
            fees = {}
            j = i + 1
            while j < len(lines) and j < i + 14:
                next_line = lines[j].strip()
                exch_match = re.match(
                    r"(\w+)\s+(USD|HKD|CNY)\s+(\d{4}/\d{1,2}/\d{1,2})(?:\s+\S+)?\s+(\d{4}/\d{1,2}/\d{1,2})",
                    next_line
                )
                if exch_match:
                    exchange = exch_match.group(1)
                    trade_date = exch_match.group(3)        # 成交日（第一个日期）
                    settlement_date = exch_match.group(4)  # 结算日（第二个日期）
                if "佣金" in next_line:
                    fee_items = re.findall(r"([一-鿿]+):\s*([\d.]+)", next_line)
                    for fname, fval in fee_items:
                        if fname == "小計":
                            continue
                        fees[fname] = float(fval)
                    break
                j += 1

            commission = fees.get("佣金", 0)
            platform_fee = fees.get("平台使用費", 0)
            delivery_fee = fees.get("交收費", 0)
            sec_fee = fees.get("證監會規費", 0)
            taf_fee = fees.get("交易活動費", 0)

            trade_date_str = settlement_date.replace("/", "-") if settlement_date else ""
            settlement_date_str = settlement_date.replace("/", "-") if settlement_date else ""

            txn_counter += 1
            ref_no = f"F{file_id}-{txn_counter:04d}"
            self.txn_repo.insert(
                broker_code="futu",
                trade_date=trade_date_str,
                settlement_date=settlement_date_str,
                reference_no=ref_no,
                symbol=symbol,
                company_name=_company_name(symbol),
                exchange=exchange,
                action=action,
                quantity=quantity,
                price=price,
                amount=abs(amount),
                commission=commission,
                platform_fee=platform_fee,
                delivery_fee=delivery_fee,
                sec_fee=sec_fee,
                taf_fee=taf_fee,
                fee_breakdown=fees if fees else None,
                currency=currency,
                statement_file_id=file_id,
            )
            total_trades += 1

            if action in ("buy", "option_buy"):
                self.tax_lot_repo.add_lot(
                    symbol=symbol,
                    acquisition_date=trade_date_str,
                    acquisition_type="buy",
                    quantity=quantity,
                    cost_per_share=price,
                    currency=currency,
                    broker_code="futu",
                )

            i += 1

        if total_trades > 0:
            print(f"    交易: {total_trades} 笔 (期权: {option_count})")

    def _parse_dividends(self, text: str, file_id: int, month_str: str):
        lines = text.split("\n")

        dividend_pattern = re.compile(
            r"(\d{4}/\d{2}/\d{2})\s+增加\s+公司行動\s+USD\s+\+([\d.]+)\s+(\S+)\s+([\d.]+)\s+SHARES\s+DIVIDENDS\s+([\d.]+)",
            re.IGNORECASE
        )

        # 预扣税行格式:
        # 2025/12/17 減少 公司行動 USD -87.95 TSLL 1518.00000000 SHARES WITHHOLDING TAX -0.05793900 USD PER SHARE - TAX
        withholding_pattern = re.compile(
            r"(\d{4}/\d{2}/\d{2})\s+減少\s+公司行動\s+USD\s+-([\d.]+)\s+(\S+)\s+[\d.]+\s+SHARES\s+WITHHOLDING\s+TAX",
            re.IGNORECASE
        )

        # 返还行格式（ROC 分红预扣税返还）:
        # Refund TSLL Withholding Tax Ex-Date 20251210
        # 2025/12/31
        # 增加
        # 公司行動
        # USD
        # +87.95
        refund_pattern = re.compile(
            r"Refund\s+(\w+)\s+Withholding\s+Tax\s+Ex-Date\s+(\d{8})",
            re.IGNORECASE
        )

        # 构建 (date, symbol) -> withholding_amount 映射
        withholding_map: dict[tuple[str, str], float] = {}
        for line in lines:
            m = withholding_pattern.search(line)
            if m:
                date_str = m.group(1).replace("/", "-")
                amount = float(m.group(2))
                symbol = m.group(3)
                withholding_map[(date_str, symbol)] = amount

        # 构建 (symbol, ex_date) -> refund_amount 映射
        # 扫描所有 "Refund X Withholding Tax Ex-Date YYYYMMDD" 行，
        # 然后在后续几行中寻找 +金额
        refund_map: dict[tuple[str, str], float] = {}
        for i, line in enumerate(lines):
            m = refund_pattern.search(line)
            if m:
                symbol = m.group(1)
                ex_date_raw = m.group(2)
                ex_date_str = f"{ex_date_raw[:4]}-{ex_date_raw[4:6]}-{ex_date_raw[6:]}"
                # 在后续行中寻找 +金额
                refund_amount = 0.0
                for j in range(i, min(len(lines), i + 5)):
                    amt_match = re.match(r"\s*\+([\d.]+)\s*$", lines[j])
                    if amt_match:
                        refund_amount = float(amt_match.group(1))
                        break
                if refund_amount > 0:
                    refund_map[(symbol, ex_date_str)] = refund_amount

        dividend_results = []
        for line in lines:
            m = dividend_pattern.search(line)
            if m:
                date_str = m.group(1).replace("/", "-")
                gross_amount = float(m.group(2))
                symbol = m.group(3)
                share_qty = float(m.group(4))
                per_share = float(m.group(5))
                if gross_amount > 0:
                    dividend_results.append({
                        "date": date_str,
                        "gross_amount": gross_amount,
                        "symbol": symbol,
                        "share_quantity": int(share_qty),
                        "per_share_amount": per_share,
                    })

        seen = set()
        for r in dividend_results:
            key = (r["date"], r["symbol"], r["gross_amount"])
            if key in seen:
                continue
            seen.add(key)

            per_share = r["per_share_amount"]

            # 优先从 PDF 提取实际预扣税（WITHHOLDING TAX 行），回退到 10% 计算
            wh_key = (r["date"], r["symbol"])
            if wh_key in withholding_map:
                withholding_tax = withholding_map[wh_key]
                withholding_rate = round(withholding_tax / r["gross_amount"], 4) if r["gross_amount"] > 0 else 0
            else:
                # 股息行已包含完整信息（股数 × 每股金额 = gross），按 10% 计算预扣税
                withholding_rate = 0.10
                withholding_tax = r["gross_amount"] * withholding_rate

            net_amount = r["gross_amount"] - withholding_tax

            # 检测预扣税返还（仅在 PDF 中有明确 "Refund X Withholding Tax" 行时）
            withholding_refund = 0.0
            # 尝试匹配：withholding 行的 ex-date 与 refund 行的 ex-date
            # 简化处理：同月份同 symbol 的 refund 匹配当前 dividend
            for (ref_sym, ref_date), ref_amt in refund_map.items():
                if ref_sym == r["symbol"] and ref_date.startswith(month_str[:7]):
                    withholding_refund = ref_amt
                    break

            self.div_repo.insert(
                broker_code="futu",
                payment_date=r["date"],
                settlement_date=r["date"],
                symbol=r["symbol"],
                company_name="",
                per_share_amount=per_share,
                share_quantity=r["share_quantity"],
                gross_amount=r["gross_amount"],
                withholding_tax=withholding_tax,
                withholding_rate=withholding_rate,
                withholding_country="US",
                withholding_refund=withholding_refund,
                collection_fee=0,
                adr_fee=0,
                other_deductions=0,
                net_amount=net_amount,
                currency="USD",
                exchange_rate=_futu_div_rate(r["date"]),
                statement_file_id=file_id,
                raw_data=f"DIVIDENDS {per_share} USD PER SHARE",
            )
            print(f"    分红: {r['symbol']} USD +{r['gross_amount']:.2f} (预扣税: ${withholding_tax:.2f}, {r['share_quantity']}股)")

        yield_pattern = re.compile(
            r"(\d{4}/\d{2}/\d{2})\s+增加\s+股票收益計劃\s+USD\s+\+([\d.]+)"
        )

        interest_results = []
        for line in lines:
            m = yield_pattern.search(line)
            if m:
                date_str = m.group(1).replace("/", "-")
                amount = float(m.group(2))
                if amount > 0:
                    interest_results.append({
                        "date": date_str,
                        "amount": amount,
                        "currency": "USD",
                    })

        seen_interest = set()
        unique_interest = []
        for r in interest_results:
            key = (r["date"], r["amount"])
            if key not in seen_interest:
                seen_interest.add(key)
                unique_interest.append(r)

        for r in unique_interest:
            self.txn_repo.insert(
                reference_no="",
                broker_code="futu",
                trade_date=r["date"],
                settlement_date=r["date"],
                symbol="STOCK_YIELD",
                action="interest",
                quantity=0,
                price=0,
                amount=r["amount"],
                commission=0,
                platform_fee=0,
                sec_fee=0,
                taf_fee=0,
                delivery_fee=0,
                other_fees=0,
                tax_withheld=0,
                currency=r["currency"],
                exchange_rate=_futu_div_rate(r["date"]),
                statement_file_id=file_id,
                raw_data="Interest Income for Stock Yield Program",
            )
            print(f"    利息: 股票收益计划 USD +{r['amount']:.2f}")

    def _parse_positions(self, text: str, file_id: int, month_str: str):
        month_num = int(month_str.split("-")[1])
        year_num = int(month_str.split("-")[0])

        import calendar
        last_day = calendar.monthrange(year_num, month_num)[1]
        end_date = f"{year_num}-{month_num:02d}-{last_day}"

        lines = text.split("\n")
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
            quantity = int(float(_clean_num(m.group(4))))
            price = float(_clean_num(m.group(5)))
            multiplier_str = m.group(6)
            market_value = float(_clean_num(m.group(7)))

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
                broker_code="futu",
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
