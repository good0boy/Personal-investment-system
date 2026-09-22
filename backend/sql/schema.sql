-- ============================================================
-- A股投资本地服务 · 元数据库 schema（SQLite: data/meta/meta.db）
-- 幂等：全部使用 CREATE TABLE IF NOT EXISTS，可重复执行
--
-- 设计原则（见 技术架构.md 5.0 节）：
--   大表时序数据 → Parquet + DuckDB
--   小表/事务/状态 → 本库（SQLite）
-- 日期一律存 TEXT ISO 格式 'YYYY-MM-DD'
-- ============================================================

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- ------------------------------------------------------------
-- 0. 键值元数据（数据新鲜度、初始化标记等）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta_kv (
  key         TEXT PRIMARY KEY,
  value       TEXT,
  updated_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- ------------------------------------------------------------
-- 1. 模块1 · 接口目录 —— 自检状态表
-- ------------------------------------------------------------
-- ★ 职责边界（避免双份真相）：
--     接口【定义】（名称/入参/出参/示例）的唯一来源是
--     `config/catalog/*.yaml`（人工维护）—— 本表不存定义。
--     自检【状态】（ok/broken + 上次验证时间）的唯一来源是本表 —— YAML 不写状态。
--     API 返回 = YAML 定义 + 本表状态 合并。
--
-- 2026-09-17 表结构调整：移除 params_json/returns_json/name/description 等
-- 定义类字段（它们属于 YAML）。旧库通过下方幂等 DROP 重建。
DROP TABLE IF EXISTS catalog_interface;

CREATE TABLE IF NOT EXISTS catalog_interface (
  id              TEXT PRIMARY KEY,          -- 如 quote_history.stock_zh_a_hist
  category        TEXT NOT NULL,             -- fundamental | quote_realtime | quote_history
  func_name       TEXT NOT NULL,
  datasource      TEXT NOT NULL DEFAULT 'akshare',
  status          TEXT NOT NULL DEFAULT 'unchecked', -- ok | broken | missing | unchecked
  status_message  TEXT,
  verified_at     TEXT,                      -- 上次自检时间（本地时间）
  source_version  TEXT,                      -- 自检时的 akshare 版本
  updated_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_catalog_category ON catalog_interface(category);
CREATE INDEX IF NOT EXISTS idx_catalog_status   ON catalog_interface(status);

-- ------------------------------------------------------------
-- 2. 【已移除】数据源能力矩阵表
-- ------------------------------------------------------------
-- 原 `datasource_capability` 表于 2026-09-17 删除。**不要恢复它。**
--
-- 为什么删：
--   能力矩阵是**配置**（人工维护、需要 diff、需要版本管理），
--   唯一来源是 `config/datasource_capabilities/*.yaml`，
--   `/api/v1/datasources` 直接读 YAML（技术架构 6.1.6 节明确如此）。
--   而原表的 `upsert_capability` / `list_capabilities` 访问器
--   **从未被任何代码调用**，表恒为 0 行 ——
--   留着它等于同一份数据有两个来源，日后必然出现
--   "改了 DB 却不知道 YAML 才是生效的那份"（或反过来）。
--
-- 下面这条 DROP 是**幂等清理**，让已存在的旧库也能把这张表去掉；
-- 新建库执行它是空操作。属于「移除式迁移」。
DROP TABLE IF EXISTS datasource_capability;

-- ------------------------------------------------------------
-- 3. 【已移除】因子注册表
-- ------------------------------------------------------------
-- 原 `factor_registry` 表于 2026-09-20（阶段 3 开发时）删除。**不要恢复它。**
--
-- 为什么删：
--   它同时存了两类东西 ——
--     · 【定义】name / cname / category / direction / impl_path / params_json / data_deps
--     · 【状态】enabled / last_calc_at / row_count
--   而定义的唯一来源必须是 `backend/factors/registry.yaml`（人工维护、可 diff、可版本管理）。
--   这与 `datasource_capability` 死表是**同一个病**：同一份数据两个来源，
--   日后必然出现"改了 DB 却不知道 YAML 才是生效的那份"。
--   阶段 3 落地时该表的访问器同样是**零引用**（又一次"写了没被调用"）。
--
-- 状态去哪儿了：
--   `data/factor/_manifest.json`（由 `python -m factors.calc` 写入）
--   —— 与模块 3 回测产物、参数寻优产物同一条纪律：**磁盘产物为唯一真相源**。
--   好处是 CLI 手跑的因子、换机器拷来的因子，前端都能直接看到，不需要"登记"这一步。
--
-- 下面这条 DROP 是**幂等清理**，让已存在的旧库也能把这张表去掉；
-- 新建库执行它是空操作。属于「移除式迁移」。
DROP TABLE IF EXISTS factor_registry;

-- ------------------------------------------------------------
-- 4. 策略注册表
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS strategy_registry (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  impl_path     TEXT,
  version       TEXT,
  params_schema TEXT,                            -- JSON Schema，供参数寻优用
  description   TEXT,
  enabled       INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- ------------------------------------------------------------
-- 5. 模块3 · 回测运行记录（由 strategies/run.py 写入）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS backtest_run (
  run_id        TEXT PRIMARY KEY,                -- 20260917_084233
  strategy      TEXT NOT NULL,
  tag           TEXT,
  start_date    TEXT NOT NULL,
  end_date      TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'running', -- running | success | failed | rejected
  is_out_sample INTEGER NOT NULL DEFAULT 0,      -- 1=样本外（sweep 拒绝该区间）
  metrics_json  TEXT,
  config_json   TEXT,
  param_json    TEXT,
  cost_seconds  REAL,
  trade_count   INTEGER,
  message       TEXT,
  data_version  TEXT,
  code_version  TEXT,
  created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
  finished_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_bt_strategy ON backtest_run(strategy, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_bt_status   ON backtest_run(status);

-- ------------------------------------------------------------
-- 6. 参数寻优
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sweep_run (
  sweep_id       TEXT PRIMARY KEY,
  strategy       TEXT NOT NULL,
  tag            TEXT,
  grid_json      TEXT,
  n_combos       INTEGER,
  n_finished     INTEGER DEFAULT 0,
  best_params_json TEXT,
  sensitivity_json TEXT,                         -- 参数敏感性分析结果
  warning        TEXT,                           -- 如"疑似噪声拟合"
  status         TEXT NOT NULL DEFAULT 'running',
  cost_seconds   REAL,
  start_date     TEXT,
  end_date       TEXT,
  created_at     TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
  finished_at    TEXT
);

-- ------------------------------------------------------------
-- 7. 模块4 · 模拟盘
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS paper_account (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  strategy      TEXT,
  param_json    TEXT,
  init_cash     REAL NOT NULL,
  cash          REAL,
  status        TEXT NOT NULL DEFAULT 'idle',    -- idle | running | paused | error
  start_date    TEXT,
  last_run_date TEXT,
  created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS paper_position (
  account_id    TEXT NOT NULL,
  symbol        TEXT NOT NULL,
  entry_date    TEXT NOT NULL,
  entry_price   REAL NOT NULL,
  shares        INTEGER NOT NULL,
  cost          REAL NOT NULL,
  last_price    REAL,
  market_value  REAL,
  pnl           REAL,
  pnl_pct       REAL,
  hold_days     INTEGER DEFAULT 0,
  updated_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
  PRIMARY KEY (account_id, symbol),
  FOREIGN KEY (account_id) REFERENCES paper_account(id) ON DELETE CASCADE
);

-- 日度净值（唯一键保证 job 幂等：先删后插）
CREATE TABLE IF NOT EXISTS paper_nav (
  account_id    TEXT NOT NULL,
  date          TEXT NOT NULL,
  nav           REAL NOT NULL,
  cash          REAL,
  market_value  REAL,
  position_count INTEGER,
  pnl           REAL,
  pnl_pct       REAL,
  created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
  PRIMARY KEY (account_id, date)
);

-- 卖出后禁买窗口（60 个交易日）
CREATE TABLE IF NOT EXISTS paper_trade_history (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id    TEXT NOT NULL,
  symbol        TEXT NOT NULL,
  action        TEXT NOT NULL,                   -- buy | sell
  date          TEXT NOT NULL,
  price         REAL,
  shares        INTEGER,
  amount        REAL,
  fee           REAL,
  pnl           REAL,
  reason        TEXT,
  created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_pth_symbol ON paper_trade_history(account_id, symbol, date DESC);

-- ------------------------------------------------------------
-- 8. 模块5 · 提醒日志（event_key 唯一 = 幂等去重）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alert_log (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  event_key     TEXT UNIQUE,                     -- date|account|symbol|action
  account_id    TEXT,
  symbol        TEXT,
  action        TEXT,                            -- open_position | close_position | test | job_failed
  level         TEXT NOT NULL DEFAULT 'info',    -- info | warning | error
  title         TEXT,
  content       TEXT,
  channel       TEXT NOT NULL DEFAULT 'webhook',
  status        TEXT NOT NULL DEFAULT 'pending', -- pending | sent | failed | skipped
  retry_count   INTEGER NOT NULL DEFAULT 0,
  error_message TEXT,
  created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
  sent_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_alert_status  ON alert_log(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_alert_created ON alert_log(created_at DESC);

-- ------------------------------------------------------------
-- 9. 调度任务执行日志
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS job_run_log (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id        TEXT NOT NULL,
  run_date      TEXT NOT NULL,                   -- 逻辑运行日（交易日）
  trigger_type  TEXT NOT NULL DEFAULT 'cron',    -- cron | catch_up | manual
  status        TEXT NOT NULL DEFAULT 'running', -- running | success | failed | skipped
  duration      REAL,
  message       TEXT,
  created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
  finished_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_jobid ON job_run_log(job_id, run_date DESC);

-- ------------------------------------------------------------
-- 10. 数据源请求日志（排查限频与失败）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS datasource_log (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  datasource    TEXT NOT NULL,
  func_name     TEXT,
  params        TEXT,
  status        TEXT NOT NULL,                   -- ok | failed | timeout | rate_limited
  rows          INTEGER,
  duration      REAL,
  error_message TEXT,
  created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_dslog ON datasource_log(datasource, created_at DESC);
