"""初始化 meta.db（建表）。

用法：
    cd backend
    python -m tools.init_db
    python -m tools.init_db --reset      # ⚠️ 危险：删除现有 meta.db 后重建

幂等：可重复执行，不会丢数据（除非显式 --reset）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 允许直接 python tools/init_db.py 运行
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.logging import logger, setup_logging
from app.core.paths import META_DB, ensure_dirs
from app.repos import db


def main() -> int:
    parser = argparse.ArgumentParser(description="初始化 meta.db")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="⚠️ 删除现有 meta.db 后重建（会丢元数据，不含 Parquet 行情数据）",
    )
    args = parser.parse_args()

    setup_logging()
    ensure_dirs()

    if args.reset and META_DB.exists():
        backup = META_DB.with_suffix(".db.bak")
        META_DB.replace(backup)
        logger.warning(f"已把旧 meta.db 重命名为 {backup.name}（未删除，可手工恢复）")

    logger.info(f"初始化元数据库：{META_DB}")
    info = db.init_schema()

    print()
    print("=" * 62)
    print("  meta.db 初始化完成")
    print("=" * 62)
    print(f"  路径      : {info['db_path']}")
    print(f"  表数量    : {info['table_count']}")
    print()
    print("  表清单：")
    for t in info["tables"]:
        cnt = db.query_one(f"SELECT COUNT(*) AS c FROM {t}")
        n = cnt["c"] if cnt else 0
        print(f"    - {t:<28} {n:>8} 行")
    print()
    print("  下一步：python -m tools.sync_trade_calendar")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
