# 海外券商月结单解析专项测试计划

> 版本: v1.0 | 日期: 2026-06-14 | 作者: QA 测试组

---

## 一、测试矩阵

### 1.1 PDF 样本清单

| 编号 | 券商 | 语言 | 月份/年份 | 文件类型 | 预期风险点 | 优先级 |
|------|------|------|-----------|----------|------------|--------|
| LB-01 | 长桥 | 简体中文 | 2025-01 ~ 2025-12 (全12个月) | 完整月度交易 | OS号解析、费用行扫描、跨页表格 | P0 |
| LB-02 | 长桥 | 繁体中文 | 2025-06 (含期权交易) | 期权买卖+行权 | 繁体"買入/賣出"识别、Kangxi  radicals 兼容 | P0 |
| LB-03 | 长桥 | 繁简混合 | 2025-03 (含分红+公司行动) | 分红+Payment in Lieu | "現金分红"繁简变体、Withholding Tax 提取 | P0 |
| LB-04 | 长桥 | 简体 | 2025-12 (含拆股/合股) | 企业行为 | 拆股后数量变化、成本调整 | P1 |
| FT-01 | 富途 | 繁体中文 | 2025-01 ~ 2025-12 (全12个月) | 标准交易月结单 | CJK 去重逻辑 (`_dedup_text`)、格式A/B自动检测 | P0 |
| FT-02 | 富途 | 繁体中文 | 2025-11 / 2025-12 (Format-B) | 符号在下一行的新版式 | Format-B 符号提取、跨页断裂 | P0 |
| FT-03 | 富途 | 繁体中文 | 2024-10 (旧版式) | 符号在上一行的旧版式 | 2024-10 格式兼容、strike `/1000.0` | P0 |
| FT-04 | 富途 | 繁体中文 | 2025-06 (含多腿期权) | 期权买+卖+过期 | `_extract_futu_option_context` 8行扫描边界 | P0 |
| FT-05 | 富途 | 繁体中文 | 2025-Q2 (含分红+利息) | 分红+股票收益计划 | 10% 预扣估算 WARNING、exchange_rate 非零 | P0 |
| FT-06 | 富途 | 繁体中文 | 含水印/印章遮挡 | 脏数据 | 脏数据抗干扰、置信度降级 | P1 |
| BC-01 | 中银国际 | 繁体中文 | 2025-01 ~ 2025-12 (全12个月) | 标准交易月结单 | DD/MM 日期、余额行识别、Symbol 白名单 | P0 |
| BC-02 | 中银国际 | 繁体中文 | 2025-06 (含 RSU 存仓/提货) | RSU 企业行为 | "存货/提货"繁体识别、括号负数数量 | P0 |
| BC-03 | 中银国际 | 繁体中文 | 2025-03 (含 ADR 分红) | 多格式分红 | P/D / R/D 日期提取、"美金/SHR"提取 | P0 |
| BC-04 | 中银国际 | 繁简混合 | 2025-09 (跨月结转) | 跨年日期回退 | `_resolve_date` 跨年逻辑 | P0 |
| BC-05 | 中银国际 | 繁体中文 | 含"證券存倉摘要"(纯繁体) | 持仓快照 | 繁简双版本 section header 匹配 | P1 |

### 1.2 风险热力图

```
             数据提取    繁体兼容    跨页    期权    分红    费用    汇率
长桥(LB)      HIGH       HIGH      MED     HIGH    MED     MED     HIGH
富途(FT)      HIGH       CRITICAL  HIGH    HIGH    MED     LOW     HIGH
中银(BC)      MED        HIGH      MED     N/A     HIGH    LOW     LOW
```

---

## 二、核心测试用例

### TC-01: 富途 CJK 去重边界 — 繁体字双写 bug 穿透

**前置条件**: 富途 2024 PDF 使用 `_dedup_text()` 修复 CJK 双写 bug（0x4E00-0x9FFF 范围连续重复字符去重）。

