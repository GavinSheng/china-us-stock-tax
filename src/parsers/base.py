from __future__ import annotations
from abc import ABC, abstractmethod
from pathlib import Path
from src.models import Transaction


class BaseParser(ABC):
    """月结单解析器基类"""

    @abstractmethod
    def parse(self, file_path: str | Path) -> list[Transaction]:
        """解析单个文件，返回交易列表"""
        ...

    def parse_directory(self, dir_path: str | Path) -> list[Transaction]:
        """解析目录下所有文件"""
        dir_p = Path(dir_path)
        all_txns = []
        for f in sorted(dir_p.glob("*.pdf")):
            all_txns.extend(self.parse(f))
        return all_txns
