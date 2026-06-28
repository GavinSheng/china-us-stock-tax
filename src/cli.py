from __future__ import annotations
import click
import json
import os
import sqlite3
from decimal import Decimal
from pathlib import Path
from datetime import datetime, date

from src.parsers.futu_pdf_parser import FutuPDFParser
from src.parsers.longbridge_pdf_parser import LongbridgePDFParser
from src.calculator.tax_engine import compute_tax
from src.calculator.exchange_rate import load_exchange_rates
from src.report.csv_report import write_tax_report
from src.database import init_db, get_connection
from src.database.repositories import (
    BrokerRepository,
    StatementFileRepository,
    TransactionRepository,
    DividendRepository,
    RSUGrantRepository,
    RSUVestRepository,
    CashRewardRepository,
    PositionRepository,
    TaxItemRepository,
    TaxSummaryRepository,
    ForeignTaxCreditCarryforwardRepository,
    TaxLotRepository,
)


@click.group()
def main():
    """美股及股权激励个税计算工具"""
    pass


@main.command()
@click.argument("input_dir", type=click.Path(exists=True))
@click.option("--broker", type=click.Choice(["futu", "longbridge"]), default="futu", help="券商类型")
@click.option("--output", "-o", default=None, help="输出 CSV 路径")
def parse(input_dir: str, broker: str, output: str | None):
    """解析月结单 PDF 为交易 CSV"""
    if broker == "futu":
        parser = FutuPDFParser()
    else:
        parser = LongbridgePDFParser()

    txns = parser.parse_directory(input_dir)
    click.echo(f"解析完成，共 {len(txns)} 笔交易")

    if output:
        out_path = Path(output)
    else:
        out_path = Path("output") / f"{broker}_transactions_{datetime.now().strftime('%Y%m%d')}.csv"

    out_path.parent.mkdir(parents=True, exist_ok=True)

    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        f.write("id,broker,date,symbol,action,quantity,price,amount,fee,tax_withheld,currency\n")
        for t in txns:
            f.write(f"{t.id},{t.broker},{t.date},{t.symbol},{t.action.value},"
                    f"{t.quantity},{t.price},{t.amount},{t.fee},{t.tax_withheld},{t.currency}\n")

    click.echo(f"已保存至: {out_path}")


@main.command()
@click.argument("transactions_file", type=click.Path(exists=True))
@click.option("--year", type=int, required=True, help="计税年度")
@click.option("--output", "-o", default=None, help="输出目录")
@click.option("--exchange-rate-file", type=click.Path(exists=True), default=None, help="汇率 CSV 文件")
@click.option("--lots-file", type=click.Path(exists=True), default=None, help="上年末持仓 JSON 文件")
@click.option("--save-lots", is_flag=True, default=False, help="保存年末持仓到 JSON")
def calc(
    transactions_file: str,
    year: int,
    output: str | None,
    exchange_rate_file: str | None,
    lots_file: str | None,
    save_lots: bool,
):
    """根据交易 CSV 计算个税"""
    if exchange_rate_file:
        load_exchange_rates(exchange_rate_file)

    # 检查并确保年末汇率存在
    _ensure_year_end_rate(year)

    transactions = _read_transactions_csv(transactions_file)
    click.echo(f"读取 {len(transactions)} 笔交易")

    existing_lots = None
    if lots_file:
        existing_lots = _load_lots_json(lots_file)
        click.echo(f"加载上年末持仓（{_count_lots(existing_lots)} 个批次）")

    summary, remaining_lots, _ = compute_tax(transactions, year, existing_lots=existing_lots)

    out_dir = output or f"output/tax_{year}"
    write_tax_report(summary, out_dir)

    if save_lots:
        lots_path = Path(out_dir) / f"lots_{year}.json"
        _save_lots_json(remaining_lots, lots_path)
        click.echo(f"年末持仓已保存至: {lots_path}")

    click.echo(f"\n=== {year} 年度税务汇总 ===")
    if summary.computation_method == "annual_net":
        click.echo(f"\n卖出计税方式：年度净额法（亏损已抵扣盈利）")
        if summary.annual_net_comparison:
            info = summary.annual_net_comparison
            click.echo(f"  逐笔计算应纳税: ¥{info['per_txn_tax_amount']:,.2f}")
            click.echo(f"  年度净额应纳税: ¥{info['tax_amount_cny']:,.2f}（已选，省 ¥{info['per_txn_tax_amount'] - info['tax_amount_cny']:,.2f}）")
    elif summary.computation_method == "per_transaction" and summary.annual_net_comparison:
        info = summary.annual_net_comparison
        click.echo(f"\n卖出计税方式：逐笔计算")
        click.echo(f"  年度净额法税额: ¥{info['tax_amount_cny']:,.2f} > 逐笔计算税额（未采用）")

    if summary.rsu_income:
        click.echo(f"\nRSU 归属（股权激励所得 3%~45% 累进）")
        click.echo(f"  归属收入: ¥{summary.rsu_income.taxable_income_cny:,.2f}")
        click.echo(f"  适用税率: {summary.rsu_income.tax_rate:.0%}")
        click.echo(f"  应纳税额: ¥{summary.rsu_income.tax_amount_cny:,.2f}")
        click.echo(f"  境内已代扣: ¥{summary.rsu_income.domestic_withheld_cny:,.2f}")
        click.echo(f"  应补缴: ¥{summary.rsu_income.tax_payable_cny:,.2f}")

    if summary.capital_gains:
        net_cg = sum(i.taxable_income_cny for i in summary.capital_gains)
        cg_tax = sum(i.tax_payable_cny for i in summary.capital_gains)
        click.echo(f"\n卖出盈利（财产转让 20%）")
        click.echo(f"  盈利总额: ¥{net_cg:,.2f}")
        click.echo(f"  应缴税额: ¥{cg_tax:,.2f}")
        if cg_tax > 0:
            click.echo(f"  境外抵免: ¥{summary.total_foreign_tax_credit_cny:,.2f}")
            click.echo(f"  应补缴: ¥{cg_tax:,.2f}")

    if summary.dividends:
        total_div = sum(i.taxable_income_cny for i in summary.dividends)
        div_tax = sum(i.tax_payable_cny for i in summary.dividends)
        click.echo(f"\n分红（股息 20%）")
        click.echo(f"  分红总额: ¥{total_div:,.2f}")
        click.echo(f"  应补缴: ¥{div_tax:,.2f}")

    total_payable = summary.total_tax_payable_cny
    click.echo(f"\n{'='*40}")
    click.echo(f"合计应补缴: ¥{total_payable:,.2f}")
    if summary.total_excess_withholding_cny > 0:
        click.echo(f"合计超额未抵免: ¥{summary.total_excess_withholding_cny:,.2f}")
    click.echo(f"报表已保存至: {out_dir}/")


@main.command()
@click.argument("input_dir", type=click.Path(exists=True))
@click.option("--year", type=int, required=True, help="计税年度")
@click.option("--broker", type=click.Choice(["futu", "longbridge"]), default="futu", help="券商类型")
@click.option("--output", "-o", default=None, help="输出目录")
@click.option("--exchange-rate-file", type=click.Path(exists=True), default=None, help="汇率 CSV 文件")
@click.option("--lots-file", type=click.Path(exists=True), default=None, help="上年末持仓 JSON 文件")
def run(
    input_dir: str,
    year: int,
    broker: str,
    output: str | None,
    exchange_rate_file: str | None,
    lots_file: str | None,
):
    """一键全流程：解析月结单 + 计算个税"""
    click.echo("=== Step 1: 解析月结单 ===")
    if broker == "futu":
        parser = FutuPDFParser()
    else:
        parser = LongbridgePDFParser()

    txns = parser.parse_directory(input_dir)
    click.echo(f"解析完成，共 {len(txns)} 笔交易")

    if not txns:
        click.echo("未找到交易记录，请检查 PDF 文件")
        return

    click.echo("\n=== Step 2: 计算个税 ===")
    if exchange_rate_file:
        load_exchange_rates(exchange_rate_file)

    # 检查并确保年末汇率存在
    _ensure_year_end_rate(year)

    existing_lots = None
    if lots_file:
        existing_lots = _load_lots_json(lots_file)
        click.echo(f"加载上年末持仓（{_count_lots(existing_lots)} 个批次）")

    summary, remaining_lots, _ = compute_tax(txns, year, existing_lots=existing_lots)

    out_dir = output or f"output/tax_{year}"
    write_tax_report(summary, out_dir)

    lots_path = Path(out_dir) / f"lots_{year}.json"
    _save_lots_json(remaining_lots, lots_path)
    click.echo(f"年末持仓已保存至: {lots_path}")

    click.echo(f"\n=== {year} 年度税务汇总 ===")
    if summary.computation_method == "annual_net":
        click.echo(f"卖出计税方式：年度净额法（亏损已抵扣盈利）")
        if summary.annual_net_comparison:
            info = summary.annual_net_comparison
            click.echo(f"  逐笔计算: ¥{info['per_txn_tax_amount']:,.2f} → 年度净额: ¥{info['tax_amount_cny']:,.2f}（已选，省 ¥{info['per_txn_tax_amount'] - info['tax_amount_cny']:,.2f}）")
    if summary.rsu_income:
        click.echo(f"RSU 归属: ¥{summary.rsu_income.taxable_income_cny:,.2f}（应纳税 ¥{summary.rsu_income.tax_amount_cny:,.2f}，境内已代扣 ¥{summary.rsu_income.domestic_withheld_cny:,.2f}，应补缴 ¥{summary.rsu_income.tax_payable_cny:,.2f}）")
    if summary.capital_gains:
        net_cg = sum(i.taxable_income_cny for i in summary.capital_gains)
        click.echo(f"卖出盈利: ¥{net_cg:,.2f}")
    if summary.dividends:
        total_div = sum(i.taxable_income_cny for i in summary.dividends)
        click.echo(f"分红: ¥{total_div:,.2f}")

    click.echo(f"\n合计应补缴: ¥{summary.total_tax_payable_cny:,.2f}")
    click.echo(f"报表已保存至: {out_dir}/")


# ============================================================
# 数据库管理命令
# ============================================================

@main.group()
def db():
    """数据库管理：初始化、查看、导入"""
    pass


@db.command("init")
@click.option("--db-path", default=None, help="数据库文件路径（默认 output/tax.db）")
def db_init(db_path: str | None):
    """初始化数据库（创建所有表）"""
    conn = init_db(db_path)
    path = conn.execute("PRAGMA database_list").fetchone()["file"]
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    click.echo(f"数据库已初始化: {path}")
    click.echo(f"共创建 {len(tables)} 张表:")
    for t in tables:
        click.echo(f"  - {t['name']}")
    conn.close()


@db.command("info")
@click.option("--db-path", default=None, help="数据库文件路径")
@click.option("--broker", default=None, help="按券商过滤")
@click.option("--year", type=int, default=None, help="按年度过滤")
def db_info(db_path: str | None, broker: str | None, year: int | None):
    """查看数据库统计信息"""
    conn = get_connection(db_path)
    brokers = BrokerRepository(db_path).get_all()
    click.echo("=== 券商 ===")
    for b in brokers:
        click.echo(f"  {b['code']} | {b['name_cn']} ({b['name_en']})")

    txn_repo = TransactionRepository(db_path)
    conditions = []
    params = []
    if broker:
        conditions.append("broker_code = ?")
        params.append(broker)
    if year:
        conditions.append("strftime('%Y', trade_date) = ?")
        params.append(str(year))
    where = " AND ".join(conditions) if conditions else "1=1"

    count = conn.execute(f"SELECT COUNT(*) as c FROM transactions WHERE {where}", params).fetchone()["c"]
    click.echo(f"\n=== 交易记录: {count} 笔 ===")

    # 按 action 统计
    rows = conn.execute(f"""
        SELECT action, COUNT(*) as cnt, SUM(amount) as total
        FROM transactions WHERE {where} GROUP BY action ORDER BY cnt DESC
    """, params).fetchall()
    for r in rows:
        click.echo(f"  {r['action']:15s}  {r['cnt']:>6} 笔  总额: {r['total']}")

    # 分红
    div_repo = DividendRepository(db_path)
    divs = div_repo.get_all(broker_code=broker, year=year)
    click.echo(f"\n=== 分红记录: {len(divs)} 笔 ===")
    for d in divs:
        click.echo(f"  {d['payment_date']} | {d['symbol']:8s} | {d['gross_amount']} {d['currency']} | "
                   f"预扣税: {d['withholding_tax']} | 净额: {d['net_amount']}")

    # RSU
    rsu_repo = RSUVestRepository(db_path)
    vests = rsu_repo.get_all(year=year)
    click.echo(f"\n=== RSU 归属: {len(vests)} 笔 ===")
    for v in vests:
        click.echo(f"  {v['vest_date']} | {v['grant_number']:15s} | {v['symbol']:8s} | "
                   f"{v['vested_quantity']}股 @ {v['fmv_per_share']} | 纳税: {v['tax_amount']}")

    # 持仓快照
    pos_repo = PositionRepository(db_path)
    pos_count = conn.execute("SELECT COUNT(*) as c FROM positions").fetchone()["c"]
    click.echo(f"\n=== 持仓快照: {pos_count} 条 ===")

    # 税务
    tax_repo = TaxItemRepository(db_path)
    if year:
        tax_items = tax_repo.get_by_year(year)
        click.echo(f"\n=== {year} 年度税务记录: {len(tax_items)} 笔 ===")
        for ti in tax_items:
            click.echo(f"  {ti['income_type']:30s} | {ti['symbol']:8s} | "
                       f"应税: ¥{ti['taxable_income_cny']:,.2f} | "
                       f"应缴: ¥{ti['tax_amount_cny']:,.2f} | "
                       f"补缴: ¥{ti['tax_payable_cny']:,.2f}")

        summary_repo = TaxSummaryRepository(db_path)
        summaries = summary_repo.get_by_year(year)
        if summaries:
            click.echo(f"\n=== {year} 年度汇总 ===")
            for s in summaries:
                click.echo(f"  {s['income_type']:30s} | 应税: ¥{s['total_taxable_cny']:,.2f} | "
                           f"应缴: ¥{s['total_tax_cny']:,.2f} | "
                           f"补缴: ¥{s['total_payable_cny']:,.2f} | "
                           f"方法: {s['computation_method']}")

    conn.close()


