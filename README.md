# A股投资本地服务

单机自用的 A 股投资研究系统。本地开发运行，迭代完善后再部署到内网服务器。

**当前进度：阶段 0~5 全部完成，P0/P1 优化项已落地** ✅（700 项测试全绿）

| 阶段 | 内容 | 状态 |
|---|---|---|
| 阶段 0 | 底座：脚手架、配置、数据源抽象层、存储层、交易日历、股票池、调度、前端骨架 | ✅ 已完成 |
| 阶段 1 | 模块 1 · 数据分类（接口目录 + 在线试跑） | ✅ 已完成 |
| 阶段 2 | 模块 3 · 策略回测（含参数寻优） | ✅ 已完成 |
| 阶段 3 | 模块 2 · 因子库（**4 类 / 19 个因子**：日度 11 · 合成 2 · 基本面 6） | ✅ 已完成 |
| 阶段 4 | 模块 4 · 模拟仿真 | ✅ 已完成 |
| 阶段 5 | 模块 5 · 用户提醒（企业微信） | ✅ 已完成 |
| 优化项 | 换机迁移加固 · 基本面因子启用 · 因子合成 · 股票池快照与基准对齐 · 补跑不阻塞端口 · 限流熔断可见 | ✅ 已完成 |

> 🔴 **重要结论：v1.2 策略被证实无效；v1.3 剔除了极端组，止住了灾难性亏损但**仍无正 alpha**。**
>
> | 策略版本 | 样本内年化 | 样本外年化 | 同期全市场等权基准 |
> |---|---|---|---|
> | v1.2（冻结原版） | **-49.54%** | **-33.49%** | +3.52% / +18.26% |
> | **v1.3（现役）** | **-3.93%** | **+7.42%** | 同上 —— 两期仍分别跑输 7.5pp / 10.8pp |
>
> **根因是 alpha 在「隔夜」漏光**（入场跳空 −1.8%，即 A 股短期反转），**不是调参问题**；
> 因子侧独立复现：G1~G4 毛年化齐平 +18%~+23%，**只有极端动量组 G5 单独崩坏 −8.9%**
> → v1.3 据此剔除 G5（`exclude_top_pct=0.2`）。
>
> ⚠️ **v1.3 的样本外数字方法学上不干净**（定规则时已看过含样本外段的因子研究），
> 只说明方向一致，不构成严格的样本外验证。详见 `docs/回测与策略研究.md` §3.3。
> **改策略前必须先改 `策略规格说明书.md` 并升版本**（现为 v1.3）。

**配套文档**

- `技术架构.md` — 技术选型、数据层设计、模块设计、实施路线图、风险清单
- `docs/技术架构-详细设计.md` — 第 5/6/7/10 章与附录的详细展开
- `docs/开发进度.md` — 每个阶段落地了什么、踩了什么坑、**问题集与纪律**
- `策略规格说明书.md` — 策略规则的唯一依据（v1.2 已冻结）

---

## 技术栈

| 层 | 选型 |
|---|---|
| 后端 | Python 3.13 + FastAPI + Uvicorn + Pydantic v2 |
| 数据源 | **baostock**（行情主源）+ **akshare**（广度补充） |
| 存储 | Parquet（大表）+ DuckDB（查询）+ SQLite `meta.db`（元数据）+ YAML（配置） |
| 调度 | APScheduler（进程内） |
| 前端 | Vue 3 + Vite + TypeScript + Element Plus + ECharts |
| 部署 | Docker Compose + Nginx（本地阶段不用） |

---

## 快速开始

### 前置要求

- Python 3.11 ~ 3.13
- Node.js ≥ 20
- 无需安装任何数据库（Parquet / SQLite 都是文件）

### 第 0 步：换机器 / 换克隆后先跑一次 `setup.bat`

**只要一次。** 双击 `setup.bat`，它会：

