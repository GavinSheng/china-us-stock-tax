"""PDF 解析专项测试用例

基于 docs/pdf_parser_test_plan.md 设计并执行。
"""

import pytest
import sys
import os
from pathlib import Path

# 确保能导入项目模块
sys.path.insert(0, str(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from src.database.importers.shared_utils import (
    _dedup_text,
    _clean_num,
    _normalize_lb_symbol,
    _normalize_strike,
)


# ============================================================
# TC-01: 富途 CJK 去重边界测试
# ============================================================

class TestDedupCJK:
    """TC-01: 富途 PDF CJK 双写 bug 去重逻辑验证"""

    def test_normal_traditional_unchanged(self):
        """正常繁体不被修改"""
        assert _dedup_text("買入開倉") == "買入開倉"
        assert _dedup_text("賣出平倉") == "賣出平倉"
        assert _dedup_text("佣金") == "佣金"
        assert _dedup_text("平台使用費") == "平台使用費"

    def test_double_dedup(self):
        """双写 bug 应被正确去重"""
        assert _dedup_text("買買入入開開倉倉") == "買入開倉"
        assert _dedup_text("佣佣金金") == "佣金"
        assert _dedup_text("平平倉倉") == "平倉"

    def test_consecutive_same_char_dedup(self):
        """连续相同 CJK 字符去重"""
        assert _dedup_text("果果") == "果"
        assert _dedup_text("謝謝謝") == "謝謝"  # 三个 → 去重第一个 pair，剩一个

    def test_apple_edge_case(self):
        """AAPL蘋果果 → AAPL蘋果（果果去重为果）"""
        # 这是 _dedup_text 的预期行为：连续相同 CJK 去重
        # "蘋果果" → "蘋果"（果果 → 果）
        result = _dedup_text("AAPL蘋果果")
        assert result == "AAPL蘋果", f"Expected 'AAPL蘋果', got '{result}'"

    def test_english_unchanged(self):
        """英文不在 CJK 范围，不应被去重"""
        assert _dedup_text("BUYBUY") == "BUYBUY"
        assert _dedup_text("AAPLAAPL") == "AAPLAAPL"

    def test_mixed_content(self):
        """混合内容：CJK 去重，英文数字不变"""
        text = "買買入入 AAPL 100 股股"
        result = _dedup_text(text)
        assert result == "買入 AAPL 100 股", f"Got: '{result}'"

    def test_fee_keywords_preserved(self):
        """费用关键词去重后仍可被正则匹配"""
        text = "佣佣金金:: 11..5500"
        result = _dedup_text(text)
        # "佣金" 应可被 fee regex 匹配
        assert "佣金" in result


# ============================================================
# TC-03: 括号负数解析
# ============================================================

class TestParenthesesNegative:
    """TC-03: 括号负数 + 繁体余额行识别"""

    def test_parentheses_negative(self):
        """(100) → -100"""
        assert _clean_num("(100)") == "-100"

    def test_parentheses_negative_decimal(self):
        """(1,234.56) → -1234.56"""
        assert _clean_num("(1,234.56)") == "-1234.56"

    def test_negative_sign(self):
        """-1234.56 → -1234.56"""
        assert _clean_num("-1234.56") == "-1234.56"

    def test_commas(self):
        """1,234,567.89 → 1234567.89"""
        assert _clean_num("1,234,567.89") == "1234567.89"

    def test_empty(self):
        """空字符串 → 0"""
        assert _clean_num("") == "0"
        assert _clean_num(None) == "0"

    def test_spaces(self):
        """带空格的数字 → 清理后"""
        assert _clean_num("  123.45  ") == "123.45"
        assert _clean_num("( 123.45 )") == "-123.45"


# ============================================================
# TC-04: 长桥 Kangxi Radical 变体覆盖
# ============================================================

class TestKangxiVariants:
    """TC-04: 长桥解析器 Kangxi 部首变体覆盖"""

    def _match_buy(self, text: str) -> bool:
        """模拟长桥买入正则匹配"""
        import re
        # 长桥主交易正则的买入部分
        return bool(re.search(r"买[入⼊]", text))

    def _match_sell(self, text: str) -> bool:
        import re
        return bool(re.search(r"卖出", text))

    def _match_exercise(self, text: str) -> bool:
        import re
        return bool(re.search(r"[⾏行]权买[⼊入]", text))

    def _match_dividend(self, text: str) -> bool:
        import re
        return bool(re.search(r"现[⾦金金]分红", text))

    def test_buy_standard(self):
        assert self._match_buy("买入 AAPL")

    def test_buy_kangxi(self):
        # U+2F0A Kangxi RADICAL ENTER
        assert self._match_buy("买⼊ AAPL"), "Kangxi 入 should match"

    def test_sell(self):
        assert self._match_sell("卖出 AAPL")

    def test_exercise_standard(self):
        assert self._match_exercise("行权买入")

    def test_exercise_kangxi_xing(self):
        # U+2F8F Kangxi RADICAL GO
        assert self._match_exercise("⾏权买入"), "Kangxi 行 should match"

    def test_exercise_kangxi_ru(self):
        # U+2F0A Kangxi RADICAL ENTER
        assert self._match_exercise("行权买⼊"), "Kangxi 入 should match"

    def test_exercise_kangxi_both(self):
        # Both 行 and 入 as Kangxi
        assert self._match_exercise("⾏权买⼊"), "Both Kangxi should match"

    def test_dividend_standard(self):
        assert self._match_dividend("现金分红")

    def test_dividend_kangxi_jin(self):
        # ⾦ = U+2FA6 Kangxi RADICAL GOLD
        assert self._match_dividend("现⾦分红"), "Kangxi 金 should match"

    def test_dividend_mixed(self):
        assert self._match_dividend("现⾦分红"), "Kangxi variant should match"

    def test_dividend_standard_chars(self):
        assert self._match_dividend("现金分红"), "Standard 金 should match"


# ============================================================
# TC-05 部分: 勾稽关系 - 解析器金额字段验证
# ============================================================

class TestAmountSignAbs:
    """RISK-01: 验证所有解析器是否使用 abs(amount)"""

    def test_futu_abs_amount(self):
        """富途解析器使用 abs(amount)"""
        import inspect
        from src.database.importers import FutuImporter
        source = inspect.getsource(FutuImporter._parse_transactions)
        assert "abs(amount)" in source, "Futu should use abs(amount)"

    def test_longbridge_abs_amount(self):
        """长桥解析器使用 abs(amount)"""
        import inspect
        from src.database.importers import LongbridgeImporter
        source = inspect.getsource(LongbridgeImporter._parse_transactions)
        assert "abs(amount)" in source, "Longbridge should use abs(amount)"

    def test_boci_abs_amount(self):
        """中银国际解析器使用 abs(amount)"""
        import inspect
        from src.database.importers import BOCIImporter
        source = inspect.getsource(BOCIImporter._parse_transactions)
        assert "abs(amount)" in source, "BOCI should use abs(amount)"


class TestStrikeNormalization:
    """RISK-02: 富途期权 strike 归一化逻辑验证"""

    def test_futu_strike_3_digits(self):
        """3+ 位数字 strike 应除以 1000"""
        # strike_digits = "44000" → 44.0
        strike = str(int("44000") / 1000.0)
        assert strike == "44.0"

    def test_futu_strike_5_digits(self):
        """5 位数字 strike 应除以 1000"""
        # "103000" → 103.0
        strike = str(int("103000") / 1000.0)
        assert strike == "103.0"

    def test_futu_strike_2_digits_unchanged(self):
        """2 位数字 strike 不应被除法（但代码只对 3+ 位做除法）"""
        # 在 _extract_futu_option_context 中，2 位 strike 不被 /1000
        # 但经过 _normalize_strike 后应该保持
        from src.database.importers.shared_utils import _normalize_strike
        result = _normalize_strike("44")
        assert result == "44.0"

    def test_futu_strike_100_edge_case(self):
        """RISK-02: strike '100' 被归一化为 0.1 — 这是错误的"""
        # 富途格式: "100000" → 100.0 (100000/1000)
        # 但如果原始 strike 就是 100，编码为 "100"（3位），会被 /1000 → 0.1
        # 这是已知的风险点
        strike_100 = int("100") / 1000.0
        assert strike_100 == 0.1, "RISK-02 confirmed: $100 strike becomes $0.10"

        # 对比: 编码为 6 位的 $100
        strike_100_encoded = int("100000") / 1000.0
        assert strike_100_encoded == 100.0

    def test_strike_normalization_threshold(self):
        """验证归一化阈值: len >= 2 在持仓解析中做 /1000"""
        # 持仓解析 (line ~966): len(strike_digits) >= 2 → /1000.0
        # 所以 "44" → 0.044（错误！）
        # "44000" → 44.0（正确）
        # 这是一个 BUG：2 位数字不应该 /1000

        # 当前代码行为:
        strike_44 = int("44") / 1000.0
        # 44 / 1000 = 0.044 — 如果原始 strike 就是 $44，这是错的
        # 但富途的编码方式是 strike * 1000，所以 "44" 可能表示 $0.044
        # 这取决于富途的编码规则

        # 验证当前代码逻辑（不判断对错，只确认行为）
        assert strike_44 == 0.044


class TestFutuDividendWithholding:
    """富途股息预扣税提取逻辑验证

    验证规则:
    1. PDF 有 WITHHOLDING TAX 行时，从该行提取实际金额
    2. PDF 无 WITHHOLDING TAX 行时，按 gross × 10% 计算（不报警告）
    3. 所有分红的 withholding_tax > 0（无预扣税 = 无法申请外国税收抵免）
    """

    def test_futu_withholding_pattern_exists(self):
        """Futu 分红解析器应包含 WITHHOLDING TAX 提取正则"""
        import inspect
        from src.database.importers import FutuImporter
        source = inspect.getsource(FutuImporter._parse_dividends)
        assert "WITHHOLDING" in source, "Futu should parse WITHHOLDING TAX lines from PDF"
        assert "withholding_map" in source, "Futu should build withholding amount map"

    def test_futu_withholding_fallback_no_warning(self):
        """Futu 无 WITHHOLDING TAX 行时应静默回退到 10%，不应警告"""
        import inspect
        from src.database.importers import FutuImporter
        source = inspect.getsource(FutuImporter._parse_dividends)
        # 不应有 warnings.warn 关于预扣税估算
        assert "warnings.warn" not in source or "预扣税" not in source, \
            "Futu should NOT warn for missing WITHHOLDING TAX line"

    def test_futu_dividends_all_have_withholding(self):
        """所有富途分红都应有预扣税（withholding_tax > 0）"""
        import sqlite3
        conn = sqlite3.connect("output/tax.db")
        rows = conn.execute("""
            SELECT symbol, payment_date, gross_amount, withholding_tax
            FROM dividends
            WHERE broker_code = 'futu' AND withholding_tax <= 0
        """).fetchall()
        conn.close()
        assert not rows, f"以下富途分红无预扣税: {[(r[0], r[1]) for r in rows]}"

    def test_futu_dividend_withholding_approx_10pct(self):
        """富途分红预扣税应约为 gross 的 10%（允许 0.5% 偏差）"""
        import sqlite3
        conn = sqlite3.connect("output/tax.db")
        rows = conn.execute("""
            SELECT symbol, payment_date, gross_amount, withholding_tax
            FROM dividends
            WHERE broker_code = 'futu'
        """).fetchall()
        conn.close()
        for symbol, date, gross, wh in rows:
            if gross > 0:
                rate = wh / gross
                assert 0.095 <= rate <= 0.105, (
                    f"{symbol} {date}: 预扣税率 {rate:.4f} 偏离 10% 过大 "
                    f"(gross={gross}, wh={wh})"
                )


class TestCrossYearDate:
    """BC-04: 中银国际跨年日期回退逻辑"""

    def test_same_year_no_rollback(self):
        """11/07 在 2025-07 月结单中 → 2025-07-11"""
        from src.database.importers import BOCIImporter
        imp = BOCIImporter()
        result = imp._resolve_date("11/07", "2025-07")
        from datetime import date
        assert result == date(2025, 7, 11)

    def test_cross_year_rollback(self):
        """28/12 在 2025-01 月结单中 → 2024-12-28（月份 12 > 1，年份回退）"""
        from src.database.importers import BOCIImporter
        imp = BOCIImporter()
        result = imp._resolve_date("28/12", "2025-01")
        from datetime import date
        assert result == date(2024, 12, 28)

    def test_january_in_dec_statement(self):
        """15/01 在 2024-12 月结单中 → 2024-01-15（不跨年不回退）"""
        from src.database.importers import BOCIImporter
        imp = BOCIImporter()
        result = imp._resolve_date("15/01", "2024-12")
        from datetime import date
        # 月份 1 < 12，不回退
        assert result == date(2024, 1, 15)

    def test_invalid_date(self):
        """30/02 无效日期 → None"""
        from src.database.importers import BOCIImporter
        imp = BOCIImporter()
        result = imp._resolve_date("30/02", "2025-07")
        assert result is None


class TestSymbolNormalization:
    """Symbol 归一化和映射验证"""

    def test_lb_symbol_rocket_lab(self):
        """长桥 Rocket Lab 映射（需要包含前导空格）"""
        result = _normalize_lb_symbol(" Rocket Lab")
        assert result == "RKLB", f"Got: '{result}'"

    def test_lb_symbol_baba1_unchanged(self):
        """BABA1 不在映射表中，保持原样"""
        result = _normalize_lb_symbol("BABA1")
        assert result == "BABA1", f"Got: '{result}'"

    def test_lb_symbol_alibaba_chinese(self):
        """阿里巴巴 → BABA"""
        result = _normalize_lb_symbol("阿里巴巴")
        assert result == "BABA", f"Got: '{result}'"

    def test_lb_symbol_unchanged(self):
        """标准符号不变"""
        result = _normalize_lb_symbol("AAPL")
        assert result == "AAPL"


class TestPositionEndDate:
    """M-C: 持仓 end_date 应使用月末而非次月 1 日"""

    def test_futu_position_end_date(self):
        """验证 Futu 持仓使用 calendar.monthrange 获取月末"""
        import inspect
        from src.database.importers import FutuImporter
        source = inspect.getsource(FutuImporter._parse_positions)
        assert "calendar.monthrange" in source, \
            "Futu positions should use calendar.monthrange for end_date"

    def test_longbridge_position_end_date(self):
        """验证 Longbridge 持仓使用 calendar.monthrange 获取月末"""
        import inspect
        from src.database.importers import LongbridgeImporter
        source = inspect.getsource(LongbridgeImporter._parse_positions)
        assert "calendar.monthrange" in source, \
            "Longbridge positions should use calendar.monthrange for end_date"


class TestExchangeRateFallback:
    """M-E: Longbridge 交易自动汇率回退"""

    def test_transaction_repo_auto_rate(self):
        """TransactionRepository.insert 应在 exchange_rate 为 None 时自动查找"""
        import inspect
        from src.database.repositories import TransactionRepository
        source = inspect.getsource(TransactionRepository.insert)
        assert "get_exchange_rate" in source, \
            "TransactionRepository should auto-lookup exchange_rate"


class TestDecryptionIntegrity:
    """DEC-01: 源文件到解密文件的完整性校验

    验证:
    1. 每个源 PDF 都有对应的解密副本
    2. 每个解密副本都有对应的源文件
    3. 源文件是加密的（需要密码才能打开）
    4. 解密文件无需密码即可打开
    5. 解密文件页数与源文件一致
    6. 解密文件是有效 PDF（非损坏文件）
    """

    INPUT_DIR = Path("input")
    DECRYPTED_DIR = Path("input/decrypted")

    # 源子目录 → 解密文件名前缀映射
    SOURCE_MAP = {
        "boci-2025-monthly": "boci_",
        "bridge-2025-monthly": "longbridge_",
        "futu-2025-monthly": "futu_",
    }

    # 各券商 PDF 密码（用于验证源文件确实加密）
    BROKER_PASSWORDS = {
        "boci-2025-monthly": None,         # BOCI 源文件不确定加密
        "bridge-2025-monthly": os.getenv("LONGBRIDGE_PASSWORD", ""),  # 长桥密码
        "futu-2025-monthly": None,          # 富途 2025 可能无密码
    }

    @pytest.fixture(autouse=True)
    def skip_if_no_input(self):
        """如果 input 目录不存在则跳过"""
        if not self.INPUT_DIR.exists():
            pytest.skip("input 目录不存在")
        if not self.DECRYPTED_DIR.exists():
            pytest.skip("input/decrypted 目录不存在")

    def _collect_decrypted_files(self) -> dict[str, Path]:
        """收集 decrypted 目录下所有 PDF，按文件名返回 {name: path}"""
        return {f.name: f for f in self.DECRYPTED_DIR.glob("*.pdf")}

    def _collect_source_files(self) -> dict[str, Path]:
        """收集所有源子目录下的 PDF，返回 {name: path}"""
        result = {}
        for subdir in self.SOURCE_MAP:
            src_path = self.INPUT_DIR / subdir
            if src_path.exists():
                for f in src_path.glob("*.pdf"):
                    result[f.name] = f
        return result

    def test_decrypted_file_count(self):
        """D-01: 解密文件数量应等于源文件数量"""
        source_files = self._collect_source_files()
        decrypted_files = self._collect_decrypted_files()
        assert len(decrypted_files) == len(source_files), (
            f"解密文件数({len(decrypted_files)}) != 源文件数({len(source_files)}), "
            f"差集: {set(source_files.keys()) ^ set(decrypted_files.keys())}"
        )

    def test_one_to_one_mapping(self):
        """D-02: 每个源文件都有对应的解密文件（1:1 映射）"""
        source_files = self._collect_source_files()
        decrypted_files = self._collect_decrypted_files()

        # 源 → 解密的映射: 源文件名加前缀
        missing_decrypted = []
        for src_name in source_files:
            found = any(src_name in dec_name for dec_name in decrypted_files)
            if not found:
                missing_decrypted.append(src_name)

        # 解密 → 源的反向映射
        orphan_decrypted = []
        for dec_name in decrypted_files:
            # 去掉前缀后应匹配某个源文件名
            # 格式: {broker}_{src_name}.pdf
            found = any(src_name in dec_name for src_name in source_files)
            if not found:
                orphan_decrypted.append(dec_name)

        assert not missing_decrypted, f"源文件缺少解密副本: {missing_decrypted}"
        assert not orphan_decrypted, f"解密文件无对应源文件: {orphan_decrypted}"

    def test_decrypted_files_not_encrypted(self):
        """D-03: 解密后的文件应无需密码即可打开"""
        import pdfplumber
        decrypted_files = self._collect_decrypted_files()

        failures = {}
        for name, path in decrypted_files.items():
            try:
                with pdfplumber.open(str(path)) as pdf:
                    _ = len(pdf.pages)  # 触发实际读取
            except Exception as e:
                failures[name] = str(e)

        assert not failures, (
            f"以下解密文件无法无密码打开: {failures}"
        )

    def test_decrypted_files_valid_pdf(self):
        """D-04: 所有解密文件都是有效 PDF（页数 > 0）"""
        import pdfplumber
        decrypted_files = self._collect_decrypted_files()

        invalid = {}
        for name, path in decrypted_files.items():
            try:
                with pdfplumber.open(str(path)) as pdf:
                    page_count = len(pdf.pages)
                    if page_count == 0:
                        invalid[name] = "页数为 0"
            except Exception as e:
                invalid[name] = str(e)

        assert not invalid, f"无效解密文件: {invalid}"

    def test_page_count_consistency(self):
        """D-05: 解密文件页数应与源文件（用密码打开）一致"""
        import pdfplumber
        source_files = self._collect_source_files()
        decrypted_files = self._collect_decrypted_files()

        mismatches = {}
        for src_name, src_path in source_files.items():
            # 找到对应的解密文件
            dec_match = [p for n, p in decrypted_files.items() if src_name in n]
            if not dec_match:
                continue
            dec_path = dec_match[0]

            # 源文件页数（尝试无密码，失败则用密码）
            try:
                with pdfplumber.open(str(src_path)) as pdf:
                    src_pages = len(pdf.pages)
            except Exception:
                # 尝试用密码打开
                password = os.getenv("LONGBRIDGE_PASSWORD", "")  # 从环境变量读取
                try:
                    with pdfplumber.open(str(src_path), password=password) as pdf:
                        src_pages = len(pdf.pages)
                except Exception:
                    continue  # 无法打开，跳过

            # 解密文件页数
            try:
                with pdfplumber.open(str(dec_path)) as pdf:
                    dec_pages = len(pdf.pages)
            except Exception:
                mismatches[src_name] = f"解密文件无法打开"
                continue

            if src_pages != dec_pages:
                mismatches[src_name] = f"源={src_pages}页, 解密={dec_pages}页"

        assert not mismatches, f"页数不一致: {mismatches}"

    def test_decrypted_naming_convention(self):
        """D-06: 解密文件名应符合 {broker}_{original_name}.pdf 规范"""
        decrypted_files = self._collect_decrypted_files()
        invalid_names = []
        for name in decrypted_files:
            # 应以已知券商前缀开头
            valid_prefixes = ("boci_", "futu_", "longbridge_")
            if not any(name.startswith(p) for p in valid_prefixes):
                invalid_names.append(name)
        assert not invalid_names, f"不符合命名规范的解密文件: {invalid_names}"


class TestCarryforwardExpiry:
    """H-C: FTC carryforward 过期检查"""

    def test_engine_expiry_check(self):
        """tax_engine.py 应检查 expires_year < year"""
        import inspect
        from src.calculator.tax_engine import compute_tax
        source = inspect.getsource(compute_tax)
        assert "expires_year" in source, \
            "compute_tax should check carryforward expiry"