@db.command("validate")
@click.option("--db-path", default=None, help="数据库文件路径")
def db_validate(db_path: str | None):
    """全部导入完成后，检查数据完整性（单文件验证 + 全局税务合规）"""
    import sqlite3
    import re
    from src.database.rebuild import DatabaseRebuilder
    from src.harness.db_validators import validate_database

    path = Path(db_path) if db_path else Path("output") / "tax.db"
    if not path.exists():
        click.echo(f"错误: 数据库不存在: {path}")
        raise SystemExit(1)

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row

    all_ok = True
    results = []

    # 1. 累计卖买缺口
    rebuilder = DatabaseRebuilder(str(path))
    gaps = rebuilder._check_cumulative_sell_buy()
    if gaps:
        all_ok = False
        lines = [f"  {sym:35s} 买入={info['bought']}, 卖出={info['sold']}, 缺口={info['gap']}"
                 for sym, info in gaps]
        results.append(("累计卖出 > 买入", len(gaps), lines))

    # 2. 畸形 symbol（期权缺少标的和到期日，如 "103.00C)"）
    option_strike_pat = re.compile(r"^[\d.]+[CP]\)$")
    rows = conn.execute("""
        SELECT id, symbol, broker_code, action, trade_date
        FROM transactions
        WHERE symbol LIKE '%.%%' OR symbol LIKE '%%C)' OR symbol LIKE '%%P)'
    """).fetchall()
    malformed = [(r["id"], r["symbol"], r["broker_code"], r["action"], r["trade_date"])
                 for r in rows if option_strike_pat.match(r["symbol"])]
    if malformed:
        all_ok = False
        lines = [f"  id={r[0]}  broker={r[2]}  date={r[4]}  action={r[3]}  symbol=\"{r[1]}\""
                 for r in malformed]
        results.append(("畸形期权符号", len(malformed), lines))

    # 3. 空 trade_date
    rows = conn.execute("""
        SELECT id, symbol, action, broker_code
        FROM transactions WHERE trade_date IS NULL OR trade_date = ''
    """).fetchall()
    if rows:
        all_ok = False
        lines = [f"  id={r['id']}  broker={r['broker_code']}  symbol={r['symbol']}  action={r['action']}"
                 for r in rows]
        results.append(("缺失交易日期", len(rows), lines))

    # 4. 期权生命周期完整性
    rows = conn.execute("""
        SELECT symbol,
            COALESCE(SUM(CASE WHEN action='option_buy' THEN quantity ELSE 0 END), 0) as bought,
            COALESCE(SUM(CASE WHEN action IN ('option_sell','option_expire','option_exercise')
                             THEN quantity ELSE 0 END), 0) as sold
        FROM transactions
        WHERE action IN ('option_buy','option_sell','option_expire','option_exercise')
        GROUP BY symbol
        HAVING sold > bought
    """).fetchall()
    if rows:
        all_ok = False
        lines = [f"  {r['symbol']:40s} 买入={r['bought']}, 卖出+过期+行权={r['sold']}"
                 for r in rows]
        results.append(("期权数量缺口", len(rows), lines))

    # 5. 每文件统计
    rows = conn.execute("""
        SELECT sf.broker_code, sf.statement_month, sf.status, COUNT(t.id) as txn_count
        FROM statement_files sf
        LEFT JOIN transactions t ON t.statement_file_id = sf.id
        GROUP BY sf.id
        ORDER BY sf.broker_code, sf.statement_month
    """).fetchall()
    file_lines = []
    for r in rows:
        status = f"OK ({r['txn_count']} 笔)" if r['txn_count'] > 0 else "⚠️ 0 笔"
        file_lines.append(f"  {r['broker_code']:12s} {r['statement_month']}  status={r['status']:10s}  {status}")
    results.append(("文件导入统计", len(rows), file_lines))

    conn.close()

    # ── 运行数据库级税务合规验证 ──
    db_result = validate_database(path)
    if not db_result.passed:
        all_ok = False
        for issue in db_result.issues:
            if issue.severity == "ERROR":
                results.append((issue.rule_id, 1, [f"  {issue}"]))
        for issue in db_result.issues:
            if issue.severity == "WARNING":
                results.append((issue.rule_id, 1, [f"  {issue}"]))

    # 输出报告
    click.echo(f"\n{'='*60}")
    click.echo("数据完整性检查报告")
    click.echo(f"{'='*60}\n")

    for title, count, lines in results:
        click.echo(f"--- {title} ({count} 项) ---")
        for line in lines[:20]:
            click.echo(line)
        if len(lines) > 20:
            click.echo(f"  ... 及其他 {len(lines) - 20} 条")
        click.echo()

    if all_ok:
        click.echo("✓ 全部检查通过")
    else:
        click.echo("⚠️  存在数据完整性问题，请先修复后再运行 calc-db。")
        raise SystemExit(1)


@db.command("seed-cash-rewards")
@click.option("--db-path", default=None, help="数据库文件路径")
def db_seed_cash_rewards(db_path: str | None):
    """导入已知的现金回报数据"""
    init_db(db_path)
    repo = CashRewardRepository(db_path)

    rewards = [
        ("2025现金回报", "RSU(港股)", "USD", 225, 42, 183),
        ("2025现金回报", "RSU(美股)", "USD", 800, 0, 800),
        ("2024现金回报", "RSU(美股)", "USD", 996, 332, 664),
        ("2024现金回报", "RSU(美股)", "USD", 124.5, 124.5, 0),
        ("2023现金回报", "RSU(美股)", "USD", 800, 400, 400),
        ("2023现金回报", "RSU(美股)", "USD", 75, 75, 0),
    ]
    for name, rsu_type, currency, total, vested, unvested in rewards:
        repo.insert(name, rsu_type, currency, total, vested, unvested)
        click.echo(f"  已导入: {name} | {rsu_type} | 总额: {total} {currency}")

    click.echo(f"\n现金回报数据已导入，共 {len(rewards)} 条")


@db.command("rebuild")
@click.option("--db-path", default=None, help="数据库文件路径")
@click.option("--broker", type=click.Choice(["longbridge", "futu", "boci"]), default=None,
              help="仅重建指定券商")
@click.option("--start-month", default=None, help="从某月开始 (YYYY-MM)")
@click.option("--non-interactive", is_flag=True, help="非交互模式")
@click.option("--skip-position-check", is_flag=True, help="跳过持仓对比验证")
@click.option("--clear-only", is_flag=True, help="仅清空数据，不重建")
def db_rebuild(db_path: str | None, broker: str | None, start_month: str | None,
               non_interactive: bool, skip_position_check: bool, clear_only: bool):
    """清空解析数据，逐文件重解析并验证"""
    from src.database.rebuild import DatabaseRebuilder
    rebuilder = DatabaseRebuilder(db_path)

    if clear_only:
        rebuilder.clear_all()
        return

    rebuilder.clear_all()
    rebuilder.rebuild(
        broker_code=broker,
        start_month=start_month,
        interactive=not non_interactive,
        skip_position=skip_position_check,
    )


