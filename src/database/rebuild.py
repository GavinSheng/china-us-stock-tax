"""数据库重建器 — 扫描 input/ 目录，解密 PDF，导入所有券商月结单"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pikepdf

from src.database.connection import get_connection, init_db, transaction
from src.database.importers import (
    BOCIImporter, FutuImporter, LongbridgeImporter,
    INPUT_DIR, DECRYPTED_DIR,
    LONGBRIDGE_PASSWORD, FUTU_2024_PASSWORD,
)
from src.database.repositories import (
    StatementFileRepository, TransactionRepository, DividendRepository,
    PositionRepository, RSUGrantRepository, RSUVestRepository,
    CashRewardRepository,
)


class DatabaseRebuilder:
    """逐文件重建数据库解析数据"""

    BROKER_IMPORTERS = {
        "futu": FutuImporter,
        "longbridge": LongbridgeImporter,
        "boci": BOCIImporter,
    }

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = str(db_path) if db_path else None
        init_db(self.db_path)

    # ============================================================
    # 清空数据
    # ============================================================

    def clear_all(self):
        """清空所有解析数据，保留 brokers 等基础配置"""
        conn = get_connection(self.db_path)
        tables_to_clear = [
            "statement_files", "transactions", "dividends",
            "positions", "tax_lots", "lot_consumptions",
            "tax_items", "tax_summaries", "rsu_vests",
            "rsu_grants", "cash_rewards",
        ]
        for table in tables_to_clear:
            conn.execute(f"DELETE FROM {table}")
        print("已清空解析数据")

    # ============================================================
    # 配置检查
    # ============================================================

    def _check_env(self):
        """检查 .env 配置，不存在时自动创建，有占位符时提示用户更新"""
        from src.config import PROJECT_ROOT
        env_file = PROJECT_ROOT / ".env"
        example_file = PROJECT_ROOT / ".env.example"

        if not env_file.exists():
            if example_file.exists():
                env_file.write_text(example_file.read_text())
                print(f"已创建 .env 文件，请编辑并填入你的密码")
                print(f"  文件位置: {env_file}\n")
            else:
                print(f"警告: .env 和 .env.example 均不存在")
                print(f"  请在项目根目录创建 .env 文件\n")
            return

        # 检查是否有未替换的占位符
        content = env_file.read_text()
        needs_update = False
        if "YOUR_PASSWORD_HERE" in content:
            needs_update = True
            print("提示: .env 中存在占位符 YOUR_PASSWORD_HERE，请替换为真实密码")
            if "LONGBRIDGE_PASSWORD" in content:
                print("  - LONGBRIDGE_PASSWORD（长桥月结单密码）")
            if "FUTU_2024_PASSWORD" in content:
                print("  - FUTU_2024_PASSWORD（富途 2024 年月结单密码）")
            print()

    # ============================================================
    # 解密 PDF
    # ============================================================

    def _get_password(self, broker_code: str, pdf_file: Path) -> str | None:
        """根据券商和文件确定密码"""
        if broker_code == "longbridge":
            return LONGBRIDGE_PASSWORD if LONGBRIDGE_PASSWORD else None
        elif broker_code == "futu":
            year_match = re.search(r"(\d{4})", pdf_file.stem)
            year = int(year_match.group(1)) if year_match else 2025
            if year <= 2024:
                return FUTU_2024_PASSWORD if FUTU_2024_PASSWORD else None
            return None
        return None

    def _decrypt_one(self, src: Path, dest: Path, password: str | None) -> bool:
        """解密单个 PDF 到 dest"""
        try:
            if password:
                pdf = pikepdf.open(src, password=password)
            else:
                pdf = pikepdf.open(src)
            pdf.save(str(dest))
            pdf.close()
            return True
        except pikepdf.PasswordError:
            return False
        except Exception:
            return False

    def _decrypt_pdfs(self, input_subdir: Path, broker_code: str, interactive: bool = True):
        """将加密 PDF 解密到 decrypted/ 目录"""
        DECRYPTED_DIR.mkdir(parents=True, exist_ok=True)

        pdf_files = sorted(input_subdir.glob("*.pdf"))
        if not pdf_files:
            return 0, 0, 0

        broker_display = {"futu": "富途", "longbridge": "长桥", "boci": "中银国际"}.get(broker_code, broker_code)
        needs_password = broker_code in ("futu", "longbridge")

        # 先检测是否需要密码但没配置
        missing_password = False
        if needs_password:
            # 尝试用第一个 PDF 检测是否需要密码
            first = pdf_files[0]
            try:
                pw = self._get_password(broker_code, first)
                if pw:
                    pikepdf.open(first, password=pw)
                else:
                    pikepdf.open(first)
            except pikepdf.PasswordError:
                missing_password = True
            except Exception:
                pass  # 文件可能已损坏，不阻塞

        if missing_password:
            if interactive:
                pw = input(f"\n  {broker_display} 月结单需要密码，请输入: ")
                if broker_code == "longbridge":
                    global LONGBRIDGE_PASSWORD
                    import src.database.importers.shared_utils as su
                    su.LONGBRIDGE_PASSWORD = pw
                else:
                    import src.database.importers.shared_utils as su
                    su.FUTU_2024_PASSWORD = pw
            else:
                if broker_code == "longbridge":
                    print(f"  ⚠ {broker_display} 月结单需要 PDF 密码，请在 .env 中添加: LONGBRIDGE_PASSWORD=你的密码")
                else:
                    print(f"  ⚠ {broker_display} 月结单需要 PDF 密码（2024 年及以前），请在 .env 中添加: FUTU_2024_PASSWORD=你的密码")
                return 0, 0, len(pdf_files)

        success = 0
        skipped = 0
        failed = 0

        for pdf_file in pdf_files:
            # 统一加券商前缀到文件名
            dest_name = f"{broker_code}_{pdf_file.name}"
            dest = DECRYPTED_DIR / dest_name
            if dest.exists():
                skipped += 1
                continue

            password = self._get_password(broker_code, pdf_file)
            ok = self._decrypt_one(pdf_file, dest, password)
            if ok:
                success += 1
            else:
                print(f"  解密失败 {pdf_file.name}")
                failed += 1

        return success, skipped, failed

    # ============================================================
    # 重建
    # ============================================================

    def rebuild(
        self,
        broker_code: str | None = None,
        start_month: str | None = None,
        interactive: bool = True,
        skip_position: bool = False,
    ):
        """扫描 input/ 目录，解密并导入所有月结单"""
        if not INPUT_DIR.exists():
            print(f"错误: input 目录不存在: {INPUT_DIR}")
            return

        # 检查 .env 配置
        self._check_env()

        # 发现券商目录
        broker_dirs = []
        for d in sorted(INPUT_DIR.iterdir()):
            if not d.is_dir():
                continue
            name = d.name.lower()
            if name.startswith("futu-"):
                broker_dirs.append(("futu", d))
            elif name.startswith("bridge-"):
                broker_dirs.append(("longbridge", d))
            elif name.startswith("boci-"):
                broker_dirs.append(("boci", d))

        if not broker_dirs:
            print("未发现任何券商月结单目录。")
            print(f"请在 {INPUT_DIR} 下创建如 futu-2025-monthly/ 的目录并放入 PDF 月结单。")
            return

        # 过滤
        if broker_code:
            broker_dirs = [(bc, d) for bc, d in broker_dirs if bc == broker_code]

        # 先统一解密
        print("步骤 1: 解密月结单 PDF")
        print("-" * 40)
        total_decrypted = 0
        total_skipped = 0
        total_failed_dec = 0
        for bc, subdir in broker_dirs:
            ok, skip, fail = self._decrypt_pdfs(subdir, bc, interactive)
            if ok > 0:
                print(f"  {bc}: 解密 {ok} 个 PDF" + (f", 跳过 {skip}" if skip > 0 else "") + (f", 失败 {fail}" if fail else ""))
            total_decrypted += ok
            total_skipped += skip
            total_failed_dec += fail

        if total_failed_dec > 0:
            print(f"\n  ⚠  {total_failed_dec} 个 PDF 解密失败，请检查 .env 中的密码配置")

        if total_decrypted == 0 and total_skipped == 0:
            print("\n没有可导入的月结单。")
            return

        # 再导入
        print(f"\n步骤 2: 导入交易数据")
        print("-" * 40)
        total_imported = 0
        # per-broker stats: {broker_code: {stmt_count, txn_count, div_count, pos_count, option_count}}
        broker_stats = {}
        for bc, subdir in broker_dirs:
            broker_stats[bc] = {"stmt_count": 0, "txn_count": 0, "div_count": 0, "pos_count": 0, "option_count": 0}

        for bc, subdir in broker_dirs:
            importer_cls = self.BROKER_IMPORTERS[bc]
            importer = importer_cls(self.db_path)

            pdf_files = sorted(DECRYPTED_DIR.glob(importer_cls.PDF_GLOB))
            broker_stats[bc]["stmt_count"] = len(pdf_files)
            if not pdf_files:
                continue

            if interactive:
                resp = input(f"\n导入 {bc} 的 {len(pdf_files)} 个月结单? [Y/n] ").strip().lower()
                if resp in ("n", "no"):
                    print(f"  跳过 {bc}")
                    continue

            conn = get_connection(self.db_path)
            before_txn = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
            before_div = conn.execute("SELECT COUNT(*) FROM dividends").fetchone()[0]

            importer.import_all()

            after_txn = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
            after_div = conn.execute("SELECT COUNT(*) FROM dividends").fetchone()[0]

            broker_stats[bc]["txn_count"] = after_txn - before_txn
            broker_stats[bc]["div_count"] = after_div - before_div
            total_imported += after_txn - before_txn

        self._print_broker_summary(broker_stats)

    def _print_broker_summary(self, broker_stats: dict):
        """按券商打印导入汇总表格（显示数据库累计数据）"""
        broker_display = {"futu": "Futu 富途", "longbridge": "长桥", "boci": "BOCI 中银国际"}
        conn = get_connection(self.db_path)

        for bc in broker_stats:
            # Cumulative stats from DB
            bc_txn = conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE broker_code = ?", (bc,)
            ).fetchone()[0]
            bc_opt = conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE broker_code = ? AND action LIKE 'option_%'", (bc,)
            ).fetchone()[0]
            bc_div = conn.execute(
                "SELECT COUNT(*) FROM dividends WHERE broker_code = ?", (bc,)
            ).fetchone()[0]
            bc_pos = conn.execute(
                "SELECT COUNT(*) FROM positions WHERE broker_code = ?", (bc,)
            ).fetchone()[0]
            broker_stats[bc]["txn_count"] = bc_txn
            broker_stats[bc]["option_count"] = bc_opt
            broker_stats[bc]["div_count"] = bc_div
            broker_stats[bc]["pos_count"] = bc_pos

        # Header
        print(f"\n{'=' * 40}")
        print(f"导入完成\n")

        # Table
        headers = ["券商", "月结单数", "交易笔数", "分红", "持仓"]
        col_widths = [15, 10, 22, 8, 6]
        sep = "+" + "+".join("-" * (w + 2) for w in col_widths) + "+"
        header_fmt = "|" + "|".join(f"{{:^{w + 2}}}" for w in col_widths) + "|"

        print(sep)
        print(header_fmt.format(*headers))
        print(sep)

        for bc in ["boci", "longbridge", "futu"]:
            if bc not in broker_stats:
                continue
            s = broker_stats[bc]
            name = broker_display.get(bc, bc)
            if s["option_count"] > 0:
                txn_str = f"{s['txn_count']}（含 {s['option_count']} 期权）"
            elif s["txn_count"] > 0:
                txn_str = str(s["txn_count"])
            else:
                txn_str = "0"
            div_str = f"{s['div_count']} 笔" if s["div_count"] > 0 else "0"
            pos_str = "有" if s["pos_count"] > 0 else "无"

            row_fmt = "|" + "|".join(f" {{:<{w}}} " for w in col_widths) + "|"
            print(row_fmt.format(name, s["stmt_count"], txn_str, div_str, pos_str))

        print(sep)

    def _check_cumulative_sell_buy(self):
        """检查累计买卖平衡"""
        conn = get_connection(self.db_path)
        rows = conn.execute("""
            SELECT symbol,
                   SUM(CASE WHEN action IN ('buy', 'rsu_vest') THEN quantity ELSE 0 END) as total_buy,
                   SUM(CASE WHEN action IN ('sell', 'expire', 'exercise') THEN quantity ELSE 0 END) as total_sell
            FROM transactions
            GROUP BY symbol
            HAVING total_sell > total_buy * 1.01
        """).fetchall()
        return rows
