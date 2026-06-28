# HTML 详细税务报告改造计划（已完成）

## 背景

`src/report/detailed_html_report.py` 已实现完整，但尚未集成到系统中。该文件生成专业级别的 HTML 税务报告，包含 9 个 section（年度汇总、资本利得、FIFO 审计、RSU、分红、利息、费用、境外税收抵免、法规依据）。

## 已完成事项

### 1. 修复 `detailed_html_report.py` 中的 bug ✅

**RSU 税率表高亮 bug**（第 619 行）：`is_active` 变量原来是空字符串。已修复为根据 `rsu_rate` 匹配 bracket 的 rate 值，高亮当前适用税率行。

**税率表缺失**（额外发现）：`bracket_rows` 变量被计算但未嵌入 HTML 返回值。已在 "税率计算说明" 后新增 "税率表" subsection，包含级距/税率/速算扣除数三列。

### 2. 注册到 `src/report/__init__.py` ✅

导出 `generate_detailed_html_report` 与 `write_tax_report` 并列。

### 3. 集成到 CLI（`calc-db` 命令）✅

- **`calc_db` 函数**（约 1864 行）：CSV 报表后追加 HTML 报告生成
- **`_run_calc_db` 函数**（约 2484 行）：同上，供 `calc_all` 复用

两处均在 `write_tax_report` 之后调用 `generate_detailed_html_report(db_path, year, html_path)`，并输出路径提示。

### 4. 更新 README ✅

目录结构描述中 `src/report/` 标注从 "CSV 报表输出" 改为 "CSV + HTML 报表输出"。

## 验收结果

- `calc-db --year 2025` 成功生成 `output/tax_2025/tax_report_2025.html`（322KB）
- RSU 税率表正确高亮 20% 行
- CSV 报表不受影响
- 全部语法检查通过