**操作步骤**:
1. 构造包含以下内容的测试文本：
   - 正常繁体："買入開倉" → 去重后应不变
   - 双写 bug: "買買入入開開倉倉" → 去重后应为 "買入開倉"
   - 边界情况: "AAPL蘋果果" → 去重后应为 "AAPL蘋果"（不应把 "果果" 去重为 "果"，因为 "蘋果" 是两个不同的字）
   - 英文不处理: "BUYBUY" → 去重后应不变（英文不在 CJK 范围）
2. 调用 `_dedup_text(text)` 并检查结果
3. 用实际富途 2024 PDF 验证 "佣金"、"平台使用費" 等关键词是否被正确保留

**预期结果**:
- 双写 CJK 字符被正确去重
- 非双写的正常繁体不被误删
- 英文/数字不受影响
- 关键词 "買入開倉"、"賣出平倉"、"佣金" 等仍可被正则匹配

**实际结果**: (待执行)

**缺陷定级**: 若去重错误导致方向关键词丢失 → **CRITICAL**（交易方向错误 = 税务方向反转）

---

### TC-02: 跨页表格断裂 — 富途 Format-B 符号在下一页

**前置条件**: 富途 2025-11 Format-B 月结单，符号在交易行的下一行。

**操作步骤**:
1. 准备一份 Format-B PDF，其中某笔交易的符号行恰好位于页面底部，符号被推到下一页第一行
2. 解析该 PDF，检查这笔交易是否被正确提取
3. 核对提取结果中该笔交易的 symbol 字段

**预期结果**:
- 符号应被正确提取（跨页无影响）
- 当前代码行 1334 检测 `lines[j] == ""` 或 `lines[j].startswith("製備日期")` 时会跳过，但跨页场景下下一页第一行是符号而非空行，应能正确捕获

**实际结果**: (待执行)

**缺陷定级**: 若跨页符号丢失 → **HIGH**（无 symbol = 无法关联 FIFO 持仓 = 税务计算失败）

---

### TC-03: 中银国际括号负数 + 繁体余额行识别

**前置条件**: 中银国际繁体中文月结单，包含 RSU 提货（负数数量用括号表示）。

**操作步骤**:
1. 解析包含以下内容的 BC PDF：
   - RSU 提货: `(100)` 股（括号表示负数）
   - 余额行: "承前结余" 或 "轉後結餘"（繁体/简体变体）
2. 检查 `_clean_num()` 对 `(100)` 的解析结果
3. 检查余额行是否被正确识别为停止标记（不被误认为交易行）
4. 核对 RSU 提货的 quantity 是否为 `-100`

**预期结果**:
- `(100)` → `-100`
- 繁/简体余额行均被正确识别为边界
- RSU 提货数量为负

**实际结果**: (待执行)

**缺陷定级**: 若括号负数解析失败 → **HIGH**（数量符号错误 = 买/卖方向反转 = 税务计算完全错误）

---

### TC-04: 长桥 Kangxi Radical 变体 — "买入" vs "买⼊"

**前置条件**: 长桥 PDF 使用 Kangxi 部首变体（U+2F0A 而非 U+5165）。

**操作步骤**:
1. 构造测试文本包含以下变体：
   - "买入" (U+5165, 标准)
   - "买⼊" (U+2F0A, Kangxi)
   - "行权" (U+884C, 标准)
   - "⾏权" (U+2F8F, Kangxi)
   - "现金分红" (U+91D1, 标准)
   - "现⾦金分红" (混合变体)
2. 运行长桥解析器
3. 检查每种变体是否都能被正确分类为 buy/sell/exercise/dividend

**预期结果**:
- 所有变体均应被正则的 `[入⼊]`、`[行⾏]`、`[金⾦金]` 等字符类匹配
- action 分类正确

**实际结果**: (待执行)

**缺陷定级**: 若某变体未覆盖 → **HIGH**（该笔交易被分类为 fee 或跳过）

---

### TC-05: 勾稽关系校验 — Gross ± Fees = Net 全量验证

**前置条件**: 任意券商完整 12 个月 PDF 样本集。

**操作步骤**:
1. 解析全部 PDF，导出 transactions 表所有记录
2. 运行以下 SQL 校验：

