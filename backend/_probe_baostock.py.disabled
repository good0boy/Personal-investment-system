"""baostock 黑名单解除探测 + 自动跑批（临时兜底，2026-10-10）。

用法：cd backend && python _probe_baostock.py（后台跑）—— 解除后自动跑批，
跑完打印 RESULT 后退出。黑名单解除后此脚本可删。

背景：baostock 风控拉黑（10001011 黑名单用户）→ 10-08/10-09 行情缺口无法回补。
本脚本每 20 分钟探测一次登录；解除后自动跑 run_paper_day('2026-10-09')
（sync_all 回补行情 → 因子 → 增量撮合逐日补 2 天）。最多探测 48 次（16 小时）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.services import paper_service  # noqa: E402
from app.services.calendar_service import calendar  # noqa: E402
from app.services.data_source.baostock_client import BaostockClient  # noqa: E402

RUN_DATE = "2026-10-09"
MAX_ATTEMPTS = 48
INTERVAL = 20 * 60


def login_ok() -> bool:
    c = None
    try:
        c = BaostockClient()
        df = c.fetch_daily("sh.600519", "2026-10-09", "2026-10-09")
        ok = not df.empty
        print(f"[probe] fetch_daily 行数={len(df)} → {'已解除' if ok else '无数据（仍异常）'}", flush=True)
        return ok
    except Exception as exc:
        print(f"[probe] 未解除：{exc}", flush=True)
        return False
    finally:
        if c is not None:
            try:
                c.logout()
            except Exception:
                pass


for i in range(1, MAX_ATTEMPTS + 1):
    print(f"[probe] 第 {i}/{MAX_ATTEMPTS} 次探测（{time.strftime('%H:%M:%S')}）", flush=True)
    if login_ok():
        print("[probe] baostock 已解除！自动跑批开始", flush=True)
        res = paper_service.run_paper_day(RUN_DATE, trigger="manual", notify=False)
        print(f"[probe] RESULT: {res}", flush=True)
        sys.exit(0)
    if i < MAX_ATTEMPTS:
        time.sleep(INTERVAL)

print("[probe] 16 小时仍未解除 —— 请检查 baostock 账号状态", flush=True)
sys.exit(1)