1. 在 `backend\.venv` 建一个**项目专属**虚拟环境（不污染系统 Python，也不依赖任何用户目录）
2. 安装 `backend\requirements.txt` 全部依赖（约 2~5 分钟，需要网络）
3. 前端 `node_modules` 缺失时执行 `npm install`
4. 校验关键依赖可导入，失败会明确指出原因

> **脚本不含任何硬编码绝对路径。** 所有 `.bat` 都用 `%~dp0` 定位自身目录，
> 由 `_env.bat` 探测解释器（**`backend\.venv` → `%PYTHON%` 环境变量 → PATH 上的 `python`**）。
> 换盘符、换用户名、换机器、上服务器都不用改脚本。
>
> 想用自己已有的 Python 环境？设 `set PYTHON=D:\path\to\python.exe` 再跑 `setup.bat` 即可，
> 或直接让 `.venv` 指向别处 —— 脚本按上面三条优先级自动选。

### 1. 启动服务（两种模式，选一个）

#### 方式一：脚本一键启动（推荐）

| 脚本 | 用途 | 行为 |
|---|---|---|
| `start-dev.bat` | **开发模式** | 开两个窗口：后端 `uvicorn --reload` @ `127.0.0.1:8000` + 前端 `vite dev` @ `127.0.0.1:5173` |
| `start-service.bat` | **服务模式** | 只起后端、**不带 `--reload`**，且同时托管 `frontend\dist` —— **一个进程一个端口跑整站**（`http://127.0.0.1:8000`）。调度器与启动补跑只有在这个模式下才可靠工作 |

> 这两个脚本都会先做**前置检查**：找不到 Python、解释器缺依赖、`node_modules` 缺失，
> 都会在启动前给出可执行的修复提示，而不是让窗口一闪而过。
>
> ✅ **启动后端口立刻可用**（2026-09-22 起）。启动补跑跑在**后台线程**里，
> 不再挡住 uvicorn 监听 —— 实测 **0.1 秒**内 `/api/health` 就能响应。
> 补跑本身照跑（本地阶段关机必漏跑，这是刚需），日志会打印
> `[paper_daily] 检测到 N 个缺失交易日 … 开始补跑`，一次增量回补约 **30~90 分钟**。
> **补跑期间的进度**可以在前端「系统状态」页看到（会显示"启动补跑进行中"横幅），
> 也可以直接打 `GET /api/v1/system/jobs` 看 `scheduler.catch_up`。
>
> ⚠️ 补跑期间**数据可能不完整**（行情还在往回补），所以各页面的指标会随后变全 ——
> 这不是算错了。想看"稳定态"，等日志出现 `启动补跑结束`。
>
> ⚠️ **服务模式要单端口跑整站，需先 `npm run build`** 生成 `frontend\dist`
> （`start-service.bat` 会自动置 `SERVE_FRONTEND=1`；没有 dist 时后端只打一条警告，只提供 API）。

#### 方式二：手工分窗口启动

```bash
# ---- 后端（工作目录必须是 backend/）----
cd backend
.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

# ---- 前端（工作目录 frontend/）----
cd frontend
npm run dev
```

> `.venv\Scripts\python.exe` 是 `setup.bat` 建的项目虚拟环境，**用相对路径就能引用**。
> 换成别的解释器也行，只要它装齐了 `backend\requirements.txt`。

**访问地址**

| 入口 | 地址 |
|---|---|
| 前端页面（开发模式） | http://127.0.0.1:5173 |
| 前端页面（服务模式，需先 build） | http://127.0.0.1:8000 |
| API 文档（Swagger） | http://127.0.0.1:8000/docs |
| 健康检查 | http://127.0.0.1:8000/api/health |

> ⚠️ **`--reload` 对新增文件不可靠** —— 新增模块后直接重启进程，
> 别指望热加载。杀进程用 `taskkill /F /T /PID <pid>`。

### 2. 首次初始化（新环境才需要）

> 下面的 `python` 一律指**项目虚拟环境里的解释器**。两种等价写法，任选：
> 先激活 —— `backend\.venv\Scripts\activate`（激活后 `python` 就是它）；
> 或直接用全路径 —— `backend\.venv\Scripts\python.exe`。