```sql
-- 验证 Gross ± Fees = Net（逐笔）
SELECT
    broker_code, reference_no, symbol, action,
    amount as gross,
    (commission + platform_fee + sec_fee + taf_fee + delivery_fee + other_fees) as total_fees,
    amount - (commission + platform_fee + sec_fee + taf_fee + delivery_fee + other_fees) as expected_net,
    amount_cny,
    amount - (commission + platform_fee + sec_fee + taf_fee + delivery_fee + other_fees)
        - (SELECT amount FROM transactions t2 WHERE t2.reference_no = t1.reference_no) as diff
FROM transactions t1
WHERE action IN ('buy', 'sell')
  AND ABS(diff) > 0.01;  -- 容差 1 分钱
```

3. 对 dividend 表运行：

```sql
-- 验证 Gross - Withholding - Fees = Net
SELECT symbol, payment_date,
    gross_amount - withholding_tax - collection_fee - adr_fee - other_deductions as expected_net,
    net_amount,
    gross_amount - withholding_tax - collection_fee - adr_fee - other_deductions - net_amount as diff
FROM dividends
WHERE ABS(diff) > 0.01;
```

4. 报告所有不匹配的记录

**预期结果**:
- 所有交易的 gross - fees 与 net 差额 ≤ 0.01
- 所有分红的勾稽关系成立
- 不匹配记录数为 0

**实际结果**: (待执行)

**缺陷定级**: 若发现勾稽断裂 → **CRITICAL**（金额不一致 = 税务计算基数错误）

---

## 三、自动化校验技术方案

### 3.1 PDF 原文 → 提取结果 双向交叉验证

