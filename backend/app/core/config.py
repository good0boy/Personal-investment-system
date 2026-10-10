"""配置加载：.env（密钥）+ config.yaml（业务配置），合并为一个 Settings 对象。

用法：
    from app.core.config import settings
    settings.data.start_date
    settings.cost.commission_rate
    settings.wecom_webhook_url
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.paths import CONFIG_FILE, DATA_DIR, ENV_FILE


# ============================================================
# 一、.env → 环境密钥
# ============================================================
class EnvSettings(BaseSettings):
    """来自 .env 的敏感/环境相关配置。"""

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: Literal["dev", "prod"] = "dev"
    debug: bool = True
    host: str = "127.0.0.1"
    port: int = 8000
    data_root: str = ""

    # 是否由后端直接托管前端构建产物（frontend/dist）—— 即"单端口跑整站"。
    # 默认 False，两种既有跑法都不受影响：
    #   · 开发模式  → Vite dev server 托管（5173），后端只管 API
    #   · 容器部署  → nginx 托管静态文件 + 反代 /api（见 deploy/nginx.conf）
    # 打开它的场景只有一个：**本地服务模式**（start-service.bat），
    # 一个进程、一个端口就能看到完整 UI，不用再惦记 npm run dev。
    # ⚠️ 刻意做成显式开关而不是"dist 存在就自动挂载" —— 后者会让
    #    跑测试时（仓库里确实有 dist/）突然多出一个 SPA 兜底路由，
    #    属于"配置/行为被隐式改变"，与本项目纪律 6「不搞双份真相」相悖。
    serve_frontend: bool = False

    # 企微提醒（方案 A 群机器人 / 方案 B 自建应用）
    wecom_channel: Literal["webhook", "app"] = "webhook"
    wecom_webhook_url: str = ""
    wecom_corp_id: str = ""
    wecom_agent_id: str = ""
    wecom_corp_secret: str = ""
    wecom_touser: str = ""

    akshare_version_pin: str = ""

    # ---------- 同花顺 fuyao REST（涨跌停 / 板块 / 特色数据）----------
    # ★ 密钥只允许出现在这里（EnvSettings）—— 它来自 .env，且
    #   `DatasourceConfig` 只存"读哪个环境变量"的名字，绝不含真值。
    #   原因：`GET /api/v1/system/config` 会 `model_dump()` 整个 datasource 段，
    #   把 key 放进去就会随接口泄露。对外只能给 `fuyao_key_masked`。
    fuyao_api_key: str = ""

    @property
    def fuyao_configured(self) -> bool:
        """是否已配置 fuyao API Key（不打印 key 本身）。"""
        return bool(self.fuyao_api_key.strip())

    @property
    def fuyao_key_masked(self) -> str:
        """脱敏后的 key，仅用于界面展示。"""
        key = self.fuyao_api_key.strip()
        if not key:
            return ""
        if len(key) <= 8:
            return "*" * len(key)
        return f"{key[:4]}****{key[-4:]}"

    @property
    def wecom_webhook_configured(self) -> bool:
        """是否已配置可用的 Webhook（不打印 URL 本身）。"""
        return self.wecom_webhook_url.startswith("https://qyapi.weixin.qq.com/")

    @property
    def wecom_webhook_key_masked(self) -> str:
        """脱敏后的 key，仅用于界面展示。"""
        url = self.wecom_webhook_url
        if "key=" not in url:
            return ""
        key = url.split("key=", 1)[1]
        if len(key) <= 8:
            return "*" * len(key)
        return f"{key[:4]}****{key[-4:]}"


# ============================================================
# 二、config.yaml → 业务配置
# ============================================================
class AppMeta(BaseModel):
    name: str = "A股投资本地服务"
    version: str = "0.1.0"


class DataConfig(BaseModel):
    start_date: str = "2020-01-01"
    end_date: str = "latest"
    adjust: str = "hfq"
    frequency: str = "d"


class BacktestConfig(BaseModel):
    in_sample_start: str = "2020-06-01"
    in_sample_end: str = "2024-12-31"
    out_sample_start: str = "2025-01-01"
    out_sample_end: str = "2026-09-16"


class BoardsConfig(BaseModel):
    sh_main: list[str] = Field(default_factory=lambda: ["600", "601", "603", "605"])
    sz_main: list[str] = Field(default_factory=lambda: ["000", "001", "002", "003"])
    gem: list[str] = Field(default_factory=lambda: ["300", "301"])


class UniverseConfig(BaseModel):
    boards: BoardsConfig = Field(default_factory=BoardsConfig)
    exclude_prefixes: list[str] = Field(
        default_factory=lambda: ["688", "689", "8", "4", "900", "200"]
    )
    listed_days_min: int = 90      # 新股入池门槛
    no_rebuy_days: int = 60        # 卖出后禁买窗口
    include_delisted: bool = True
    # 退市（数据永久终止）持仓强平的确认窗口（交易日）：
    # 标的无行情且矩阵中已无后续数据，超过该窗口 → 按最后可用收盘价强平
    delist_confirm_days: int = 3


class StampTaxTier(BaseModel):
    from_date: str
    to_date: str
    rate: float


class CostConfig(BaseModel):
    commission_rate: float = 0.00025
    commission_min: float = 5.0
    transfer_fee_rate: float = 0.00001
    slippage_bp: float = 5.0
    stamp_tax: list[StampTaxTier] = Field(default_factory=list)
    lot_size: int = 100


class TimingConfig(BaseModel):
    """闸门 1 · 大盘择时（v1.4 起为**逐日**口径）。

    v1.4 变更：旧版 `ret3_min`（近3日**累计**下限）已被 `daily_min` 取代 ——
    现在要求近 3 个交易日（T、T-1、T-2）**逐日**涨跌幅**均** > `daily_min`。
    """

    index_code: str = "sh.000001"
    daily_min: float = -0.01      # 近3个交易日逐日涨跌幅均须 > 此值
    today_min: float = -0.005     # 当日涨跌幅下限


class BoardEntryConfig(BaseModel):
    """闸门 3 里**按板块**差异化的一档阈值（主板 main / 创业板 gem）。"""

    today_pct_max: float          # 当日涨跌幅上限（开区间，不含）
    ret10_min: float              # 近10日累计涨跌幅下限
    ret10_max: float              # 近10日累计涨跌幅上限


class EntryConfig(BaseModel):
    """闸门 3（个股筛选）+ 闸门 4（量比）+ 闸门 5（近期无涨停）的参数。

    v1.4 变更：闸门 3 的今日上限、近10日区间改为**板块化**（`boards.main` / `boards.gem`）；
    新增闸门 4 的量比阈值 `volume_ratio_min`；`ret3_min` / `ret20_min` 作为**通用**条件保留。
    v1.5 变更：新增闸门 5 —— `no_limit_up_days`（近 N 个交易日，**含当日**，不得涨停/触板；0 = 关闭）。
    """

    today_pct_min: float = 0.01                 # 今日涨跌幅下限（通用）
    ret3_min: float = 0.05                      # 近3日累计 > 此值（通用，保留）
    ret20_min: float = 0.05                     # 近20日累计 > 此值（通用，保留）
    amplitude_window: int = 5                   # 排序键：近 N 日平均振幅
    volume_ratio_min: float = 1.5               # 闸门 4：量比 1 日与 5 日均须 > 此值
    no_limit_up_days: int = 10                  # 闸门 5：近 N 日（含当日）不得涨停/触板；0 = 关闭
    boards: dict[str, BoardEntryConfig] = Field(
        default_factory=lambda: {
            "main": BoardEntryConfig(today_pct_max=0.06, ret10_min=0.03, ret10_max=0.15),
            "gem": BoardEntryConfig(today_pct_max=0.11, ret10_min=0.05, ret10_max=0.20),
        }
    )


class StrategyDefaults(BaseModel):
    init_cash: float = 1_000_000
    max_positions: int = 5
    max_weight_per_stock: float = 0.20
    keep_cash: bool = True
    hold_days_max: int = 10
    # --- v1.4 起止盈为**毛口径**；v1.5 起主板/创业板**取同值 6%** ---
    take_profit_gross_main: float = 0.06
    take_profit_gross_gem: float = 0.06
    index_panic_pct: float = -0.015
    # --- v1.4 新增个股止损；v1.5 收紧到 1.2× 且**两板块同值** ---
    use_stop_loss: bool = True
    stop_loss_amp_window: int = 5
    stop_loss_amp_mult_main: float = 1.2
    stop_loss_amp_mult_gem: float = 1.2
    timing: TimingConfig = Field(default_factory=TimingConfig)
    entry: EntryConfig = Field(default_factory=EntryConfig)
    benchmark: str = "sh.000001"
    # ⚠️ 原 `fill_rule` 字段已删除（v1.6）：它**从未被引擎读取**，是死配置。
    #    成交时点现由引擎硬编码：买入 = T+1 开盘；清仓卖出 = T 日尾盘收盘价
    #    （跌停/停牌顺延 T+1 开盘）。口径会写进 config_snapshot，
    #    见 策略规格说明书.md v1.6 §2.1 / §7.6。


class StrategyDefaultsV2(BaseModel):
    """策略 v2.0「中频质量反转」（`quality_reversal`）的默认参数。

    ★ 独立于 `strategy_defaults`（那是现役 v1.6 `momentum` 的）。
      两个策略**并存**，参数各存一份、互不干扰 —— 改 v2.0 不会动到 v1.6。
      规则唯一依据：`docs/策略规格说明书-v2.0-中频质量反转.md`。

    与 v1.6 的本质差别：买**温和弱势**（反转）而非最强；持有 20~40 个交易日
    （而非 ≤10）；每 `rebalance_every` 个交易日才建仓一次（而非每日）。
    """

    init_cash: float = 1_000_000
    max_positions: int = 10                 # 更分散：逆向选股靠广度
    max_weight_per_stock: float = 0.10      # 10 只 × 10% = 100%
    hold_days_max: int = 30                 # 目标持有期（交易日）
    rebalance_every: int = 5                # 每 N 个交易日才建仓一次（控换手）
    # --- 门 1 · 大盘非急跌（粗择时，比 v1.6 的逐日口径宽松）---
    index_ret20_min: float = -0.10          # 上证近 20 日累计涨跌幅下限
    # --- 门 2 · 反转门（不追高）---
    reversal_pct: float = 0.5               # ret_50 ≤ 截面该分位（0.5 = 不买最强的半数）
    max_ret_20_max: float = 0.15            # 近 20 日最大单日涨幅上限（排除脉冲股）
    # --- 门 3 · 质量门（基本面成长）---
    use_quality_gate: bool = True
    # --- 门 4 · 低波动 / 低换手 ---
    use_lowvol_gate: bool = True
    use_lowturn_gate: bool = True
    # --- 门 0 · 流动性下限 ---
    use_liquidity_gate: bool = True
    min_amount_mean_20: float = 5.0e7       # 近 20 日日均成交额下限（元）
    # v2.3：个股 MA 上行确认（门2c）。0 = 关闭；3/5/10 = 个股 MA_w(t) >= MA_w(t-1) 才可买
    ma_rise_confirm_window: int = 0
    # --- 退出：过度兑现 ---
    realized_ret20_max: float = 0.30        # 近 20 日累计涨幅 ≥ 此值 → 反转已过度兑现，卖出
    # --- 退出：止损 / 止盈（v2.1 新增）-------------------------------------------------
    # ★ 阈值基准 `p` = |个股近 5 日累计涨跌幅| ÷ 5（近 5 日**日均**涨跌幅的绝对值，**不是振幅**）。
    #   p 是个股价、每只不同：一只近 5 日只动 2% 的票 p=0.4%，1×p 止损线仅 −0.4%（日内级）。
    #   0 = **关闭**该规则（网格里的 0 档就是内置消融）。
    stop_loss_p_mult: float = 0.0           # 毛亏 ≥ mult × p → 止损；0 = 关闭
    take_profit_p_mult: float = 0.0         # 毛盈 ≥ mult × p → 止盈；0 = 关闭
    take_profit_abs: float = 0.0            # 毛盈 ≥ 绝对阈值（如 0.06 = 6%）→ 止盈；0 = 关闭
    benchmark: str = "sh.000001"


class AkshareThrottleConfig(BaseModel):
    """akshare 专项节流与熔断参数。

    akshare 的数据源（东财/新浪）对高频访问比 baostock 敏感得多，
    因此单独配置更严格的速率约束。
    """

    min_interval: float = 1.0            # 1 秒最多 1 次
    max_calls_per_minute: int = 30       # 60 秒最多 30 次
    circuit_break_seconds: int = 300     # 触限后熔断时长
    cache_ttl: int = 300                 # 历史类接口缓存
    spot_cache_ttl: int = 10             # 实时快照缓存


class FuyaoThrottleConfig(BaseModel):
    """同花顺 fuyao REST 的节流与连接参数。

    ★ 这里**故意不含 API Key** —— 只记录"去哪个环境变量取"（`api_key_env`）。
      密钥真值在 `EnvSettings.fuyao_api_key`（来自 .env）。这条边界是硬的：
      `GET /api/v1/system/config` 会把本对象整体 `model_dump()` 出去。

    实测（2026-10-01）：`fuyao.aicubes.cn` 本机可达，首页响应 ~0.13s；
    官方声明"不限制累计调用次数"，但会动态限流（HTTP 429 / `code=4001`）。
    """

    api_key_env: str = "FUYAO_API_KEY"          # 仅键名，不是值
    base_url: str = "https://fuyao.aicubes.cn"
    min_interval: float = 0.2                   # 比东财宽松（官方 REST，非爬虫）
    max_calls_per_minute: int = 60
    circuit_break_seconds: int = 120            # 触发限流后静默时长
    cache_ttl: int = 300                        # 历史类接口缓存
    timeout: int = 20


class DatasourceConfig(BaseModel):
    primary: str = "baostock"
    #: baostock 每日 API 请求上限（2026-10-10 主人给定：超 5 万次/天触发 6 小时封禁）。
    #: 客户端在每次 login/数据请求前计数，达到上限当天拒绝再发（次日自动恢复）。
    baostock_daily_api_limit: int = 50_000
    secondary: str = "akshare"
    min_interval: float = 0.12
    max_retries: int = 3
    timeout: int = 30
    try_cache_ttl: int = 300
    akshare: AkshareThrottleConfig = Field(default_factory=AkshareThrottleConfig)
    fuyao: FuyaoThrottleConfig = Field(default_factory=FuyaoThrottleConfig)


class CronSpec(BaseModel):
    hour: int = 15
    minute: int = 35
    # 周频任务用（如 "mon"）；日频任务留空（周几粗筛走 _WEEKDAY_ONLY_JOBS）
    day_of_week: str | None = None


class JobSpec(BaseModel):
    cron: CronSpec = Field(default_factory=CronSpec)
    enabled: bool = False


class JobsConfig(BaseModel):
    paper_daily: JobSpec = Field(default_factory=JobSpec)
    alert_daily_summary: JobSpec = Field(
        default_factory=lambda: JobSpec(cron=CronSpec(hour=8, minute=0))
    )
    catalog_check: JobSpec = Field(default_factory=lambda: JobSpec(cron=CronSpec(hour=9, minute=0)))
    # 基本面/财务周频回补（主人 2026-10-10 拍板）：每周一 21:00
    paper_intraday_check: JobSpec = Field(
        default_factory=lambda: JobSpec(cron=CronSpec(hour=14, minute=45))
    )
    fundamental_weekly: JobSpec = Field(
        default_factory=lambda: JobSpec(cron=CronSpec(day_of_week="mon", hour=21, minute=0))
    )


class SchedulerConfig(BaseModel):
    enabled: bool = True
    timezone: str = "Asia/Shanghai"
    jobs: JobsConfig = Field(default_factory=JobsConfig)
    catch_up_on_startup: bool = True
    catch_up_max_days: int = 30


class PaperConfig(BaseModel):
    default_account_id: str = "default"
    default_account_name: str = "默认模拟账户"
    init_cash: float = 1_000_000
    #: 模拟盘策略（2026-10-09 主人拍板：切换为 quality_reversal 的 oosma10-base 口径，
    #: 原 momentum 仿真下线）。空串 = 不切换（保留账户现状）。
    strategy: str = ""
    #: 随切换一起写入账户行的参数（oosma10-base 口径显式固化，防默认值漂移）。
    params: dict[str, Any] | None = None
    #: 跑批执行模式（2026-10-10 主人拍板）：
    #:   ``full``        每晚从 PAPER_START_DATE 全量重算（旧语义，峰值内存 3.3GB）
    #:   ``incremental`` 单日增量撮合（PaperEngine，峰值 <500MB；state.json 跨日续状态；
    #:                   state 缺失/损坏/不连续 → 自动回退全量重建）
    engine_mode: str = "full"


class DataSyncConfig(BaseModel):
    """本地数据同步（每日跑批 / 启动补跑共用的「把本地数据补到最新」）。

    背景（2026-10-08 审计）：此前就绪探针只有 2 个标的，2026-09-18 一次中断的
    回补留下 ~555 只的部分数据，探针恰好在其中 → 每晚都判「就绪」不回补，
    09-18~09-30 的缺口挂了两周。覆盖率闸门就是防这个的。
    """

    #: 当日有个股 K 线的数量下限（全市场 ~4570 只的 65%）。低于它 = 行情未就绪。
    min_daily_symbols: int = 3000
    #: 行情就绪后是否同步股票池快照（tools.backfill_universe，秒级）
    sync_universe: bool = True
    #: 行情就绪后是否重算因子（factors.calc，约 10~20 分钟）。
    #: ★ 必须在净值重算之前 —— 策略打分读的是 factor/*.parquet，
    #:   行情新了、因子旧了 = 用旧信号跑新行情。
    sync_factors: bool = True
    #: 基本面原始数据陈旧告警阈值（天）。⚠️ 不自动补：backfill_fundamental 全量约 9 小时。
    fundamental_stale_days: int = 10


class AlertConfig(BaseModel):
    enabled: bool = False
    rate_limit_per_minute: int = 20
    retry_backoff: list[int] = Field(default_factory=lambda: [1, 5, 30])
    max_retry: int = 3
    templates_file: str = "config/alert_templates.yaml"


class LoggingConfig(BaseModel):
    level: str = "INFO"
    dir: str = "logs"
    rotation: str = "10 MB"
    retention: str = "30 days"
    console: bool = True


class YamlSettings(BaseModel):
    """对应 config/config.yaml 的完整结构。"""

    app: AppMeta = Field(default_factory=AppMeta)
    data: DataConfig = Field(default_factory=DataConfig)
    backtest: BacktestConfig = Field(default_factory=BacktestConfig)
    universe: UniverseConfig = Field(default_factory=UniverseConfig)
    cost: CostConfig = Field(default_factory=CostConfig)
    strategy_defaults: StrategyDefaults = Field(default_factory=StrategyDefaults)
    # ★ v2.0「中频质量反转」独立配置节（与 strategy_defaults 并存，互不干扰）
    strategy_defaults_v2: StrategyDefaultsV2 = Field(default_factory=StrategyDefaultsV2)
    datasource: DatasourceConfig = Field(default_factory=DatasourceConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    paper: PaperConfig = Field(default_factory=PaperConfig)
    data_sync: DataSyncConfig = Field(default_factory=DataSyncConfig)
    alert: AlertConfig = Field(default_factory=AlertConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


# ============================================================
# 三、合并
# ============================================================
class Settings:
    """合并后的全局配置。懒加载，首次访问时读取文件。"""

    def __init__(self) -> None:
        self._env: EnvSettings | None = None
        self._yaml: YamlSettings | None = None

    # --- env ---
    @property
    def env(self) -> EnvSettings:
        if self._env is None:
            self._env = EnvSettings()
        return self._env

    # --- yaml ---
    @property
    def yaml(self) -> YamlSettings:
        if self._yaml is None:
            self._yaml = self._load_yaml()
        return self._yaml

    def _load_yaml(self) -> YamlSettings:
        if not CONFIG_FILE.exists():
            raise FileNotFoundError(f"配置文件不存在：{CONFIG_FILE}")
        raw: dict[str, Any] = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
        return YamlSettings.model_validate(raw)

    def reload(self) -> None:
        """热重载配置（改完 config.yaml 后调用）。"""
        self._env = None
        self._yaml = None

    # --- 便捷代理 ---
    @property
    def app(self) -> AppMeta:
        return self.yaml.app

    @property
    def data(self) -> DataConfig:
        return self.yaml.data

    @property
    def backtest(self) -> BacktestConfig:
        return self.yaml.backtest

    @property
    def universe(self) -> UniverseConfig:
        return self.yaml.universe

    @property
    def cost(self) -> CostConfig:
        return self.yaml.cost

    @property
    def strategy(self) -> StrategyDefaults:
        return self.yaml.strategy_defaults

    @property
    def strategy_v2(self) -> StrategyDefaultsV2:
        """策略 v2.0「中频质量反转」的默认参数（与 v1.6 并存）。"""
        return self.yaml.strategy_defaults_v2

    @property
    def datasource(self) -> DatasourceConfig:
        return self.yaml.datasource

    @property
    def scheduler(self) -> SchedulerConfig:
        return self.yaml.scheduler

    @property
    def paper(self) -> PaperConfig:
        return self.yaml.paper

    @property
    def data_sync(self) -> DataSyncConfig:
        return self.yaml.data_sync

    @property
    def alert(self) -> AlertConfig:
        return self.yaml.alert

    @property
    def logging(self) -> LoggingConfig:
        return self.yaml.logging

    # --- 组合 ---
    @property
    def is_dev(self) -> bool:
        return self.env.app_env == "dev"

    @property
    def data_dir(self):
        return DATA_DIR


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
