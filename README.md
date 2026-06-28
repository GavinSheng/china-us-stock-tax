# 美股及股权激励个税计算工具

自动解析富途/长桥/BOCI 月结单，核算美股交易、RSU 归属、分红、利息等收入的中国个人所得税（IIT）。

## 功能

- **多券商 PDF 月结单解析** — 支持富途（Futu）、长桥（Longbridge）、中银国际（BOCI）
- **数据库持久化** — 交易、分红、持仓、税务批次全部存入 SQLite，支持逐文件重建与验证
- **FIFO 成本核算** — 按先进先出法计算每股成本，支持期权买入/卖出/过期/行权全生命周期
- **年度结转（Carryforward）** — 从上年 12 月 31 日期末持仓快照创建结转税务批次，无需追溯历史交易
- **汇率转换** — USD/HKD → CNY，优先使用交易日汇率，回退至年末汇率（税法要求）
- **税务计算** — 四类收入独立计算：
  - RSU 归属 → 工资薪金所得，3%~45% 累进税率，境外税收抵免
  - 卖出盈利 → 财产转让所得（20%），支持逐笔 vs 年度净额择优
  - 分红 → 股息红利所得（20%），已预扣 10% 补缴差额（含杠杆 ETF 分红，统一按股息申报）
  - 利息/投资收益 → 20% 税率
- **境外税收抵免** — 分国限额法（US / HK 分别计算），超额结转 5 年
  - 净亏损年度：境外预扣税当年作废，不生成结转额度（财税〔2020〕3号）
- **税务合规 Harness** — 124 个校验点覆盖 61 条唯一规则：输入校验(26)、对账(28)、计算验证(41)、多账户核算(9)、多缴税检测(7)、数据库校验(13)
- **HTML 税务报告** — 生成可视化税务清算报告，包含年度汇总、资本利得明细、FIFO 成本审计追踪、RSU 股权激励明细（含税率表高亮）、分红明细（逐笔+汇总）、利息所得、可抵扣费用、境外税收抵免明细、法规依据

## 环境要求

- **Python 3.10+**（使用 `match` 语句和 `typing.ParamSpec`）
- 操作系统：macOS / Linux / Windows
- 无需额外系统依赖，`pip install` 即可使用

## 安装

```bash
pip install -r requirements.txt
```

## 配置

复制 `.env.example` 为 `.env` 并填写必要信息：

```bash
cp .env.example .env
```

`.env` 文件中的配置项：

| 配置项 | 说明 | 必填 |
|--------|------|------|
| `DEFAULT_EXCHANGE_RATE` | 默认 USD/CNY 汇率，无汇率文件时使用 | 否（默认 7.10） |
| `EXCHANGE_RATE_FILE` | 汇率 CSV 文件路径 | 否（默认 `output/exchange_rates.csv`） |
| `LONGBRIDGE_PASSWORD` | 长桥 PDF 月结单密码 | **是**（使用长桥月结单时） |
| `FUTU_2024_PASSWORD` | 富途 PDF 月结单密码（2024 年及以前） | **是**（使用富途 2024 月结单时） |

> **安全提醒**：`.env` 文件已加入 `.gitignore`，不会被提交到 Git。请勿将密码硬编码在源代码中。
>
> **首次使用**：复制 `.env.example` 后，务必修改其中的示例密码为你自己的真实密码。

## 快速开始

### 0. 环境检查

```bash
# 确认 Python 版本 >= 3.10
python3 --version

# 安装依赖
pip install -r requirements.txt

# 确认安装成功（应无报错）
python -c "import click, pdfplumber, pandas; print('依赖就绪')"
```

### 1. 配置 .env

```bash
cp .env.example .env
# 编辑 .env 文件，填入你的券商月结单密码
```

### 2. 准备月结单

将 PDF 月结单放入 `input/` 目录，按券商分类：

```
input/
  boci-2025-monthly/     # 中银国际月结单（PDF）
  futu-2025-monthly/     # 富途月结单（PDF）
  bridge-2025-monthly/   # 长桥月结单（PDF）
  rsu/                   # RSU 归属截图（JPG/PNG/PDF，可选）
  decrypted/             # 解密后的 PDF（自动生成，无需手动放置）
```

**目录说明**：

| 目录 | 内容 | 必填 |
|------|------|------|
| `boci-2025-monthly/` | BOCI 月结单 PDF（如 `20241231_2_xxx.pdf`） | 使用 BOCI 时必填 |
| `futu-2025-monthly/` | 富途月结单 PDF（如 `futu_xxx.pdf`） | 使用富途时必填 |
| `bridge-2025-monthly/` | 长桥月结单 PDF（如 `statement-monthly-xxx.pdf`） | 使用长桥时必填 |
| `rsu/` | RSU 归属截图（JPG/PNG），文件名建议为归属日期 | 有 RSU 时选填 |
| `decrypted/` | 解密后的 PDF，导入时自动生成 | 无需手动管理 |

