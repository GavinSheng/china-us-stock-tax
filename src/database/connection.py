"""数据库连接管理和初始化"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from src.database.schema import get_schema_sql


DEFAULT_DB_PATH = Path(__file__).parent.parent.parent / "output" / "tax.db"

# Thread-local storage for shared transaction connections
_local = threading.local()


def _get_active_connection() -> sqlite3.Connection | None:
    """返回当前线程的共享连接（如果存在）"""
    return getattr(_local, "connection", None)


def _set_active_connection(conn: sqlite3.Connection) -> None:
    _local.connection = conn


def _clear_active_connection() -> None:
    _local.connection = None


def get_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    """获取数据库连接"""
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row  # 支持字典式访问
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str | Path | None = None) -> sqlite3.Connection:
    """初始化数据库（创建所有表）"""
    conn = get_connection(db_path)
    schema = get_schema_sql()
    conn.executescript(schema)
    conn.commit()
    return conn


def migrate_transactions_actions(db_path: str | Path | None = None) -> None:
    """迁移 transactions 表的 action CHECK 约束，添加 option_buy/option_sell/option_expire。

    SQLite 不支持 ALTER TABLE MODIFY CONSTRAINT，需 rename → recreate → copy。
    使用 autocommit 模式确保每次操作都立即生效。
    """
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    conn = sqlite3.connect(str(path), isolation_level=None)

    constraint_sql = conn.execute("""
        SELECT sql FROM sqlite_master
        WHERE type='table' AND name='transactions'
    """).fetchone()
    if constraint_sql and "option_expire" in constraint_sql[0]:
        conn.close()
        return  # 已迁移

    cols = [r[1] for r in conn.execute("PRAGMA table_info(transactions)").fetchall()]
    col_list = ", ".join(cols)

    conn.execute("ALTER TABLE transactions RENAME TO transactions_old")
    conn.executescript(get_schema_sql())
    try:
        conn.execute(f"INSERT INTO transactions ({col_list}) SELECT {col_list} FROM transactions_old")
    except Exception as e:
        conn.execute("DROP TABLE IF EXISTS transactions")
        conn.execute("ALTER TABLE transactions_old RENAME TO transactions")
        raise RuntimeError(f"迁移失败，已回退: {e}") from e
    finally:
        conn.execute("DROP TABLE IF EXISTS transactions_old")
        conn.close()


def migrate_rsu_actions(db_path: str | Path | None = None) -> None:
    """迁移 transactions 表的 action CHECK 约束，添加 rsu_vest/rsu_sell。

    SQLite 不支持 ALTER TABLE MODIFY CONSTRAINT，需 rename → recreate → copy。
    使用 autocommit 模式确保每次操作都立即生效。
    """
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    conn = sqlite3.connect(str(path), isolation_level=None)

    constraint_sql = conn.execute("""
        SELECT sql FROM sqlite_master
        WHERE type='table' AND name='transactions'
    """).fetchone()
    if constraint_sql and "rsu_vest" in constraint_sql[0]:
        conn.close()
        return  # 已迁移

    cols = [r[1] for r in conn.execute("PRAGMA table_info(transactions)").fetchall()]
    col_list = ", ".join(cols)

    conn.execute("ALTER TABLE transactions RENAME TO transactions_old")
    conn.executescript(get_schema_sql())
    try:
        conn.execute(f"INSERT INTO transactions ({col_list}) SELECT {col_list} FROM transactions_old")
    except Exception as e:
        conn.execute("DROP TABLE IF EXISTS transactions")
        conn.execute("ALTER TABLE transactions_old RENAME TO transactions")
        raise RuntimeError(f"迁移失败，已回退: {e}") from e
    finally:
        conn.execute("DROP TABLE IF EXISTS transactions_old")
        conn.close()


@contextmanager
def get_db(db_path: str | Path | None = None) -> Generator[sqlite3.Connection, None, None]:
    """上下文管理器：自动提交/回滚"""
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def transaction(db_path: str | Path | None = None) -> Generator[sqlite3.Connection, None, None]:
    """上下文管理器：共享连接 + 显式事务。

    所有在 with 块内的 repo 调用会自动使用同一连接和同一事务。
    使用 isolation_level='' (deferred) 而非 None (autocommit)，
    使 BEGIN/COMMIT/ROLLBACK 生效。
    """
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path), isolation_level="")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    try:
        conn.execute("BEGIN")
        _set_active_connection(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _clear_active_connection()
        conn.close()


def migrate_add_dividend_refund_columns(db_path: str | Path | None = None) -> None:
    """为 dividends 表添加 withholding_refund 和 withholding_refund_cny 列。

    使用 ALTER TABLE ADD COLUMN（SQLite 3.35+ 支持），可安全重复运行。
    回退方案：如果 ADD COLUMN 失败，使用 rename → recreate → copy。
    """
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    conn = sqlite3.connect(str(path), isolation_level=None)

    # 检查列是否已存在
    cols = {r[1] for r in conn.execute("PRAGMA table_info(dividends)").fetchall()}
    if "withholding_refund" in cols and "withholding_refund_cny" in cols:
        conn.close()
        return  # 已迁移

    try:
        if "withholding_refund" not in cols:
            conn.execute("ALTER TABLE dividends ADD COLUMN withholding_refund DECIMAL(18, 2) DEFAULT 0")
        if "withholding_refund_cny" not in cols:
            conn.execute("ALTER TABLE dividends ADD COLUMN withholding_refund_cny DECIMAL(18, 2) DEFAULT 0")
    except Exception as e:
        # 回退方案：rename → recreate → copy
        conn.close()
        conn = sqlite3.connect(str(path), isolation_level=None)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(dividends)").fetchall()]
        col_list = ", ".join(cols)

        conn.execute("ALTER TABLE dividends RENAME TO dividends_old")
        conn.executescript(get_schema_sql())
        try:
            conn.execute(f"INSERT INTO dividends ({col_list}) SELECT {col_list} FROM dividends_old")
        except Exception as e2:
            conn.execute("DROP TABLE IF EXISTS dividends")
            conn.execute("ALTER TABLE dividends_old RENAME TO dividends")
            raise RuntimeError(f"迁移失败，已回退: {e2}") from e2
        finally:
            conn.execute("DROP TABLE IF EXISTS dividends_old")
    finally:
        conn.close()


def migrate_add_indexes(db_path: str | Path | None = None) -> None:
    """为已有数据库添加缺失的性能索引。可安全重复运行。"""
    conn = get_connection(db_path)
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_stmt_file_hash ON statement_files(file_hash)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_txn_statement_file ON transactions(statement_file_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_div_statement_file ON dividends(statement_file_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pos_statement_file ON positions(statement_file_id)")
        conn.commit()
    finally:
        conn.close()