```bash
cd backend

# 建表（14 张，幂等）
python -m tools.init_db

# 同步交易日历（约 1 秒）
python -m tools.sync_trade_calendar

# 全市场行情回补（4792 只 × 单只一请求，约 30~50 分钟）
python -m tools.backfill_quotes
```

### 3. 配置环境变量

```bash
cp .env.example .env   # 然后填入真实值
```

必填项只有一个 —— 企业微信群机器人 Webhook：

```ini
WECOM_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx
```

> 群里加个机器人（群设置 → 群机器人 → 添加）就能拿到这个 URL，5 分钟搞定。
> ⚠️ 若开了「自定义关键词」安全设置，**关键词必须填「持仓提醒」** ——
> 本项目所有消息都带它，不匹配会被企微**静默丢弃**（接口返回 `errcode=0` 但收不到）。

### 4. 验证链路

打开前端 → 侧边栏「用户提醒」→ 点「发送测试消息」。手机/群里收到即链路打通。

---

## 定时任务时刻表

| 任务 | 时刻 | 说明 |
|---|---|---|
| 每日持仓早报 | **每日 08:00** | 「今日持仓 + 仓位」。读已有产物，**周末也发** |
| 模拟盘跑批 | **周一至周五 23:00** | 全量重算净值 + 当日成交提醒（开仓/清仓） |
| 接口目录自检 | 周一至周五 09:00 | 校验接口可用性 |
| 任务失败告警 | 即时 | 任一任务记 failed 时推企微 |

**为什么跑批是 23:00**：baostock 日线一般 17:30~19:00 才更新完，23:00 回补（约 30 分钟）+ 重算（约 45s）当天 23:35 前后净值就绪。数据源若延迟 → 就绪校验拦截记 failed → **次日启动补跑兜底**。

配置在 `backend/config/config.yaml` 的 `scheduler.jobs`，改完重启后端生效。

---

## 常用命令

```bash
# ---------- 数据与元数据库（cd backend）----------
python -m tools.init_db                  # 建表
python -m tools.init_db --reset          # ⚠️ 重建（旧库会改名备份，不删除）
python -m tools.sync_trade_calendar      # 同步交易日历
python -m tools.sync_trade_calendar --source akshare   # 用备用源
python -m tools.backfill_quotes          # 全市场行情回补（约 30~50 分钟）
python -m tools.backfill_quotes --incremental          # 增量补当日

# ---------- 服务（cd backend）----------
python -m app.main                       # 开发模式
uvicorn app.main:app --host 127.0.0.1 --port 8000      # 服务模式（无 reload）

# ---------- 回测与寻优（CLI only，前端不提供入口）----------
python -m strategies.run --strategy momentum           # 跑一次回测
python -m strategies.sweep --strategy momentum         # 参数寻优

# ---------- 因子库（CLI only）—— 4 个分类 / 19 个因子 ----------
python -m factors.calc --list                          # 列注册表 + 已有产物
python -m factors.calc --check                         # 自检（导入/签名/冒烟）
python -m factors.calc --category fundamental          # 基本面因子（6 个，按公告日对齐）
python -m factors.calc --category combo                # 合成因子（2 个；组件须同区间）
python -m factors.analyze --category fundamental       # 分层回测 + RankIC

# ---------- 股票池快照 + 等权基准（口径与回测内股票池对齐）----------
python -m tools.backfill_universe                      # 逐日快照 → data/raw/universe/
python -m tools.backfill_universe --status             # 只看进度
python -m tools.benchmark_ew                           # 基准对照报告（含口径差）

# ---------- 前端（cd frontend）----------
npm run dev          # 开发服务器
npm run type-check   # 类型检查
npm run build        # 生产构建

# ---------- 测试与静态检查（cd backend）----------
python -m pytest -q                                  # 全量测试（700 项）
python -m pytest -m network                          # 真实发送用例（默认 skip）
python -m ruff check app/ tests/                     # 静态检查

# ---------- 容器（本地阶段不用）----------
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml down
```