**命名规则**：目录名前缀必须为 `boci-`、`futu-`、`bridge-`，程序据此自动识别券商。

### 3. 导入并计算（精简版）

如果你已经熟悉流程，可直接执行：

```bash
# 初始化数据库
python -m src.cli db init

# 导入所有券商月结单
python -m src.database.import_statements all

# 创建上年结转持仓（以 2024 年末持仓作为 2025 年成本基础）
python -m src.cli carryforward --year 2024

# 计算 2025 年税务，结果持久化到数据库 + 输出报表
python -m src.cli calc-db --year 2025
```

### 4. 验证数据完整性

```bash
# 运行 Harness 校验（输入 + 对账 + 计算验证）
python -m src.cli harness --year 2025

# 查看数据库统计信息
python -m src.cli db info
```

## 从零开始：首次报税完整教程

> 假设你是一个有 RSU 和股票交易的程序员，第一次使用本工具完成 2025 年度个税申报。

### 前置条件

1. **Python 3.10+**：运行 `python3 --version` 确认
2. **你的券商月结单 PDF**：从券商 App 下载 2024 年 12 月 ~ 2025 年 12 月的月结单
3. **月结单密码**：富途/长桥的 PDF 月结单通常有密码，在 App 设置中查看或修改
4. **2024 年末持仓**：需要 2024 年 12 月的月结单（含期末持仓快照），作为 2025 年成本基础

### Step 1：安装与配置

```bash
# 克隆项目（如果还没克隆）
git clone <repo-url>
cd china-us-stock-tax

# 安装依赖
pip install -r requirements.txt

# 复制并编辑配置文件
cp .env.example .env
# 用编辑器打开 .env，填入你的券商月结单密码
```

### Step 2：放入月结单 PDF

```
input/
  futu-2025-monthly/       # 富途月结单（2024-12 ~ 2025-12）
  bridge-2025-monthly/     # 长桥月结单（如有）
  boci-2025-monthly/       # 中银国际月结单（如有）
```

> **关键**：目录名前缀必须是 `futu-`、`bridge-` 或 `boci-`，程序据此自动识别券商。
> 2024 年 12 月的月结单也必须放入对应目录（用于提取结转持仓）。

### Step 3：导入 + 结转 + 计税

```bash
# ① 初始化数据库
python -m src.cli db init

# ② 导入所有券商月结单（自动解密 PDF、解析交易、写入数据库）
python -m src.database.import_statements all

# ③ 创建结转持仓（以 2024 年末持仓作为 2025 年成本基础）
python -m src.cli carryforward --year 2024

# ④ 计算 2025 年个税
python -m src.cli calc-db --year 2025
```

### Step 4：查看结果

```bash
# 查看数据库统计信息（确认导入成功）
python -m src.cli db info

# 运行合规校验
python -m src.cli harness --year 2025
```

输出文件在 `output/` 目录：

| 文件 | 内容 |
|------|------|
| `tax_2025/tax_summary.csv` | 税务汇总（各收入类型税额） |
| `tax_2025/tax_detail_capital_gain.csv` | 每笔卖出交易的资本利得 |
| `tax_2025/tax_detail_dividend.csv` | 每笔分红明细 |
| `tax_2025/tax_report_2025.html` | **可视化税务报告**（浏览器打开查看） |

### Step 5：常见问题排查

| 问题 | 原因 | 解决方法 |
|------|------|---------|
| `ModuleNotFoundError` | 依赖未安装 | `pip install -r requirements.txt` |
| PDF 解密失败 | 密码错误或未配置 `.env` | 检查 `.env` 中的密码是否正确 |
| 解析后 0 笔交易 | 目录名前缀不对或 PDF 格式不匹配 | 确认目录名以 `futu-`/`bridge-`/`boci-` 开头 |
| `carryforward` 报错 | 缺少 2024 年 12 月月结单 | 放入上年 12 月结单并重新 import |
| harness 报 ERROR | 数据异常或缺失 | 查看具体 rule_id，对照 `src/harness/` 中规则说明 |
| Python 版本 < 3.10 | 不支持 `match` 语法 | 升级 Python 到 3.10+ |

## 命令行参考

| 命令 | 说明 |
|------|------|
| `db init` | 初始化数据库 |
| `db info` | 查看数据库统计（交易数、文件数、税批次等） |
| `db rebuild` | 清空解析数据，逐文件重解析并验证 |
| `db validate` | 全部导入后检查数据完整性 |
| `carryforward --year 2024` | 从 2024-12-31 期末持仓创建结转税务批次 |
| `calc-db --year 2025` | 从数据库加载交易并计算个税 |
| `harness --year 2025` | 运行税务合规校验 |
| `seed-rsu` | 从 RSU 归属表合成交易记录 |

### carryforward — 年度结转

从指定年度 12 月 31 日期末持仓创建结转税务批次，作为下一年度的成本基础：

```bash
carryforward --year 2024   # 从 2024-12-31 结转 → 用于 2025 年税务
carryforward --year 2025   # 从 2025-12-31 结转 → 用于 2026 年税务
```