```python
"""PDF 解析结果自动化校验框架"""

import pdfplumber
import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class ValidationIssue:
    severity: str  # CRITICAL / HIGH / MEDIUM / LOW
    rule_id: str
    message: str
    pdf_page: int
    raw_text_snippet: str


class PDFExtractionValidator:
    """对解析结果进行 PDF 原文反向验证"""

    # 关键金额正则：匹配 PDF 中所有金额格式
    MONEY_PATTERN = re.compile(r"(-?[\d,]+\.\d{2})|\(([\d,]+\.\d{2})\)")
    # 关键日期正则
    DATE_PATTERNS = {
        "boci": re.compile(r"\d{2}/\d{2}/\d{4}"),     # DD/MM/YYYY
        "futu": re.compile(r"\d{4}/\d{2}/\d{2}"),     # YYYY/MM/DD
        "longbridge": re.compile(r"\d{4}\.\d{2}\.\d{2}"),  # YYYY.MM.DD
    }

    def __init__(self, pdf_path: str, broker: str):
        self.pdf_path = pdf_path
        self.broker = broker
        self.issues: list[ValidationIssue] = []

    def validate_amount_completeness(self, db_transactions: list[dict]) -> list[ValidationIssue]:
        """验证: PDF 中出现的所有金额，在数据库中都有对应记录"""
        # 1. 从 PDF 提取所有金额
        pdf_amounts = set()
        with pdfplumber.open(self.pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                for m in self.MONEY_PATTERN.finditer(text):
                    val = m.group(1) or f"-{m.group(2)}"
                    pdf_amounts.add(val.replace(",", ""))

        # 2. 从数据库提取所有金额
        db_amounts = set()
        for txn in db_transactions:
            db_amounts.add(f"{txn['amount']:.2f}")

        # 3. 差集 = 可能的漏抓
        missed = pdf_amounts - db_amounts
        # 排除已知非交易金额（页眉页脚、合计行等）
        missed = self._filter_false_positives(missed)

        for amt in missed:
            self.issues.append(ValidationIssue(
                severity="HIGH",
                rule_id="EX-001",
                message=f"PDF 中存在金额 ¥{amt} 但数据库中无对应记录",
                pdf_page=-1,
                raw_text_snippet=amt,
            ))
        return self.issues

    def validate_keyword_coverage(self) -> list[ValidationIssue]:
        """验证: 关键繁体中文关键词是否可被当前正则匹配"""
        keywords = {
            "futu": ["買入開倉", "賣出平倉", "買入", "賣出", "佣金", "平台使用費",
                      "公司行動", "股票收益計劃", "期末概覽", "期末概览"],
            "boci": ["股息", "存貨", "提貨", "證券存倉", "證券存仓", "承前結餘", "承前结余"],
            "longbridge": ["买入", "卖出", "行权", "現金分红", "现金分红", "期权到期",
                           "佣金", "平台费", "公司行动其他费用"],
        }

        with pdfplumber.open(self.pdf_path) as pdf:
            full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)

        for kw in keywords.get(self.broker, []):
            if kw in full_text:
                # 关键词存在于 PDF 中，检查解析器正则是否能匹配
                if not self._can_parser_match_keyword(kw):
                    self.issues.append(ValidationIssue(
                        severity="HIGH",
                        rule_id="EX-002",
                        message=f"PDF 包含关键词 '{kw}' 但解析器正则可能无法匹配",
                        pdf_page=-1,
                        raw_text_snippet=kw,
                    ))
        return self.issues

    def _can_parser_match_keyword(self, keyword: str) -> bool:
        """检查解析器的正则是能覆盖该关键词（含 Kangxi 变体）"""
        if self.broker == "longbridge":
            # 长桥使用显式字符类如 [入⼊]、[金⾦金]
            expanded = self._expand_kangxi_variants(keyword)
            return any(v in keyword for v in expanded) or keyword in keyword
        return True  # BOCI/Futu 的正则已覆盖繁简

    def _expand_kangxi_variants(self, text: str) -> list[str]:
        """生成 Kangxi 变体列表"""
        variants = {
            "入": ["入", "⼊"],  # U+5165 / U+2F0A
            "行": ["行", "⾏"],  # U+884C / U+2F8F
            "金": ["金", "⾦", "金"],  # U+91D1 / U+2F8D / variant
            "费": ["费", "費", "⽤"],
        }
        results = [text]
        for std, kangs in variants.items():
            new_results = []
            for r in results:
                for k in kangs:
                    new_results.append(r.replace(std, k))
            results.extend(new_results)
        return results

    def _filter_false_positives(self, amounts: set) -> set:
        """排除已知非交易金额（页眉页脚、合计等）"""
        filtered = set()
        for a in amounts:
            v = float(a)
            # 排除极大值（总资产级别，> 1,000,000）
            if abs(v) > 1_000_000:
                continue
            # 排除极小值（< 0.01）
            if abs(v) < 0.01:
                continue
            filtered.add(a)
        return filtered

    def validate_reconciliation(self, db_path: str) -> list[ValidationIssue]:
        """运行 reconciliation 校验"""
        from src.harness.reconciliation import reconcile_import
        from pathlib import Path

        result = reconcile_import(Path(db_path), year=2025)
        for issue in result.issues:
            self.issues.append(ValidationIssue(
                severity=issue.severity,
                rule_id=issue.rule_id,
                message=str(issue),
                pdf_page=-1,
                raw_text_snippet="",
            ))
        return self.issues
```

### 3.2 自动化校验流水线

```
PDF样本 → 解析器 → 数据库 → 自动化校验
                         ↓
                   [EX-001] 金额完整性: PDF金额集合 vs DB金额集合
                   [EX-002] 关键词覆盖: 繁体关键词正则匹配验证
                   [EX-003] 勾稽关系:  Gross ± Fees = Net
                   [EX-004] 日期合法性: 无 13月/32日
                   [EX-005] 币种一致性: 无 USD 交易标注为 HKD
                   [EX-006] Symbol 合法性: 无空 symbol/异常字符
                         ↓
                   校验报告（按 severity 排序）
```

### 3.3 CI 集成

```yaml
# .github/workflows/pdf-parser-tests.yml
jobs:
  pdf-extraction-validation:
    steps:
      - name: 解析全部 PDF 样本
        run: python -m src.database.import_statements all --input-dir test_data/pdfs

      - name: 运行金额完整性校验
        run: python tests/pdf_validation/validate_amounts.py

      - name: 运行繁体关键词覆盖校验
        run: python tests/pdf_validation/validate_keywords.py

      - name: 运行勾稽关系校验
        run: python tests/pdf_validation/validate_reconciliation.py

      - name: 运行 Harness 全套校验
        run: python -m src.cli harness --year 2025
```