---

## 目录结构

```
├── backend/
│   ├── app/
│   │   ├── main.py                    FastAPI 入口（含 lifespan 启动流程）
│   │   ├── core/                      paths / config / logging / errors
│   │   ├── api/v1/                    system / catalog / backtest / paper / alerts
│   │   ├── schemas/                   Pydantic 模型
│   │   ├── repos/                     SQLite 访问层（db.py / meta_repo.py）
│   │   ├── services/
│   │   │   ├── data_source/           ★ 数据源抽象层
│   │   │   │   ├── symbols.py         5 种代码格式互转
│   │   │   │   ├── baostock_client.py 行情主源
│   │   │   │   └── akshare_client.py  广度源
│   │   │   ├── data_store.py          ★ 唯一允许读写 Parquet/DuckDB 的地方
│   │   │   ├── calendar_service.py    交易日历
│   │   │   ├── cost_model.py          ★ 唯一交易成本实现
│   │   │   ├── paper_service.py       模块 4 模拟仿真
│   │   │   ├── wecom_client.py        模块 5 企微推送唯一出口
│   │   │   └── alert_service.py       模块 5 事件派发 / 模板 / 幂等 / 重试
│   │   └── scheduler/                 APScheduler + 任务注册表 + 启动补跑
│   ├── config/
│   │   ├── config.yaml                业务配置（含 scheduler.jobs 时刻表）
│   │   ├── alert_templates.yaml       ★ 提醒模板（唯一人工维护入口）
│   │   └── datasource_capabilities/   数据源能力声明（YAML）
│   ├── sql/schema.sql                 表定义
│   ├── strategies/                    引擎 + 策略实现（与 app/ 平级）
│   ├── tools/
│   │   ├── init_db.py
│   │   ├── sync_trade_calendar.py
│   │   ├── backfill_quotes.py         全市场行情回补
│   │   └── universe.py                ★ 股票池过滤规则
│   ├── .venv/                         ★ 项目虚拟环境（setup.bat 生成，不进 git）
│   └── requirements.txt
├── frontend/
│   └── src/
│       ├── api/                       axios 封装 + 接口定义
│       ├── layouts/                   侧边栏 + 顶栏
│       ├── components/                ModulePlaceholder（模块占位页）
│       ├── views/                     系统状态 + 5 个模块页
│       ├── router/                    路由与菜单
│       ├── stores/                    Pinia
│       └── types/                     TS 类型（与后端 schema 对齐）
├── data/                              ← 运行期产物，已 gitignore
│   ├── raw/quote_daily/               个股日线（后复权）
│   ├── raw/quote_index/               指数日线
│   ├── raw/universe/                  每日股票池快照
│   ├── factor/daily/                  因子
│   ├── backtest/                      回测结果（策略 CLI 写入）
│   ├── sweep/                         参数寻优结果
│   ├── paper/                         模拟盘
│   └── meta/
│       ├── meta.db                    元数据库
│       └── trade_calendar.parquet     交易日历
├── deploy/                            Dockerfile × 2 + compose + nginx.conf
├── .env                               ★ 密钥，不进 git
├── _env.bat                           共享环境探测（被下面三个脚本 call，勿直接双击）
├── setup.bat                          ★ 换机器后跑一次：建 venv + 装依赖
├── start-dev.bat                      开发模式：后端 --reload + vite dev
└── start-service.bat                  服务模式：单端口跑整站（含调度器）
```

---

## 三条硬性纪律

改代码时请遵守，review 时会检查。理由见 `技术架构.md` 第 4 章。

### 1. 数据源只能通过抽象层访问

`baostock` / `akshare` **只能被 `services/data_source/` 下的 client 导入**。业务层一律走 `data_source` 抽象层。

```python
# ✅ 正确
from app.services.data_source import fetch_daily
df = fetch_daily("sh.600519", "2024-01-01", "2024-12-31")

# ❌ 错误
import baostock as bs
import akshare as ak
```

