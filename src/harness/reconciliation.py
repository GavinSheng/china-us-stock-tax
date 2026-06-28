"""对账校验层 — 导入后自动验证数据一致性和完整性

对应 Harness 规则：
  RC-001: 持仓数量级合理
  RC-002: 月结单文件完整性
  RC-003: 无重复交易（已在 validators.py 实现）
  RC-004: 年末持仓结转一致性
  RC-006: statement_files 状态全 parsed
  RC-007: 期权数据缺口检查
  RC-010: 月结单交易数量匹配
  RC-011: 期权行权完整性检查
  RC-012: 期权过期成本基础检查
  RC-013: 月末持仓连续性检查
  RC-014: 期权生命周期追踪
  RC-015: 月末持仓连续性检查
  RC-016: 期权已过期但无到期记录检测
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from src.harness.validators import MAX_SINGLE_STOCK_POSITION


DB_PATH = Path("output") / "tax.db"


@dataclass
class ReconciliationIssue:
    rule_id: str
    severity: str    # "ERROR" | "WARNING"
    message: str
    details: str = ""

    def __str__(self) -> str:
        return f"[{self.severity}] {self.rule_id}: {self.message}"


@dataclass
class ReconciliationResult:
    issues: list[ReconciliationIssue] = field(default_factory=list)
    passed: bool = True
    stats: dict[str, int] = field(default_factory=dict)

    def add(self, rule_id: str, severity: str, message: str, details: str = ""):
        issue = ReconciliationIssue(rule_id, severity, message, details)
        self.issues.append(issue)
        if severity == "ERROR":
            self.passed = False

    def summary(self) -> str:
        status = "PASSED" if self.passed else "FAILED"
        lines = [f"Reconciliation {status}:"]
        for issue in self.issues:
            lines.append(f"  {issue}")
        for key, val in self.stats.items():
            lines.append(f"  {key}: {val}")
        return "\n".join(lines)


def reconcile_import(db_path: Path | None = None, year: int | None = None) -> ReconciliationResult:
    """对已导入的数据库运行对账检查

    Args:
        db_path: 数据库路径，默认 output/tax.db
        year: 检查的年度，None = 不限制

    Returns:
        ReconciliationResult
    """
    path = db_path or DB_PATH
    result = ReconciliationResult()

    if not path.exists():
        result.add("RC-000", "ERROR", f"数据库不存在: {path}")
        return result

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row

    try:
        _check_statement_files(conn, result, year)
        _check_position_sanity(conn, result, year)
        _check_duplicate_transactions(conn, result, year)
        _check_option_data_gaps(conn, result, year)
        _check_statement_trade_count_match(conn, result, year)
        _check_option_exercise_integrity(conn, result, year)
        _check_option_expire_cost_basis(conn, result, year)
        _check_position_continuity(conn, result, year)
        _check_option_lifecycle(conn, result, year)
        _check_monthly_position_balance(conn, result, year)
        _check_expired_options_without_record(conn, result, year)
        _check_dividend_summary_vs_report(conn, result, year)
        _check_boci_pdf_dividend_coverage(conn, result)
        _collect_stats(conn, result)
    finally:
        conn.close()

    return result


def _check_statement_files(conn: sqlite3.Connection, result: ReconciliationResult, year: int | None):
    """RC-006: 检查 statement_files 状态"""
    # 检查 error/pending 状态
    if year:
        where_err = "WHERE statement_month LIKE ? AND status IN ('error', 'pending')"
        err_params: tuple = (f"{year}-%",)
    else:
        where_err = "WHERE status IN ('error', 'pending')"
        err_params = ()

    rows = conn.execute(f"""
        SELECT id, broker_code, statement_month, status, error_message
        FROM statement_files {where_err}
        ORDER BY statement_month
    """, err_params).fetchall()

    for r in rows:
        msg = f"broker={r['broker_code']}, month={r['statement_month']}, status={r['status']}"
        if r["error_message"]:
            msg += f", error={r['error_message']}"
        result.add("RC-006", "ERROR", f"月结单文件状态异常: {msg}")

    # RC-002: 检查月份完整性
    if year:
        all_months = conn.execute("""
            SELECT DISTINCT statement_month, broker_code
            FROM statement_files
            WHERE statement_month LIKE ?
            ORDER BY statement_month
        """, (f"{year}-%",)).fetchall()

        # 按 broker 分组检查
        broker_months: dict[str, set[str]] = {}
        for r in all_months:
            broker = r["broker_code"]
            if broker not in broker_months:
                broker_months[broker] = set()
            broker_months[broker].add(r["statement_month"])

        for broker, months in broker_months.items():
            sorted_months = sorted(months)
            # 检查连续性
            if len(sorted_months) < 12:
                result.add("RC-002", "WARNING",
                           f"{broker}: {year} 年仅有 {len(sorted_months)} 个月结单 "
                           f"({', '.join(sorted_months)})，可能有缺失")


def _check_position_sanity(conn: sqlite3.Connection, result: ReconciliationResult, year: int | None):
    """RC-001: 持仓数量级检查 — 防止解析错误"""
    where = "WHERE strftime('%Y', as_of_date) = ? AND quantity > ?" if year else "WHERE quantity > ?"
    params = (str(year), MAX_SINGLE_STOCK_POSITION) if year else (MAX_SINGLE_STOCK_POSITION,)

    rows = conn.execute(f"""
        SELECT broker_code, symbol, as_of_date, quantity, statement_file_id
        FROM positions {where}
        ORDER BY quantity DESC
    """, params).fetchall()

    for r in rows:
        result.add("RC-001", "ERROR",
                   f"持仓数量异常: {r['broker_code']} {r['symbol']} "
                   f"on {r['as_of_date']} qty={r['quantity']} "
                   f"(可能为解析错误，file_id={r['statement_file_id']})")


def _check_duplicate_transactions(conn: sqlite3.Connection, result: ReconciliationResult, year: int | None):
    """RC-003: 数据库层面重复交易检查

    使用 reference_no 作为去重键（Longbridge 同一订单可能多次成交，
    reference_no 不同但 date/symbol/qty/price 相同）。

    对于没有 reference_no 的交易，回退到 (broker, date, symbol, action, qty, price) 组合键。
    """
    year_cond = "AND strftime('%Y', trade_date) = ?" if year else ""
    params = (str(year),) if year else ()

    # 检查 1: 相同 reference_no 出现多次（真正的重复）
    rows = conn.execute(f"""
        SELECT reference_no, broker_code, trade_date, symbol, action,
               COUNT(*) as cnt, GROUP_CONCAT(id) as ids
        FROM transactions
        WHERE reference_no IS NOT NULL AND reference_no != '' {year_cond}
        GROUP BY reference_no
        HAVING cnt > 1
        ORDER BY cnt DESC
    """, params).fetchall()

    for r in rows:
        result.add("RC-003", "ERROR",
                   f"重复交易 (reference_no={r['reference_no']}): "
                   f"{r['broker_code']} {r['trade_date']} {r['symbol']} "
                   f"{r['action']}, 出现 {r['cnt']} 次, IDs=[{r['ids']}]")

    # 检查 2: 无 reference_no 的交易，用组合键去重
    rows = conn.execute(f"""
        SELECT broker_code, trade_date, symbol, action, quantity, price,
               COUNT(*) as cnt, GROUP_CONCAT(id) as ids
        FROM transactions
        WHERE (reference_no IS NULL OR reference_no = '') {year_cond}
        GROUP BY broker_code, trade_date, symbol, action, quantity, price
        HAVING cnt > 1
        ORDER BY cnt DESC
    """, params).fetchall()

    for r in rows:
        result.add("RC-003", "ERROR",
                   f"重复交易 (无reference_no): {r['broker_code']} {r['trade_date']} {r['symbol']} "
                   f"{r['action']} qty={r['quantity']} price={r['price']}, "
                   f"出现 {r['cnt']} 次, IDs=[{r['ids']}]")


def _check_option_data_gaps(conn: sqlite3.Connection, result: ReconciliationResult, year: int | None):
    """RC-007: 检查期权交易数据缺口 — 有卖出但无对应买入

    区分写仓（sell-to-open，合法策略）和真实数据缺口：
    - 买入 > 0 且卖出 > 买入 + 过期 = 可能缺失买入记录
    - 买入 == 0 且卖出 > 0 = 可能是纯写仓策略（正常）
    """
    year_cond = "WHERE strftime('%Y', trade_date) = ? AND" if year else "WHERE"
    params = (str(year),) if year else ()

    rows = conn.execute(f"""
        SELECT symbol,
               SUM(CASE WHEN action = 'option_buy' THEN quantity ELSE 0 END) as bought,
               SUM(CASE WHEN action = 'option_sell' THEN quantity ELSE 0 END) as sold,
               SUM(CASE WHEN action = 'option_expire' THEN quantity ELSE 0 END) as expired
        FROM transactions
        {year_cond} symbol LIKE '%OPT%'
        GROUP BY symbol
        HAVING sold > 0
        ORDER BY symbol
    """, params).fetchall()

    for r in rows:
        # 纯写仓（无买入、有过期）= 合法策略，跳过
        if r['bought'] == 0 and r['expired'] == 0:
            continue

        # 买入 > 0 但卖出超出可平仓范围 = 数据缺口
        missing = r['sold'] - r['bought'] - r['expired']
        if missing > 0 and r['bought'] > 0:
            sell_amount = conn.execute("""
                SELECT COALESCE(SUM(amount), 0) FROM transactions
                WHERE symbol = ? AND action = 'option_sell'
            """, (r['symbol'],)).fetchone()[0]

            result.add("RC-007", "WARNING",
                       f"期权数据缺口: {r['symbol']} — "
                       f"买入 {r['bought']} 份, 卖出 {r['sold']} 份, "
                       f"缺失 {missing} 份买入记录 (卖出金额 ${sell_amount:.2f})。"
                       f"缺少买入成本，无法正确计算盈亏。")


def _collect_stats(conn: sqlite3.Connection, result: ReconciliationResult):
    """收集统计信息"""
    stats = {}

    stats["brokers"] = conn.execute("SELECT COUNT(*) FROM brokers").fetchone()[0]
    stats["statement_files"] = conn.execute("SELECT COUNT(*) FROM statement_files").fetchone()[0]
    stats["transactions"] = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    stats["dividends"] = conn.execute("SELECT COUNT(*) FROM dividends").fetchone()[0]
    stats["tax_lots"] = conn.execute("SELECT COUNT(*) FROM tax_lots WHERE remaining > 0").fetchone()[0]
    stats["positions"] = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]

    result.stats.update(stats)


def _check_statement_trade_count_match(conn: sqlite3.Connection, result: ReconciliationResult, year: int | None):
    """RC-010: 月结单交易数量匹配检查

    对比每个月结单文件中应该解析的交易数量与实际数据库记录。
    通过检查交易记录数量是否合理来发现解析遗漏。

    检查逻辑：
    - 每个已解析的月结单(statement_files)必须有对应的交易记录
    - 如果某月结单没有交易记录，可能是解析失败或遗漏
    """
    year_cond = "AND strftime('%Y', trade_date) = ?" if year else ""
    stmt_cond = "AND statement_month LIKE ?" if year else ""
    params = (str(year),) if year else ()
    stmt_params = (f"{year}-%",) if year else ()

    # 检查每个已解析的月结单是否有对应的交易记录
    parsed_files = conn.execute(f"""
        SELECT id, broker_code, statement_month, parsed_at, page_count
        FROM statement_files
        WHERE status = 'parsed' {stmt_cond}
        ORDER BY statement_month
    """, stmt_params).fetchall()

    for stmt in parsed_files:
        stmt_id = stmt["id"]
        stmt_month = stmt["statement_month"]
        broker = stmt["broker_code"]

        # 检查该月结单关联的交易记录数量
        txn_count = conn.execute(f"""
            SELECT COUNT(*) as cnt
            FROM transactions
            WHERE statement_file_id = ? {year_cond}
        """, (stmt_id,) + params).fetchone()["cnt"]

        # 检查分红记录数量
        div_count = conn.execute("""
            SELECT COUNT(*) as cnt
            FROM dividends
            WHERE statement_file_id = ?
        """, (stmt_id,)).fetchone()["cnt"]

        # 检查持仓记录数量
        pos_count = conn.execute("""
            SELECT COUNT(*) as cnt
            FROM positions
            WHERE statement_file_id = ?
        """, (stmt_id,)).fetchone()["cnt"]

        # 如果月结单有页面但完全没有解析出任何记录，可能是解析失败
        if stmt["page_count"] and stmt["page_count"] > 1:
            if txn_count == 0 and div_count == 0 and pos_count == 0:
                result.add("RC-010", "ERROR",
                           f"月结单解析遗漏: {broker} {stmt_month} (file_id={stmt_id}, "
                           f"{stmt['page_count']}页) — 无任何交易/分红/持仓记录，可能解析失败")
            elif txn_count == 0 and pos_count == 0:
                # 有分红但无交易和持仓，可能是纯分红月结单
                result.add("RC-010", "WARNING",
                           f"月结单可能遗漏: {broker} {stmt_month} (file_id={stmt_id}) — "
                           f"仅有分红记录({div_count}条)，无交易和持仓")

        result.stats[f"stmt_{broker}_{stmt_month}_txns"] = txn_count
        result.stats[f"stmt_{broker}_{stmt_month}_divs"] = div_count

    # 检查是否有交易记录没有关联的月结单文件
    orphan_txns = conn.execute(f"""
        SELECT COUNT(*) as cnt
        FROM transactions
        WHERE statement_file_id IS NULL {year_cond}
    """, params).fetchone()["cnt"]

    if orphan_txns > 0:
        result.add("RC-010", "WARNING",
                   f"有 {orphan_txns} 条交易记录未关联月结单文件，可能是手动导入或数据迁移遗留")


def _check_option_exercise_integrity(conn: sqlite3.Connection, result: ReconciliationResult, year: int | None):
    """RC-011: 期权行权完整性检查

    期权行权必须有：
    1. 行权买入股票记录 (action='option_exercise' 或 buy)
    2. 期权 lot 消耗记录
    3. 股票成本 = 行权价 + 权利金

    检查逻辑：
    - 每条期权行权记录应该有对应的股票买入和期权消耗
    - 行权买入的股票成本应该包含权利金
    """
    year_cond = "AND strftime('%Y', trade_date) = ?" if year else ""
    params = (str(year),) if year else ()

    # 查找所有期权行权买入股票的记录
    exercise_txns = conn.execute(f"""
        SELECT id, broker_code, trade_date, symbol, quantity, price, amount,
               currency, exchange_rate
        FROM transactions
        WHERE action = 'option_exercise' {year_cond}
        ORDER BY trade_date
    """, params).fetchall()

    for txn in exercise_txns:
        txn_id = txn["id"]
        symbol = txn["symbol"]
        date_str = txn["trade_date"]
        quantity = txn["quantity"]
        exercise_price = txn["price"]

        # 检查是否有对应的 tax_lot（行权买入的股票应该有 lot）
        lot = conn.execute("""
            SELECT id, cost_per_share, acquisition_type, source_txn_id
            FROM tax_lots
            WHERE symbol = ? AND acquisition_date = ? AND acquisition_type = 'exercise'
            LIMIT 1
        """, (symbol, date_str)).fetchone()

        if lot is None:
            result.add("RC-011", "ERROR",
                       f"期权行权缺少股票lot: {symbol} 于 {date_str} 行权买入 {quantity} 股，"
                       f"但 tax_lots 表中无对应记录，无法追踪成本")
        else:
            # 检查成本是否合理（行权价 ≤ 成本 ≤ 行权价+最大权利金）
            # 这里只做简单检查，实际成本应该 = 行权价 + 权利金
            lot_cost = lot["cost_per_share"]
            if lot_cost < exercise_price:
                result.add("RC-011", "WARNING",
                           f"期权行权成本可疑: {symbol} 行权价 ${exercise_price:.2f}, "
                           f"但 lot 成本 ${lot_cost:.2f} (应 ≥ 行权价)")

        # 检查是否有对应的期权消耗记录
        # 期权行权时应该消耗对应的期权 lot
        opt_symbol_pattern = f"{symbol}_OPT_%"
        consumed_lot = conn.execute("""
            SELECT id, symbol, quantity, remaining, cost_per_share
            FROM tax_lots
            WHERE symbol LIKE ? AND remaining = 0 AND acquisition_date <= ?
            ORDER BY acquisition_date DESC LIMIT 1
        """, (opt_symbol_pattern, date_str)).fetchone()

        if consumed_lot is None:
            result.add("RC-011", "WARNING",
                       f"期权行权缺少期权消耗记录: {symbol} 于 {date_str} 行权，"
                       f"但未找到消耗的期权 lot，可能无法正确计算成本基础")

    # 检查是否有期权行权消耗记录但没有对应的股票买入
    exercise_consumes = conn.execute("""
        SELECT DISTINCT tl.symbol as opt_symbol, tl.acquisition_date, tl.quantity, tl.remaining
        FROM tax_lots tl
        WHERE tl.acquisition_type = 'buy'
          AND tl.symbol LIKE '%_OPT_%'
          AND tl.remaining = 0
          AND NOT EXISTS (
              SELECT 1 FROM transactions t
              WHERE t.action = 'option_exercise'
                AND t.trade_date = tl.acquisition_date
                AND t.symbol LIKE SUBSTR(tl.symbol, 1, INSTR(tl.symbol, '_OPT_') - 1)
          )
    """, ()).fetchall()

    for lot in exercise_consumes:
        # 提取 underlying symbol
        opt_sym = lot["opt_symbol"]
        underlying = opt_sym.split("_OPT_")[0] if "_OPT_" in opt_sym else opt_sym

        result.add("RC-011", "WARNING",
                   f"期权消耗无行权记录: {opt_sym} 于 {lot['acquisition_date']} 被消耗，"
                   f"但未找到 {underlying} 的行权买入记录")


def _check_option_expire_cost_basis(conn: sqlite3.Connection, result: ReconciliationResult, year: int | None):
    """RC-012: 期权过期成本基础检查

    期权过期必须有对应的期权买入 lot，过期损失需正确计算成本基础。

    检查逻辑：
    - 每条期权过期记录应该有对应的期权 lot
    - 过期损失 = 买入数量 × 权利金
    """
    year_cond = "AND strftime('%Y', trade_date) = ?" if year else ""
    params = (str(year),) if year else ()

    # 查找所有期权过期记录
    expire_txns = conn.execute(f"""
        SELECT id, broker_code, trade_date, symbol, quantity, amount, currency, raw_data
        FROM transactions
        WHERE action = 'option_expire' {year_cond}
        ORDER BY trade_date
    """, params).fetchall()

    import json
    for txn in expire_txns:
        txn_id = txn["id"]
        symbol = txn["symbol"]
        date_str = txn["trade_date"]
        expire_qty = txn["quantity"]

        # 跳过自动生成的 expire 记录（可能没有对应 tax_lot）
        raw = txn["raw_data"]
        if raw:
            try:
                raw_dict = json.loads(raw)
                if raw_dict.get("auto_generated"):
                    continue
            except (json.JSONDecodeError, TypeError):
                pass

        # 检查是否有对应的期权 lot（买入时创建的）
        # 期权过期应该消耗对应的 lot
        available_lots = conn.execute("""
            SELECT id, quantity, remaining, cost_per_share, acquisition_type
            FROM tax_lots
            WHERE symbol = ? AND remaining > 0 AND acquisition_date <= ?
            ORDER BY acquisition_date
        """, (symbol, date_str)).fetchall()

        if len(available_lots) == 0:
            # 检查是否已经被消耗（可能是其他校验已报错）
            consumed_lots = conn.execute("""
                SELECT id, quantity, remaining, cost_per_share
                FROM tax_lots
                WHERE symbol = ? AND remaining = 0 AND acquisition_date <= ?
                ORDER BY acquisition_date DESC
            """, (symbol, date_str)).fetchall()

            if len(consumed_lots) == 0:
                result.add("RC-012", "ERROR",
                           f"期权过期缺少成本基础: {symbol} 于 {date_str} 过期 {expire_qty} 份，"
                           f"但 tax_lots 表中无任何 lot 记录，无法计算过期损失")
            else:
                # 检查消耗数量是否匹配
                total_consumed = sum(l["quantity"] for l in consumed_lots)
                if total_consumed < expire_qty:
                    result.add("RC-012", "WARNING",
                               f"期权过期数量不匹配: {symbol} 过期 {expire_qty} 份，"
                               f"但仅有 {total_consumed} 份 lot 记录")
        else:
            # 检查可用数量是否足够
            available_qty = sum(l["remaining"] for l in available_lots)
            if available_qty < expire_qty:
                result.add("RC-012", "WARNING",
                           f"期权过期数量不足: {symbol} 过期 {expire_qty} 份，"
                           f"但仅有 {available_qty} 份可用 lot")

        # 检查过期记录的 amount（应该是负数，代表损失）
        if txn["amount"] is not None and float(txn["amount"]) >= 0:
            result.add("RC-012", "WARNING",
                       f"期权过期金额可疑: {symbol} 于 {date_str} 过期，"
                       f"amount={txn['amount']} (过期损失应为负数或零)")


def _check_position_continuity(conn: sqlite3.Connection, result: ReconciliationResult, year: int | None):
    """RC-013: 月末持仓连续性检查

    验证月末持仓与交易记录的一致性：
    月末持仓 ≈ 月初持仓 + 买入 - 卖出 + RSU归属

    检查逻辑：
    - 对每个有持仓记录的月份，计算交易变动后的持仓
    - 与实际月末持仓对比，差异过大则报警
    """
    if not year:
        return  # 需要 year 参数才能计算

    # 获取年初持仓（从 carryforward lots）
    year_start_lots = conn.execute("""
        SELECT symbol, broker_code, SUM(remaining) as total_qty
        FROM tax_lots
        WHERE acquisition_type = 'carryforward'
          AND acquisition_date = ?
        GROUP BY symbol, broker_code
    """, (f"{year-1}-12-31",)).fetchall()

    initial_holdings: dict[tuple[str, str], int] = {}
    for lot in year_start_lots:
        key = (lot["symbol"], lot["broker_code"])
        initial_holdings[key] = lot["total_qty"]

    # 获取年末持仓
    year_end_positions = conn.execute("""
        SELECT symbol, broker_code, quantity
        FROM positions
        WHERE as_of_date = ?
    """, (f"{year}-12-31",)).fetchall()

    # 计算当年交易变动
    buy_actions = ("buy", "option_buy", "rsu_vest", "option_exercise")
    sell_actions = ("sell", "option_sell", "rsu_sell", "option_expire")

    trade_changes = conn.execute("""
        SELECT symbol, broker_code,
               SUM(CASE WHEN action IN ('buy', 'option_exercise', 'rsu_vest') THEN quantity ELSE 0 END) as bought,
               SUM(CASE WHEN action IN ('sell', 'rsu_sell') THEN quantity ELSE 0 END) as sold,
               SUM(CASE WHEN action = 'option_expire' THEN quantity ELSE 0 END) as expired
        FROM transactions
        WHERE strftime('%Y', trade_date) = ?
        GROUP BY symbol, broker_code
    """, (str(year),)).fetchall()

    for change in trade_changes:
        symbol = change["symbol"]
        broker = change["broker_code"]
        key = (symbol, broker)

        initial = initial_holdings.get(key, 0)
        bought = change["bought"] or 0
        sold = change["sold"] or 0
        expired = change["expired"] or 0

        expected_end = initial + bought - sold

        # 查找实际年末持仓
        actual_end = 0
        for pos in year_end_positions:
            if pos["symbol"] == symbol and pos["broker_code"] == broker:
                actual_end = pos["quantity"]
                break

        # 如果有差异，报告
        if abs(expected_end - actual_end) > 0:
            diff = actual_end - expected_end
            result.add("RC-013", "WARNING",
                       f"持仓连续性差异: {broker}/{symbol} — "
                       f"年初{initial} + 买入{bought} - 卖出{sold} = {expected_end}，"
                       f"实际年末{actual_end}，差异{diff}股 "
                       f"(可能存在交易遗漏或月结单数据缺失)")


def _check_option_lifecycle(conn: sqlite3.Connection, result: ReconciliationResult, year: int | None):
    """RC-014: 期权生命周期追踪

    检查期权从买入 → 卖出/行权/过期的完整追踪：
    - 买入期权后必须有后续的终结操作
    - 未终结的期权 lot 应该还在持仓中

    检查逻辑：
    - 对于每笔期权买入，追踪其后续处理
    - 检查是否有未终结的期权 lot（可能是过期未处理）
    """
    year_cond = "AND strftime('%Y', trade_date) = ?" if year else ""
    params = (str(year),) if year else ()

    # 查找所有期权买入记录
    option_buys = conn.execute(f"""
        SELECT id, broker_code, trade_date, symbol, quantity, price, amount
        FROM transactions
        WHERE action = 'option_buy' {year_cond}
        ORDER BY trade_date
    """, params).fetchall()

    for buy in option_buys:
        symbol = buy["symbol"]
        buy_date = buy["trade_date"]
        buy_qty = buy["quantity"]

        # 查找该期权后续的卖出、行权、过期记录
        sell_qty = conn.execute("""
            SELECT COALESCE(SUM(quantity), 0) as total
            FROM transactions
            WHERE symbol = ? AND action = 'option_sell' AND trade_date > ?
        """, (symbol, buy_date)).fetchone()["total"]

        exercise_qty = conn.execute("""
            SELECT COALESCE(SUM(quantity), 0) as total
            FROM transactions
            WHERE action = 'option_exercise' AND trade_date > ?
              AND symbol LIKE SUBSTR(?, 1, INSTR(?, '_OPT_') - 1)
        """, (buy_date, symbol, symbol)).fetchone()["total"]

        expire_qty = conn.execute("""
            SELECT COALESCE(SUM(quantity), 0) as total
            FROM transactions
            WHERE symbol = ? AND action = 'option_expire' AND trade_date > ?
        """, (symbol, buy_date)).fetchone()["total"]

        total_closed = sell_qty + expire_qty

        # 检查是否有未终结的期权
        remaining = buy_qty - total_closed

        if remaining > 0:
            # 检查是否还有持仓记录（可能是仍在持有的期权）
            lot_remaining = conn.execute("""
                SELECT COALESCE(SUM(remaining), 0) as total
                FROM tax_lots
                WHERE symbol = ? AND remaining > 0
            """, (symbol,)).fetchone()["total"]

            if lot_remaining == 0:
                # 没有持仓记录，但有未终结的期权买入，可能是数据缺失
                result.add("RC-014", "WARNING",
                           f"期权生命周期不完整: {symbol} 于 {buy_date} 买入 {buy_qty} 份，"
                           f"卖出{sell_qty} + 过期{expire_qty} = {total_closed}，"
                           f"剩余{remaining}份无 lot 记录（可能过期未处理或数据遗漏）")
            elif lot_remaining != remaining:
                result.add("RC-014", "WARNING",
                           f"期权生命周期数量不匹配: {symbol} 买入{buy_qty}份，"
                           f"终结{total_closed}份，应有{remaining}份，"
                           f"但 lot 表显示{lot_remaining}份")

    # 检查是否有未终结的期权 lot（已过期的期权应该 remaining=0）
    stale_options = conn.execute("""
        SELECT symbol, acquisition_date, quantity, remaining, cost_per_share
        FROM tax_lots
        WHERE symbol LIKE '%_OPT_%' AND remaining > 0 AND acquisition_date < DATE('now', '-30 days')
    """, ()).fetchall()

    for lot in stale_options:
        # 期权已超过30天未终结，可能已经过期但未处理
        result.add("RC-014", "WARNING",
                   f"期权可能过期未处理: {lot['symbol']} 于 {lot['acquisition_date']} 买入，"
                   f"剩余{lot['remaining']}份，距今已超过30天 "
                   f"(可能已过期但未生成 option_expire 记录)")


def _parse_option_expiry(symbol: str) -> str | None:
    """从期权 symbol 提取到期日字符串 (YYYY-MM-DD)

    例如: XPEV_OPT_250221_15.0_C → "2025-02-21"
    """
    if "_OPT_" not in symbol:
        return None
    parts = symbol.split("_OPT_")
    if len(parts) < 2:
        return None
    date_part = parts[1].split("_")[0]  # "250221"
    if len(date_part) != 6:
        return None
    try:
        yy, mm, dd = int(date_part[:2]), int(date_part[2:4]), int(date_part[4:6])
        year = 2000 + yy if yy < 100 else yy
        return f"{year:04d}-{mm:02d}-{dd:02d}"
    except (ValueError, IndexError):
        return None


def _check_monthly_position_balance(conn: sqlite3.Connection, result: ReconciliationResult, year: int | None):
    """RC-015: 月末持仓平衡校验

    对每个券商的每个 symbol，逐月验证：
    月末持仓 = 上月末持仓 + 本月买入 - 本月卖出

    如果不等说明有交易遗漏。
    """
    if not year:
        return

    # 获取所有有持仓快照的月份（按 broker + symbol + month 分组）
    months = conn.execute("""
        SELECT DISTINCT broker_code, symbol,
               strftime('%Y-%m', as_of_date) as month,
               as_of_date
        FROM positions
        WHERE strftime('%Y', as_of_date) = ?
        ORDER BY broker_code, symbol, as_of_date
    """, (str(year),)).fetchall()

    if not months:
        return

    # 按 (broker, symbol) 分组，逐月追踪
    groups: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for m in months:
        key = (m["broker_code"], m["symbol"])
        if key not in groups:
            groups[key] = []
        groups[key].append((m["month"], m["as_of_date"]))

    for (broker, symbol), month_list in groups.items():
        # 获取上年末 positions 快照作为年初起始持仓
        dec31_row = conn.execute("""
            SELECT quantity FROM positions
            WHERE UPPER(symbol) = UPPER(?)
              AND (broker_code = ? OR (? = '' AND broker_code IS NULL))
              AND as_of_date = ?
        """, (symbol, broker if broker else None, broker, f"{year-1}-12-31")).fetchone()
        expected = dec31_row["quantity"] if dec31_row else None

        prev_month = None
        prev_expected: int | None = expected
        for month_str, as_of_date in month_list:
            # 计算从起始点到本月末的全部交易变动（包含所有历史交易，不限年度）
            # 使用 UPPER(symbol) 匹配，因为 positions 和 transactions 的 symbol 大小写可能不一致
            start_date = f"{year-1}-12-31" if expected is not None else "2000-01-01"
            txns = conn.execute("""
                SELECT
                    COALESCE(SUM(CASE WHEN action IN ('buy', 'option_exercise', 'rsu_vest') THEN quantity ELSE 0 END), 0) as bought,
                    COALESCE(SUM(CASE WHEN action IN ('sell', 'rsu_sell') THEN quantity ELSE 0 END), 0) as sold,
                    COALESCE(SUM(CASE WHEN action = 'option_expire' THEN quantity ELSE 0 END), 0) as expired
                FROM transactions
                WHERE UPPER(symbol) = UPPER(?)
                  AND (broker_code = ? OR (? = '' AND broker_code IS NULL))
                  AND trade_date > ?
                  AND trade_date <= ?
                  AND action NOT IN ('dividend', 'interest', 'fee', 'option_buy', 'option_write')
            """, (symbol, broker if broker else None, broker, start_date, as_of_date)).fetchone()

            bought = txns["bought"] or 0
            sold = txns["sold"] or 0
            expired = txns["expired"] or 0

            if expected is not None:
                calc = expected + bought - sold - expired
            else:
                # 无上年快照：用"首次快照 - 到该月的交易变动"反推起始持仓
                # 如果反推起始持仓 < 0，说明有数据缺口
                calc = None

            # 实际月末持仓
            actual = conn.execute("""
                SELECT quantity FROM positions
                WHERE broker_code = ? AND symbol = ? AND as_of_date = ?
            """, (broker if broker else None, symbol, as_of_date)).fetchone()

            actual_qty = actual["quantity"] if actual else 0

            if calc is not None and calc != actual_qty:
                diff = actual_qty - calc
                result.add("RC-015", "WARNING",
                           f"月末持仓不平衡: {broker}/{symbol} {month_str} — "
                           f"预期{calc}股，实际{actual_qty}股，差异{diff}股 "
                           f"(可能存在交易遗漏或重复)")
            elif calc is None:
                # 无上年快照：检查月间 delta 是否匹配交易变动
                if prev_expected is not None and prev_month:
                    # 月间交易变动
                    delta_txns = conn.execute("""
                        SELECT
                            COALESCE(SUM(CASE WHEN action IN ('buy', 'option_exercise', 'rsu_vest') THEN quantity ELSE 0 END), 0) as bought,
                            COALESCE(SUM(CASE WHEN action IN ('sell', 'rsu_sell') THEN quantity ELSE 0 END), 0) as sold,
                            COALESCE(SUM(CASE WHEN action = 'option_expire' THEN quantity ELSE 0 END), 0) as expired
                        FROM transactions
                        WHERE UPPER(symbol) = UPPER(?)
                          AND (broker_code = ? OR (? = '' AND broker_code IS NULL))
                          AND trade_date > ?
                          AND trade_date <= ?
                          AND action NOT IN ('dividend', 'interest', 'fee', 'option_buy', 'option_write')
                    """, (symbol, broker if broker else None, broker, prev_month, as_of_date)).fetchone()
                    expected_delta = prev_expected + (delta_txns["bought"] or 0) - (delta_txns["sold"] or 0) - (delta_txns["expired"] or 0)
                    if expected_delta != actual_qty:
                        diff = actual_qty - expected_delta
                        result.add("RC-015", "WARNING",
                                   f"月末持仓不平衡: {broker}/{symbol} {month_str} — "
                                   f"预期{expected_delta}股，实际{actual_qty}股，差异{diff}股 "
                                   f"(可能存在交易遗漏或重复)")

            prev_month = as_of_date
            if calc is not None:
                prev_expected = calc


def _check_expired_options_without_record(conn: sqlite3.Connection, result: ReconciliationResult, year: int | None):
    """RC-016: 期权已过期但无到期记录检测

    解析期权 symbol 中的到期日，如果已过期但无 option_expire 记录：
    - 标记为潜在多缴税风险（损失未计入年度净额抵扣）
    - 检查对应 lot 是否已被消耗
    """
    if not year:
        return

    year_end = f"{year}-12-31"

    # 查找所有期权 lot（包括 remaining > 0 的，因为 DB 可能未更新）
    option_symbols = conn.execute("""
        SELECT DISTINCT symbol, broker_code
        FROM tax_lots
        WHERE symbol LIKE '%_OPT_%'
    """, ()).fetchall()

    seen: set[tuple[str, str]] = set()
    for lot in option_symbols:
        symbol = lot["symbol"]
        broker = lot["broker_code"]
        key = (symbol, broker)
        if key in seen:
            continue
        seen.add(key)

        expiry_date = _parse_option_expiry(symbol)
        if not expiry_date:
            continue

        # 到期日在当年内或之前
        if expiry_date > year_end:
            continue

        # 跨年数据：上一年过期且已无剩余成本的，属于历史遗留问题，不在此年度的校验范围内
        expiry_year = int(expiry_date[:4])
        if expiry_year < year:
            # 检查是否还有未处理的剩余成本
            remaining_cost_check = conn.execute("""
                SELECT COALESCE(SUM(remaining * cost_per_share), 0) as cost
                FROM tax_lots
                WHERE symbol = ? AND remaining > 0
            """, (symbol,)).fetchone()["cost"] or 0
            if remaining_cost_check == 0:
                continue  # 已无剩余，属历史问题，跳过
            # 仍有剩余成本但过期日在上年之前：说明是 2025 calc 系统建立前的历史数据
            # 这些选项的损失尚未被 calc-db 处理（因为 2024 calc 可能未运行）
            # 检查是否有任意年份的 expire 记录
            any_expire = conn.execute("""
                SELECT COUNT(*) as cnt FROM transactions
                WHERE symbol = ? AND action = 'option_expire'
            """, (symbol,)).fetchone()["cnt"]
            if any_expire > 0:
                continue  # 已有 expire 记录，跳过

        # 检查是否有 option_expire 记录（按到期年份检查，而非校验年份）
        expiry_year = expiry_date[:4]
        expire_count = conn.execute("""
            SELECT COUNT(*) as cnt FROM transactions
            WHERE symbol = ? AND action = 'option_expire'
              AND strftime('%Y', trade_date) = ?
        """, (symbol, expiry_year)).fetchone()["cnt"]

        # 已有 expire 记录则跳过（即使 tax_lots.remaining 未更新）
        if expire_count > 0:
            continue

        # 检查是否有 lot 消耗记录（通过卖出消耗，说明不是真的过期未处理）
        consumed = conn.execute("""
            SELECT COALESCE(SUM(lc.consumed_qty), 0) as total
            FROM lot_consumptions lc
            JOIN transactions t ON lc.sell_txn_id = t.id
            JOIN tax_lots tl ON lc.tax_lot_id = tl.id
            WHERE tl.symbol = ? AND strftime('%Y', t.trade_date) = ?
        """, (symbol, str(year))).fetchone()["total"] or 0

        total_bought = conn.execute("""
            SELECT COALESCE(SUM(quantity), 0) as total
            FROM tax_lots
            WHERE symbol = ?
        """, (symbol,)).fetchone()["total"] or 0

        # 如果全部通过卖出消耗，说明不是过期未记录
        if consumed >= total_bought and total_bought > 0:
            continue

        # 确实缺少 expire 记录
        remaining_cost = conn.execute("""
            SELECT COALESCE(SUM(remaining * cost_per_share), 0) as cost
            FROM tax_lots
            WHERE symbol = ? AND remaining > 0
        """, (symbol,)).fetchone()["cost"] or 0

        # 剩余成本为 $0，无税务风险，跳过
        if remaining_cost == 0:
            continue

        result.add("RC-016", "WARNING",
                   f"期权已过期但无到期记录: {symbol} 到期日={expiry_date}，"
                   f"买入{total_bought}份，消耗{consumed}份，"
                   f"剩余成本${remaining_cost:.2f}未计入年度净额抵扣 "
                   f"(可能导致多缴税)")


def _check_dividend_summary_vs_report(conn: sqlite3.Connection, result: ReconciliationResult, year: int | None):
    """RC-005: 股息汇总与券商报告对比 — dividends 表总额应与 transactions 表一致"""
    where = ""
    params: tuple = ()
    if year:
        where = "WHERE strftime('%Y', payment_date) = ?"
        params = (str(year),)

    # 从 dividends 表汇总
    div_summary = conn.execute(f"""
        SELECT broker_code,
               COUNT(*) as div_count,
               COALESCE(SUM(gross_amount), 0) as total_gross,
               COALESCE(SUM(withholding_tax), 0) as total_wh
        FROM dividends {where}
        GROUP BY broker_code
    """, params).fetchall()

    div_by_broker: dict[str, dict] = {}
    for r in div_summary:
        div_by_broker[r["broker_code"]] = {
            "count": r["div_count"],
            "gross": r["total_gross"],
            "wh": r["total_wh"],
        }

    # 从 transactions 表汇总（action='dividend'）
    txn_where = "WHERE action = 'dividend'"
    txn_params: tuple = ()
    if year:
        txn_where += " AND strftime('%Y', trade_date) = ?"
        txn_params = (str(year),)

    txn_summary = conn.execute(f"""
        SELECT broker_code,
               COUNT(*) as txn_count,
               COALESCE(SUM(amount), 0) as total_amount,
               COALESCE(SUM(tax_withheld), 0) as total_tax_wh
        FROM transactions {txn_where}
        GROUP BY broker_code
    """, txn_params).fetchall()

    for r in txn_summary:
        broker = r["broker_code"]
        div_info = div_by_broker.get(broker, {})
        div_gross = div_info.get("gross", 0)
        div_wh = div_info.get("wh", 0)
        txn_amount = r["total_amount"]
        txn_wh = r["total_tax_wh"]

        # dividends 表可能包含更多历史数据，只检查反向：transactions 有但 dividends 没有
        if r["txn_count"] > 0 and div_gross == 0:
            result.add("RC-005", "WARNING",
                       f"{broker} 有 {r['txn_count']} 笔股息交易（总额 ${txn_amount:.2f}），"
                       f"但 dividends 表无对应记录")

        # 预扣税对比
        if abs(txn_wh - div_wh) > Decimal("0.01") and txn_wh > 0 and div_wh > 0:
            result.add("RC-005", "WARNING",
                       f"{broker} 预扣税不一致: "
                       f"transactions=${txn_wh:.2f}, dividends=${div_wh:.2f}, "
                       f"差异 ${abs(txn_wh - div_wh):.2f}")


def _check_boci_pdf_dividend_coverage(conn: sqlite3.Connection, result: ReconciliationResult):
    """RC-017: 扫描 BOCI PDF 月结单中的分红关键词，验证 dividends 表有对应记录。

    BOCI 分红由 _parse_dividends 单独解析（不进 transactions 表），
    如果解析器漏掉某个 PDF 中的分红，dividends 表不会有记录。
    此检查直接扫描 PDF 文本，确保没有遗漏。
    """
    import pdfplumber
    from src.database.importers.shared_utils import DECRYPTED_DIR

    # 获取所有 BOCI 月结单文件
    boci_files = conn.execute("""
        SELECT id, statement_month, file_path
        FROM statement_files
        WHERE broker_code = 'boci'
        ORDER BY statement_month
    """).fetchall()

    for sf in boci_files:
        pdf_path = Path(sf["file_path"])
        if not pdf_path.exists():
            continue

        # 提取 PDF 文本
        try:
            with pdfplumber.open(pdf_path) as pdf:
                text = ""
                for page in pdf.pages:
                    text += page.extract_text() or ""
        except Exception:
            continue

        # 检查是否有分红关键词
        has_dividend = "股息" in text or "dividend" in text.lower()
        if not has_dividend:
            continue

        # 统计该月结单对应的 dividends 记录数
        div_count = conn.execute(
            "SELECT COUNT(*) FROM dividends WHERE statement_file_id = ?",
            (sf["id"],)
        ).fetchone()[0]

        if div_count == 0:
            month = sf["statement_month"]
            result.add("RC-017", "WARNING",
                       f"BOCI {month} 月结单包含分红关键词，"
                       f"但 dividends 表无对应记录（file_id={sf['id']}），"
                       f"可能 _parse_dividends 漏解析")