**税务合规依据**：中国个税按年计算，年初持仓 = 上年末持仓快照。券商官方月结对账单的期末持仓是审计认可的成本基础凭证。无需追溯历史交易，直接使用期末概览中的成本价。

### calc-db — 数据库计税

```bash
calc-db --year 2025                  # 计算 2025 年税务
calc-db --year 2025 --output reports/  # 指定输出目录
```

输出文件：
- `tax_summary.csv` — 税务汇总
- `tax_detail_capital_gain.csv` — 资本利得明细
- `tax_detail_dividend.csv` — 分红明细
- `tax_detail_fees.csv` — 可抵扣费用明细
- `tax_report_{year}.html` — 可视化税务清算报告（含完整明细 + FIFO 审计追踪）
- `lots_{year}.json` — 年末持仓快照

### harness — 合规校验

```bash
harness --year 2025                          # 全量校验
harness --year 2025 --skip-validation        # 跳过输入校验
harness --year 2025 --skip-reconciliation    # 跳过对账校验
harness --year 2025 --skip-verification      # 跳过计算验证
```

## 年度税务工作流

```
2024-12 月结单  →  carryforward --year 2024  →  2025-01~12 月结单  →  calc-db --year 2025
                                                                    →  harness --year 2025
                                                                    →  输出报表
```

每年度独立核算：
1. 先导入上年 12 月结单（提取期末持仓快照）
2. 运行 `carryforward --year <上年>` 创建结转批次
3. 导入本年各月月结单
4. 运行 `calc-db --year <本年>` 计算税务
5. 运行 `harness --year <本年>` 校验合规

## 税务规则

详见 [src/harness/tax_rules.md](src/harness/tax_rules.md)，法律依据详见 [src/harness/legal_basis.md](src/harness/legal_basis.md)。

| 收入类型 | 中国税法分类 | 税率 |
|---------|-------------|------|
| 股票/期权卖出盈利 | 财产转让所得 | 20% |
| 分红 | 股息红利所得 | 20% |
| 杠杆 ETF 分红 | 股息红利所得 | 20%（统一申报，不认可 ROC 递延） |
| RSU 归属 | 工资薪金所得 | 3%~45%（单独计税） |
| 利息/投资收益 | 利息/投资收益所得 | 20% |
| 期权过期损失 | 财产转让损失 | 参与年度净额抵扣 |

## 目录结构

```
input/              # 月结单 PDF（.gitignore 排除）
output/             # 计算结果 + 数据库（.gitignore 排除）
src/
  cli.py            # 命令行入口
  parsers/          # PDF 解析器（Futu / Longbridge）
  database/
    importers/      # 月结单导入器（BOCI / Futu / Longbridge）
    connection.py   # 数据库连接与迁移
    repositories.py # 数据访问层
    schema.py       # 数据库表结构
  calculator/       # FIFO 引擎、税务计算、汇率
  harness/          # 税务合规校验层（校验规则 + 法律依据）
  models/           # 数据模型
  report/           # CSV + HTML 报表输出
tests/              # 单元测试
```

### Harness 校验规则一览

Harness 共 **124 个校验点**，覆盖 **61 条唯一规则**，按模块分类：

| 模块 | 文件 | 唯一规则 | 校验点 | 说明 |
|------|------|---------|--------|------|
| 输入验证 | `validators.py` | IV-001 ~ IV-015 | 26 | 日期、数量、价格、金额、symbol 格式等 |
| 对账校验 | `reconciliation.py` | RC-001 ~ RC-017 | 28 | 文件完整性、持仓合理性、期权生命周期等 |
| 计算验证 | `tax_verify.py` | CV-001 ~ CV-011 + LC/FC/TI | 41 | 税额非负、FIFO 一致性、汇率、跨券商对比等 |
| 多账户核算 | `multi_account_verify.py` | MA-001 ~ MA-007 | 9 | 跨券商 FIFO、重复分红、汇率一致性等 |
| 多缴税检测 | `overpayment_detect.py` | OP-001 ~ OP-006 | 7 | 费用未扣、抵免遗漏、双重计税等 |
| 数据库校验 | `db_validators.py` | RC-005 ~ RC-012 | 13 | 跨文件全局检查、孤立卖出、结转完整性 |
| 预计算检查 | `pre_calc.py` | PC-001 ~ PC-005 | — | 计税前就绪检查（汇率、月结单、FIFO 缺口） |

## 合规声明

- 本工具计算结果仅供参考，不构成正式税务建议
- 实际申报请与主管税务机关或执业税务师确认
- 境外所得需在次年 3 月 1 日至 6 月 30 日办理汇算清缴
- 境外税收抵免超额部分可在以后 5 个纳税年度内结转
- 净亏损年度境外预扣税不生成结转额度（抵免限额为 0）
- 杠杆 ETF 分红统一按股息红利所得计税，不采用美国 ROC 递延处理
