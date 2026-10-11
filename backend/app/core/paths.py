"""统一路径常量。

纪律：项目中任何地方都不得硬编码路径，一律从这里取。
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path


def _find_backend_dir() -> Path:
    """定位 **backend 目录本身**（代码/config/logs 的根）。

    ★ 为什么单独解析、不能全靠 PROJECT_ROOT（2026-10-11 部署阻塞修复）：
      Docker 里 `COPY backend /app` → 代码在 `/app/app/core/paths.py`，
      **没有 `/app/backend/` 子目录，也没有 .gitignore**（.dockerignore 排除了
      `.git/`，根 `.gitignore` 也不在 `/app` 下）。旧实现只认
      「含 backend/ + .gitignore 的父目录」，容器里必然 fallback 到 `parents[3]`
      = `/` → `CONFIG_FILE = /backend/config/config.yaml`（不存在）→
      `config.py` 直接 FileNotFoundError，**容器启动即崩**。
      现在按「有 config/config.yaml 且有 app/ 子目录」认 backend 根，
      两种布局都能命中；`BACKEND_ROOT` 环境变量是最后的逃生门。
    """
    env = os.getenv("BACKEND_ROOT", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "config" / "config.yaml").exists() and (parent / "app").is_dir():
            return parent
    # 兜底：core -> app -> backend -> root
    return here.parents[3]


def _find_project_root() -> Path:
    """项目根（本地 = 仓库根；容器 = backend 目录的上级）。

    判据沿用「含 backend/ + .gitignore 的目录」；找不到时退化为 backend 的上级
    —— 容器布局下 backend 目录本身就是挂载点（/app），其上级 / 只是占位，
    真正用得到的是 `BACKEND_DIR`/`CONFIG_DIR`/`LOGS_DIR`（都在 backend 下解析）。
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "backend").is_dir() and (parent / ".gitignore").exists():
            return parent
    return _find_backend_dir().parent


PROJECT_ROOT: Path = _find_project_root()

# ---------- 后端 ----------
BACKEND_DIR: Path = _find_backend_dir()
APP_DIR: Path = BACKEND_DIR / "app"
CONFIG_DIR: Path = BACKEND_DIR / "config"
SQL_DIR: Path = BACKEND_DIR / "sql"
TOOLS_DIR: Path = BACKEND_DIR / "tools"
TESTS_DIR: Path = BACKEND_DIR / "tests"
LOGS_DIR: Path = BACKEND_DIR / "logs"

# ---------- 配置与密钥 ----------
CONFIG_FILE: Path = CONFIG_DIR / "config.yaml"
ENV_FILE: Path = PROJECT_ROOT / ".env"
ENV_EXAMPLE_FILE: Path = PROJECT_ROOT / ".env.example"

# ---------- 模块 1 · 接口目录（YAML 是唯一人工维护入口）----------
CATALOG_DIR: Path = CONFIG_DIR / "catalog"
#: gen_catalog.py 的原始扫描结果（生成物，不可手改）
CATALOG_RAW_DUMP: Path = CATALOG_DIR / "_raw_dump.yaml"
#: 人工精选的三大类接口定义
CATALOG_CATEGORY_FILES: dict[str, Path] = {
    "fundamental": CATALOG_DIR / "fundamental.yaml",
    "quote_realtime": CATALOG_DIR / "quote_realtime.yaml",
    "quote_history": CATALOG_DIR / "quote_history.yaml",
}

#: 数据源能力矩阵（YAML 是唯一来源，刻意不进数据库）
DATASOURCE_CAPABILITY_DIR: Path = CONFIG_DIR / "datasource_capabilities"
#: 能力项清单（中文名与说明），下划线开头表示它是元数据而非数据源
CAPABILITY_META_FILE: Path = DATASOURCE_CAPABILITY_DIR / "_capabilities.yaml"

