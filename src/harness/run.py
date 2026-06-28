#!/usr/bin/env python3
"""税务合规 Harness 独立运行脚本

用法:
    python -m src.harness.run              # 运行全部校验
    python -m src.harness.run --year 2025  # 指定年度
    python -m src.harness.run --skip-validation
    python -m src.harness.run --db-path /path/to/tax.db
"""

from __future__ import annotations

import argparse
from pathlib import Path

from src.harness.quality import run_full_harness


def main():
    parser = argparse.ArgumentParser(description="税务合规 Harness 校验")
    parser.add_argument("--year", type=int, default=2025, help="校验年度")
    parser.add_argument("--db-path", type=str, default=None, help="数据库路径")
    parser.add_argument("--skip-validation", action="store_true", help="跳过输入验证")
    parser.add_argument("--skip-reconciliation", action="store_true", help="跳过对账校验")
    parser.add_argument("--skip-verification", action="store_true", help="跳过计算验证")
    args = parser.parse_args()

    db_path = Path(args.db_path) if args.db_path else None

    report = run_full_harness(
        db_path=db_path,
        year=args.year,
        skip_validation=args.skip_validation,
        skip_reconciliation=args.skip_reconciliation,
        skip_verification=args.skip_verification,
    )

    print(report.summary())

    # 返回码: 0 = 全部通过, 1 = 有失败
    raise SystemExit(0 if report.all_passed else 1)


if __name__ == "__main__":
    main()