def _read_transactions_csv(file_path: str):
    from src.models import Transaction, Action
    import csv

    txns = []
    with open(file_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            txns.append(Transaction(
                id=row["id"],
                broker=row["broker"],
                date=datetime.strptime(row["date"], "%Y-%m-%d").date(),
                symbol=row["symbol"],
                action=Action(row["action"]),
                quantity=int(row["quantity"]),
                price=Decimal(row.get("price", "0")),
                amount=Decimal(row.get("amount", "0")),
                fee=Decimal(row.get("fee", "0")),
                tax_withheld=Decimal(row.get("tax_withheld", "0")),
                currency=row.get("currency", "USD"),
            ))
    return txns


def _save_lots_json(lots: dict, file_path: Path):
    """保存年末剩余持仓到 JSON 文件

    lots 的 key 为 symbol（跨券商合并）。
    每个 TaxLot 上的 broker_code 字段记录该批次归属的券商（审计追踪）。
    """
    import json

    data = []
    for symbol, lot_list in lots.items():
        for lot in lot_list:
            if lot.remaining > 0:
                data.append({
                    "broker_code": lot.broker_code,
                    "symbol": lot.symbol,
                    "quantity": lot.quantity,
                    "cost_per_share": str(lot.cost_per_share),
                    "acquire_date": str(lot.acquire_date),
                    "remaining": lot.remaining,
                    "origin": lot.origin,
                })

    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _load_lots_json(file_path: str) -> dict:
    """加载上年末持仓 JSON 文件

    支持两种格式：
    1. 新格式（list）：每条记录包含 broker_code
    2. 旧格式（dict）：key 为 symbol，无 broker_code（向后兼容，默认 broker_code="unknown"）

    返回 dict，key 为 symbol（跨券商合并），每个 TaxLot 保留 broker_code 用于审计。
    """
    import json
    from datetime import date
    from src.models import TaxLot

    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    lots: dict[str, list] = {}

    if isinstance(data, list):
        # 新格式：list of dicts
        for lot_data in data:
            broker_code = lot_data.get("broker_code")
            symbol = lot_data["symbol"]
            lot = TaxLot(
                symbol=symbol,
                quantity=lot_data["quantity"],
                cost_per_share=Decimal(lot_data["cost_per_share"]),
                acquire_date=date.fromisoformat(lot_data["acquire_date"]),
                remaining=lot_data["remaining"],
                origin=lot_data.get("origin", "buy"),
                broker_code=broker_code,
            )
            lots.setdefault(symbol, []).append(lot)
    else:
        # 旧格式：dict keyed by symbol
        for symbol, lot_list in data.items():
            lots[symbol] = [
                TaxLot(
                    symbol=lot["symbol"],
                    quantity=lot["quantity"],
                    cost_per_share=Decimal(lot["cost_per_share"]),
                    acquire_date=date.fromisoformat(lot["acquire_date"]),
                    remaining=lot["remaining"],
                    origin=lot.get("origin", "buy"),
                    broker_code="unknown",
                )
                for lot in lot_list
            ]
    return lots


def _load_existing_lots_from_db(db_path: Path, year: int) -> dict | None:
    """从数据库加载上年末持仓作为 existing_lots（跨年度持有基础）

    策略：通过 FIFO 模拟运行上年及以前全部交易，还原年末剩余批次。
    保留原始批次（acquisition_date + cost_per_share），不使用加权平均。
    按券商隔离：不同券商的同一标的各自维护独立 FIFO 队列。
    """
    from src.calculator.fifo import FIFOEngine
    from src.models import TaxLot

    conn = get_connection(db_path)
    conn.row_factory = sqlite3.Row
    try:
        prev_year = year - 1

        # 加载上年及以前全部交易（按 trade_date + rowid 排序）
        rows = conn.execute("""
            SELECT * FROM transactions
            WHERE strftime('%Y', trade_date) <= ?
              AND action IN ('buy', 'sell', 'rsu_vest', 'rsu_sell', 'rsu_cancel',
                             'option_buy', 'option_sell', 'option_expire',
                             'option_exercise')
            ORDER BY trade_date, rowid
        """, (str(prev_year),)).fetchall()

        if not rows:
            return None

        # 运行 FIFO 模拟（按券商隔离）
        fifo = FIFOEngine()

        buy_actions = {"BUY", "RSU_VEST", "OPTION_BUY", "OPTION_EXERCISE"}
        sell_actions = {"SELL", "RSU_SELL", "OPTION_SELL", "OPTION_EXPIRE"}

        option_premiums: dict[str, Decimal] = {}  # track option premiums for exercises

        for r in rows:
            action = r["action"].upper()
            broker_code = r["broker_code"]
            sym = r["symbol"]
            qty = r["quantity"] or 0
            price = Decimal(str(r["price"] or 0))
            td = date.fromisoformat(r["trade_date"])

            if action in ("BUY", "OPTION_BUY"):
                origin = "option_buy" if action == "OPTION_BUY" else "buy"
                fifo.buy(broker_code, sym, qty, price, td, origin=origin)

            elif action == "RSU_VEST":
                fifo.buy(broker_code, sym, qty, price, td, origin="rsu_vest")

            elif action == "RSU_CANCEL":
                # RSU 提货/取消：减少对应 lot（RSU 归属产生的持仓被取消）
                try:
                    fifo.sell(broker_code, sym, abs(qty), Decimal("0"), td, short_allowed=False)
                except ValueError:
                    pass

            elif action == "OPTION_EXERCISE":
                # 行权：从 raw_data 提取权利金，成本 = strike + premium
                cost = price
                if r["raw_data"]:
                    try:
                        rd = json.loads(r["raw_data"])
                        premium = rd.get("option_premium", 0)
                        cost = price + Decimal(str(premium))
                    except (json.JSONDecodeError, TypeError):
                        pass
                fifo.exercise(broker_code, sym, qty, cost, td)

            elif action in ("SELL", "RSU_SELL", "OPTION_SELL"):
                try:
                    fifo.sell(broker_code, sym, qty, price, td, short_allowed=False)
                except ValueError:
                    # 历史数据可能有缺失，跳过
                    pass

            elif action == "OPTION_EXPIRE":
                try:
                    fifo.expire(broker_code, sym, qty, td)
                except ValueError:
                    pass

        # 获取年末剩余批次
        remaining = fifo.get_remaining_lots()
        lot_count = sum(len(v) for v in remaining.values() if v)

        if lot_count == 0:
            click.echo(f"从数据库计算上年末持仓：无剩余批次")
            return None

        msg = f"从数据库计算上年末持仓（{lot_count} 个批次，来自 FIFO 模拟推导）"
        click.echo(msg)
        return remaining
    finally:
        conn.close()


def _count_lots(lots: dict) -> int:
    return sum(len(v) for v in lots.values()) if lots else 0


def _ensure_year_end_rate(year: int, usd_cny: float | None = None) -> Decimal:
    """确保指定年度的年末汇率存在，缺失时提示用户输入。

    依据《个人所得税法实施条例》第三十二条，年度汇算清缴需使用
    纳税年度最后一日（12月31日）人民币汇率中间价。

    Args:
        year: 计税年度
        usd_cny: 用户指定的 USD/CNY 汇率（如提供则直接注册）

    Returns:
        可用的 USD/CNY 年末汇率
    """
    from src.calculator.exchange_rate import (
        check_year_end_rate, register_year_end_rate, get_exchange_rate,
    )

    # 用户指定了汇率，直接注册
    if usd_cny is not None:
        rate = Decimal(str(usd_cny))
        register_year_end_rate("USD", rate, year)
        click.echo(f"  年末汇率: USD/CNY = {rate}（用户指定）")
        return rate

    # 检查汇率文件中是否有年末汇率
    found, rate, actual_date = check_year_end_rate("USD", year)
    if found:
        date_str = actual_date.strftime("%Y-%m-%d") if actual_date else ""
        if actual_date and actual_date != date(year, 12, 31):
            click.echo(f"  年末汇率: USD/CNY = {rate}（回退至 {date_str}，12-31 非交易日）")
        else:
            click.echo(f"  年末汇率: USD/CNY = {rate}")
        return rate

    # 未找到，提示用户输入
    click.echo(f"\n⚠️  未找到 {year} 年年末（12月31日及回退10天内）USD/CNY 汇率中间价。")
    click.echo(f"   依据《个人所得税法实施条例》第三十二条，年度汇算清缴需使用年末汇率。")
    click.echo(f"   请前往中国外汇交易中心（http://www.chinamoney.com.cn）查询 {year}-12-31 或最近工作日中间价。")
    click.echo(f"   默认汇率参考：{get_exchange_rate(date(year, 1, 1), 'USD')}")
    user_input = click.prompt(f"  请输入 {year} 年年末 USD/CNY 汇率中间价", type=float)
    rate = Decimal(str(user_input))
    register_year_end_rate("USD", rate, year)
    click.echo(f"  年末汇率: USD/CNY = {rate}（手动输入）")
    return rate


def _backfill_cost_from_history(db_path: Path, symbol: str, up_to_date: str) -> Decimal:
    """从历史数据回溯最佳成本基础，避免 phantom lot 使用 $0

    优先级：
      1. 历史买入交易的加权平均成本（buy / option_exercise / rsu_vest）
      2. RSU 归属 FMV（rsu_vests 表，取最近一次归属）
      3. positions 表最近的收盘价（作为粗略替代）
      4. 以上都没有 → 返回 Decimal("0")
    """
    conn = get_connection(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # 1. 历史买入加权平均成本
        row = conn.execute("""
            SELECT SUM(quantity) AS total_qty,
                   SUM(quantity * price) AS total_cost
            FROM transactions
            WHERE symbol = ?
              AND action IN ('buy', 'option_exercise', 'rsu_vest')
              AND trade_date IS NOT NULL AND trade_date != ''
              AND trade_date <= ?
              AND price > 0 AND quantity > 0
        """, (symbol, up_to_date)).fetchone()

        if row and row["total_qty"] and row["total_qty"] > 0:
            avg = Decimal(str(row["total_cost"])) / Decimal(str(row["total_qty"]))
            if avg > 0:
                return avg.quantize(Decimal("0.000001"))

        # 2. RSU 归属 FMV
        rsu = conn.execute("""
            SELECT fmv_per_share
            FROM rsu_vests
            WHERE symbol = ? AND vest_date <= ?
              AND fmv_per_share > 0
            ORDER BY vest_date DESC
            LIMIT 1
        """, (symbol, up_to_date)).fetchone()

        if rsu and rsu["fmv_per_share"] > 0:
            return Decimal(str(rsu["fmv_per_share"])).quantize(Decimal("0.000001"))

        # 3. positions 表最近收盘价
        pos = conn.execute("""
            SELECT closing_price
            FROM positions
            WHERE symbol = ? AND as_of_date <= ?
              AND closing_price > 0
            ORDER BY as_of_date DESC
            LIMIT 1
        """, (symbol, up_to_date)).fetchone()

        if pos and pos["closing_price"] and pos["closing_price"] > 0:
            return Decimal(str(pos["closing_price"])).quantize(Decimal("0.000001"))

        return Decimal("0")
    finally:
        conn.close()


def _add_missing_lots(txns: list, existing_lots: dict, year: int):
    """为缺失买入记录的卖出交易添加 $0 成本基数的前置持仓

    某些月份可能因为解析问题没有买入记录（如 FUTU 2025-01），
    但其期权/股票已在后续月份卖出。为避免 FIFO 持仓不足报错，
    自动添加成本为 $0 的买入批次。

    跨券商合并：按 symbol 统计总买入/卖出量，补充批次。
    """
    from collections import defaultdict
    from src.models import TaxLot

    buy_actions = {"BUY", "RSU_VEST", "OPTION_BUY", "OPTION_EXERCISE"}
    sell_actions = {"SELL", "OPTION_SELL", "RSU_SELL"}
    expire_actions = {"OPTION_EXPIRE"}

    # 统计每个 symbol 的总买入量（跨券商合并）
    total_bought: dict[str, int] = defaultdict(int)
    for txn in txns:
        if txn.action.name in buy_actions:
            total_bought[txn.symbol] += txn.quantity

    # 统计每个 symbol 的总卖出 + 过期量（跨券商合并）
    total_sold: dict[str, int] = defaultdict(int)
    for txn in txns:
        if txn.action.name in sell_actions or txn.action.name in expire_actions:
            total_sold[txn.symbol] += txn.quantity

    # 找出需要补充的 symbol
    for symbol, sold_qty in total_sold.items():
        bought_qty = total_bought.get(symbol, 0)
        existing_qty = sum(l.quantity for l in existing_lots.get(symbol, []))
        if bought_qty + existing_qty < sold_qty:
            deficit = sold_qty - bought_qty - existing_qty
            from datetime import date
            lot = TaxLot(
                symbol=symbol,
                quantity=deficit,
                cost_per_share=Decimal("0"),
                acquire_date=date(year - 1, 12, 31),
                remaining=deficit,
                origin="carry_forward_missing",
                broker_code=None,
            )
            if symbol not in existing_lots:
                existing_lots[symbol] = []
            existing_lots[symbol].append(lot)


def _load_carryforward_lots_from_db(db_path: Path, year: int) -> dict | None:
    """从 carryforward tax_lots 加载上年末结转持仓

    使用年度结转规则：从2024年12月31日期末持仓作为2025年税务起始成本基础。
    避免对历史交易进行 FIFO 模拟推导（可能不完整）。

    返回 dict，key 为 symbol（跨券商合并）。
    每个 TaxLot 保留 broker_code 用于审计追踪。
    """
    from src.models import TaxLot

    conn = get_connection(db_path)
    conn.row_factory = sqlite3.Row
    try:
        carry_date = f"{year - 1}-12-31"
        rows = conn.execute("""
            SELECT * FROM tax_lots
            WHERE acquisition_type = 'carryforward'
              AND acquisition_date = ?
              AND remaining > 0
            ORDER BY acquisition_date, id
        """, (carry_date,)).fetchall()

        if not rows:
            return None

        existing_lots: dict[str, list] = {}
        for r in rows:
            symbol = r["symbol"]
            broker_code = r["broker_code"]
            lot = TaxLot(
                symbol=symbol,
                quantity=r["quantity"],
                cost_per_share=Decimal(str(r["cost_per_share"])),
                acquire_date=date.fromisoformat(r["acquisition_date"]),
                remaining=r["remaining"],
                origin="carryforward",
                broker_code=broker_code,
            )
            existing_lots.setdefault(symbol, []).append(lot)

        return existing_lots
    finally:
        conn.close()


@main.command()
@click.option("--year", type=int, default=2025, help="校验年度")
@click.option("--db-path", type=str, default=None, help="数据库路径")
@click.option("--skip-pre-calc", is_flag=True, help="跳过预计算就绪检查")
@click.option("--skip-validation", is_flag=True, help="跳过输入验证")
@click.option("--skip-reconciliation", is_flag=True, help="跳过对账校验")
@click.option("--skip-verification", is_flag=True, help="跳过计算验证")
@click.option("--skip-multi-account", is_flag=True, help="跳过多账户核算验证")
@click.option("--skip-overpayment", is_flag=True, help="跳过多缴税检测")
@click.option("--usd-cny", type=float, default=None, help="USD/CNY 汇率（传预计算检查）")
def harness(year: int, db_path: str | None, skip_pre_calc: bool, skip_validation: bool,
            skip_reconciliation: bool, skip_verification: bool,
            skip_multi_account: bool, skip_overpayment: bool, usd_cny: float | None):
    """运行税务合规 Harness 校验"""
    from src.harness.quality import run_full_harness

    path = Path(db_path) if db_path else Path("output") / "tax.db"
    if not path.exists():
        click.echo(f"错误: 数据库不存在: {path}")
        raise SystemExit(1)

    report = run_full_harness(
        db_path=path,
        year=year,
        skip_pre_calc=skip_pre_calc,
        skip_validation=skip_validation,
        skip_reconciliation=skip_reconciliation,
        skip_verification=skip_verification,
        skip_multi_account=skip_multi_account,
        skip_overpayment=skip_overpayment,
        usd_cny=usd_cny,
    )
    click.echo(report.summary())
    raise SystemExit(0 if report.all_passed else 1)


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


def _inject_option_expire_transactions(db_path: Path, year: int) -> list[dict]:
    """同步 tax_lots.remaining 并补充缺失的期权过期交易记录

    在 compute_tax 运行前调用，分为两步：

    1. **同步 remaining**：根据 lot_consumptions（上次 compute_tax 的结果）
       更新 tax_lots.remaining，确保与 FIFO 消耗一致。
    2. **插入 expire 交易**：查找 remaining > 0 且已过期但无 option_expire
       记录的期权，在 transactions 表中插入 option_expire 交易。
       同时将该 lot 的 remaining 清零。

    返回新插入的 expire 交易信息列表，供调用方添加到 txns 列表中。

    这样 compute_tax 加载交易时会自动包含 option_expire，
    FIFO 引擎会将成本记为资本损失，纳入年度净额抵扣。
    """
    from src.database import get_connection
    import sqlite3
    import json

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # 步骤 1：根据 lot_consumptions 同步 tax_lots.remaining
    # lot_consumptions 是上次 compute_tax 的 FIFO 消耗记录
    conn.execute("""
        UPDATE tax_lots
        SET remaining = quantity - COALESCE(
            (SELECT SUM(consumed_qty) FROM lot_consumptions
             WHERE lot_consumptions.tax_lot_id = tax_lots.id), 0)
    """)
    conn.commit()

    # 步骤 1.5：修复已存在的 expire 交易金额（旧代码存储的 amount 未 ×100 且为负数）
    expire_rows = conn.execute("""
        SELECT id, quantity, price, amount
        FROM transactions
        WHERE action = 'option_expire' AND amount < 0
    """).fetchall()

    fixed_count = 0
    for row in expire_rows:
        qty = row["quantity"]
        price = row["price"]
        # price 为每份期权合约权利金，amount = quantity × price × 100（合约乘数）
        correct_amount = abs(qty * price * 100)
        if correct_amount > 0:
            conn.execute(
                "UPDATE transactions SET amount = ? WHERE id = ?",
                (correct_amount, row["id"])
            )
            fixed_count += 1

    if fixed_count > 0:
        click.echo(f"  修复 {fixed_count} 笔历史 expire 交易金额（×100 合约乘数修正）")
    conn.commit()

    # 步骤 1.6：清理 phantom expire 交易（全部合约已卖出，不应有 expire）
    # 这些是旧代码生成的，新代码已阻止但不删除已有记录
    phantom_txns = conn.execute("""
        SELECT t.id, t.symbol, t.broker_code, t.quantity, t.amount
        FROM transactions t
        WHERE t.action = 'option_expire'
          AND t.amount <= 1  -- $0 或接近 $0
          AND EXISTS (
              SELECT 1 FROM transactions b
              WHERE b.symbol = t.symbol AND b.broker_code = t.broker_code
                AND b.action = 'option_buy'
              GROUP BY b.symbol, b.broker_code
              HAVING SUM(b.quantity) <= (
                  SELECT COALESCE(SUM(s.quantity), 0)
                  FROM transactions s
                  WHERE s.symbol = b.symbol AND s.broker_code = b.broker_code
                    AND s.action IN ('sell', 'option_sell')
              )
          )
    """).fetchall()

    if phantom_txns:
        phantom_ids = [p['id'] for p in phantom_txns]
        conn.execute(
            f"DELETE FROM transactions WHERE id IN ({','.join('?' for _ in phantom_ids)})",
            phantom_ids
        )
        conn.commit()
        click.echo(f"  清理 {len(phantom_txns)} 笔 phantom expire（全部合约已卖出，不应有 expire）")

    # 步骤 2：查找所有有剩余持仓且在本年度到期的期权
    # 只处理本年度过期的期权，不混入历史年度数据
    year_start = f"{year}-01-01"
    year_end = f"{year}-12-31"
    option_lots = conn.execute("""
        SELECT tl.id, tl.symbol, tl.broker_code, tl.remaining,
               tl.quantity, tl.cost_per_share
        FROM tax_lots tl
        WHERE tl.remaining > 0
          AND (tl.symbol LIKE '%_OPT_%' OR tl.symbol LIKE '%OPT%')
        ORDER BY tl.symbol, tl.broker_code
    """).fetchall()

    # 按 (symbol, broker) 聚合
    from collections import defaultdict
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for lot in option_lots:
        key = (lot["symbol"], lot["broker_code"])
        groups[key].append(dict(lot))

    expire_txns = []
    generated = 0

    for (symbol, broker), lots in groups.items():
        expiry = _parse_option_expiry(symbol)
        if not expiry or expiry < year_start or expiry > year_end:
            continue

        # 检查是否已有 option_expire 记录（任意年份，防止重复生成）
        existing = conn.execute("""
            SELECT COALESCE(SUM(quantity), 0) as qty
            FROM transactions
            WHERE symbol = ? AND action = 'option_expire'
              AND broker_code = ?
        """, (symbol, broker)).fetchone()["qty"]

        if existing > 0:
            continue

        # 计算到期前的实际卖出数量（pre-expiry sells），
        # 这样 expire qty = tax_lots.remaining - pre-expiry sells
        pre_expiry_sells = conn.execute("""
            SELECT COALESCE(SUM(quantity), 0) as sold_qty
            FROM transactions
            WHERE symbol = ? AND broker_code = ?
              AND action IN ('sell', 'option_sell')
              AND trade_date < ?
        """, (symbol, broker, expiry)).fetchone()["sold_qty"]

        # 查找到期后的卖出（post-expiry sells）— 这些是券商平仓操作，
        # 期权过期后无法卖出，应归入 expire 事件
        post_expiry_sell_rows = conn.execute("""
            SELECT id, quantity, amount FROM transactions
            WHERE symbol = ? AND broker_code = ?
              AND action IN ('sell', 'option_sell')
              AND trade_date > ?
        """, (symbol, broker, expiry)).fetchall()

        post_expiry_sell_qty = 0
        post_expiry_sell_amount = Decimal("0")
        post_sell_ids_to_delete = []
        for row in post_expiry_sell_rows:
            post_expiry_sell_qty += row["quantity"]
            post_expiry_sell_amount += Decimal(str(row["amount"]) or "0")
            post_sell_ids_to_delete.append(row["id"])

        # 安全约束：计算最大可能到期数量 = 总买入 - 总卖出
        # 防止将到期日当天的正常交易误判为过期平仓
        total_bought = conn.execute("""
            SELECT COALESCE(SUM(quantity), 0) FROM transactions
            WHERE symbol = ? AND broker_code = ? AND action IN ('option_buy', 'buy')
        """, (symbol, broker)).fetchone()[0]
        total_sold = conn.execute("""
            SELECT COALESCE(SUM(quantity), 0) FROM transactions
            WHERE symbol = ? AND broker_code = ? AND action IN ('sell', 'option_sell')
        """, (symbol, broker)).fetchone()[0]
        max_possible_expire = max(0, total_bought - total_sold)

        # 实际到期数量 = tax_lots 剩余 - 到期前已卖出 + 到期后"卖出"（实为过期）
        total_remaining = sum(l["remaining"] for l in lots)
        actual_expire_qty = total_remaining - pre_expiry_sells + post_expiry_sell_qty

        # 上限约束：不能超过实际可到期数量
        if actual_expire_qty > max_possible_expire:
            actual_expire_qty = max_possible_expire

        if actual_expire_qty <= 0:
            # 全部已在到期前卖出，无需生成 expire
            continue

        # 总成本 = 实际到期数量 × 平均成本 × 期权合约乘数(100)
        # 与 FIFO 引擎 _calc() 方法保持一致：price × quantity × multiplier
        total_cost = sum(l["remaining"] * l["cost_per_share"] for l in lots)
        avg_cost = total_cost / total_remaining if total_remaining > 0 else 0
        expire_cost = actual_expire_qty * avg_cost * 100  # 期权合约乘数

        # 删除 post-expiry sell 交易记录
        if post_sell_ids_to_delete:
            conn.execute(
                f"DELETE FROM transactions WHERE id IN ({','.join('?' for _ in post_sell_ids_to_delete)})",
                post_sell_ids_to_delete
            )

        # 插入 option_expire 交易记录
        # amount 为正数，表示该期权的原始买入成本（已按合约乘数计算），
        # 与 FIFO 引擎计算的 cost_basis 一致，供审计核对
        expire_amount = expire_cost
        cursor = conn.execute("""
            INSERT INTO transactions
                (broker_code, trade_date, symbol, action,
                 quantity, price, amount, currency,
                 raw_data)
            VALUES (?, ?, ?, 'option_expire', ?, ?, ?, 'USD', ?)
        """, (
            broker, expiry, symbol, actual_expire_qty,
            float(avg_cost), float(expire_amount),
            json.dumps({"expiry": expiry, "reason": "expired"})
        ))
        db_txn_id = cursor.lastrowid

        expire_txns.append({
            "db_id": db_txn_id,
            "broker_code": broker,
            "trade_date": expiry,
            "symbol": symbol,
            "action": "option_expire",
            "quantity": actual_expire_qty,
            "amount": expire_amount,
        })
        generated += 1

    if generated > 0:
        conn.commit()
        click.echo(f"  自动补充 {generated} 笔缺失的期权过期记录（成本计入年度净额抵扣）")

    conn.close()
    return expire_txns


def _sync_expire_amounts_to_fifo(db_path: Path, lot_consumptions: list[dict], sell_txn_map: dict[str, int]):
    """将 expire 交易金额同步为 FIFO 实际消耗成本（审计一致性约束）

    compute_tax 完成后，lot_consumptions 包含了 FIFO 引擎计算的权威成本。
    此函数将每个 expire 交易的 amount 更新为对应 lot_consumptions 的
    cost_basis 总和，确保数据库内部自洽。

    约束：transaction.amount = SUM(lot_consumptions.cost_basis)
         对于所有 action = 'option_expire' 的交易

    安全保护：
    - 如果 FIFO 成本为 $0 但该 option 有真实买入记录，保留原始金额
      （说明 FIFO 因时序问题未找到 lot，不应覆盖为 $0 导致成本丢失）
    """
    from decimal import Decimal
    from collections import defaultdict

    # 按 sell_txn_id 聚合 FIFO 实际消耗成本
    fifo_cost_by_txn: dict[str, Decimal] = defaultdict(Decimal)
    for lc in lot_consumptions:
        if lc.get("sell_action") == "OPTION_EXPIRE":
            txn_id = lc["sell_txn_id"]
            cost = Decimal(lc["cost_basis"])
            fifo_cost_by_txn[txn_id] += abs(cost)

    if not fifo_cost_by_txn:
        return

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    synced = 0
    skipped_zero = 0
    for txn_id_str, fifo_cost in fifo_cost_by_txn.items():
        db_id = sell_txn_map.get(txn_id_str)
        if not db_id:
            continue

        current = conn.execute(
            "SELECT amount, symbol, broker_code FROM transactions WHERE id = ? AND action = 'option_expire'",
            (db_id,)
        ).fetchone()
        if current is None:
            continue

        current_amount = Decimal(str(current["amount"]) or "0")

        # 安全保护：FIFO 成本为 $0 且存在真实买入 → 不覆盖
        if fifo_cost == 0:
            buy_check = conn.execute(
                "SELECT COUNT(*) as cnt FROM transactions "
                "WHERE symbol = ? AND broker_code = ? AND action = 'option_buy'",
                (current["symbol"], current["broker_code"])
            ).fetchone()
            if buy_check["cnt"] > 0:
                skipped_zero += 1
                continue

        if abs(current_amount - fifo_cost) > Decimal("0.01"):
            conn.execute(
                "UPDATE transactions SET amount = ? WHERE id = ?",
                (float(fifo_cost), db_id)
            )
            synced += 1

    conn.commit()
    conn.close()

    msg_parts = []
    if synced > 0:
        msg_parts.append(f"同步 {synced} 笔 expire 交易金额到 FIFO 实际成本")
    if skipped_zero > 0:
        msg_parts.append(f"保留 {skipped_zero} 笔（FIFO 未匹配 lot，保留原始金额防成本丢失）")
    if msg_parts:
        click.echo(f"   expire 审计：{'；'.join(msg_parts)}")


@main.command("calc-db")
@click.option("--year", type=int, default=2025, help="计税年度")
@click.option("--db-path", type=str, default=None, help="数据库路径")
@click.option("--output", "-o", default=None, help="输出目录")
@click.option("--usd-cny", type=float, default=None, help="USD/CNY 汇率（覆盖默认值，如 7.0288）")
def calc_db(year: int, db_path: str | None, output: str | None, usd_cny: float | None):
    """从数据库加载交易并计算个税，结果持久化到数据库"""
    from src.database import get_connection
    from src.database.repositories import TaxItemRepository, TaxSummaryRepository, LotConsumptionRepository
    from src.calculator.tax_engine import compute_tax
    from src.calculator.exchange_rate import load_exchange_rates
    from src.report.csv_report import write_tax_report
    from src.models import Transaction, Action, TaxLot
    from datetime import date

    path = Path(db_path) if db_path else Path("output") / "tax.db"
    if not path.exists():
        click.echo(f"错误: 数据库不存在: {path}")
        raise SystemExit(1)

    # 加载汇率
    load_exchange_rates()

    # 用户指定的汇率覆盖（如 --usd-cny 7.0288）
    if usd_cny is not None:
        import src.config
        import src.calculator.exchange_rate
        src.config.DEFAULT_EXCHANGE_RATE = usd_cny
        # 清空缓存使新汇率生效
        src.calculator.exchange_rate._rate_cache.clear()
        load_exchange_rates()
        click.echo(f"  使用指定汇率: USD/CNY = {usd_cny}")

    # 检查并确保年末汇率存在（用户指定汇率已在上面注册）
    _ensure_year_end_rate(year, usd_cny=usd_cny)

    # 从数据库加载交易 — 仅加载目标年度的交易
    # 跨年度持仓通过 existing_lots 传入（来自上年 positions 快照或 JSON）

    # 同步 tax_lots.remaining 并补充缺失的期权过期交易记录
    # 必须在加载交易前调用，这样 expire 交易会被自动加载到 txns 列表
    _inject_option_expire_transactions(path, year)

    conn = get_connection(path)
    conn.row_factory = sqlite3.Row
    # 同日内严格按 rowid（PDF 原文顺序）排序，不使用 action 优先级
    # 因为日内交易的实际时间顺序已在 PDF 中体现，action 优先级会把
    # "先卖后买"强行改成"先买后卖"，导致 FIFO 匹配错误的成本批次
    rows = conn.execute("""
        SELECT * FROM transactions
        WHERE strftime('%Y', trade_date) = ?
          AND NOT (broker_code = 'boci' AND action = 'rsu_vest')
        ORDER BY trade_date,
                 CASE action
                     WHEN 'buy' THEN 0
                     WHEN 'rsu_vest' THEN 0
                     WHEN 'option_buy' THEN 0
                     WHEN 'option_exercise' THEN 0
                     WHEN 'sell' THEN 1
                     WHEN 'rsu_sell' THEN 1
                     WHEN 'option_sell' THEN 1
                     WHEN 'option_expire' THEN 1
                     WHEN 'dividend' THEN 2
                     WHEN 'interest' THEN 2
                     WHEN 'cash_reward' THEN 2
                     ELSE 3
                 END,
                 rowid
    """, (str(year),)).fetchall()

    txns = []
    for r in rows:
        try:
            action = Action(r['action'])
        except ValueError:
            continue
        td = date.fromisoformat(r['trade_date'])

        # 期权行权：从 raw_data 提取权利金，成本 = strike + premium
        price_val = r['price'] or 0
        if r['action'] == 'option_exercise' and r['raw_data']:
            try:
                rd = json.loads(r['raw_data'])
                premium = rd.get('option_premium', 0)
                price_val = (r['price'] or 0) + premium
            except (json.JSONDecodeError, TypeError):
                pass

        amount_val = r['amount'] or 0

        txns.append(Transaction(
            id=str(r['id']),
            broker=r['broker_code'],
            date=td,
            symbol=r['symbol'],
            action=action,
            quantity=r['quantity'] or 0,
            price=Decimal(str(price_val)),
            amount=Decimal(str(amount_val)),
            fee=Decimal(str(r['commission'] or 0)) + Decimal(str(r['platform_fee'] or 0))
                + Decimal(str(r['sec_fee'] or 0)) + Decimal(str(r['taf_fee'] or 0))
                + Decimal(str(r['delivery_fee'] or 0)) + Decimal(str(r['other_fees'] or 0)),
            tax_withheld=Decimal(str(r['tax_withheld'] or 0)),
            currency=r['currency'] or 'USD',
            exchange_rate=Decimal(str(r['exchange_rate'])) if r['exchange_rate'] else Decimal('0'),
        ))
    click.echo(f"加载 {len(txns)} 笔交易")

    # 从 dividends 表加载分红记录（独立于 transactions 表）
    # 注意：BOCI 分红也需要计税（RSU 归属时通过工资完税，但归属后
    # 持有期间的现金分红仍需缴纳 20% 股息税，可申请外国税收抵免）
    div_rows = conn.execute("""
        SELECT * FROM dividends
        WHERE strftime('%Y', payment_date) = ?
        ORDER BY payment_date
    """, (str(year),)).fetchall()
    for r in div_rows:
        td = date.fromisoformat(r['payment_date'])
        # 净预扣 = 初始预扣 - ROC 返还（杠杆 ETF 净预扣 = $0）
        net_withholding = Decimal(str(r['withholding_tax'] or 0)) - Decimal(str(r['withholding_refund'] or 0))
        txns.append(Transaction(
            id=f"div_{r['id']}",
            broker=r['broker_code'],
            date=td,
            symbol=r['symbol'],
            action=Action.DIVIDEND,
            quantity=r['share_quantity'] or 0,
            price=Decimal(str(r['per_share_amount'] or 0)),
            amount=Decimal(str(r['gross_amount'] or 0)),
            fee=Decimal(str(r['collection_fee'] or 0)) + Decimal(str(r['adr_fee'] or 0)),
            tax_withheld=net_withholding,
            currency=r['currency'] or 'USD',
            exchange_rate=Decimal(str(r['exchange_rate'] or 0)),
        ))

    # 从 rsu_vests 表加载 RSU 归属记录（用于累进税计算 + FIFO 成本追溯）
    # 替代原需手动运行的 seed-rsu 命令
    rsu_rows = conn.execute("""
        SELECT * FROM rsu_vests
        WHERE strftime('%Y', vest_date) = ?
        ORDER BY vest_date
    """, (str(year),)).fetchall()
    for r in rsu_rows:
        td = date.fromisoformat(r['vest_date'])
        txns.append(Transaction(
            id=f"rsu_vest_{r['id']}",
            broker=r['custody_broker'] if r['custody_broker'] else 'unknown',
            date=td,
            symbol=r['symbol'],
            action=Action.RSU_VEST,
            quantity=r['vested_quantity'],
            price=Decimal(str(r['fmv_per_share'])),
            amount=Decimal(str(r['taxable_income'] or 0)),
            fee=Decimal("0"),
            tax_withheld=Decimal(str(r['tax_amount'] or 0)),
            currency=r['currency'] or 'USD',
            exchange_rate=Decimal(str(r['exchange_rate'] or 0)),
        ))
        # sell-to-cover：雇主自动卖出部分 RSU 股用于缴税，需同步创建
        # RSU_SELL 交易以消除 FIFO 中的幻影股。卖出价 = FMV（归属日公允价）
        # → 卖出价 = 成本价 → 零资本利得/损失
        sell_to_cover = r['sell_to_cover'] if r['sell_to_cover'] else 0
        if sell_to_cover > 0:
            txns.append(Transaction(
                id=f"rsu_sell_to_cover_{r['id']}",
                broker=r['custody_broker'] if r['custody_broker'] else 'unknown',
                date=td,
                symbol=r['symbol'],
                action=Action.RSU_SELL,
                quantity=sell_to_cover,
                price=Decimal(str(r['fmv_per_share'])),
                amount=Decimal(str(r['fmv_per_share'])) * sell_to_cover,
                fee=Decimal("0"),
                tax_withheld=Decimal(str(r['tax_amount'] or 0)),
                currency=r['currency'] or 'USD',
                exchange_rate=Decimal(str(r['exchange_rate'] or 0)),
                domestic_tax_paid=False,
            ))
    conn.close()

    if div_rows:
        click.echo(f"加载 {len(div_rows)} 笔分红记录")

    if rsu_rows:
        click.echo(f"加载 {len(rsu_rows)} 笔 RSU 归属记录")

    click.echo(f"总计 {len(txns)} 笔交易")

    # 加载上年末持仓（跨年度持有基础）
    # 优先级：1) JSON 文件（上年计算结果）  2) 数据库 carryforward tax_lots  3) 数据库 positions 表（FIFO 模拟）
    lots_path = Path(f"output/tax_{year-1}/lots_{year-1}.json")
    existing_lots = None
    if lots_path.exists():
        existing_lots = _load_lots_json(str(lots_path))
        click.echo(f"加载上年末持仓（{_count_lots(existing_lots)} 个批次）")
    else:
        # 尝试从 carryforward tax_lots 加载（年度结转批次）
        existing_lots = _load_carryforward_lots_from_db(path, year)
        if existing_lots:
            click.echo(f"加载上年末结转持仓（{_count_lots(existing_lots)} 个批次，来自 carryforward tax_lots）")
        else:
            existing_lots = _load_existing_lots_from_db(path, year)

    # 不再使用 _add_missing_lots：卖出 > 买入说明数据不完整，应报错而非创建 phantom lot
    # phantom lot 会导致卖出全额征税（成本=0），造成虚假应税收入

    # H-1 修复：保留完整结转记录（含 source_year），供 FIFO 顺序消耗
    ftc_repo = ForeignTaxCreditCarryforwardRepository(path)
    carryforwards: dict[tuple[str, str], list[dict]] = {}
    for country in ["US", "HK"]:
        for category in ["capital_gain", "dividend", "interest"]:
            available = ftc_repo.get_available(year, country, category)
            if available:
                key = (country, category)
                total = sum(Decimal(str(r["remaining_amount"])) for r in available)
                carryforwards[key] = available
                click.echo(f"  可用结转抵免: {country} {category} ¥{total:,.2f}")

    # 数据完整性预检：检测 2025 卖出 > (上年结转 + 2025 买入) 的缺口
    # 按时间线扫描，确保 FIFO 不会因时序问题报错（转仓、先卖后买等）
    from collections import defaultdict
    buy_actions = {"BUY", "RSU_VEST", "OPTION_BUY", "OPTION_EXERCISE"}
    sell_actions = {"SELL", "OPTION_SELL", "RSU_SELL", "OPTION_EXPIRE"}

    # 按 symbol 收集全年交易并按时序扫描（跨券商合并）
    # 合并后：同一 symbol 的所有券商交易放在同一队列中
    txns_by_sym: dict[str, list] = defaultdict(list)
    for txn in txns:
        if txn.action.name in buy_actions or txn.action.name in sell_actions:
            txns_by_sym[txn.symbol].append(txn)

    gaps_found = 0
    for sym, sym_txns in txns_by_sym.items():
        # 按券商分组统计 carryforward
        carry_by_broker: dict[str, int] = defaultdict(int)
        if existing_lots:
            for lot in existing_lots.get(sym, []):
                broker = lot.broker_code or ""
                carry_by_broker[broker] += lot.quantity

        # 按日期排序，同一天内买入优先于卖出（避免假时序赤字）
        sorted_sym_txns = sorted(sym_txns, key=lambda t: (
            t.date,
            0 if t.action.name in buy_actions else 1,
            t.id or ""
        ))

        # 按券商独立时序扫描：追踪各券商运行余额，记录最大赤字
        running_by_broker: dict[str, int] = defaultdict(int, carry_by_broker)
        max_deficit_by_broker: dict[str, int] = defaultdict(int)
        for txn in sorted_sym_txns:
            broker = txn.broker or ""
            if txn.action.name in buy_actions:
                running_by_broker[broker] += txn.quantity
            else:
                running_by_broker[broker] -= txn.quantity
            if running_by_broker[broker] < 0:
                max_deficit_by_broker[broker] = max(
                    max_deficit_by_broker[broker], -running_by_broker[broker]
                )

        for broker, max_deficit in max_deficit_by_broker.items():
            if max_deficit <= 0:
                continue
            # 期权不做 gap-fill（由 FIFO expire 的优雅降级处理为 $0 损失）
            # 避免 gap-fill lot 排在 FIFO 队列最前被卖出消耗导致虚假盈利
            is_option = "OPT_" in sym
            if is_option:
                click.echo(f"  跳过: {sym}[{broker}] 时序赤字 {max_deficit} 份（期权不补 lot，"
                           f"由 FIFO expire 优雅降级）")
                continue
            # 需要添加前置持仓来覆盖时序赤字
            # 尝试从历史数据回溯成本，避免 $0 成本导致虚假应税收入
            backfilled_cost = _backfill_cost_from_history(path, sym, f"{year - 1}-12-31")
            if existing_lots is None:
                existing_lots = {}
            # gap-fill lot 归属赤字的券商（per-broker FIFO 要求）
            existing_lots.setdefault(sym, []).append(TaxLot(
                symbol=sym,
                quantity=max_deficit,
                cost_per_share=backfilled_cost,
                acquire_date=date(year - 1, 12, 31),
                remaining=max_deficit,
                origin="gap_fill",
                broker_code=broker if broker else None,
            ))
            cost_note = f"${backfilled_cost:,.4f}" if backfilled_cost > 0 else "$0"
            broker_note = f"[{broker}]" if broker else ""
            click.echo(f"  警告: {sym}{broker_note} 时序赤字 {max_deficit} 份（可能为转仓），"
                       f"自动补 {cost_note} 成本 lot"
                       + ("（回溯历史数据）" if backfilled_cost > 0 else "（保守估计）"))
            gaps_found += 1

    if gaps_found:
        click.echo(f"共发现 {gaps_found} 个数据缺口（已自动修复小缺口）")

    # 保存 carryforwards 原始快照（用于追踪已消耗金额）
    from copy import deepcopy
    original_carryforwards = deepcopy(carryforwards)

    # 计算税务
    summary, remaining_lots, lot_consumptions = compute_tax(
        txns, year, existing_lots=existing_lots, carryforwards=carryforwards
    )

    # 持久化 FTC carryforward 已消耗金额（财税〔2020〕3号）
    # H-1 修复：compute_tax 已直接修改 records 的 remaining_amount（FIFO 顺序消耗）
    # 此处只需将变化写回数据库，不再按比例分摊
    ftc_repo = ForeignTaxCreditCarryforwardRepository(path)
    for key, records in original_carryforwards.items():
        country, category = key
        consumed_total = Decimal("0")
        for orig_rec, curr_rec in zip(records, carryforwards.get(key, [])):
            orig_remaining = Decimal(str(orig_rec.get("remaining_amount", 0)))
            curr_remaining = Decimal(str(curr_rec.get("remaining_amount", 0)))
            consumed = orig_remaining - curr_remaining
            if consumed > 0 and orig_rec.get("id"):
                ftc_repo.use_carryforward(int(orig_rec["id"]), float(consumed))
                consumed_total += consumed
        if consumed_total > 0:
            click.echo(f"  FTC 结转消耗: {country} {category} ¥{consumed_total:,.2f}")

    # 持久化当年新生成的结转记录（如净亏损年份的外国已扣税结转）
    for key, records in carryforwards.items():
        country, category = key
        for rec in records:
            if rec.get("id") is None and rec.get("remaining_amount", 0) > 0:
                ftc_repo.insert(
                    source_year=rec["source_year"],
                    country=rec["country"],
                    income_category=rec["income_category"],
                    carryforward_amount=rec["remaining_amount"],
                )

    # 持久化 lot_consumptions 到数据库（FIFO 审计追踪）
    lot_consumption_repo = LotConsumptionRepository(path)
    lot_consumption_repo.delete_all()

    # 建立 sell_txn_id 映射：calc-db 生成的临时 ID → DB transaction ID
    # 同时包含 OPTION_EXPIRE（过期也消耗 lot，需持久化到 lot_consumptions）
    sell_txn_map: dict[str, int] = {}
    expire_txn_ids: set[str] = set()
    for txn in txns:
        if txn.action.name in ("SELL", "RSU_SELL", "OPTION_SELL", "OPTION_EXPIRE"):
            if str(txn.id).isdigit():
                sell_txn_map[str(txn.id)] = int(txn.id)
                if txn.action.name == "OPTION_EXPIRE":
                    expire_txn_ids.add(str(txn.id))

    # 缓存已查找的 tax_lot（避免重复查询）
    lot_cache: dict[tuple, int | None] = {}

    # origin 到 acquisition_type 的映射（FIFO 引擎用 origin，DB 用 acquisition_type）
    origin_to_type = {
        "buy": "buy",
        "option_buy": "buy",
        "rsu_vest": "rsu_vest",
        "option_exercise": "exercise",
        "carryforward": "carryforward",
        "gap_fill": "gap_fill",
        "option_write": None,  # 写仓，无对应 lot
    }

    # 持久化每个 lot 消耗记录
    conn = get_connection(path)
    conn.row_factory = sqlite3.Row
    consumed_count = 0
    unmatched_count = 0
    for lc in lot_consumptions:
        # 查找对应的 sell transaction DB ID
        sell_db_id = sell_txn_map.get(lc["sell_txn_id"], 0)
        if not sell_db_id:
            # 非卖出交易（如合成的 RSU 分红等），跳过
            continue

        # 查找对应的 tax_lot
        cache_key = (lc["symbol"], lc["lot_date"], lc["lot_origin"], lc["cost_per_share"])
        if cache_key in lot_cache:
            tax_lot_id = lot_cache[cache_key]
        else:
            # 将 FIFO origin 映射为 DB acquisition_type
            acq_type = origin_to_type.get(lc["lot_origin"], lc["lot_origin"])

            lot_row = conn.execute("""
                SELECT id FROM tax_lots
                WHERE symbol = ? AND acquisition_date = ? AND acquisition_type = ?
                  AND CAST(cost_per_share AS REAL) = ?
                LIMIT 1
            """, (
                lc["symbol"],
                lc["lot_date"],
                acq_type,
                float(Decimal(lc["cost_per_share"])),
            )).fetchone()

            tax_lot_id = lot_row["id"] if lot_row else None

            # 未找到：该 lot 在 import 阶段未被创建（如 option_buy 未生成 tax_lot）
            # 插入该 lot 到 tax_lots 以确保审计完整
            if tax_lot_id is None:
                # 跳过 option_write（写仓，无对应买入 lot）
                if acq_type is None:
                    pass
                else:
                    cursor = conn.execute("""
                        INSERT INTO tax_lots
                            (symbol, broker_code, acquisition_date, acquisition_type,
                             quantity, remaining, cost_per_share, total_cost, currency)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        lc["symbol"],
                        lc.get("lot_broker_code"),
                        lc["lot_date"],
                        acq_type,
                        lc["consumed_qty"],
                        lc["consumed_qty"],
                        float(Decimal(lc["cost_per_share"])),
                        float(Decimal(lc["cost_basis"])),
                        lc["currency"],
                    ))
                    tax_lot_id = cursor.lastrowid

            lot_cache[cache_key] = tax_lot_id

        # 无 tax_lot_id 则跳过（写仓期权无对应买入 lot）
        if tax_lot_id is None:
            unmatched_count += 1
            if unmatched_count <= 3:
                click.echo(f"  未匹配 lot: {lc['symbol']} origin={lc['lot_origin']} date={lc['lot_date']} cost={lc['cost_per_share']}")
            continue

        consumption_type = "expire" if lc["sell_txn_id"] in expire_txn_ids else "sell"
        conn.execute("""
            INSERT INTO lot_consumptions
                (sell_txn_id, tax_lot_id, consumed_qty, cost_per_share,
                 cost_basis, realized_gain, consumption_type, currency)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            sell_db_id,
            tax_lot_id,
            lc["consumed_qty"],
            float(Decimal(lc["cost_per_share"])),
            float(Decimal(lc["cost_basis"])),
            float(Decimal(lc["gain_loss"])),
            consumption_type,
            lc["currency"],
        ))
        consumed_count += 1

    conn.commit()
    conn.close()
    msg = f"已持久化 {consumed_count} 笔 lot 消耗记录（FIFO 审计追踪）"
    if unmatched_count:
        msg += f"（{unmatched_count} 笔未匹配到 tax_lot）"
    click.echo(msg)

    # 同步 expire 交易金额到 FIFO 实际消耗成本（审计一致性约束）
    # 约束：transaction.amount 必须等于 lot_consumptions 中该 expire 的 cost_basis 总和
    # 这是税务审计的底线——数据库内部必须自洽
    _sync_expire_amounts_to_fifo(path, lot_consumptions, sell_txn_map)

    # 持久化到数据库
    tax_repo = TaxItemRepository(path)
    summary_repo = TaxSummaryRepository(path)

    # 先清除该年度的旧数据
    old_items = tax_repo.get_by_year(year)
    if old_items:
        conn = get_connection(path)
        conn.execute("DELETE FROM tax_items WHERE tax_year = ?", (year,))
        conn.execute("DELETE FROM tax_summaries WHERE tax_year = ?", (year,))
        conn.commit()
        conn.close()
        click.echo(f"清除 {len(old_items)} 条旧税务记录")

    # 保存 capital gains
    for item in summary.capital_gains:
        tax_repo.insert(
            tax_year=year,
            symbol=item.symbol,
            income_type=item.income_type,
            gross_income=float(item.gross_income_cny),
            taxable_income=float(item.taxable_income_cny),
            tax_rate=float(item.tax_rate),
            tax_amount_cny=float(item.tax_amount_cny),
            deductible=float(item.deductible_cny),
            trade_date=item.date,
            currency="USD",
            tax_withheld_cny=float(item.tax_withheld_cny),
            foreign_credit_cny=float(item.foreign_tax_credit_cny),
            excess_withholding_cny=float(item.excess_withholding_cny),
            tax_payable_cny=float(item.tax_payable_cny),
            detail=item.detail,
        )

    # 保存 dividends
    for item in summary.dividends:
        tax_repo.insert(
            tax_year=year,
            symbol=item.symbol,
            income_type=item.income_type,
            gross_income=float(item.gross_income_cny),
            taxable_income=float(item.taxable_income_cny),
            tax_rate=float(item.tax_rate),
            tax_amount_cny=float(item.tax_amount_cny),
            trade_date=item.date,
            currency="USD",
            tax_withheld_cny=float(item.tax_withheld_cny),
            foreign_credit_cny=float(item.foreign_tax_credit_cny),
            excess_withholding_cny=float(item.excess_withholding_cny),
            tax_payable_cny=float(item.tax_payable_cny),
            detail=item.detail,
        )

    # 保存 fees
    for item in summary.fees:
        tax_repo.insert(
            tax_year=year,
            symbol=item.symbol,
            income_type=item.income_type,
            gross_income=float(item.gross_income_cny),
            taxable_income=float(item.taxable_income_cny),
            tax_rate=float(item.tax_rate),
            tax_amount_cny=float(item.tax_amount_cny),
            deductible=float(item.deductible_cny),
            trade_date=item.date,
            currency="USD",
            detail=item.detail,
        )

    # 保存 RSU 收入
    if summary.rsu_income:
        rsu = summary.rsu_income
        tax_repo.insert(
            tax_year=year,
            symbol=rsu.symbol,
            income_type=rsu.income_type,
            gross_income=float(rsu.gross_income_cny),
            taxable_income=float(rsu.taxable_income_cny),
            tax_rate=float(rsu.tax_rate),
            tax_amount_cny=float(rsu.tax_amount_cny),
            trade_date=rsu.date,
            currency="CNY",
            tax_withheld_cny=float(rsu.tax_withheld_cny),
            foreign_credit_cny=float(rsu.foreign_tax_credit_cny),
            excess_withholding_cny=float(rsu.excess_withholding_cny),
            tax_payable_cny=float(rsu.tax_payable_cny),
            detail=rsu.detail,
        )

    # 保存汇总
    summary_repo.upsert(
        tax_year=year,
        income_type="capital_gain",
        total_taxable_cny=float(sum(i.taxable_income_cny for i in summary.capital_gains)),
        total_tax_cny=float(sum(i.tax_amount_cny for i in summary.capital_gains)),
        total_withheld_cny=float(sum(i.tax_withheld_cny for i in summary.capital_gains)),
        total_credit_cny=float(summary.total_foreign_tax_credit_cny),
        total_excess_cny=float(summary.total_excess_withholding_cny),
        total_payable_cny=float(sum(i.tax_payable_cny for i in summary.capital_gains)),
        computation_method=summary.computation_method,
    )
    summary_repo.upsert(
        tax_year=year,
        income_type="dividend",
        total_taxable_cny=float(sum(i.taxable_income_cny for i in summary.dividends)),
        total_tax_cny=float(sum(i.tax_amount_cny for i in summary.dividends)),
        total_withheld_cny=float(sum(i.tax_withheld_cny for i in summary.dividends)),
        total_payable_cny=float(sum(i.tax_payable_cny for i in summary.dividends)),
    )
    summary_repo.upsert(
        tax_year=year,
        income_type="fee_expense",
        total_deductible_cny=float(summary.total_deductible_fees_cny),
    )
    if summary.rsu_income:
        rsu = summary.rsu_income
        summary_repo.upsert(
            tax_year=year,
            income_type="rsu_income",
            total_taxable_cny=float(rsu.taxable_income_cny),
            total_tax_cny=float(rsu.tax_amount_cny),
            total_withheld_cny=float(rsu.tax_withheld_cny),
            total_credit_cny=float(rsu.foreign_tax_credit_cny),
            total_excess_cny=float(rsu.excess_withholding_cny),
            total_payable_cny=float(rsu.tax_payable_cny),
        )

    # 保存境外税收抵免结转（超额未抵免可向后结转 5 年）
    ftc_repo = ForeignTaxCreditCarryforwardRepository(path)
    for item in summary.capital_gains + summary.dividends:
        if item.excess_withholding_cny > 0:
            from src.calculator.tax_engine import detect_country
            country = detect_country(item)
            category = "dividend" if item.income_type.startswith("dividend") else "capital_gain"
            ftc_repo.insert(
                source_year=year,
                country=country,
                income_category=category,
                carryforward_amount=float(item.excess_withholding_cny),
            )
            click.echo(f"  结转抵免: {country} {category} ¥{item.excess_withholding_cny:,.2f}")

    click.echo(f"税务记录已保存到数据库")

    # 生成 CSV 报表
    out_dir = output or f"output/tax_{year}"
    write_tax_report(summary, out_dir)

    # 生成 HTML 详细报告
    db_file = db_path or "output/tax.db"
    html_path = Path(out_dir) / f"tax_report_{year}.html"
    from src.report.detailed_html_report import generate_detailed_html_report
    generate_detailed_html_report(db_file, year, str(html_path))
    click.echo(f"  HTML 详细报告: {html_path}")

    # 保存年末持仓
    lots_path = Path(out_dir) / f"lots_{year}.json"
    _save_lots_json(remaining_lots, lots_path)

    # 打印税务汇总
    click.echo(f"\n=== {year} 年度税务汇总 ===")
    click.echo(f"计税方法: {summary.computation_method}")
    if summary.computation_method == "annual_net" and summary.annual_net_comparison:
        info = summary.annual_net_comparison
        click.echo(f"  逐笔计算: ¥{info['per_txn_tax_amount']:,.2f} → 年度净额: ¥{info['tax_amount_cny']:,.2f}（省 ¥{info['per_txn_tax_amount'] - info['tax_amount_cny']:,.2f}）")

    if summary.capital_gains:
        net_cg = sum(i.taxable_income_cny for i in summary.capital_gains)
        cg_tax = sum(i.tax_payable_cny for i in summary.capital_gains)
        click.echo(f"\n资本利得（财产转让 20%）")
        click.echo(f"  盈利总额: ¥{net_cg:,.2f}")
        click.echo(f"  应补缴: ¥{cg_tax:,.2f}")

    if summary.rsu_income:
        rsu = summary.rsu_income
        click.echo(f"\nRSU 归属（股权激励所得 3%~45% 累进）")
        click.echo(f"  归属收入: ¥{rsu.taxable_income_cny:,.2f}")
        click.echo(f"  适用税率: {rsu.tax_rate * 100:.0f}%")
        click.echo(f"  应纳税额: ¥{rsu.tax_amount_cny:,.2f}")
        click.echo(f"  境内已代扣: ¥{rsu.domestic_withheld_cny:,.2f}")
        click.echo(f"  应补缴: ¥{rsu.tax_payable_cny:,.2f}")

    if summary.fees:
        total_fees = sum(f.deductible_cny for f in summary.fees)
        click.echo(f"\n可抵扣费用")
        click.echo(f"  费用总额: ¥{total_fees:,.2f}")
        for f in summary.fees:
            click.echo(f"  {f.date} {f.symbol} ¥{f.deductible_cny:,.2f}")

    # 分离分红和利息所得
    div_items = [i for i in summary.dividends if i.income_type == "dividend"]
    int_items = [i for i in summary.dividends if i.income_type.startswith("interest")]

    if div_items:
        total_div = sum(i.taxable_income_cny for i in div_items)
        div_tax = sum(i.tax_amount_cny for i in div_items)
        div_withheld = sum(i.tax_withheld_cny for i in div_items)
        div_payable = sum(i.tax_payable_cny for i in div_items)
        click.echo(f"\n分红（股息红利 20%）")
        click.echo(f"  分红总额: ¥{total_div:,.2f}")
        click.echo(f"  应纳税额: ¥{div_tax:,.2f}")
        click.echo(f"  境外已扣: ¥{div_withheld:,.2f}")
        click.echo(f"  应补缴: ¥{div_payable:,.2f}")

    if int_items:
        total_int = sum(i.taxable_income_cny for i in int_items)
        int_tax = sum(i.tax_amount_cny for i in int_items)
        int_withheld = sum(i.tax_withheld_cny for i in int_items)
        int_payable = sum(i.tax_payable_cny for i in int_items)
        click.echo(f"\n利息所得（20%）")
        click.echo(f"  利息总额: ¥{total_int:,.2f}")
        click.echo(f"  应纳税额: ¥{int_tax:,.2f}")
        click.echo(f"  境外已扣: ¥{int_withheld:,.2f}")
        click.echo(f"  应补缴: ¥{int_payable:,.2f}")

    total_payable = summary.total_tax_payable_cny
    click.echo(f"\n{'='*40}")
    click.echo(f"合计应补缴: ¥{total_payable:,.2f}")
    if summary.total_foreign_tax_credit_cny > 0:
        click.echo(f"境外税收抵免: ¥{summary.total_foreign_tax_credit_cny:,.2f}")
    if summary.total_excess_withholding_cny > 0:
        click.echo(f"超额未抵免（可结转5年）: ¥{summary.total_excess_withholding_cny:,.2f}")
    click.echo(f"报表已保存至: {out_dir}/")


@main.command("calc-all")
@click.option("--year", type=int, required=True, help="计税年度")
@click.option("--db-path", type=str, default=None, help="数据库路径")
@click.option("--usd-cny", type=float, default=None, help="USD/CNY 年末汇率（如 7.0288）")
@click.option("--output", "-o", default=None, help="输出目录")
@click.option("--skip-harness", is_flag=True, help="跳过 harness 校验")
@click.option("--skip-carryforward", is_flag=True, help="跳过年度结转（已有结转数据时）")
def calc_all(year: int, db_path: str | None, usd_cny: float | None,
             output: str | None, skip_harness: bool, skip_carryforward: bool):
    """一键算税：导入月结单 → 就绪检查 → 年度结转 → 税务计算 → 合规验证

    给定年份 + 年结单 PDF + 年末汇率 → 自动完成全流程。
    幂等：可重复运行，税务结果按年覆盖。
    """
    from src.database import init_db, get_connection
    from src.database.repositories import ExchangeRateRepository
    from src.database.rebuild import DatabaseRebuilder
    from src.harness.pre_calc import check_pre_calc_readiness
    from src.harness.quality import run_full_harness

    path = Path(db_path) if db_path else Path("output") / "tax.db"

    # ═══════════════════════════════════════════
    # Step 1: 数据库初始化
    # ═══════════════════════════════════════════
    click.echo("=== Step 1: 数据库初始化 ===")
    init_db(path)
    click.echo(f"  数据库: {path}")

    # ═══════════════════════════════════════════
    # Step 2: 汇率持久化
    # ═══════════════════════════════════════════
    click.echo("\n=== Step 2: 汇率持久化 ===")
    if usd_cny is not None:
        rate_repo = ExchangeRateRepository(path)
        year_end = f"{year}-12-31"
        rate_repo.upsert(year_end, "USD", "CNY", usd_cny, source="calc_all")
        click.echo(f"  已写入 exchange_rates: {year_end} USD/CNY = {usd_cny}")

        # 同时更新内存缓存
        import src.config
        import src.calculator.exchange_rate
        src.config.DEFAULT_EXCHANGE_RATE = usd_cny
        src.calculator.exchange_rate._rate_cache.clear()
    else:
        # 用户未指定汇率，检查并提示输入
        year_end_rate = _ensure_year_end_rate(year)
        # 将确认的汇率持久化到数据库
        rate_repo = ExchangeRateRepository(path)
        year_end = f"{year}-12-31"
        rate_repo.upsert(year_end, "USD", "CNY", float(year_end_rate), source="calc_all")

    # ═══════════════════════════════════════════
    # Step 3: 导入月结单（三券商逐文件重解析）
    # ═══════════════════════════════════════════
    click.echo("\n=== Step 3: 导入月结单 ===")
    rebuilder = DatabaseRebuilder(path)
    rebuilder.rebuild(
        interactive=False,
        skip_position=True,  # calc-all 不阻塞于持仓验证
    )

    # ═══════════════════════════════════════════
    # Step 4: 预计算就绪检查
    # ═══════════════════════════════════════════
    click.echo("\n=== Step 4: 预计算就绪检查 ===")
    readiness = check_pre_calc_readiness(
        db_path=path, year=year, usd_cny=usd_cny,
    )
    click.echo(readiness.summary())

    if not readiness.passed:
        errors = [i for i in readiness.issues if i.severity == "ERROR"]
        if errors:
            click.echo(f"\n❌ 存在 {len(errors)} 个阻断项，无法继续算税：")
            for e in errors:
                click.echo(f"  {e}")
            raise SystemExit(1)
        else:
            click.echo("\n⚠️  存在 WARNING，但可继续算税")

    # ═══════════════════════════════════════════
    # Step 5: 年度结转持仓
    # ═══════════════════════════════════════════
    if not skip_carryforward:
        click.echo(f"\n=== Step 5: 年度结转（{year-1}-12-31 → {year}-01-01）===")
        # 检查是否已有结转数据
        conn = get_connection(path)
        conn.row_factory = sqlite3.Row
        existing_cf = conn.execute(
            "SELECT COUNT(*) as cnt FROM tax_lots WHERE acquisition_type = 'carryforward' AND acquisition_date = ?",
            (f"{year-1}-12-31",)
        ).fetchone()["cnt"]
        conn.close()

        if existing_cf > 0:
            click.echo(f"  已有 {existing_cf} 个 carryforward 批次，跳过结转")
        else:
            click.echo("  无结转数据，尝试创建...")
            carryforward(year - 1, str(path), usd_cny)
    else:
        click.echo("\n=== Step 5: 跳过年度结转 ===")

    # ═══════════════════════════════════════════
    # Step 6: 税务计算（调用 calc-db 核心逻辑）
    # ═══════════════════════════════════════════
    click.echo("\n=== Step 6: 税务计算 ===")
    # 直接调用 calc-db 的核心逻辑（避免子进程调用）
    _run_calc_db(path, year, output, usd_cny)

    # ═══════════════════════════════════════════
    # Step 7: 合规验证（Harness）
    # ═══════════════════════════════════════════
    if not skip_harness:
        click.echo("\n=== Step 7: 合规验证 ===")
        report = run_full_harness(
            db_path=path,
            year=year,
            skip_validation=False,
            skip_reconciliation=False,
            skip_verification=False,
            skip_multi_account=False,
            skip_overpayment=False,
            usd_cny=usd_cny,
        )
        click.echo(report.summary())
        if not report.all_passed:
            click.echo("\n⚠️  Harness 校验未全部通过，请检查报告")

    click.echo(f"\n{'='*60}")
    click.echo(f"  {year} 年度一键算税完成")
    click.echo(f"{'='*60}")


def _run_calc_db(db_path: Path, year: int, output: str | None, usd_cny: float | None):
    """calc-db 核心逻辑（从 calc-db 命令提取，供 calc-all 复用）"""
    from src.database import get_connection
    from src.database.repositories import (
        TaxItemRepository, TaxSummaryRepository, LotConsumptionRepository,
        ForeignTaxCreditCarryforwardRepository,
    )
    from src.calculator.tax_engine import compute_tax
    from src.calculator.exchange_rate import load_exchange_rates
    from src.report.csv_report import write_tax_report
    from src.models import Transaction, Action, TaxLot

    # 加载汇率
    load_exchange_rates()

    # 同步 tax_lots.remaining 并补充缺失的期权过期交易记录
    expire_txns = _inject_option_expire_transactions(db_path, year)

    conn = get_connection(db_path)
    conn.row_factory = sqlite3.Row

    # 加载目标年度交易
    rows = conn.execute("""
        SELECT * FROM transactions
        WHERE strftime('%Y', trade_date) = ?
          AND NOT (broker_code = 'boci' AND action = 'rsu_vest')
        ORDER BY trade_date,
                 CASE action
                     WHEN 'buy' THEN 0 WHEN 'rsu_vest' THEN 0
                     WHEN 'option_buy' THEN 0 WHEN 'option_exercise' THEN 0
                     WHEN 'sell' THEN 1 WHEN 'rsu_sell' THEN 1
                     WHEN 'option_sell' THEN 1 WHEN 'option_expire' THEN 1
                     WHEN 'dividend' THEN 2 WHEN 'interest' THEN 2
                     WHEN 'cash_reward' THEN 2 ELSE 3
                 END,
                 rowid
    """, (str(year),)).fetchall()

    txns = []
    for r in rows:
        try:
            action = Action(r['action'])
        except ValueError:
            continue
        td = date.fromisoformat(r['trade_date'])
        price_val = r['price'] or 0
        if r['action'] == 'option_exercise' and r['raw_data']:
            try:
                rd = json.loads(r['raw_data'])
                premium = rd.get('option_premium', 0)
                price_val = (r['price'] or 0) + premium
            except (json.JSONDecodeError, TypeError):
                pass
        amount_val = r['amount'] or 0

        txns.append(Transaction(
            id=str(r['id']),
            broker=r['broker_code'],
            date=td,
            symbol=r['symbol'],
            action=action,
            quantity=r['quantity'] or 0,
            price=Decimal(str(price_val)),
            amount=Decimal(str(amount_val)),
            fee=Decimal(str(r['commission'] or 0)) + Decimal(str(r['platform_fee'] or 0))
                + Decimal(str(r['sec_fee'] or 0)) + Decimal(str(r['taf_fee'] or 0))
                + Decimal(str(r['delivery_fee'] or 0)) + Decimal(str(r['other_fees'] or 0)),
            tax_withheld=Decimal(str(r['tax_withheld'] or 0)),
            currency=r['currency'] or 'USD',
            exchange_rate=Decimal(str(r['exchange_rate'])) if r['exchange_rate'] else Decimal('0'),
        ))
    click.echo(f"  加载 {len(txns)} 笔交易")

    # 加载分红记录
    div_rows = conn.execute("""
        SELECT * FROM dividends WHERE strftime('%Y', payment_date) = ? ORDER BY payment_date
    """, (str(year),)).fetchall()
    for r in div_rows:
        td = date.fromisoformat(r['payment_date'])
        txns.append(Transaction(
            id=f"div_{r['id']}", broker=r['broker_code'], date=td, symbol=r['symbol'],
            action=Action.DIVIDEND, quantity=r['share_quantity'] or 0,
            price=Decimal(str(r['per_share_amount'] or 0)),
            amount=Decimal(str(r['gross_amount'] or 0)),
            fee=Decimal(str(r['collection_fee'] or 0)) + Decimal(str(r['adr_fee'] or 0)),
            tax_withheld=Decimal(str(r['withholding_tax'] or 0)),
            currency=r['currency'] or 'USD',
            exchange_rate=Decimal(str(r['exchange_rate'] or 0)),
        ))

    # 加载 RSU 归属记录
    rsu_rows = conn.execute("""
        SELECT * FROM rsu_vests WHERE strftime('%Y', vest_date) = ? ORDER BY vest_date
    """, (str(year),)).fetchall()
    for r in rsu_rows:
        td = date.fromisoformat(r['vest_date'])
        txns.append(Transaction(
            id=f"rsu_vest_{r['id']}", broker=r['custody_broker'] or 'unknown', date=td,
            symbol=r['symbol'], action=Action.RSU_VEST, quantity=r['vested_quantity'],
            price=Decimal(str(r['fmv_per_share'])),
            amount=Decimal(str(r['taxable_income'] or 0)), fee=Decimal("0"),
            tax_withheld=Decimal(str(r['tax_amount'] or 0)),
            currency=r['currency'] or 'USD',
            exchange_rate=Decimal(str(r['exchange_rate'] or 0)),
        ))
        sell_to_cover = r['sell_to_cover'] or 0
        if sell_to_cover > 0:
            txns.append(Transaction(
                id=f"rsu_sell_to_cover_{r['id']}", broker=r['custody_broker'] or 'unknown',
                date=td, symbol=r['symbol'], action=Action.RSU_SELL, quantity=sell_to_cover,
                price=Decimal(str(r['fmv_per_share'])),
                amount=Decimal(str(r['fmv_per_share'])) * sell_to_cover,
                fee=Decimal("0"), tax_withheld=Decimal(str(r['tax_amount'] or 0)),
                currency=r['currency'] or 'USD',
                exchange_rate=Decimal(str(r['exchange_rate'] or 0)),
            ))
    conn.close()

    if div_rows:
        click.echo(f"  加载 {len(div_rows)} 笔分红记录")
    if rsu_rows:
        click.echo(f"  加载 {len(rsu_rows)} 笔 RSU 归属记录")
    click.echo(f"  总计 {len(txns)} 笔交易")

    # 加载上年末持仓
    lots_path = Path(f"output/tax_{year-1}/lots_{year-1}.json")
    existing_lots = None
    if lots_path.exists():
        existing_lots = _load_lots_json(str(lots_path))
        click.echo(f"  加载上年末持仓（{_count_lots(existing_lots)} 个批次）")
    else:
        existing_lots = _load_carryforward_lots_from_db(db_path, year)
        if existing_lots:
            click.echo(f"  加载上年末结转持仓（{_count_lots(existing_lots)} 个批次）")
        else:
            existing_lots = _load_existing_lots_from_db(db_path, year)

    # 加载 FTC 结转
    # H-1 修复：保留完整结转记录
    ftc_repo = ForeignTaxCreditCarryforwardRepository(db_path)
    carryforwards: dict[tuple[str, str], list[dict]] = {}
    for country in ["US", "HK"]:
        for category in ["capital_gain", "dividend", "interest"]:
            available = ftc_repo.get_available(year, country, category)
            if available:
                key = (country, category)
                total = sum(Decimal(str(r["remaining_amount"])) for r in available)
                carryforwards[key] = available
                click.echo(f"  可用结转抵免: {country} {category} ¥{total:,.2f}")

    # FIFO 时序扫描 + gap-fill（复用 calc-db 逻辑）
    from collections import defaultdict
    buy_actions = {"BUY", "RSU_VEST", "OPTION_BUY", "OPTION_EXERCISE"}
    sell_actions = {"SELL", "OPTION_SELL", "RSU_SELL", "OPTION_EXPIRE"}

    txns_by_sym: dict[str, list] = defaultdict(list)
    for txn in txns:
        if txn.action.name in buy_actions or txn.action.name in sell_actions:
            txns_by_sym[txn.symbol].append(txn)

    for sym, sym_txns in txns_by_sym.items():
        carry_by_broker: dict[str, int] = defaultdict(int)
        if existing_lots:
            for lot in existing_lots.get(sym, []):
                carry_by_broker[lot.broker_code or ""] += lot.quantity

        sorted_sym_txns = sorted(sym_txns, key=lambda t: (
            t.date, 0 if t.action.name in buy_actions else 1, t.id or ""
        ))

        running_by_broker: dict[str, int] = defaultdict(int, carry_by_broker)
        max_deficit_by_broker: dict[str, int] = defaultdict(int)
        for txn in sorted_sym_txns:
            broker = txn.broker or ""
            if txn.action.name in buy_actions:
                running_by_broker[broker] += txn.quantity
            else:
                running_by_broker[broker] -= txn.quantity
            if running_by_broker[broker] < 0:
                max_deficit_by_broker[broker] = max(max_deficit_by_broker[broker], -running_by_broker[broker])

        for broker, max_deficit in max_deficit_by_broker.items():
            if max_deficit <= 0:
                continue
            if "OPT_" in sym:
                continue
            backfilled_cost = _backfill_cost_from_history(db_path, sym, f"{year - 1}-12-31")
            if existing_lots is None:
                existing_lots = {}
            existing_lots.setdefault(sym, []).append(TaxLot(
                symbol=sym, quantity=max_deficit, cost_per_share=backfilled_cost,
                acquire_date=date(year - 1, 12, 31), remaining=max_deficit,
                origin="gap_fill", broker_code=broker if broker else None,
            ))
            cost_note = f"${backfilled_cost:,.4f}" if backfilled_cost > 0 else "$0"
            click.echo(f"  警告: {sym}[{broker}] 时序赤字 {max_deficit} 份，自动补 {cost_note} 成本 lot")

    from copy import deepcopy
    original_carryforwards = deepcopy(carryforwards)

    # 计算税务
    summary, remaining_lots, lot_consumptions = compute_tax(
        txns, year, existing_lots=existing_lots, carryforwards=carryforwards
    )

    # 持久化 FTC 消耗
    # H-1 修复：compute_tax 已直接修改 records 的 remaining_amount（FIFO 顺序消耗）
    ftc_repo = ForeignTaxCreditCarryforwardRepository(db_path)
    for key, records in original_carryforwards.items():
        country, category = key
        consumed_total = Decimal("0")
        for orig_rec, curr_rec in zip(records, carryforwards.get(key, [])):
            orig_remaining = Decimal(str(orig_rec.get("remaining_amount", 0)))
            curr_remaining = Decimal(str(curr_rec.get("remaining_amount", 0)))
            consumed = orig_remaining - curr_remaining
            if consumed > 0 and orig_rec.get("id"):
                ftc_repo.use_carryforward(int(orig_rec["id"]), float(consumed))
                consumed_total += consumed
        if consumed_total > 0:
            click.echo(f"  FTC 结转消耗: {country} {category} ¥{consumed_total:,.2f}")

    # 持久化当年新生成的结转记录（如净亏损年份的外国已扣税结转）
    for key, records in carryforwards.items():
        country, category = key
        for rec in records:
            if rec.get("id") is None and rec.get("remaining_amount", 0) > 0:
                ftc_repo.insert(
                    source_year=rec["source_year"],
                    country=rec["country"],
                    income_category=rec["income_category"],
                    carryforward_amount=rec["remaining_amount"],
                )

    # 持久化 lot_consumptions
    lot_consumption_repo = LotConsumptionRepository(db_path)
    lot_consumption_repo.delete_all()

    sell_txn_map: dict[str, int] = {}
    expire_txn_ids: set[str] = set()
    for txn in txns:
        if txn.action.name in ("SELL", "RSU_SELL", "OPTION_SELL", "OPTION_EXPIRE"):
            if str(txn.id).isdigit():
                sell_txn_map[str(txn.id)] = int(txn.id)
                if txn.action.name == "OPTION_EXPIRE":
                    expire_txn_ids.add(str(txn.id))

    origin_to_type = {
        "buy": "buy", "option_buy": "buy", "rsu_vest": "rsu_vest",
        "option_exercise": "exercise", "carryforward": "carryforward",
        "gap_fill": "gap_fill", "option_write": None,
    }

    lot_cache: dict[tuple, int | None] = {}
    consumed_count = 0
    unmatched_count = 0

    conn = get_connection(db_path)
    conn.row_factory = sqlite3.Row
    for lc in lot_consumptions:
        sell_db_id = sell_txn_map.get(lc["sell_txn_id"], 0)
        if not sell_db_id:
            continue

        cache_key = (lc["symbol"], lc["lot_date"], lc["lot_origin"], lc["cost_per_share"])
        if cache_key in lot_cache:
            tax_lot_id = lot_cache[cache_key]
        else:
            acq_type = origin_to_type.get(lc["lot_origin"], lc["lot_origin"])
            lot_row = conn.execute("""
                SELECT id FROM tax_lots
                WHERE symbol = ? AND acquisition_date = ? AND acquisition_type = ?
                  AND CAST(cost_per_share AS REAL) = ? LIMIT 1
            """, (lc["symbol"], lc["lot_date"], acq_type, float(Decimal(lc["cost_per_share"])))).fetchone()
            tax_lot_id = lot_row["id"] if lot_row else None

            if tax_lot_id is None:
                if acq_type is not None:
                    cursor = conn.execute("""
                        INSERT INTO tax_lots (symbol, broker_code, acquisition_date, acquisition_type,
                            quantity, remaining, cost_per_share, total_cost, currency)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (lc["symbol"], lc.get("lot_broker_code"), lc["lot_date"], acq_type,
                          lc["consumed_qty"], lc["consumed_qty"],
                          float(Decimal(lc["cost_per_share"])), float(Decimal(lc["cost_basis"])),
                          lc["currency"]))
                    tax_lot_id = cursor.lastrowid

            lot_cache[cache_key] = tax_lot_id

        if tax_lot_id is None:
            unmatched_count += 1
            if unmatched_count <= 3:
                click.echo(f"  未匹配 lot: {lc['symbol']} origin={lc['lot_origin']}")
            continue

        consumption_type = "expire" if lc["sell_txn_id"] in expire_txn_ids else "sell"
        conn.execute("""
            INSERT INTO lot_consumptions (sell_txn_id, tax_lot_id, consumed_qty, cost_per_share,
                cost_basis, realized_gain, consumption_type, currency)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (sell_db_id, tax_lot_id, lc["consumed_qty"], float(Decimal(lc["cost_per_share"])),
              float(Decimal(lc["cost_basis"])), float(Decimal(lc["gain_loss"])),
              consumption_type, lc["currency"]))
        consumed_count += 1

    conn.commit()
    conn.close()
    click.echo(f"  已持久化 {consumed_count} 笔 lot 消耗记录")

    # 同步 expire 金额
    _sync_expire_amounts_to_fifo(db_path, lot_consumptions, sell_txn_map)

    # 持久化税务记录
    tax_repo = TaxItemRepository(db_path)
    summary_repo = TaxSummaryRepository(db_path)

    conn = get_connection(db_path)
    conn.execute("DELETE FROM tax_items WHERE tax_year = ?", (year,))
    conn.execute("DELETE FROM tax_summaries WHERE tax_year = ?", (year,))
    conn.commit()
    conn.close()

    for item in summary.capital_gains:
        tax_repo.insert(
            tax_year=year, symbol=item.symbol, income_type=item.income_type,
            gross_income=float(item.gross_income_cny), taxable_income=float(item.taxable_income_cny),
            tax_rate=float(item.tax_rate), tax_amount_cny=float(item.tax_amount_cny),
            deductible=float(item.deductible_cny), trade_date=item.date, currency="USD",
            tax_withheld_cny=float(item.tax_withheld_cny), foreign_credit_cny=float(item.foreign_tax_credit_cny),
            excess_withholding_cny=float(item.excess_withholding_cny), tax_payable_cny=float(item.tax_payable_cny),
            detail=item.detail,
        )
    for item in summary.dividends:
        tax_repo.insert(
            tax_year=year, symbol=item.symbol, income_type=item.income_type,
            gross_income=float(item.gross_income_cny), taxable_income=float(item.taxable_income_cny),
            tax_rate=float(item.tax_rate), tax_amount_cny=float(item.tax_amount_cny),
            trade_date=item.date, currency="USD", tax_withheld_cny=float(item.tax_withheld_cny),
            foreign_credit_cny=float(item.foreign_tax_credit_cny),
            excess_withholding_cny=float(item.excess_withholding_cny), tax_payable_cny=float(item.tax_payable_cny),
            detail=item.detail,
        )
    for item in summary.fees:
        tax_repo.insert(
            tax_year=year, symbol=item.symbol, income_type=item.income_type,
            gross_income=float(item.gross_income_cny), taxable_income=float(item.taxable_income_cny),
            tax_rate=float(item.tax_rate), tax_amount_cny=float(item.tax_amount_cny),
            deductible=float(item.deductible_cny), trade_date=item.date, currency="USD",
            detail=item.detail,
        )
    if summary.rsu_income:
        rsu = summary.rsu_income
        tax_repo.insert(
            tax_year=year, symbol=rsu.symbol, income_type=rsu.income_type,
            gross_income=float(rsu.gross_income_cny), taxable_income=float(rsu.taxable_income_cny),
            tax_rate=float(rsu.tax_rate), tax_amount_cny=float(rsu.tax_amount_cny),
            trade_date=rsu.date, currency="CNY", tax_withheld_cny=float(rsu.tax_withheld_cny),
            foreign_credit_cny=float(rsu.foreign_tax_credit_cny),
            excess_withholding_cny=float(rsu.excess_withholding_cny), tax_payable_cny=float(rsu.tax_payable_cny),
            detail=rsu.detail,
        )

    summary_repo.upsert(
        tax_year=year, income_type="capital_gain",
        total_taxable_cny=float(sum(i.taxable_income_cny for i in summary.capital_gains)),
        total_tax_cny=float(sum(i.tax_amount_cny for i in summary.capital_gains)),
        total_withheld_cny=float(sum(i.tax_withheld_cny for i in summary.capital_gains)),
        total_credit_cny=float(summary.total_foreign_tax_credit_cny),
        total_excess_cny=float(summary.total_excess_withholding_cny),
        total_payable_cny=float(sum(i.tax_payable_cny for i in summary.capital_gains)),
        computation_method=summary.computation_method,
    )
    summary_repo.upsert(
        tax_year=year, income_type="dividend",
        total_taxable_cny=float(sum(i.taxable_income_cny for i in summary.dividends)),
        total_tax_cny=float(sum(i.tax_amount_cny for i in summary.dividends)),
        total_withheld_cny=float(sum(i.tax_withheld_cny for i in summary.dividends)),
        total_payable_cny=float(sum(i.tax_payable_cny for i in summary.dividends)),
    )
    summary_repo.upsert(
        tax_year=year, income_type="fee_expense",
        total_deductible_cny=float(summary.total_deductible_fees_cny),
    )
    if summary.rsu_income:
        rsu = summary.rsu_income
        summary_repo.upsert(
            tax_year=year, income_type="rsu_income",
            total_taxable_cny=float(rsu.taxable_income_cny),
            total_tax_cny=float(rsu.tax_amount_cny),
            total_withheld_cny=float(rsu.tax_withheld_cny),
            total_credit_cny=float(rsu.foreign_tax_credit_cny),
            total_excess_cny=float(rsu.excess_withholding_cny),
            total_payable_cny=float(rsu.tax_payable_cny),
        )

    # FTC 结转
    ftc_repo = ForeignTaxCreditCarryforwardRepository(db_path)
    for item in summary.capital_gains + summary.dividends:
        if item.excess_withholding_cny > 0:
            from src.calculator.tax_engine import detect_country
            country = detect_country(item)
            category = "dividend" if item.income_type.startswith("dividend") else "capital_gain"
            ftc_repo.insert(source_year=year, country=country, income_category=category,
                           carryforward_amount=float(item.excess_withholding_cny))

    # 生成报表
    out_dir = output or f"output/tax_{year}"
    write_tax_report(summary, out_dir)

    # 生成 HTML 详细报告
    html_path = Path(out_dir) / f"tax_report_{year}.html"
    from src.report.detailed_html_report import generate_detailed_html_report
    generate_detailed_html_report(str(db_path), year, str(html_path))
    click.echo(f"  HTML 详细报告: {html_path}")

    lots_path = Path(out_dir) / f"lots_{year}.json"
    _save_lots_json(remaining_lots, lots_path)

    # 打印汇总
    click.echo(f"\n=== {year} 年度税务汇总 ===")
    if summary.computation_method == "annual_net" and summary.annual_net_comparison:
        info = summary.annual_net_comparison
        click.echo(f"  逐笔计算: ¥{info['per_txn_tax_amount']:,.2f} → 年度净额: ¥{info['tax_amount_cny']:,.2f}（省 ¥{info['per_txn_tax_amount'] - info['tax_amount_cny']:,.2f}）")

    if summary.capital_gains:
        net_cg = sum(i.taxable_income_cny for i in summary.capital_gains)
        cg_tax = sum(i.tax_payable_cny for i in summary.capital_gains)
        click.echo(f"  资本利得: ¥{net_cg:,.2f}, 应补缴: ¥{cg_tax:,.2f}")
    if summary.rsu_income:
        click.echo(f"  RSU 归属: ¥{summary.rsu_income.taxable_income_cny:,.2f}")

    div_items = [i for i in summary.dividends if i.income_type == "dividend"]
    int_items = [i for i in summary.dividends if i.income_type.startswith("interest")]
    if div_items:
        total_div = sum(i.taxable_income_cny for i in div_items)
        div_payable = sum(i.tax_payable_cny for i in div_items)
        click.echo(f"  分红: ¥{total_div:,.2f}, 应补缴: ¥{div_payable:,.2f}")
    if int_items:
        total_int = sum(i.taxable_income_cny for i in int_items)
        click.echo(f"  利息所得: ¥{total_int:,.2f}")

    click.echo(f"  合计应补缴: ¥{summary.total_tax_payable_cny:,.2f}")
    if summary.total_excess_withholding_cny > 0:
        click.echo(f"  超额未抵免: ¥{summary.total_excess_withholding_cny:,.2f}")
    click.echo(f"  报表: {out_dir}/")


@main.command("seed-rsu")
@click.option("--db-path", type=str, default=None, help="数据库路径")
def seed_rsu(db_path: str | None):
    """从 rsu_vests 表合成 rsu_vest 类型的 transaction 记录，供 FIFO 成本追溯"""
    import sqlite3
    from src.database.connection import get_connection, migrate_rsu_actions
    import json

    path = Path(db_path) if db_path else Path("output") / "tax.db"
    if not path.exists():
        click.echo(f"错误: 数据库不存在: {path}")
        raise SystemExit(1)

    # 先运行迁移（添加 rsu_vest/rsu_sell 到 CHECK 约束）
    migrate_rsu_actions(path)

    conn = get_connection(path)
    conn.row_factory = sqlite3.Row
    vests = conn.execute("SELECT * FROM rsu_vests").fetchall()
    inserted = 0

    for v in vests:
        ref = f"RSU-{v['id']}"
        existing = conn.execute(
            "SELECT id FROM transactions WHERE reference_no = ?", (ref,)
        ).fetchone()
        if existing:
            click.echo(f"  SKIP RSU-{v['id']} (already exists)")
            continue

        conn.execute("""
            INSERT INTO transactions
                (broker_code, trade_date, settlement_date, reference_no,
                 symbol, company_name, exchange,
                 action, quantity, price, amount, currency, exchange_rate,
                 statement_file_id, raw_data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            v["custody_broker"] or "boci",
            v["vest_date"],
            v["deposit_date"],
            ref,
            v["symbol"],
            v["company_name"] or v["symbol"],
            "NYSE" if v["symbol"] == "BABA" else "HKEX",
            "rsu_vest",
            v["vested_quantity"],
            v["fmv_per_share"],
            v["taxable_income"],
            v["currency"],
            v["exchange_rate"],
            None,
            json.dumps({"source": "rsu_vest", "vest_id": v["id"]}),
        ))
        inserted += 1
        click.echo(f"  RSU-{v['id']}: {v['symbol']} {v['vest_date']} qty={v['vested_quantity']} fmv=${v['fmv_per_share']}")

    conn.close()
    click.echo(f"\n共插入 {inserted} 笔 rsu_vest 交易记录")


@main.command("carryforward")
@click.option("--year", type=int, required=True, help="结转年度（从该年12月31日结转至下年1月1日）")
@click.option("--db-path", type=str, default=None, help="数据库路径")
@click.option("--usd-cny", type=float, default=None, help="USD/CNY 汇率（覆盖默认值，如 7.0288）")
def carryforward(year: int, db_path: str | None, usd_cny: float | None):
    """从指定年度12月31日期末持仓创建结转税务批次（carryforward tax lots）

    用法:
        carryforward --year 2024   # 从2024-12-31结转至2025-01-01
        carryforward --year 2025   # 从2025-12-31结转至2026-01-01

    税务合规依据：
    1. 中国个税按年计算，每年以年初持仓状态为起点
    2. 券商期末概览中的成本价可直接用作结转成本
    3. 无转仓操作时，上年12月31日持仓 = 下年1月1日持仓
    4. 券商官方月结对账单的期末持仓是审计认可的成本基础凭证
    """
    import pdfplumber
    import re
    from collections import defaultdict

    path = Path(db_path) if db_path else Path("output") / "tax.db"
    if not path.exists():
        click.echo(f"错误: 数据库不存在: {path}")
        raise SystemExit(1)

    carry_date = f"{year}-12-31"
    next_year = year + 1
    USDCNY = usd_cny if usd_cny is not None else 7.10  # 默认汇率，可通过 --usd-cny 或 exchange_rate 表覆盖

    init_db(path)
    tax_lot_repo = TaxLotRepository(path)
    conn = get_connection(path)
    conn.row_factory = sqlite3.Row

    # 清除已有的 carryforward 批次（避免重复）
    old_carryforwards = conn.execute(
        "SELECT COUNT(*) as cnt FROM tax_lots WHERE acquisition_type = 'carryforward'"
    ).fetchone()["cnt"]
    if old_carryforwards > 0:
        conn.execute("""
            DELETE FROM lot_consumptions
            WHERE tax_lot_id IN (SELECT id FROM tax_lots WHERE acquisition_type = 'carryforward')
        """)
        conn.execute("DELETE FROM tax_lots WHERE acquisition_type = 'carryforward'")
        conn.commit()
        click.echo(f"  清除旧 carryforward 批次: {old_carryforwards}")

    created = 0

    # ── 1. Longbridge 持仓 ──
    lb_positions = conn.execute("""
        SELECT symbol, quantity, avg_cost, closing_price
        FROM positions
        WHERE broker_code = 'longbridge' AND as_of_date = ?
    """, (carry_date,)).fetchall()

    click.echo(f"\n=== Longbridge 结转持仓（{carry_date}）===")
    for pos in lb_positions:
        symbol = pos["symbol"]
        qty = pos["quantity"]
        cost = pos["avg_cost"]

        if qty <= 0:
            continue

        if cost and cost > 0:
            cost_per_share = cost
        elif pos["closing_price"] and pos["closing_price"] > 0:
            cost_per_share = pos["closing_price"]
        else:
            click.echo(f"  SKIP {symbol}: 无成本数据")
            continue

        tax_lot_repo.add_lot(
            symbol=symbol, broker_code="longbridge",
            acquisition_date=carry_date, acquisition_type="carryforward",
            quantity=qty, cost_per_share=round(cost_per_share, 6),
            currency="USD", exchange_rate=USDCNY,
        )
        click.echo(f"  {symbol:15s} qty={qty:6d}  cost=${cost_per_share:,.4f}  total=${cost_per_share * qty:,.2f}")
        created += 1

    # ── 2. BOCI 持仓 ──
    boci_positions = conn.execute("""
        SELECT symbol, quantity, closing_price
        FROM positions
        WHERE broker_code = 'boci' AND as_of_date = ?
    """, (carry_date,)).fetchall()

    click.echo(f"\n=== BOCI 结转持仓（{carry_date}）===")
    for pos in boci_positions:
        symbol = pos["symbol"]
        qty = pos["quantity"]
        if qty <= 0:
            continue

        existing = conn.execute(
            "SELECT cost_per_share FROM tax_lots WHERE symbol = ? AND remaining > 0 AND acquisition_type != 'carryforward' LIMIT 1",
            (symbol,)
        ).fetchone()

        if existing:
            cost_per_share = existing["cost_per_share"]
        elif pos["closing_price"] and pos["closing_price"] > 0:
            cost_per_share = pos["closing_price"]
        else:
            click.echo(f"  SKIP {symbol}: 无成本数据")
            continue

        tax_lot_repo.add_lot(
            symbol=symbol, broker_code="boci",
            acquisition_date=carry_date, acquisition_type="carryforward",
            quantity=qty, cost_per_share=round(cost_per_share, 6),
            currency="USD", exchange_rate=USDCNY,
        )
        click.echo(f"  {symbol:15s} qty={qty:6d}  cost=${cost_per_share:,.4f}  total=${cost_per_share * qty:,.2f}")
        created += 1

    # ── 3. Futu 持仓（从 Dec 月结单 PDF 解析） ──
    from src.database.import_statements import INPUT_DIR, FUTU_2024_PASSWORD

    futu_dir = INPUT_DIR / "futu-2025-monthly"
    futu_dec_file = None
    if futu_dir.exists():
        target = f"{year:04d}12"
        for pdf_file in sorted(futu_dir.glob("*.pdf")):
            if target in pdf_file.stem:
                futu_dec_file = pdf_file
                break

    if futu_dec_file:
        click.echo(f"\n=== Futu 结转持仓（{carry_date}）===")
        with pdfplumber.open(str(futu_dec_file), password=FUTU_2024_PASSWORD) as pdf:
            full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)

        def dedup_chinese(text):
            result = []
            i = 0
            while i < len(text):
                ch = text[i]
                if ('一' <= ch <= '鿿' and i + 1 < len(text) and text[i + 1] == ch):
                    result.append(ch)
                    i += 2
                else:
                    result.append(ch)
                    i += 1
            return "".join(result)

        full_text = dedup_chinese(full_text)

        futu_pos_pattern = re.compile(
            r"^([A-Z0-9.]+)\((.+?)\)\s+US\s+USD\s+"
            r"([\d,]+)\s+([\d.]+)\s+(-|[\d,]+)\s+([\d,]+\.?\d*)",
            re.MULTILINE,
        )

        end_portfolio_section = full_text.split("期末概覽")[-1] if "期末概覽" in full_text else full_text
        end_portfolio_section = end_portfolio_section.split("製備日期")[0]

        for m in futu_pos_pattern.finditer(end_portfolio_section):
            symbol = m.group(1).strip()
            name = m.group(2).strip()
            qty = int(m.group(3).replace(",", ""))
            price = float(m.group(4))
            market_value = float(m.group(6).replace(",", ""))

            if qty <= 0 or market_value <= 0:
                continue

            is_option = name.endswith('C') or name.endswith('P') or "C)" in name or "P)" in name
            multiplier = 100 if is_option else 1
            cost_per_share = price if is_option else (market_value / qty if qty > 0 else 0)

            if cost_per_share <= 0:
                continue

            existing = conn.execute(
                "SELECT cost_per_share FROM tax_lots WHERE symbol = ? AND remaining > 0 AND acquisition_type != 'carryforward' LIMIT 1",
                (symbol,)
            ).fetchone()
            if existing:
                cost_per_share = existing["cost_per_share"]

            tax_lot_repo.add_lot(
                symbol=symbol, broker_code="futu",
                acquisition_date=carry_date, acquisition_type="carryforward",
                quantity=qty, cost_per_share=round(cost_per_share, 6),
                currency="USD", exchange_rate=USDCNY,
            )
            total = round(cost_per_share * qty * multiplier, 2)
            click.echo(f"  {symbol:25s} qty={qty:4d}  cost=${cost_per_share:,.4f}  total=${total:,.2f}")
            created += 1
    else:
        click.echo(f"\n=== Futu 结转持仓 ===")
        click.echo(f"  未找到 {year}年12月 Futu 月结单 PDF")

    conn.close()
    click.echo(f"\n--- 券商持仓快照汇总: {created} 个批次 ---")

    # ── 4. 补充遗漏持仓：从历史交易推导年末净持仓 ──
    conn = get_connection(path)
    conn.row_factory = sqlite3.Row

    buy_actions = ("buy", "option_exercise", "rsu_vest")
    sell_actions = ("sell", "option_sell", "rsu_sell", "option_expire")

    net_qty: dict[str, int] = defaultdict(int)
    buy_costs: dict[str, list] = defaultdict(list)

    rows = conn.execute(f"""
        SELECT symbol, action, quantity, price
        FROM transactions
        WHERE strftime('%Y', trade_date) <= ?
          AND action IN (?, ?, ?, ?, ?, ?, ?)
        ORDER BY trade_date, rowid
    """, (carry_date, *buy_actions, *sell_actions)).fetchall()

    for r in rows:
        sym = r["symbol"]
        qty = r["quantity"] or 0
        price = r["price"] or 0
        if r["action"] in buy_actions:
            net_qty[sym] += qty
            if qty > 0 and price > 0:
                buy_costs[sym].append({"qty": qty, "price": price})
        elif r["action"] in sell_actions:
            net_qty[sym] -= qty

    existing_symbols = set(
        r["symbol"] for r in conn.execute(
            "SELECT DISTINCT symbol FROM tax_lots WHERE acquisition_type = 'carryforward'"
        ).fetchall()
    )

    click.echo(f"\n=== 补充历史净持仓（{carry_date} 前）===")
    for sym, net in sorted(net_qty.items()):
        if net > 0 and sym not in existing_symbols:
            total_shares = sum(b["qty"] for b in buy_costs.get(sym, []))
            weighted_cost = sum(b["qty"] * b["price"] for b in buy_costs[sym]) / total_shares if total_shares > 0 else 0

            # 如果加权成本为 0（无有效买入记录），尝试从 RSU/positions 回溯
            if weighted_cost <= 0:
                backfilled = _backfill_cost_from_history(path, sym, carry_date)
                if backfilled > 0:
                    weighted_cost = float(backfilled)
                    click.echo(f"  {sym:25s} qty={net:6d}  cost=${weighted_cost:,.4f} (回溯)  total=${weighted_cost * net:,.2f}")
                else:
                    weighted_cost = 0
                    click.echo(f"  {sym:25s} qty={net:6d}  cost=$0.0000  (无法回溯)  total=$0.00")
            else:
                click.echo(f"  {sym:25s} qty={net:6d}  cost=${weighted_cost:,.4f}  total=${weighted_cost * net:,.2f}")

            tax_lot_repo.add_lot(
                symbol=sym, broker_code="longbridge",
                acquisition_date=carry_date, acquisition_type="carryforward",
                quantity=net, cost_per_share=round(weighted_cost, 6),
                currency="USD", exchange_rate=USDCNY,
            )
            created += 1

    conn.close()
    click.echo(f"\n最终共创建 {created} 个 carryforward tax lots")
    click.echo(f"结转日期: {carry_date} → {next_year}-01-01 | acquisition_type=carryforward")


if __name__ == "__main__":
    main()