*理由*：数据源有 5 种代码格式、两种会话模型。换源/加缓存/改限流只改一个目录。

### 2. 文件 IO 只能走 `data_store.py`

业务层**不得**直接 `pd.read_parquet(...)`。

```python
# ✅ 正确
from app.services import data_store
df = data_store.read_quotes("sh.600519", "2024-01-01", "2024-12-31")

# ❌ 错误
df = pd.read_parquet("data/raw/quote_daily/sh.600519.parquet")
```

*理由*：`data_store` 承担了**时间锁校验**（防止读未来数据）、分区裁剪、LTTB 降采样。绕过它就绕过了正确性保护。

### 3. 交易成本只能走 `cost_model.py`

**禁止在策略或引擎里硬编码费率**。

*理由*：印花税在 2023-08-28 从 0.1% 减半为 0.05%，回测跨越了这个变更点，必须按日期分段。且成本模型有三个消费者（止盈判定、引擎成交成本、年化交易成本指标），必须共用同一实现。

---

## 数据正确性红线

比功能更重要。这些错了，整条链路的结论都是废的。

| 项 | 规则 |
|---|---|
| **复权** | 历史行情统一存**后复权**（baostock `adjustflag="1"`）。前复权会随时间漂移，导致回测结果不可复现 |
| **未来函数** | 信号 T 日收盘生成 → **T+1 日开盘成交**。`data_store` 有全局 `READ_UNTIL` 时间锁 |
| **ST / 停牌** | 必须用 baostock 日线的 `isST` / `tradestatus` 字段的**逐日历史值**，不能用当前 ST 列表或成交量推断 |
| **退市股票** | 必须纳入数据（消除幸存者偏差）。baostock 支持 |
| **股票池** | 剔除 ST / 停牌 / 上市不足 **90 交易日** / 一字涨跌停 / 科创板 / 北交所 / B股 |
| **禁买窗口** | 卖出后 **60 交易日**内不得再买同一标的（⚠️ 与上面的 90 是两个独立参数） |
| **跌停/停牌** | 无法成交时订单作废并**顺延**，绝不按理想价撮合 |

---

## 中国市场惯例

- 图表配色：**涨红跌绿**（与欧美相反）
- 货币符号：¥
- 交易记录表：盈利红、亏损绿
- 已统一在 `frontend/src/styles/main.css` 的 CSS 变量中：`--color-up` / `--color-down`

---

## 常见问题

**Q: 双击 `start-dev.bat` / `start-service.bat`，窗口一闪就没了 / 服务起不来**
按顺序排查，脚本本身已经把大部分情况变成了可读提示：

1. **没跑过 `setup.bat`** —— 最常见。脚本会打印「missing this project's dependencies」
   或「No Python interpreter found」。跑一次 `setup.bat` 即可。
2. **`backend\.venv` 不存在**（换机器拷过来时通常不会带 `.venv`，
   它是机器相关的，本来也不该拷）。同上，`setup.bat` 会重建。
3. **前端窗口报 `npm` 不是内部命令** —— Node.js 没装或没进 PATH。装 Node ≥ 20 后跑 `setup.bat`。
4. **想看真实报错** —— 直接开一个 cmd 手敲：
   `cd /d <项目>\backend` 然后 `.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000`，
   报错就留在屏幕上了。

> 这三个 `.bat` 里**没有任何绝对路径**，换盘符/用户名/机器都不需要改。

**Q: 换机器 / 换了目录后要注意什么**
只有一件：跑一次 `setup.bat`。因为路径全是相对的（`paths.py` 靠 `backend/` + `.gitignore`
两个标记向上找项目根），`data/`、`config/`、`.env` 跟着目录一起走，不需要改任何东西。
`.venv` 和 `node_modules` 属于机器相关产物，重新生成即可。

**Q: 服务起来了，但浏览器打开 `127.0.0.1:8000` 报「拒绝连接」**
先确认三件事：① 后端那个窗口有没有报错（最可能是没跑 `setup.bat`，见上一题）；
② 端口是不是被别的程序占了（`netstat -ano | findstr 8000`）；③ 用的是不是 `start-service.bat`
（只有它会托管前端 `dist`，`start-dev.bat` 的界面在 `127.0.0.1:5173`）。

