"""Longbridge 长桥月结单导入"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

from .shared_utils import (
    DECRYPTED_DIR, LONGBRIDGE_PASSWORD, _clean_text, _clean_num,
    _normalize_strike, _normalize_option_underlying, _company_name, _infer_exchange,
    _normalize_lb_symbol,
)
from .base import BaseImporter


class LongbridgeImporter(BaseImporter):
    """长桥月结单解析

    结构特点:
    - 密码保护（通过 .env 配置）
    - Page 1: 持仓总览 + 交易开始
    - Page 2-3: 交易明细（逐笔）
    - Page 4: 交易继续 + 分红 + 公司行动
    - Page 5: 备注
    """

    broker_code = "longbridge"
    PDF_GLOB = "longbridge_*.pdf"

    def _display_name(self) -> str:
        return "Longbridge 长桥"

    def _extract_month(self, pdf_file: Path) -> str:
        m = re.search(r"(\d{6})", pdf_file.stem)
        if m:
            month_raw = m.group(1)
            return f"{month_raw[:4]}-{month_raw[4:]}"
        return "unknown"

    def _pdf_password(self, month_str: str) -> str | None:
        return LONGBRIDGE_PASSWORD

    def _preprocess_text(self, text: str) -> str:
        return _clean_text(text)

    def _import_notes(self) -> str:
        return "Longbridge - 主动交易账户"

    def _parse_all(self, full_text: str, file_id: int, month_str: str) -> None:
        self._parse_transactions(full_text, file_id, month_str)
        self._parse_dividends(full_text, file_id, month_str)
        self._parse_positions(full_text, file_id, month_str)

    def _parse_transactions(self, text: str, file_id: int, month_str: str):
        year = month_str.split("-")[0]

        trade_pattern = re.compile(
            r"(\d{4}\.\d{2}\.\d{2})\s+(\d{4}\.\d{2}\.\d{2})\s+(OS\w+)\s+(买[入⼊]|卖出)\s+"
            r"([\w⼀-⿟\.\-]+(?:\s+[\w⼀-⿟\.\-]+)*)\s+"
            r"([\d,]+\.?\d*)\s+([\d,]+\.?\d+)\s+([\d,]+-?\.\d+)\s+(-?[\d,]+\.\d+)"
        )

        option_no_symbol_pattern = re.compile(
            r"(\d{4}\.\d{2}\.\d{2})\s+(\d{4}\.\d{2}\.\d{2})\s+(OS\w+)\s+(买[入⼊]|卖出)\s+"
            r"([\d,]+\.?\d*)\s+([\d,]+\.?\d+)\s+([\d,]+-?\.\d+)\s+(-?[\d,]+\.\d+)"
        )

        exercise_stock_pattern = re.compile(
            r"(\d{4}\.\d{2}\.\d{2})\s+(\d{4}\.\d{2}\.\d{2})\s+(OS\w+)\s+([⾏行]权买[⼊入])\s+"
            r"([A-Za-z0-9_]+)\s+([一-鿿⼀-⿟\s]+?)\s+"
            r"([\d,]+\.?\d*)\s+([\d,]+\.?\d+)\s+([\d,]+-?\.\d+)\s+(-?[\d,]+\.\d+)"
        )

        exercise_contract_pattern = re.compile(
            r"(\d{4}\.\d{2}\.\d{2})\s+期权[⾏行]权\s+"
            r"([A-Za-z0-9]+)\s+([A-Za-z]+)\s+(\d{6})\s+([\d.]+)\s+(Call|Put)\s+-1\.00",
            re.IGNORECASE
        )

        expire_contract_pattern = re.compile(
            r"(\d{4}\.\d{2}\.\d{2})\s+期权到期未[⾏行]权\s+"
            r"\s*([A-Za-z]+)\s+([0-9]{6})\s+([\d]+)\s+(Call|Put)\s+-([\d.]+)",
            re.IGNORECASE
        )

        fee_patterns = {
            "佣金": r"佣[⾦金]\s+([\d.]+)",
            "平台费": r"平台费\s+([\d.]+)",
            "交收费": r"交收费\s+([\d.]+)",
            "sec_fee": r"证券交易委员会费\s+([\d.]+)",
            "taf_fee": r"交易活动收费\s+([\d.]+)",
            "期权清算费": r"期权清算费\s+([\d.]+)",
            "期权监管费": r"期权监管费\s+([\d.]+)",
            "期权交收费": r"期权交收费\s+([\d.]+)",
        }

        lines = text.split("\n")
        i = 0
        while i < len(lines):
            ex_stock = exercise_stock_pattern.search(lines[i])
            if ex_stock:
                trade_date_str = ex_stock.group(1).replace(".", "-")
                settle_date_str = ex_stock.group(2).replace(".", "-")
                ref_no = ex_stock.group(3)
                underlying = ex_stock.group(5).strip()
                quantity = int(float(_clean_num(ex_stock.group(7))))
                price = float(_clean_num(ex_stock.group(8)))
                amount = float(_clean_num(ex_stock.group(9)))

                fees = defaultdict(float)
                j = i + 1
                while j < len(lines) and j < i + 20:
                    fee_line = lines[j]
                    for fee_name, fee_re in fee_patterns.items():
                        fee_match = re.search(fee_re, fee_line)
                        if fee_match:
                            fees[fee_name] = float(fee_match.group(1))
                    if trade_pattern.search(fee_line):
                        break
                    j += 1

                txn_id = self.txn_repo.insert(
                    broker_code="longbridge",
                    trade_date=trade_date_str,
                    settlement_date=settle_date_str,
                    reference_no=ref_no,
                    symbol=underlying,
                    company_name=_company_name(underlying),
                    exchange=_infer_exchange(underlying),
                    action="option_exercise",
                    quantity=quantity,
                    price=price,
                    amount=abs(amount),
                    commission=fees.get("佣金", 0),
                    platform_fee=fees.get("平台费", 0),
                    delivery_fee=fees.get("交收费", 0),
                    sec_fee=fees.get("sec_fee", 0),
                    taf_fee=fees.get("taf_fee", 0),
                    fee_breakdown=dict(fees),
                    currency="USD",
                    statement_file_id=file_id,
                )

                option_premium = 0.0
                for k in range(1, 10):
                    if i + k < len(lines):
                        ex_c = exercise_contract_pattern.search(lines[i + k])
                        if ex_c:
                            opt_underlying = ex_c.group(3)
                            opt_expiry = ex_c.group(4)
                            opt_strike_raw = ex_c.group(5)
                            opt_cp = ex_c.group(6)[0].upper()
                            try:
                                opt_strike = float(opt_strike_raw) / 1000 if int(opt_strike_raw) > 1000 else float(opt_strike_raw)
                            except ValueError:
                                opt_strike = 0.0
                            opt_symbol_suffix = f"_OPT_{opt_expiry}_{opt_strike}_{opt_cp}"
                            all_lots = self.tax_lot_repo.get_all_available_lots()
                            matched_lots = [l for l in all_lots if l["symbol"].endswith(opt_symbol_suffix)]
                            if matched_lots:
                                option_premium = float(matched_lots[0]["cost_per_share"])
                            break

                exercise_cost_per_share = price + option_premium

                self.txn_repo.update_raw_data(
                    txn_id,
                    json.dumps({
                        "source": "option_exercise",
                        "strike_price": price,
                        "option_premium": option_premium,
                        "total_cost_per_share": exercise_cost_per_share,
                    })
                )

                self.tax_lot_repo.add_lot(
                    symbol=underlying,
                    broker_code="longbridge",
                    acquisition_date=trade_date_str,
                    acquisition_type="exercise",
                    source_txn_id=txn_id,
                    quantity=quantity,
                    cost_per_share=exercise_cost_per_share,
                    currency="USD",
                )
                if option_premium > 0:
                    print(f"    行权买入: {quantity} {underlying} @ ${exercise_cost_per_share} (strike=${price} + premium=${option_premium})")
                else:
                    print(f"    行权买入: {quantity} {underlying} @ ${exercise_cost_per_share} (strike=${price})")
                i += 1
                continue

            m = trade_pattern.search(lines[i])
            if not m:
                m2 = option_no_symbol_pattern.search(lines[i])
                if m2:
                    next_line = lines[i + 1] if i + 1 < len(lines) else ""
                    opt_info = re.search(r"([A-Za-z]+)\s+(\d{6})\s+([\d.]+)\s+(Call|Put)", next_line, re.IGNORECASE)
                    if opt_info:
                        underlying = opt_info.group(1)
                        expiry = opt_info.group(2)
                        strike_str = opt_info.group(3)
                        cp = opt_info.group(4)[0].upper()
                        try:
                            strike = float(strike_str)
                        except ValueError:
                            strike = 0.0
                        txn_symbol = f"{underlying}_OPT_{expiry}_{_normalize_strike(strike)}_{cp}"

                        trade_date_str = m2.group(1).replace(".", "-")
                        settle_date_str = m2.group(2).replace(".", "-")
                        ref_no = m2.group(3)
                        action_str = m2.group(4)
                        quantity = int(float(_clean_num(m2.group(5))))
                        price = float(_clean_num(m2.group(6)))
                        amount = float(_clean_num(m2.group(7)))

                        action = "option_buy" if "买" in action_str else "option_sell"

                        fees = defaultdict(float)
                        j = i + 1
                        while j < len(lines) and j < i + 20:
                            fee_line = lines[j]
                            for fee_name, fee_re in fee_patterns.items():
                                fee_match = re.search(fee_re, fee_line)
                                if fee_match:
                                    fees[fee_name] = float(fee_match.group(1))
                            if trade_pattern.search(fee_line) or option_no_symbol_pattern.search(fee_line):
                                break
                            j += 1

                        self.txn_repo.insert(
                            broker_code="longbridge",
                            trade_date=trade_date_str,
                            settlement_date=settle_date_str,
                            reference_no=ref_no,
                            symbol=txn_symbol,
                            company_name=_company_name(underlying),
                            exchange=_infer_exchange(txn_symbol),
                            action=action,
                            quantity=quantity,
                            price=price,
                            amount=abs(amount),
                            commission=fees.get("佣金", 0),
                            platform_fee=fees.get("平台费", 0),
                            delivery_fee=fees.get("交收费", 0),
                            sec_fee=fees.get("sec_fee", 0),
                            taf_fee=fees.get("taf_fee", 0),
                            fee_breakdown=dict(fees),
                            currency="USD",
                            statement_file_id=file_id,
                        )

                        if action == "option_buy":
                            self.tax_lot_repo.add_lot(
                                symbol=txn_symbol,
                                broker_code="longbridge",
                                acquisition_date=trade_date_str,
                                acquisition_type="buy",
                                source_txn_id=None,
                                quantity=quantity,
                                cost_per_share=price,
                                currency="USD",
                            )

                        i += 1
                        continue

            ex_contract = exercise_contract_pattern.search(lines[i])
            if ex_contract:
                trade_date = ex_contract.group(1).replace(".", "-")
                compressed_sym = ex_contract.group(2)
                underlying = ex_contract.group(3)
                expiry = ex_contract.group(4)
                strike_str = ex_contract.group(5)
                cp = ex_contract.group(6)[0].upper()
                try:
                    strike = float(strike_str) / 1000 if int(strike_str) > 1000 else float(strike_str)
                except ValueError:
                    strike = 0.0
                opt_symbol = f"{underlying}_OPT_{expiry}_{_normalize_strike(strike)}_{cp}"

                lots = self.tax_lot_repo.get_available_lots(opt_symbol)
                if lots:
                    lot = lots[0]
                    self.tax_lot_repo.consume_lot_for_exercise(
                        lot_id=lot["id"],
                        exercise_txn_id=None,
                    )
                    print(f"    期权行权: {opt_symbol} (lot #{lot['id']} 消耗)")
                else:
                    print(f"    期权行权 WARNING: 找不到 lot for {opt_symbol}")
                i += 1
                continue

            exp_contract = expire_contract_pattern.search(lines[i])
            if exp_contract:
                trade_date = exp_contract.group(1).replace(".", "-")
                underlying = exp_contract.group(2)
                expiry = exp_contract.group(3)
                strike_raw = exp_contract.group(4)
                cp = exp_contract.group(5)[0].upper()
                expire_qty = float(exp_contract.group(6))
                try:
                    strike = float(strike_raw) / 1000 if int(strike_raw) > 1000 else float(strike_raw)
                except ValueError:
                    strike = 0.0
                opt_symbol = f"{underlying}_OPT_{expiry}_{_normalize_strike(strike)}_{cp}"

                lots = self.tax_lot_repo.get_available_lots(opt_symbol)
                consumed = 0
                total_cost_basis = 0.0

                remaining_needed = int(expire_qty)
                for lot in lots:
                    if remaining_needed <= 0:
                        break
                    consume = min(lot["remaining"], remaining_needed)
                    if consume > 0:
                        total_cost_basis += lot["cost_per_share"] * consume
                        consumed += consume
                        remaining_needed -= consume

                expire_txn_id = None
                if consumed > 0 or total_cost_basis > 0:
                    expire_txn_id = self.txn_repo.insert(
                        broker_code="longbridge",
                        trade_date=trade_date,
                        settlement_date=trade_date,
                        reference_no="",
                        symbol=opt_symbol,
                        company_name=_company_name(underlying),
                        exchange=None,
                        action="option_expire",
                        quantity=int(expire_qty),
                        price=0,
                        amount=0,
                        currency="USD",
                        statement_file_id=file_id,
                    )

                remaining_needed = int(expire_qty)
                for lot in lots:
                    if remaining_needed <= 0:
                        break
                    consume = min(lot["remaining"], remaining_needed)
                    if consume > 0:
                        self.tax_lot_repo.consume_lot_for_expiration(
                            lot_id=lot["id"],
                            expire_txn_id=expire_txn_id,
                        )
                        consumed += consume
                        remaining_needed -= consume
                if consumed > 0:
                    print(f"    期权到期: {opt_symbol} ({consumed} 合约消耗, 损失=${total_cost_basis:.2f})")
                else:
                    print(f"    期权到期 WARNING: 找不到 lot for {opt_symbol} (qty={expire_qty})")
                i += 1
                continue

            if m:
                trade_date_str = m.group(1).replace(".", "-")
                settle_date_str = m.group(2).replace(".", "-")
                ref_no = m.group(3)
                action_str = m.group(4)
                symbol = m.group(5)
                raw_symbol = symbol
                symbol = _normalize_lb_symbol(symbol)
                quantity_str = m.group(6)
                price_str = m.group(7)
                amount_str = m.group(8)
                net_str = m.group(9)

                quantity = int(float(_clean_num(quantity_str)))
                price = float(_clean_num(price_str))
                amount = float(_clean_num(amount_str))

                next_line = lines[i + 1] if i + 1 < len(lines) else ""
                combined_raw = raw_symbol + " " + next_line

                option_continuation = re.search(r"(\d{6})\s+([\d.]+)\s+(Call|Put)", next_line, re.IGNORECASE)

                is_option = (
                    bool(re.search(r"[A-Za-z]+\d?\d{6}[CP]\d*", symbol))
                    or "Call" in raw_symbol or "Put" in raw_symbol
                    or "call" in raw_symbol.lower() or "put" in raw_symbol.lower()
                    or option_continuation is not None
                )

                if is_option:
                    if option_continuation:
                        underlying = symbol.strip()
                        expiry = option_continuation.group(1)
                        strike_str = option_continuation.group(2)
                        cp = option_continuation.group(3)[0].upper()
                        try:
                            strike = float(strike_str)
                        except ValueError:
                            strike = 0.0
                        txn_symbol = f"{underlying}_OPT_{expiry}_{_normalize_strike(strike)}_{cp}"
                    elif re.search(r"[A-Za-z]+\d?\d{6}[CP]\d*", symbol):
                        option_match = re.search(r"([A-Za-z]+\d?)(\d{6})([CP])(\d*)", symbol)
                        if option_match:
                            underlying = _normalize_option_underlying(option_match.group(1))
                            expiry = option_match.group(2)
                            cp_type = option_match.group(3)
                            strike_raw = option_match.group(4)
                            try:
                                strike = float(strike_raw) / 1000 if strike_raw and int(strike_raw) > 1000 else float(strike_raw) if strike_raw else 0.0
                            except ValueError:
                                strike = 0.0
                            txn_symbol = f"{underlying}_OPT_{expiry}_{_normalize_strike(strike)}_{cp_type}"
                        else:
                            txn_symbol = symbol.replace(" ", "_")
                    else:
                        txn_symbol = symbol.replace(" ", "_")
                    action = "option_buy" if "买" in action_str else "option_sell"
                else:
                    txn_symbol = symbol
                    action = "buy" if "买" in action_str else "sell"

                fees = defaultdict(float)
                j = i + 1
                while j < len(lines) and j < i + 20:
                    fee_line = lines[j]
                    for fee_name, fee_re in fee_patterns.items():
                        fee_match = re.search(fee_re, fee_line)
                        if fee_match:
                            fees[fee_name] = float(fee_match.group(1))
                    if trade_pattern.search(fee_line):
                        break
                    j += 1

                self.txn_repo.insert(
                    broker_code="longbridge",
                    trade_date=trade_date_str,
                    settlement_date=settle_date_str,
                    reference_no=ref_no,
                    symbol=txn_symbol,
                    company_name=_company_name(symbol),
                    exchange=_infer_exchange(txn_symbol),
                    action=action,
                    quantity=quantity,
                    price=price,
                    amount=abs(amount),
                    commission=fees.get("佣金", 0),
                    platform_fee=fees.get("平台费", 0),
                    delivery_fee=fees.get("交收费", 0),
                    sec_fee=fees.get("sec_fee", 0),
                    taf_fee=fees.get("taf_fee", 0),
                    fee_breakdown=dict(fees),
                    currency="USD",
                    statement_file_id=file_id,
                )

                if action in ("buy", "option_buy"):
                    self.tax_lot_repo.add_lot(
                        symbol=txn_symbol,
                        acquisition_date=trade_date_str,
                        acquisition_type="buy",
                        quantity=quantity,
                        cost_per_share=price,
                        currency="USD",
                        broker_code="longbridge",
                    )

            i += 1

    def _parse_dividends(self, text: str, file_id: int, month_str: str):
        year = month_str.split("-")[0]
        lines = text.split("\n")

        # Pattern 0: dividend with amount on same line, Held on next line (most common)
        # e.g. "2025.12.19 现金分红 TSLL.US Cash Dividend: 0.57939 USD per Share , 2,387.15"
        div_patterns = [
            re.compile(
                r"(\d{4}\.\d{2}\.\d{2})\s+现[⾦金金]分红\s+(\w+)\.US\s+Cash Dividend:\s*([\d.]+)\s+USD per Share\s*,\s+([\d,.]+)"
            ),
            re.compile(
                r"(\d{4}\.\d{2}\.\d{2})\s+现[⾦金金]分红\s+(\w+)\([^)]*\)\s+Payment in Lieu of Dividend\s+([\d.]+)\s*\([^)]*\)\s*,\s*Held:(\d+)"
            ),
        ]

        held_pattern = re.compile(r"Held:\s*(\d+)")
        tax_fee_pattern = re.compile(
            r"(\d{4}\.\d{2}\.\d{2})\s+公司⾏动其他费[⽤用]\s+(\w+)\.US\s+.*?\s+(-[\d.]+)\s*$"
        )

        results = []

        for i, line in enumerate(lines):
            matched = False
            for pat_idx, pat in enumerate(div_patterns):
                m = pat.search(line)
                if m:
                    date_str = m.group(1).replace(".", "-")
                    symbol = m.group(2)
                    per_share = float(m.group(3))

                    if pat_idx == 0:
                        amount = float(_clean_num(m.group(4)))
                        shares = int(amount / per_share) if per_share > 0 else 0
                        # Look at next line for Held
                        if i + 1 < len(lines):
                            held_m = held_pattern.search(lines[i + 1])
                            if held_m:
                                shares = int(held_m.group(1))
                    else:
                        shares = int(m.group(4))
                        amount = float(m.group(3))

                    # Detect withholding tax: look for "公司行动其他费用" line
                    # within next 10 lines for the same symbol, followed by "Withholding"
                    withholding_tax = 0.0
                    for j in range(i, min(len(lines), i + 10)):
                        tax_m = tax_fee_pattern.search(lines[j])
                        if tax_m and tax_m.group(2) == symbol:
                            if j + 1 < len(lines) and "Withholding" in lines[j + 1]:
                                withholding_tax = abs(float(_clean_num(tax_m.group(3))))
                                break

                    results.append({
                        "date": date_str,
                        "symbol": symbol,
                        "per_share": per_share,
                        "shares": shares,
                        "amount": amount,
                        "withholding_tax": withholding_tax,
                    })
                    matched = True
                    break

            if matched:
                continue

        seen = set()
        for r in sorted(results, key=lambda x: x["date"]):
            key = (r["date"], r["symbol"], r["amount"])
            if key in seen:
                continue
            seen.add(key)

            self.div_repo.insert(
                broker_code="longbridge",
                payment_date=r["date"],
                symbol=r["symbol"],
                company_name=_company_name(r["symbol"]),
                per_share_amount=r["per_share"],
                share_quantity=r["shares"],
                gross_amount=r["amount"],
                withholding_tax=r["withholding_tax"],
                withholding_rate=r["withholding_tax"] / r["amount"] if r["amount"] > 0 else 0,
                withholding_country="US",
                net_amount=r["amount"] - r["withholding_tax"],
                currency="USD",
                statement_file_id=file_id,
            )
            print(f"    分红: {r['symbol']} 每股${r['per_share']:.5f} × {r['shares']}股 = ${r['amount']:.2f} (预扣税: ${r['withholding_tax']:.2f})")

    def _parse_positions(self, text: str, file_id: int, month_str: str):
        month_num = int(month_str.split("-")[1])
        year_num = int(month_str.split("-")[0])

        pos_pattern = re.compile(
            r"^([一-鿿\w]+(?:\s*[一-鿿\w]+)*?)\s+"
            r"([\d,]+\.?\d*)\s+(-?[\d,]+\.?\d*)\s+([\d,]+\.?\d*)\s+"
            r"([\d,]+\.?\d*)\s+(-?[\d,]+\.?\d*)\s+"
            r"([\d,]+\.?\d*|N/A)\s+(-?[\d,]+\.?\d*)"
        )

        lines = text.split("\n")
        for line in lines:
            m = pos_pattern.search(line)
            if m:
                name_cn = m.group(1).strip()
                begin_qty = float(_clean_num(m.group(2)))
                change_qty = float(_clean_num(m.group(3)))
                end_qty = float(_clean_num(m.group(4)))
                cost = float(_clean_num(m.group(5)))
                total_cost = float(_clean_num(m.group(6)))
                closing_price_str = m.group(7)
                pnl = float(_clean_num(m.group(8)))

                if end_qty <= 0:
                    continue

                symbol = self._reverse_lookup_symbol(name_cn)
                if not symbol:
                    continue

                closing_price = float(_clean_num(closing_price_str)) if closing_price_str != "N/A" else None

                import calendar
                last_day = calendar.monthrange(year_num, month_num)[1]
                end_date = f"{year_num}-{month_num:02d}-{last_day}"

                self.pos_repo.upsert(
                    broker_code="longbridge",
                    as_of_date=end_date,
                    symbol=symbol,
                    company_name=name_cn,
                    quantity=int(end_qty),
                    avg_cost=cost,
                    closing_price=closing_price,
                    market_value=abs(total_cost),
                    unrealized_pnl=pnl,
                    currency="USD",
                    statement_file_id=file_id,
                )

    def _reverse_lookup_symbol(self, name_cn: str) -> str:
        mapping = {
            "博通": "AVGO",
            "谷歌": "GOOGL",
            "英伟达": "NVDA",
            "拼多多": "PDD",
            "台积电": "TSM",
            "富时中国": "YINN",
        }
        for cn_name, symbol in mapping.items():
            if cn_name in name_cn:
                return symbol
        if "Oklo" in name_cn:
            return "Oklo"
        if "BABA" in name_cn or "阿里巴巴" in name_cn:
            return "BABA"
        return ""