# ---------- 数据根目录（可被 .env 的 DATA_ROOT 覆盖）----------
def _resolve_data_root() -> Path:
    override = os.getenv("DATA_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return PROJECT_ROOT / "data"


DATA_DIR: Path = _resolve_data_root()

# 数据子目录
RAW_DIR: Path = DATA_DIR / "raw"
QUOTE_DAILY_DIR: Path = RAW_DIR / "quote_daily"
QUOTE_INDEX_DIR: Path = RAW_DIR / "quote_index"
UNIVERSE_DIR: Path = RAW_DIR / "universe"
#: 基本面/财务数据（tools/backfill_fundamental.py 落盘）
#: 结构：逐只类 {dataset}/{symbol}.parquet；全市场批量类 {dataset}/all.parquet
FUNDAMENTAL_DIR: Path = RAW_DIR / "fundamental"

# ---------- 同花顺特色数据（fuyao REST，tools/backfill_special.py 落盘）----------
# ★ 为什么这么切目录：local_data_service 的行数统计**刻意不加 union_by_name**
#   （加了会从 0.9s 退化到 9.4s）→ **同一个 glob 内的文件必须 schema 完全一致**。
#   所以凡是列集不同的数据集一律分目录/分文件，绝不混在一个 glob 里。
#: 涨跌停三池：up/down/break **各一个文件**（三池字段数 14/9/9 不同）。
#: ⚠️ 严禁用 `limit_pool/*.parquet` 当统计 glob —— DuckDB 会直接报 schema 不一致。
LIMIT_POOL_DIR: Path = RAW_DIR / "limit_pool"
#: 板块指数历史：board/{industry,concept}/{thscode}.parquet（文件名含 `.TI`）
BOARD_DIR: Path = RAW_DIR / "board"
#: 板块清单快照（tag/thscode/name/snapshot_date）
BOARD_CATALOG_DIR: Path = BOARD_DIR / "catalog"
#: 热榜：history/ 与 current/ 按自然日（两者字段集不同）、rank_trend/ 按标的
HOT_LIST_DIR: Path = RAW_DIR / "hot_list"
#: 龙虎榜：stock/（stock_items，14 列）与 hot_money/（游资×个股，18 列）→ 必须两张表
DRAGON_TIGER_DIR: Path = RAW_DIR / "dragon_tiger"
#: 个股异动（接口只有当日 → 只能从今天开始按日累积）
ANOMALY_DIR: Path = RAW_DIR / "anomaly"

FACTOR_DIR: Path = DATA_DIR / "factor"
BACKTEST_DIR: Path = DATA_DIR / "backtest"
SWEEP_DIR: Path = DATA_DIR / "sweep"
PAPER_DIR: Path = DATA_DIR / "paper"
META_DIR: Path = DATA_DIR / "meta"
CACHE_DIR: Path = DATA_DIR / "cache"

META_DB: Path = META_DIR / "meta.db"
TRADE_CALENDAR_PARQUET: Path = META_DIR / "trade_calendar.parquet"
#: 全市场日线回补的断点续传状态（阶段 2 · tools/backfill_quotes.py）
BACKFILL_STATE_FILE: Path = META_DIR / "backfill_state.json"

#: baostock 每日 API 请求计数（风控保护：超 5 万次/天会触发 6 小时封禁）
BAOSTOCK_QUOTA_FILE: Path = META_DIR / "baostock_quota.json"
#: 基本面数据回补的断点续传状态（tools/backfill_fundamental.py）
BACKFILL_FUND_STATE_FILE: Path = META_DIR / "backfill_fundamental_state.json"
#: 同花顺特色数据回补的断点续传状态（tools/backfill_special.py）
BACKFILL_SPECIAL_STATE_FILE: Path = META_DIR / "backfill_special_state.json"

# ---------- 策略与回测（阶段 2）----------
#: 策略代码目录。刻意放在 backend/ 下（而非仓库根）：它要 `from app.*` 导入，
#: 放在 backend/ 里 `python -m strategies.run` 才开箱可用，无需改 sys.path。
STRATEGIES_DIR: Path = BACKEND_DIR / "strategies"
STRATEGY_IMPL_DIR: Path = STRATEGIES_DIR / "impl"
#: 参数寻优网格定义（YAML 是唯一人工入口）
SWEEPS_DIR: Path = BACKEND_DIR / "sweeps"

# ---------- 模块 2 · 因子库（阶段 3）----------
#: 因子代码目录。同 `strategies/` 的理由放在 backend/ 下
#: —— `python -m factors.calc` 要 `from app.*` 导入，放这里开箱可用。
FACTORS_DIR: Path = BACKEND_DIR / "factors"
FACTOR_IMPL_DIR: Path = FACTORS_DIR / "impl"
#: ★ 因子注册表（**定义的唯一来源**，人工维护）。
#:   计算【状态】不写在这里，也不写 DB —— 见 FACTOR_MANIFEST_FILE 的说明。
FACTOR_REGISTRY_FILE: Path = FACTORS_DIR / "registry.yaml"
#: ★ 因子产物清单（由 `factors/calc.py` 写入）。
#:   为什么用文件而不是 DB 表：与模块 3 / 参数寻优同一条纪律 ——
#:   「磁盘产物为唯一真相源」，CLI 手跑的结果、换机器拷来的结果都能被前端直接看到，
#:   不需要"登记"这一步，也不存在 DB 与文件不一致的问题。
#:   （原 `factor_registry` 表已于 2026-09-20 删除，见 sql/schema.sql 第 3 节。）
FACTOR_MANIFEST_FILE: Path = FACTOR_DIR / "_manifest.json"
#: 因子有效性分析产物（分层回测 + IC），离线算、前端只读
FACTOR_ANALYSIS_DIR: Path = FACTOR_DIR / "analysis"

# ---------- 模块 5 · 用户提醒（阶段 5）----------
#: 提醒消息模板（YAML 是唯一人工入口）。
#: ★ 路径写死在这里，不用 `config.yaml` 的 `alert.templates_file` 去拼 ——
#:   本项目头号静默失效就是「配置写了没被读」。该配置项保留仅作**校验**，
#:   真值一律走本常量（见 `alert_service._templates_path()`）。
ALERT_TEMPLATES_FILE: Path = CONFIG_DIR / "alert_templates.yaml"

# ---------- 前端 ----------
FRONTEND_DIR: Path = PROJECT_ROOT / "frontend"

# ---------- 部署 ----------
DEPLOY_DIR: Path = PROJECT_ROOT / "deploy"

#: 需要在启动时确保存在的目录
ALL_DIRS: tuple[Path, ...] = (
    DATA_DIR, RAW_DIR, QUOTE_DAILY_DIR, QUOTE_INDEX_DIR, UNIVERSE_DIR,
    FUNDAMENTAL_DIR,
    LIMIT_POOL_DIR, BOARD_DIR, BOARD_CATALOG_DIR, HOT_LIST_DIR,
    DRAGON_TIGER_DIR, ANOMALY_DIR,
    FACTOR_DIR, FACTOR_ANALYSIS_DIR, BACKTEST_DIR, SWEEP_DIR, PAPER_DIR, META_DIR, CACHE_DIR,
    LOGS_DIR,
)


def ensure_dirs() -> None:
    """创建所有必需目录（幂等）。"""
    for d in ALL_DIRS:
        d.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1024)
def relative_to_project(path: Path | str) -> str:
    """把绝对路径转成相对项目根的展示路径，用于日志与 UI。

    ★ 入参**同时接受 `Path` 与 `str`**，别把签名收成 `Path` 一种。
    产物清单（`summary` / `run.as_dict()`）里 `root` 是 `str`（要能 JSON 序列化、
    要能跨进程传），而内部计算拿到的是 `Path` —— 两种都会流到这里。
    收窄成 `Path` 就会在"用产物清单打印结果目录"这类地方抛
    `AttributeError: 'str' object has no attribute 'resolve'`，
    而且是在**算完、落盘完之后**才炸，退出码 1 但结果其实完好。
    """
    p = Path(path)
    try:
        return str(p.resolve().relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        return str(p)
