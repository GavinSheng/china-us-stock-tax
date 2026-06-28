"""富途月结单端到端解析测试

验证 parser 从真实 PDF 中提取的数据是否与 PDF 原文一致。
Ground truth 直接从 PDF 文本行正则提取，不依赖数据库。
"""

import pytest
import sys
import os
import re
import tempfile
import hashlib
import sqlite3

sys.path.insert(0, str(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pdfplumber
from pathlib import Path
from src.database import init_db
from src.database.importers import FutuImporter
from src.database.importers.shared_utils import _dedup_text, _normalize_strike


# ============================================================
# Ground Truth 提取（从 PDF 原文直接提取）
# ============================================================

def _extract_gt_transactions(pdf_text: str) -> list[dict]:
    """从 PDF 原文直接提取交易 ground truth

    匹配行格式:
        賣出平倉 XPEV250221C1 USD 25 2.4000 6,000.00 5,970.71
        買入開倉 SMCI(超微電腦) USD 136 54.6900 7,437.84 -7,440.24
        買入開倉 TQQQ(3倍做多 USD 135 87.9350 11,871.23 -11,873.63
    """
    results = []
    for line in pdf_text.split('\n'):
        # 格式A: 買入開倉 SYMBOL(name) CURRENCY QTY PRICE AMOUNT CHANGE
        m = re.match(
            r"(買入開倉|賣出平倉|買入|賣出)\s+(\S+)\s+(USD|HKD|CNY)\s+"
            r"([\d,]+\.?\d*)\s+([\d,]+\.?\d+)\s+(-?[\d,]+\.?\d+)",
            line.strip()
        )
        if m:
            direction, symbol_raw, currency, qty_str, price_str, amount_str = m.groups()

            # 提取纯 ticker（第一个连续字母部分）
            sym_match = re.match(r"([A-Za-z]+)", symbol_raw)
            symbol = sym_match.group(1) if sym_match else symbol_raw

            qty = int(qty_str.replace(',', ''))
            price = float(price_str.replace(',', ''))
            amount = abs(float(amount_str.replace(',', '')))

            # 检测是否为期权（符号含6位日期+CP标识）
            is_option = bool(re.match(r"[A-Za-z]+\d{6}", symbol_raw))

            # 期权交易 action 前缀为 option_buy/option_sell
            if is_option:
                action = 'option_buy' if '買入' in direction else 'option_sell'
            else:
                action = 'buy' if '買入' in direction else 'sell'

            results.append({
                'action': action,
                'direction_raw': direction,
                'symbol_raw': symbol_raw,
                'symbol': symbol,
                'is_option': is_option,
                'currency': currency,
                'quantity': qty,
                'price': price,
                'amount': amount,
            })
    return results


def _extract_gt_expires(pdf_text: str) -> list[dict]:
    """从 PDF 原文提取期权到期 ground truth

    匹配行格式:
        2025/02/28 減少 期權行權 NVDA250228C140000(NVDA USD -16 0.00 Opt EXP-NA-
        2025/02/28 減少 期權行權 YINN250228C46000(YINN 250228 USD -8 0.00 Opt EXP-NA-
    """
    results = []
    for line in pdf_text.split('\n'):
        m = re.match(
            r"(\d{4}/\d{2}/\d{2})\s+減少\s+期權行權\s+"
            r"([A-Za-z]+\d{6}[CP]\d+)\s*\("
            r"([A-Za-z]+)\s*(?:\d{6})?\s*USD\s+"
            r"(-[\d,]+\.?\d*)\s+([\d.]+)",
            line.strip()
        )
        if m:
            date_str = m.group(1).replace("/", "-")
            raw_symbol = m.group(2)
            underlying = m.group(3)
            qty = abs(int(float(m.group(4).replace(',', ''))))
            amount = float(m.group(5))

            if amount == 0.0:
                opt_m = re.match(r"([A-Za-z]+)(\d{6})([CP])(\d+)", raw_symbol)
                if opt_m:
                    expiry = opt_m.group(2)
                    cp = opt_m.group(3)
                    strike = str(int(opt_m.group(4)) / 1000.0)
                    symbol = f"{underlying}_OPT_{expiry}_{strike}_{cp}"
                    results.append({
                        'date': date_str,
                        'symbol': symbol,
                        'quantity': qty,
                    })
    return results


# ============================================================
# E2E 测试基类
# ============================================================

class FutuE2ETestBase:
    """富途 E2E 测试基类"""

    PDF_PATH: str = ""
    MONTH: str = ""

    def _read_pdf_text(self) -> str:
        """读取 PDF 文本并应用富途 CJK 去重"""
        with pdfplumber.open(self.PDF_PATH) as pdf:
            text = '\n'.join(p.extract_text() or '' for p in pdf.pages)
        return _dedup_text(text)

    def _parse_to_temp_db(self) -> tuple[list[dict], list[dict], list[dict]]:
        """解析 PDF 到临时 DB，返回 (transactions, positions, dividends)"""
        tmp = tempfile.mktemp(suffix='.db')
        try:
            init_db(tmp)

            pdf_path = Path(self.PDF_PATH)
            importer = FutuImporter(db_path=tmp)

            full_text = self._read_pdf_text()

            file_id = importer.stmt_repo.insert(
                broker_code='futu', file_path=str(pdf_path),
                statement_month=self.MONTH, page_count=0
            )

            # 直接调用解析（跳过 PDF 读取，因为我们已有文本）
            importer.txn_repo.delete_by_statement_file(file_id)
            importer.div_repo.delete_by_statement_file(file_id)
            importer.pos_repo.delete_by_statement_file(file_id)
            importer._parse_transactions(full_text, file_id, self.MONTH)
            importer._parse_dividends(full_text, file_id, self.MONTH)
            importer._parse_positions(full_text, file_id, self.MONTH)

            # 从 DB 读回
            conn = sqlite3.connect(tmp)
            conn.row_factory = sqlite3.Row
            txns = [dict(r) for r in conn.execute(
                'SELECT * FROM transactions ORDER BY id'
            ).fetchall()]
            positions = [dict(r) for r in conn.execute(
                'SELECT * FROM positions ORDER BY id'
            ).fetchall()]
            divs = [dict(r) for r in conn.execute(
                'SELECT * FROM dividends ORDER BY id'
            ).fetchall()]
            conn.close()

            return txns, positions, divs
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


# ============================================================
# TestFutu202502: 2025-02 月结单（33 笔买卖交易 + 3 笔期权到期）
# ============================================================

class TestFutu202502(FutuE2ETestBase):
    PDF_PATH = "input/decrypted/futu_XXXXXXXXXXXXXXX-6-20250228-1779555702898.pdf"
    MONTH = "2025-02"

    def test_transaction_count_matches_pdf(self):
        """交易笔数应与 PDF 原文一致（买卖 + 到期）"""
        pdf_text = self._read_pdf_text()
        gt_txns = _extract_gt_transactions(pdf_text)
        gt_expires = _extract_gt_expires(pdf_text)
        parsed_txns, _, _ = self._parse_to_temp_db()

        expected_total = len(gt_txns) + len(gt_expires)
        assert len(parsed_txns) == expected_total, (
            f"交易数不匹配: parser={len(parsed_txns)}, PDF原文买卖={len(gt_txns)}, PDF原文到期={len(gt_expires)}, 合计={expected_total}"
        )

    def test_all_transactions_match_pdf_ground_truth(self):
        """逐笔验证: parser 买卖输出与 PDF 原文交易行匹配（不含到期）"""
        pdf_text = self._read_pdf_text()
        gt_txns = _extract_gt_transactions(pdf_text)
        parsed_txns, _, _ = self._parse_to_temp_db()

        # 只比对买卖交易
        buy_sell_parsed = [t for t in parsed_txns if t['action'] not in ('option_expire',)]

        assert len(buy_sell_parsed) == len(gt_txns), (
            f"买卖交易数不匹配: parser={len(buy_sell_parsed)}, PDF原文={len(gt_txns)}"
        )

        # 逐笔比对
        for i, (gt, parsed) in enumerate(zip(gt_txns, buy_sell_parsed)):
            assert parsed['action'] == gt['action'], (
                f"交易#{i}: action 不匹配: parser={parsed['action']}, PDF={gt['action']} "
                f"(原文: {gt['direction_raw']} {gt['symbol_raw']})"
            )
            assert parsed['quantity'] == gt['quantity'], (
                f"交易#{i}: quantity 不匹配: parser={parsed['quantity']}, PDF={gt['quantity']}"
            )
            assert abs(parsed['price'] - gt['price']) < 0.01, (
                f"交易#{i}: price 不匹配: parser={parsed['price']}, PDF={gt['price']}"
            )
            assert abs(parsed['amount'] - gt['amount']) < 0.01, (
                f"交易#{i}: amount 不匹配: parser={parsed['amount']}, PDF={gt['amount']}"
            )
            assert parsed['currency'] == gt['currency'], (
                f"交易#{i}: currency 不匹配: parser={parsed['currency']}, PDF={gt['currency']}"
            )

    def test_no_extra_transactions(self):
        """parser 不应产生 PDF 原文不存在的买卖交易"""
        pdf_text = self._read_pdf_text()
        gt_txns = _extract_gt_transactions(pdf_text)
        parsed_txns, _, _ = self._parse_to_temp_db()

        buy_sell_parsed = [t for t in parsed_txns if t['action'] not in ('option_expire',)]

        # 每个 parser 买卖交易都应对应一条 PDF 原文交易行
        for parsed in buy_sell_parsed:
            found = any(
                p['symbol'] == g['symbol'] and
                p['action'] == g['action'] and
                p['quantity'] == g['quantity'] and
                abs(p['price'] - g['price']) < 0.01
                for p, g in zip(buy_sell_parsed, gt_txns)
            )
            assert found, f"Parser 产生多余交易: {parsed['symbol']} {parsed['action']} qty={parsed['quantity']}"

    def test_option_count(self):
        """期权交易数量（含到期）"""
        pdf_text = self._read_pdf_text()
        gt_txns = _extract_gt_transactions(pdf_text)
        gt_expires = _extract_gt_expires(pdf_text)
        parsed_txns, _, _ = self._parse_to_temp_db()

        gt_options = sum(1 for g in gt_txns if g['is_option']) + len(gt_expires)
        parsed_options = sum(1 for t in parsed_txns if 'option' in t['action'])

        assert parsed_options == gt_options, (
            f"期权数不匹配: parser={parsed_options}, PDF原文={gt_options}"
        )

    def test_option_expire_transactions(self):
        """期权到期交易应与 PDF 原文一致"""
        pdf_text = self._read_pdf_text()
        gt_expires = _extract_gt_expires(pdf_text)
        parsed_txns, _, _ = self._parse_to_temp_db()

        parsed_expires = [t for t in parsed_txns if t['action'] == 'option_expire']
        assert len(parsed_expires) == len(gt_expires), (
            f"期权到期数不匹配: parser={len(parsed_expires)}, PDF原文={len(gt_expires)}"
        )

        # 逐笔比对
        gt_map = {g['symbol']: g['quantity'] for g in gt_expires}
        for p in parsed_expires:
            assert p['symbol'] in gt_map, f"Parser 产生多余到期: {p['symbol']}"
            assert p['quantity'] == gt_map[p['symbol']], (
                f"到期 #{p['symbol']}: qty={p['quantity']}, PDF={gt_map[p['symbol']]}"
            )

    def test_stock_count(self):
        """股票交易数量"""
        pdf_text = self._read_pdf_text()
        gt_txns = _extract_gt_transactions(pdf_text)
        parsed_txns, _, _ = self._parse_to_temp_db()

        gt_stocks = sum(1 for g in gt_txns if not g['is_option'])
        parsed_stocks = sum(1 for t in parsed_txns if t['action'] in ('buy', 'sell'))

        assert parsed_stocks == gt_stocks, (
            f"股票数不匹配: parser={parsed_stocks}, PDF原文={gt_stocks}"
        )

    def test_all_transactions_have_currency(self):
        """每笔交易都应提取币种"""
        parsed_txns, _, _ = self._parse_to_temp_db()
        for t in parsed_txns:
            assert t['currency'] in ('USD', 'HKD', 'CNY'), f"缺少币种: {t}"

    def test_all_transactions_have_fees(self):
        """每笔买卖交易都应提取费用（佣金 > 0）"""
        parsed_txns, _, _ = self._parse_to_temp_db()
        no_fee = [t for t in parsed_txns if t['action'] != 'option_expire' and t['commission'] <= 0]
        assert not no_fee, f"以下交易缺少费用: {[(t['symbol'], t['action']) for t in no_fee]}"

    def test_option_symbols_format(self):
        """期权 symbol 格式: TICKER_OPT_YYMMDD_STRIKE_C/P"""
        parsed_txns, _, _ = self._parse_to_temp_db()
        opt_pattern = re.compile(r"^[A-Z]+_OPT_\d{6}_\d+\.?\d*_[CP]$")
        for t in parsed_txns:
            if 'option' in t['action']:
                assert opt_pattern.match(t['symbol']), (
                    f"期权符号格式错误: {t['symbol']}"
                )

    def test_positions_exist(self):
        """期末持仓快照不为空"""
        _, positions, _ = self._parse_to_temp_db()
        assert len(positions) > 0, "期末持仓为空"

    def test_dividends_zero(self):
        """2025-02 月结单无分红"""
        _, _, divs = self._parse_to_temp_db()
        assert len(divs) == 0, f"2月不应有分红，实际: {len(divs)}"


# ============================================================
# 成交日 vs 结算日解析验证（基于真实 PDF 数据）
# ============================================================

def test_futu_trade_date_not_settlement_date():
    """验证：所有期权交易的 trade_date 取的是成交日（第一个日期），不是结算日（第二个日期）

    PDF exchange 行有两个日期：
        XCBO USD 2025/10/18 2025/10/20
         ^     ^    group(3)     group(4)
       exchange currency  成交日       结算日

    Bug 修复前：trade_date = group(4) = 结算日，导致 13 笔 OKLO 买入
    被记录到 10/20（到期日后），触发 FIFO 校验告警 CV-002。
    修复后：trade_date = group(3) = 成交日。

    本测试基于真实 PDF 导入结果验证。
    """
    db_path = 'output/tax.db'
    if not os.path.exists(db_path):
        pytest.skip("数据库 output/tax.db 不存在，请先导入月结单")

    conn = sqlite3.connect(db_path)
    # 查找 trade_date != settlement_date 的期权交易
    diff_rows = conn.execute('''
        SELECT id, trade_date, settlement_date, symbol, action
        FROM transactions
        WHERE trade_date != settlement_date
          AND symbol LIKE '%OPT_%'
        ORDER BY id
    ''').fetchall()
    conn.close()

    # 有差异的交易，trade_date 应早于 settlement_date
    for r in diff_rows:
        tid, trade_d, settle_d, symbol, action = r
        assert trade_d < settle_d, (
            f"id={tid} {symbol} {action}: trade_date {trade_d} 应早于 settlement_date {settle_d}"
        )


def test_futu_option_no_buy_after_expiry():
    """验证：没有期权买入发生在到期日之后

    Futu 账单采用 UTC 时区，美东盘中成交对应 UTC 已是次日凌晨。
    Bug 修复前：OKLO 13 笔买入 trade_date=10/20 > 到期日 10/17，
    形成「到期后买入」的逻辑矛盾。
    修复后：trade_date > expiry 时，锁定为到期日。
    """
    db_path = 'output/tax.db'
    if not os.path.exists(db_path):
        pytest.skip("数据库 output/tax.db 不存在，请先导入月结单")

    conn = sqlite3.connect(db_path)
    # 查找 option_buy，其 trade_date > 期权到期日的交易
    # 期权 symbol 格式: SYMBOL_OPT_YYMMDD_STRIKE_C/P
    rows = conn.execute('''
        SELECT id, trade_date, symbol
        FROM transactions
        WHERE action = 'option_buy'
          AND symbol LIKE '%OPT_%'
    ''').fetchall()

    import re
    bad_buys = []
    for tid, trade_d, symbol in rows:
        m = re.search(r'OPT_(\d{6})_', symbol)
        if m:
            expiry = f"20{m.group(1)[:2]}-{m.group(1)[2:4]}-{m.group(1)[4:]}"
            if trade_d > expiry:
                bad_buys.append((tid, trade_d, symbol, expiry))

    conn.close()

    assert len(bad_buys) == 0, (
        f"发现 {len(bad_buys)} 笔期权买入 trade_date > expiry: {bad_buys}"
    )