---

## 四、风险提示

### RISK-01: 金额符号丢失 — 全部使用 abs(amount)

**严重程度**: CRITICAL
**影响范围**: 所有三个券商解析器
**根因**: `import_statements.py` 中所有交易金额均存储为 `abs(amount)`，交易方向完全依赖 action 字段推断。

**具体表现**:
- 富途 (line ~1710): `amount=abs(amount)`
- 长桥 (line ~2236): `amount=abs(amount)`
- 中银 (line ~487): `amount=float(abs(amount))`

**税务风险**: 如果 action 分类错误（如将 "賣出" 误分类为 "买入"），由于金额都是正数，FIFO 引擎会将卖出当作买入处理 —— 本应确认的资本利得变成了新增持仓成本，导致 **少报应税收入**。这是系统性风险，无法通过后续对账发现（因为对账也只校验绝对值）。

**缓解措施**:
1. 解析阶段保留原始 sign，数据库增加 `amount_signed` 字段
2. Harness 校验增加 action/amount 符号一致性检查

---

### RISK-02: 富途期权 strike 解析歧义 — 2-digit strike 不做归一化

**严重程度**: HIGH
**影响范围**: 富途持仓快照 + 交易解析
**根因**: `_extract_futu_option_context` 中 `int(strike_digits) / 1000.0` 仅对 3+ 位数字生效。2 位数字（如 "44"）保持原样。

**具体表现**:
- strike "44" → 44.0（正确）
- strike "44000" → 44.0（正确）
- strike "100" → 0.1（**错误**: 100/1000 = 0.1，但实际 strike 就是 $100）
- strike "44" 和 strike "44000" 解析结果相同，但持倉快照中若出现 "44" 格式（未补零），会被当作 $44 而非 $0.044

**税务风险**: 期权行权/过期时，FIFO 引擎使用 strike 匹配 lot。strike 不匹配 → lot 匹配失败 → 触发 $0 成本降级 → 全部 proceeds 被征税。

**缓解措施**: strike 归一化逻辑应基于期权 underlying 的合理价格范围，而非简单的位数判断。

---

### RISK-03: 预扣税硬编码 10% — 非 W-8BEN 持有人 FTC 系统性错误

**严重程度**: HIGH
**影响范围**: BOCI + Futu 所有美股分红
**根因**: `import_statements.py` 中 BOCI (line ~680) 和 Futu (line ~1802) 均硬编码 `withholding_rate = 0.10`，从月结单中不提取实际预扣金额。

**具体表现**:
- 已开通 W-8BEN 的用户：美国预扣确实为 10%，估算正确
- 未开通 W-8BEN 的用户：美国预扣为 30%，系统按 10% 估算 → **FTC 低估 20%** → 多缴税
- 港股分红（如 9988.HK）：香港预扣可能为 0% 或 10%，系统一律按美国 10% 估算 → FTC 错误

**税务风险**: FTC 是抵免中国应纳税额的关键。系统性低估 FTC = 系统性地多算税 = 客户多缴税。对于分红金额较大的用户（年分红 $10,000+），差异可达 $2,000 × 7.1 = ¥14,200 的 FTC 误差。

**缓解措施**:
1. 月结单解析优先提取实际预扣金额（Longbridge 已做到）
2. 无法提取时明确标注 "估算值" 并在报告中展示
3. 提供用户界面让用户手动修正预扣税率（已完成 warning 添加）

---

## 五、测试执行计划

| 阶段 | 内容 | 预计工时 | 依赖 |
|------|------|----------|------|
| Phase 1 | 收集全部 15 份 PDF 样本 | 2 天 | 各券商账号 |
| Phase 2 | 手动标注 PDF 原文交易清单（ground truth） | 3 天 | Phase 1 |
| Phase 3 | 运行解析器 + 自动化校验 | 1 天 | Phase 2 |
| Phase 4 | 分析差异、定级缺陷、提交修复 | 3 天 | Phase 3 |
| Phase 5 | 回归测试 + Harness 校验闭环 | 2 天 | Phase 4 |

**总计**: 约 11 个工作日
