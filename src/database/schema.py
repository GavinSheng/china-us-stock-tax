"""SQLite 数据库表结构定义

设计原则：
1. 完整审计追踪：月结单 → 交易记录 → 税务计算 → 年度汇总
2. 费用明细可追溯：每笔交易的费用逐项记录，支持成本扣除计算
3. 多币种与汇率：所有金额保留原始币种 + CNY 折算
4. 境外税收抵免：分红预扣税逐项记录，支持分国限额法计算
5. FIFO 成本批次：独立表维护持仓批次，支持跨年追溯
"""

SCHEMA_SQL = """
-- ============================================================
-- 基础参考表
-- ============================================================

CREATE TABLE IF NOT EXISTS brokers (
    code            TEXT PRIMARY KEY,   -- 'boci', 'futu', 'longbridge'
    name_cn         TEXT NOT NULL,      -- '中银国际', '富途', '长桥'
    name_en         TEXT,               -- 'BOCI', 'Futu', 'Longbridge'
    account_number  TEXT,               -- 账户号码
    base_currency   TEXT DEFAULT 'USD', -- 基准货币
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS exchange_rates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            DATE NOT NULL,      -- 汇率日期
    from_currency   TEXT NOT NULL,      -- 'USD', 'HKD'
    to_currency     TEXT NOT NULL,      -- 'CNY'
    rate            REAL NOT NULL,      -- 汇率值
    source          TEXT DEFAULT 'user_provided',  -- 'user_provided', 'pbo', 'ecb'
    UNIQUE(date, from_currency, to_currency)
);

-- ============================================================
-- 月结单文件追踪
-- ============================================================

CREATE TABLE IF NOT EXISTS statement_files (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    broker_code     TEXT NOT NULL REFERENCES brokers(code),
    file_path       TEXT NOT NULL,      -- 原始文件路径
    file_hash       TEXT,               -- SHA256 去重
    statement_month TEXT NOT NULL,      -- '2025-01'
    parsed_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    parser_version  TEXT,               -- 解析器版本
    page_count      INTEGER,
    has_password    BOOLEAN DEFAULT 0,
    status          TEXT DEFAULT 'pending',  -- 'pending', 'parsed', 'error'
    error_message   TEXT,
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_stmt_file_hash ON statement_files(file_hash);

-- ============================================================
-- 交易主表
-- 记录所有买入、卖出、费用类交易
-- ============================================================

CREATE TABLE IF NOT EXISTS transactions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    broker_code     TEXT NOT NULL REFERENCES brokers(code),
    trade_date      DATE NOT NULL,      -- 交易日期
    settlement_date DATE,               -- 交收日期（T+2 等）
    reference_no    TEXT,               -- 交易参考编号/流水号

    -- 交易标的
    symbol          TEXT NOT NULL,      -- 股票代码：BABA, AVGO, SOXL
    company_name    TEXT,               -- 公司名称
    exchange        TEXT,               -- 交易所：NASDAQ, NYSE, HKEX

    -- 交易信息
    action          TEXT NOT NULL,      -- 'buy', 'sell', 'fee', 'interest', 'yield_income'
    quantity        INTEGER,            -- 股数/份数
    price           DECIMAL(18, 6),     -- 单价（原始币种）
    amount          DECIMAL(18, 2),     -- 总金额（原始币种）

    -- 费用明细（逐项记录，支持税务成本扣除）
    commission      DECIMAL(18, 4) DEFAULT 0,     -- 佣金
    platform_fee    DECIMAL(18, 4) DEFAULT 0,     -- 平台费
    sec_fee         DECIMAL(18, 4) DEFAULT 0,     -- SEC 规费
    taf_fee         DECIMAL(18, 4) DEFAULT 0,     -- TAF 交易活动费
    delivery_fee    DECIMAL(18, 4) DEFAULT 0,     -- 交收费
    other_fees      DECIMAL(18, 4) DEFAULT 0,     -- 其他费用
    fee_breakdown   TEXT,               -- JSON: 完整费用明细 {"commission": 0.99, "platform": 1.00, ...}

    -- 税务相关
    tax_withheld    DECIMAL(18, 2) DEFAULT 0,     -- 预扣税金额（原始币种）
    withholding_tax_type TEXT,          -- 'dividend_withholding', 'rsu_withholding', null

    -- 币种与汇率
    currency        TEXT NOT NULL DEFAULT 'USD',  -- 'USD', 'HKD'
    exchange_rate   DECIMAL(10, 6),               -- 原始币种 → CNY 汇率
    amount_cny      DECIMAL(18, 2),               -- 总金额折 CNY
    fee_total_cny   DECIMAL(18, 2),               -- 总费用折 CNY
    tax_withheld_cny DECIMAL(18, 2),              -- 预扣税折 CNY

    -- 审计追溯
    statement_file_id INTEGER REFERENCES statement_files(id),
    raw_data        TEXT,               -- JSON: 原始解析数据（用于排查）
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    -- 索引
    CONSTRAINT chk_action CHECK (action IN ('buy', 'sell', 'fee', 'interest', 'yield_income', 'dividend', 'option_buy', 'option_sell', 'option_expire', 'option_exercise', 'rsu_vest', 'rsu_sell'))
);

CREATE INDEX IF NOT EXISTS idx_txn_broker_date ON transactions(broker_code, trade_date);
CREATE INDEX IF NOT EXISTS idx_txn_symbol ON transactions(symbol);
CREATE INDEX IF NOT EXISTS idx_txn_action ON transactions(action);
CREATE INDEX IF NOT EXISTS idx_txn_statement_file ON transactions(statement_file_id);

-- ============================================================
-- 分红表
-- 独立于交易主表，因为分红有独特的税务字段
-- ============================================================

CREATE TABLE IF NOT EXISTS dividends (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    broker_code     TEXT NOT NULL REFERENCES brokers(code),
    payment_date    DATE NOT NULL,      -- 分红到账日期
    settlement_date DATE,               -- 交收日期

    symbol          TEXT NOT NULL,      -- 股票代码
    company_name    TEXT,

    -- 分红金额
    per_share_amount DECIMAL(18, 6) NOT NULL,  -- 每股分红（原始币种）
    share_quantity  INTEGER NOT NULL,   -- 分红基准股数
    gross_amount    DECIMAL(18, 2) NOT NULL,  -- 分红总额（原始币种）

    -- 税费明细
    withholding_tax DECIMAL(18, 2) DEFAULT 0,   -- 预扣税（原始币种）
    withholding_rate REAL DEFAULT 0,            -- 预扣税率（如 0.10 = 10%）
    withholding_country TEXT,                   -- 预扣税国家（'US', 'HK'）
    withholding_refund DECIMAL(18, 2) DEFAULT 0, -- 预扣税返还（原始币种，杠杆 ETF ROC）
    collection_fee  DECIMAL(18, 2) DEFAULT 0,   -- 分红代收手续费
    adr_fee         DECIMAL(18, 2) DEFAULT 0,   -- ADR 发行费
    other_deductions DECIMAL(18, 2) DEFAULT 0,  -- 其他扣款

    net_amount      DECIMAL(18, 2) NOT NULL,    -- 净到账金额（原始币种）

    -- 币种与汇率
    currency        TEXT NOT NULL DEFAULT 'USD',
    exchange_rate   DECIMAL(10, 6),
    gross_amount_cny DECIMAL(18, 2),            -- 分红总额折 CNY
    withholding_tax_cny DECIMAL(18, 2),         -- 预扣税折 CNY
    withholding_refund_cny DECIMAL(18, 2) DEFAULT 0, -- 预扣税返还折 CNY
    net_amount_cny  DECIMAL(18, 2),             -- 净额折 CNY

    -- 中国税务计算
    china_tax_rate  REAL DEFAULT 0.20,          -- 中国分红税率（默认 20%）
    china_tax_amount DECIMAL(18, 2),            -- 中国应纳税额
    foreign_credit  DECIMAL(18, 2) DEFAULT 0,   -- 境外可抵免税额
    tax_payable     DECIMAL(18, 2),             -- 应补缴税额

    -- 审计追溯
    statement_file_id INTEGER REFERENCES statement_files(id),
    raw_data        TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_div_broker_date ON dividends(broker_code, payment_date);
CREATE INDEX IF NOT EXISTS idx_div_symbol ON dividends(symbol);
CREATE INDEX IF NOT EXISTS idx_div_statement_file ON dividends(statement_file_id);

-- ============================================================
-- RSU 授予表
-- 记录每次 RSU 授予的基本信息
-- ============================================================

CREATE TABLE IF NOT EXISTS rsu_grants (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    grant_number    TEXT NOT NULL UNIQUE,  -- 授予编号
    symbol          TEXT NOT NULL,         -- 股票代码
    company_name    TEXT,
    total_shares    INTEGER NOT NULL,      -- 授予总股数
    vested_shares   INTEGER DEFAULT 0,     -- 已归属股数
    unvested_shares INTEGER,               -- 未归属股数
    vesting_schedule TEXT,                 -- JSON: 归属计划 [{"date": "2025-04-01", "shares": 200}]
    exercise_price  DECIMAL(18, 6),        -- 行使价（如有）
    grant_date      DATE,                  -- 授予日期
    expiry_date     DATE,                  -- 到期日期
    currency        TEXT DEFAULT 'USD',
    market          TEXT,                  -- 'US', 'HK'
    notes           TEXT
);

-- ============================================================
-- RSU 归属表
-- 每次归属生成一条记录
-- ============================================================

CREATE TABLE IF NOT EXISTS rsu_vests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    grant_id        INTEGER REFERENCES rsu_grants(id),
    grant_number    TEXT NOT NULL,         -- 冗余字段，便于查询

    vest_date       DATE NOT NULL,         -- 归属日期
    deposit_date    DATE,                  -- 存股日期（股份到账日）

    symbol          TEXT NOT NULL,
    company_name    TEXT,

    vested_quantity INTEGER NOT NULL,      -- 归属股数
    sell_to_cover   INTEGER DEFAULT 0,     -- 以股抵税出售股数
    shares_deposited INTEGER,              -- 实际存入证券账户的股数

    fmv_per_share   DECIMAL(18, 6) NOT NULL,  -- 归属日 FMV（计税价格）
    taxable_income  DECIMAL(18, 2) NOT NULL,  -- 纳税收益（原始币种）
    tax_amount      DECIMAL(18, 2) NOT NULL,  -- 应缴税金

    currency        TEXT NOT NULL DEFAULT 'USD',
    exchange_rate   DECIMAL(10, 6),           -- 归属日汇率
    taxable_income_cny DECIMAL(18, 2),        -- 纳税收益折 CNY
    tax_amount_cny  DECIMAL(18, 2),           -- 应缴税金折 CNY

    tax_method      TEXT NOT NULL DEFAULT 'cash',  -- 'cash', 'sell_to_cover'
    tax_paid        BOOLEAN DEFAULT 0,           -- 是否已完税
    tax_paid_date   DATE,                        -- 完税日期

    -- 审计追溯
    custody_broker TEXT,                   -- 存管券商：'boci'
    source_image    TEXT,                   -- 截图文件名
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_rsu_grant ON rsu_vests(grant_number);
CREATE INDEX IF NOT EXISTS idx_rsu_vest_date ON rsu_vests(vest_date);
CREATE INDEX IF NOT EXISTS idx_rsu_symbol ON rsu_vests(symbol);

-- ============================================================
-- 现金回报表
-- RSU 现金激励记录
-- ============================================================

CREATE TABLE IF NOT EXISTS cash_rewards (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    reward_name     TEXT NOT NULL,         -- '2025现金回报'
    rsu_type        TEXT,                  -- 'RSU(美股)', 'RSU(港股)'
    currency        TEXT NOT NULL DEFAULT 'USD',
    total_amount    DECIMAL(18, 2) NOT NULL,
    vested_amount   DECIMAL(18, 2) DEFAULT 0,
    unvested_amount DECIMAL(18, 2) DEFAULT 0,
    notes           TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================
-- 持仓批次表（FIFO 成本核算）
-- 每买入/归属一次生成一个批次，卖出时按 FIFO 消耗
-- ============================================================

CREATE TABLE IF NOT EXISTS tax_lots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    broker_code     TEXT REFERENCES brokers(code),

    acquisition_date DATE NOT NULL,       -- 取得日期
    acquisition_type TEXT NOT NULL,       -- 'buy', 'rsu_vest'
    source_txn_id   INTEGER REFERENCES transactions(id),  -- 来源交易/归属记录

    quantity        INTEGER NOT NULL,     -- 原始股数
    remaining       INTEGER NOT NULL,     -- 剩余股数
    cost_per_share  DECIMAL(18, 6) NOT NULL,  -- 每股成本（原始币种）
    total_cost      DECIMAL(18, 2) NOT NULL,  -- 总成本

    currency        TEXT NOT NULL DEFAULT 'USD',
    exchange_rate   DECIMAL(10, 6),           -- 取得日汇率
    total_cost_cny  DECIMAL(18, 2),           -- 总成本折 CNY

    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_lots_symbol_remaining ON tax_lots(symbol, remaining);

-- ============================================================
-- 持仓消耗记录（FIFO 追溯）
-- 记录每次卖出消耗了哪个批次的多少股
-- ============================================================

CREATE TABLE IF NOT EXISTS lot_consumptions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sell_txn_id     INTEGER REFERENCES transactions(id),  -- NULL for exercise/expiration
    tax_lot_id      INTEGER NOT NULL REFERENCES tax_lots(id),

    consumed_qty    INTEGER NOT NULL,     -- 消耗股数
    cost_per_share  DECIMAL(18, 6) NOT NULL,  -- 该批次成本
    cost_basis      DECIMAL(18, 2) NOT NULL,  -- 消耗部分的成本总额
    realized_gain   DECIMAL(18, 2),           -- 已实现盈亏
    consumption_type TEXT NOT NULL DEFAULT 'sell',  -- 'sell' (卖出) 或 'exercise' (行权)
    currency        TEXT NOT NULL DEFAULT 'USD',

    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================
-- 持仓快照表（月末持仓）
-- 每月末记录各券商持仓情况，用于对账和审计
-- ============================================================

CREATE TABLE IF NOT EXISTS positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    broker_code     TEXT NOT NULL REFERENCES brokers(code),
    as_of_date      DATE NOT NULL,        -- 月末日期

    symbol          TEXT NOT NULL,
    company_name    TEXT,
    exchange        TEXT,

    quantity        INTEGER NOT NULL,     -- 持仓数量
    avg_cost        DECIMAL(18, 6),       -- 平均成本
    closing_price   DECIMAL(18, 6),       -- 月末收市价
    market_value    DECIMAL(18, 2),       -- 总市值（原始币种）
    unrealized_pnl  DECIMAL(18, 2),       -- 浮动盈亏

    currency        TEXT NOT NULL DEFAULT 'USD',
    exchange_rate   DECIMAL(10, 6),
    market_value_cny DECIMAL(18, 2),      -- 总市值折 CNY

    statement_file_id INTEGER REFERENCES statement_files(id),
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(broker_code, as_of_date, symbol)
);

CREATE INDEX IF NOT EXISTS idx_pos_broker_date ON positions(broker_code, as_of_date);
CREATE INDEX IF NOT EXISTS idx_pos_symbol ON positions(symbol);
CREATE INDEX IF NOT EXISTS idx_pos_statement_file ON positions(statement_file_id);

-- ============================================================
-- 税务计算结果表
-- 每笔应税事件一条记录
-- ============================================================

CREATE TABLE IF NOT EXISTS tax_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tax_year        INTEGER NOT NULL,     -- 纳税年度

    source_type     TEXT NOT NULL,        -- 'transaction', 'dividend', 'rsu_vest'
    source_id       INTEGER,              -- 关联的源记录 ID
    source_ref      TEXT,                 -- 人类可读引用：'TXN-123', 'DIV-45'

    symbol          TEXT NOT NULL,
    income_type     TEXT NOT NULL,        -- 'capital_gain_per_txn', 'capital_gain_annual_net',
                                        -- 'dividend_income', 'rsu_income'

    trade_date      DATE,                 -- 交易/分红日期
    quantity        INTEGER,
    gross_income    DECIMAL(18, 2),       -- 应税收入（原始币种）
    deductible      DECIMAL(18, 2) DEFAULT 0,  -- 可扣除金额
    taxable_income  DECIMAL(18, 2),       -- 应纳税所得

    currency        TEXT DEFAULT 'USD',
    exchange_rate   DECIMAL(10, 6),
    gross_income_cny DECIMAL(18, 2),     -- 应税收入折 CNY
    taxable_income_cny DECIMAL(18, 2),   -- 应纳税所得折 CNY

    tax_rate        REAL NOT NULL,        -- 税率（0.20 = 20%）
    tax_amount_cny  DECIMAL(18, 2),       -- 应纳税额 CNY
    tax_withheld_cny DECIMAL(18, 2) DEFAULT 0,  -- 已预扣 CNY
    foreign_credit_cny DECIMAL(18, 2) DEFAULT 0,  -- 境外抵免 CNY
    excess_withholding_cny DECIMAL(18, 2) DEFAULT 0,  -- 超额未抵免
    tax_payable_cny DECIMAL(18, 2),       -- 应补缴 CNY

    detail          TEXT,                 -- 备注/明细说明
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_tax_year ON tax_items(tax_year);
CREATE INDEX IF NOT EXISTS idx_tax_type ON tax_items(income_type);
CREATE INDEX IF NOT EXISTS idx_tax_symbol ON tax_items(symbol);

-- ============================================================
-- 境外税收抵免结转表
-- 依据财税〔2020〕3号，超额境外税收抵免可向后结转 5 个纳税年度
-- 每年计算抵免时先检查可用结转额度，优先使用历史结转
-- ============================================================

CREATE TABLE IF NOT EXISTS foreign_tax_credit_carryforward (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_year     INTEGER NOT NULL,     -- 产生结转自的纳税年度
    target_year     INTEGER NOT NULL,     -- 可结转使用的目标年度
    country         TEXT NOT NULL,        -- 'US', 'HK'
    income_category TEXT NOT NULL,        -- 'capital_gain', 'dividend', 'interest'
    carryforward_amount DECIMAL(18, 2) NOT NULL,  -- 可结转金额（原始产生额）
    used_amount     DECIMAL(18, 2) DEFAULT 0,     -- 已使用金额
    remaining_amount DECIMAL(18, 2),              -- 剩余可结转金额
    expires_year    INTEGER NOT NULL,     -- 到期年度（source_year + 5）
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(source_year, country, income_category)
);

CREATE INDEX IF NOT EXISTS idx_ftc_target_year ON foreign_tax_credit_carryforward(target_year);
CREATE INDEX IF NOT EXISTS idx_ftc_expires ON foreign_tax_credit_carryforward(expires_year);

-- ============================================================
-- 年度税务汇总表
-- 每年每个收入类型一条汇总记录
-- ============================================================

CREATE TABLE IF NOT EXISTS tax_summaries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tax_year        INTEGER NOT NULL,
    income_type     TEXT NOT NULL,        -- 'capital_gain', 'dividend', 'rsu'

    total_income_cny DECIMAL(18, 2),      -- 总收入 CNY
    total_deductible_cny DECIMAL(18, 2),  -- 总扣除 CNY
    total_taxable_cny DECIMAL(18, 2),     -- 总应纳税所得 CNY
    total_tax_cny   DECIMAL(18, 2),       -- 总应纳税额 CNY
    total_withheld_cny DECIMAL(18, 2),    -- 总预扣 CNY
    total_credit_cny DECIMAL(18, 2),      -- 总抵免 CNY
    total_excess_cny DECIMAL(18, 2),      -- 总超额未抵免
    total_payable_cny DECIMAL(18, 2),     -- 总应补缴 CNY

    computation_method TEXT,              -- 'per_transaction', 'annual_net', 'single'
    notes           TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(tax_year, income_type)
);

-- ============================================================
-- 初始化默认券商数据
-- ============================================================

INSERT OR IGNORE INTO brokers (code, name_cn, name_en, account_number, base_currency)
VALUES
    ('boci', '中银国际', 'BOCI', NULL, 'USD'),
    ('futu', '富途', 'Futu', NULL, 'HKD'),
    ('longbridge', '长桥', 'Longbridge', NULL, 'USD');
"""


def get_schema_sql() -> str:
    """返回完整的建表 SQL 语句"""
    return SCHEMA_SQL
