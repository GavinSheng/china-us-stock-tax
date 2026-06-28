"""券商月结单导入基类 -- Template Method pattern

子类必须实现:
  - broker_code: 类属性 ("boci", "futu", "longbridge")
  - PDF_GLOB: 类属性 glob 模式 (e.g. "boci_*.pdf")
  - _display_name() -> str: 显示名称
  - _extract_month(pdf_file) -> str: 从文件名提取月份
  - _pdf_password(month_str) -> str | None: PDF 密码
  - _preprocess_text(text) -> str: 文本预处理
  - _import_notes() -> str: statement_files.notes 值
  - _parse_all(full_text, file_id, month_str): 解析所有数据
"""
from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from pathlib import Path

import pdfplumber

from src.database import init_db
from src.database.connection import migrate_transactions_actions, transaction
from src.database.repositories import (
    StatementFileRepository,
    TransactionRepository,
    DividendRepository,
    PositionRepository,
    TaxLotRepository,
)
from .shared_utils import DECRYPTED_DIR


class BaseImporter(ABC):
    broker_code: str
    PDF_GLOB: str

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = db_path
        self.stmt_repo = StatementFileRepository(db_path)
        self.txn_repo = TransactionRepository(db_path)
        self.div_repo = DividendRepository(db_path)
        self.pos_repo = PositionRepository(db_path)
        self.tax_lot_repo = TaxLotRepository(db_path)

    @abstractmethod
    def _display_name(self) -> str: ...

    @abstractmethod
    def _extract_month(self, pdf_file: Path) -> str: ...

    @abstractmethod
    def _pdf_password(self, month_str: str) -> str | None: ...

    @abstractmethod
    def _preprocess_text(self, text: str) -> str: ...

    @abstractmethod
    def _import_notes(self) -> str: ...

    @abstractmethod
    def _parse_all(self, full_text: str, file_id: int, month_str: str) -> None: ...

    # ============================================================
    # Template methods (共享骨架)
    # ============================================================

    def import_all(self):
        if not DECRYPTED_DIR.exists():
            print(f"  解密目录不存在: {DECRYPTED_DIR}")
            return

        pdf_files = sorted(DECRYPTED_DIR.glob(self.PDF_GLOB))
        print(f"\n=== {self._display_name()} ({len(pdf_files)} 个月结单) ===")

        for pdf_file in pdf_files:
            self._import_one(pdf_file)

    def _import_one(self, pdf_file: Path):
        month_str = self._extract_month(pdf_file)
        pdf_password = self._pdf_password(month_str)

        with pdfplumber.open(pdf_file, password=pdf_password) as pdf:
            page_count = len(pdf.pages)
            all_text = []
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    all_text.append(text)
            full_text = "\n".join(all_text)

        full_text = self._preprocess_text(full_text)

        file_hash = hashlib.sha256(pdf_file.read_bytes()).hexdigest()
        existing = self.stmt_repo.get_by_hash(file_hash)
        if existing:
            print(f"  {month_str}: SKIP (already imported, id={existing['id']})")
            return

        # All writes in a single transaction
        with transaction(self.db_path):
            file_id = self.stmt_repo.insert(
                broker_code=self.broker_code,
                file_path=str(pdf_file),
                statement_month=month_str,
                page_count=page_count,
                has_password=pdf_password is not None,
                notes=self._import_notes(),
            )

            return self._import_one_by_id(file_id, pdf_file, month_str, full_text, page_count)

    def _import_one_by_id(
        self,
        file_id: int,
        pdf_file: Path,
        month_str: str,
        full_text: str | None = None,
        page_count: int | None = None,
    ):
        """使用已有的 file_id 解析并导入数据（跳过文件注册步骤）"""
        if full_text is None:
            pdf_password = self._pdf_password(month_str)
            print(f"(解密+提取文本 {page_count or '?'}页)", end="", flush=True)
            with pdfplumber.open(pdf_file, password=pdf_password) as pdf:
                if page_count is None:
                    page_count = len(pdf.pages)
                all_text = []
                for i, page in enumerate(pdf.pages, 1):
                    text = page.extract_text()
                    if text:
                        all_text.append(text)
                    if page_count > 5 and i % 5 == 1:
                        print(f"{i}", end="", flush=True)
                full_text = "\n".join(all_text)
            print(f" ✓", end="", flush=True)
            full_text = self._preprocess_text(full_text)
        elif page_count is None:
            page_count = 0

        # 先清理旧数据
        self.txn_repo.delete_by_statement_file(file_id)
        self.div_repo.delete_by_statement_file(file_id)
        self.pos_repo.delete_by_statement_file(file_id)

        # 委托子类解析
        self._parse_all(full_text, file_id, month_str)

        # 更新状态为 parsed
        self.stmt_repo.update_status(file_id, "parsed")