> 2026-09-22 之前，**"拒绝连接"最常见的成因是启动补跑还没跑完**（那时补跑同步跑在
> lifespan 里，30~90 分钟不监听端口）。该问题已修复：补跑改为后台线程，
> 端口 0.1 秒就绪，补跑进度改由前端「系统状态」页 / `scheduler.catch_up` 展示。

**Q: 启动报「No module named duckdb / akshare」**
用错了 Python 解释器（裸 `python` 可能指向系统 Python）。用项目虚拟环境：
`backend\.venv\Scripts\python.exe`。见上一题第 4 条。

**Q: 后端启动报「meta.db 尚未初始化」**
执行 `cd backend && python -m tools.init_db`。服务不会自动建表，避免隐式副作用。

**Q: 健康检查显示交易日历未就绪**
执行 `python -m tools.sync_trade_calendar`。服务启动时会尝试自动同步，但网络异常时需手动重试。

**Q: 前端页面能打开但数据全是空的**
确认后端已启动（`:8000`）。前端的 `/api` 请求由 Vite proxy 转发，后端没起就会全部失败。

**Q: akshare 升级后接口报错**
`akshare` 必须保持 `1.18.94`。它版本间常有 breaking change（已实测 `stock_a_indicator_lg` 在新版本被移除）。升级前先跑接口自检。

**Q: `baostock.__version__` 显示 `00.9.30`，但 pip 装的是 `0.9.3`**
这是 baostock 自己的版本格式化方式，与 pip 版本号不是同一个字符串。`requirements.txt` 里锁 `0.9.3` 是正确的。代码里通过 `importlib.metadata` 上报 pip 版本，避免混淆。

**Q: 本地关机导致模拟盘漏跑怎么办**
已内置启动补跑：服务启动时扫描 `nav.parquet` 最后日期到今天之间的缺失交易日并逐个补齐（上限 30 天，超出则告警）。可另配 Windows 任务计划做双保险。
> ⚠️ **早报任务不参与补跑**（`_NO_CATCH_UP_JOBS`）—— 它播报的是**当前**持仓，
> 补跑历史日期会与日期对不上；且 30 天补跑会连发 30 次。

**Q: 企微接口返回 `errcode=0`，但手机收不到消息**
九成是群机器人的「自定义关键词」安全设置。关键词必须是 **`持仓提醒`**（本项目所有消息都带它）。
其次检查 `93000`（webhook key 无效）、`60020`（可信 IP，仅方案 B）。
前端「用户提醒」页的日志表会显示真实错误码与中文说明。

**Q: 想只发给某个人，不发群**
需切**方案 B（自建应用）**：`WECOM_CHANNEL=app` + `CORP_ID` / `AGENT_ID` / `CORP_SECRET` / `TOUSER`。
代码已实现且测试覆盖（`wecom_client._send_via_app`），**切换步骤见 `docs/开发进度.md` §2.10**。
> ⚠️ 方案 A（群机器人 Webhook）**无法定向个人**，这是企微的限制不是代码问题。

**Q: 改了 `alert_templates.yaml` 要重启吗**
不用。模板按 **mtime 缓存**，改完立刻生效（前端「模板预览」能即时看到渲染效果）。
但改了 `config.yaml` 要重启（`.env` 同理，进程启动时读一次）。

---

## 开发约定

- **接口响应统一格式**：`{ code, message, data, ts }`，`code != 0` 时 `message` 是可直接展示的中文
- **密钥只走 `.env`**，不进 git；日志与接口响应中的密钥自动脱敏为 `***MASKED***`
- **新增数据源**：写 `config/datasource_capabilities/{name}.yaml` + 实现 client，前端能力矩阵页自动展示，不用改前端
- **规则变更流程**：先改 `策略规格说明书.md` 并升版本 → 再改代码
