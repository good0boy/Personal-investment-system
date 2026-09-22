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
    index_code: str = "sh.000001"
    ret3_min: float = -0.01
    today_min: float = -0.005


class EntryConfig(BaseModel):
    today_pct_min: float = 0.01
    today_pct_max: float = 0.05
    ret3_min: float = 0.05
    ret10_min: float = 0.05
    ret20_min: float = 0.05
    amplitude_window: int = 5


class StrategyDefaults(BaseModel):
    init_cash: float = 1_000_000
    max_positions: int = 5
    max_weight_per_stock: float = 0.20
    keep_cash: bool = True
    hold_days_max: int = 10
    take_profit_net: float = 0.10
    index_panic_pct: float = -0.015
    use_stop_loss: bool = False
    timing: TimingConfig = Field(default_factory=TimingConfig)
    entry: EntryConfig = Field(default_factory=EntryConfig)
    benchmark: str = "sh.000001"
    fill_rule: str = "next_open"


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


class DatasourceConfig(BaseModel):
    primary: str = "baostock"
    secondary: str = "akshare"
    min_interval: float = 0.12
    max_retries: int = 3
    timeout: int = 30
    try_cache_ttl: int = 300
    akshare: AkshareThrottleConfig = Field(default_factory=AkshareThrottleConfig)


class CronSpec(BaseModel):
    hour: int = 15
    minute: int = 35


class JobSpec(BaseModel):
    cron: CronSpec = Field(default_factory=CronSpec)
    enabled: bool = False


class JobsConfig(BaseModel):
    paper_daily: JobSpec = Field(default_factory=JobSpec)
    alert_daily_summary: JobSpec = Field(
        default_factory=lambda: JobSpec(cron=CronSpec(hour=8, minute=0))
    )
    catalog_check: JobSpec = Field(default_factory=lambda: JobSpec(cron=CronSpec(hour=9, minute=0)))


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
    datasource: DatasourceConfig = Field(default_factory=DatasourceConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    paper: PaperConfig = Field(default_factory=PaperConfig)
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
    def datasource(self) -> DatasourceConfig:
        return self.yaml.datasource

    @property
    def scheduler(self) -> SchedulerConfig:
        return self.yaml.scheduler

    @property
    def paper(self) -> PaperConfig:
        return self.yaml.paper

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
