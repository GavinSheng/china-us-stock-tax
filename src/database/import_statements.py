"""券商月结单解析并导入数据库 (向后兼容 shim)

所有券商 importer 已迁移到 src.database.importers.*
本文件仅做 re-export 以保持向后兼容。
"""
from __future__ import annotations

from pathlib import Path

from src.database import init_db
from src.database.connection import migrate_transactions_actions, migrate_add_indexes
from src.database.repositories import (
    RSUGrantRepository,
    RSUVestRepository,
)

# ============================================================
# Re-export broker importers
# ============================================================

from src.database.importers import (
    BOCIImporter,
    FutuImporter,
    LongbridgeImporter,
)

# ============================================================
# Re-export shared utilities (供测试和其他模块使用)
# ============================================================

from src.database.importers.shared_utils import (
    LONGBRIDGE_PASSWORD,
    FUTU_2024_PASSWORD,
    INPUT_DIR,
    DECRYPTED_DIR,
    DEFAULT_CURRENCY,
    SYMBOL_NAME_MAP,
    SYMBOL_EXCHANGE,
    _futu_div_rate,
    _normalize_option_underlying,
    _infer_exchange,
    _clean_text,
    _normalize_strike,
    _clean_num,
    _dec,
    _parse_date,
    _normalize_lb_symbol,
    _company_name,
    _dedup_text,
)

# ============================================================
# RSU 数据导入（保留在此，不是月结单解析器）
# ============================================================


class RSUImporter:
    """RSU 归属数据导入（已知数据）

    注意：此类仅包含示例数据结构，实际使用时请替换为真实的 RSU 归属记录。
    """

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = db_path

    def import_all(self):
        init_db(self.db_path)
        grant_repo = RSUGrantRepository(self.db_path)
        vest_repo = RSUVestRepository(self.db_path)

        print("\n=== RSU 归属数据 ===")
        print("提示: 请在 import_statements.py 中配置真实的 RSU 归属记录")

        # ============================================================
        # 示例数据结构（请替换为真实数据）
        # ============================================================
        #
        # grant_repo.upsert(
        #     grant_number="XX-RXXXXX", symbol="XXXX", total_shares=1000,
        #     company_name="公司名称", currency="USD", market="US",
        #     notes="RSU 说明",
        # )
        # vests = [
        #     ("grant_number", "YYYY-MM-DD", quantity, fmv_per_share,
        #      taxable_income, tax_amount, "cash/sell_to_cover",
        #      deposit_date, shares_deposited),
        # ]
        # for grant_num, vest_date, qty, fmv, income, tax, method, deposit_date, deposited in vests:
        #     vest_repo.insert(
        #         grant_number=grant_num, vest_date=vest_date, symbol="XXXX",
        #         company_name="公司名称", vested_quantity=qty,
        #         fmv_per_share=fmv, taxable_income=income, tax_amount=tax,
        #         tax_method=method, deposit_date=deposit_date,
        #         shares_deposited=deposited, currency="USD",
        #         custody_broker="券商代码", tax_paid=True, tax_paid_date=vest_date,
        #     )

        print("未配置 RSU 数据，跳过导入。")
