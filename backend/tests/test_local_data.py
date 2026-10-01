"""模块 1 · 本地数据清单的测试。

★ 纪律：**绝不读真实 `data/`**。清单服务的每条路径都在夹具里被换成 tmp 目录 ——
  否则"真实数据变了测试就红"的老毛病会立刻回来（conftest 开头的第 1 条纪律）。

这里重点钉住三件事：
  1. 清单说的是**磁盘真相**（文件数/行数/区间都来自实际落盘的文件）；
  2. 「没落盘」要**如实报**，不能静默跳过（本项目栽过一次：数据在盘上、
     分类却标着"没有数据"）；
  3. 因子产物的规模以 `_manifest.json` 为准，不重复扫磁盘（两个数就会有对不上的那天）。
"""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.errors import AppError, NotFoundError  # noqa: E402
from app.main import app  # noqa: E402
from app.services import data_store  # noqa: E402
from app.services import local_data_service as svc  # noqa: E402

SAMPLE_DATES = ("2024-01-31", "2024-02-29", "2024-03-29")


# ======================================================================
# 夹具
# ======================================================================
@pytest.fixture
def local_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """把清单服务的全部路径常量指到 tmp 目录树。"""
    root = tmp_path / "data"
    raw = root / "raw"
    for d in ("quote_daily", "quote_index", "universe", "fundamental"):
        (raw / d).mkdir(parents=True, exist_ok=True)
    # 同花顺特色数据的目录（2026-10-01 新增；不补会让用例读到真实 data/）
    for d in ("limit_pool", "board/catalog", "board/industry", "board/concept",
              "hot_list/history", "hot_list/current", "hot_list/rank_trend",
              "dragon_tiger/stock", "dragon_tiger/hot_money", "anomaly"):
        (raw / d).mkdir(parents=True, exist_ok=True)
    (root / "factor" / "daily").mkdir(parents=True, exist_ok=True)
    for d in ("backtest", "sweep", "paper", "meta"):
        (root / d).mkdir(parents=True, exist_ok=True)

    for name, value in {
        "DATA_DIR": root,
        "QUOTE_DAILY_DIR": raw / "quote_daily",
        "QUOTE_INDEX_DIR": raw / "quote_index",
        "UNIVERSE_DIR": raw / "universe",
        "FUNDAMENTAL_DIR": raw / "fundamental",
        "LIMIT_POOL_DIR": raw / "limit_pool",
        "BOARD_DIR": raw / "board",
        "BOARD_CATALOG_DIR": raw / "board" / "catalog",
        "HOT_LIST_DIR": raw / "hot_list",
        "DRAGON_TIGER_DIR": raw / "dragon_tiger",
        "ANOMALY_DIR": raw / "anomaly",
        "FACTOR_DIR": root / "factor",
        "FACTOR_MANIFEST_FILE": root / "factor" / "_manifest.json",
        "BACKTEST_DIR": root / "backtest",
        "SWEEP_DIR": root / "sweep",
        "PAPER_DIR": root / "paper",
        "META_DB": root / "meta" / "meta.db",
        "TRADE_CALENDAR_PARQUET": root / "meta" / "trade_calendar.parquet",
        "BACKFILL_FUND_STATE_FILE": root / "meta" / "backfill_fundamental_state.json",
        "BACKFILL_SPECIAL_STATE_FILE": root / "meta" / "backfill_special_state.json",
    }.items():
        monkeypatch.setattr(svc, name, value)

    # 状态库走 `app/repos/db`（连接在 import 时就按常量建好了），单测不重定向它。
    # 用固定桩替掉统计，保证用例不碰真实 meta.db。
    monkeypatch.setattr(
        svc,
        "_stats_from_sqlite",
        lambda: {
            "rows": 7,
            "files": 1,
            "bytes": 2048,
            "tables": {"alert_log": 3, "job_run_log": 4},
        },
    )
    svc.clear_cache()
    yield root
    svc.clear_cache()


def _write_quotes(directory: Path, symbols=("sh.600519", "sz.000001")) -> int:
    """写几只合成行情，返回总行数。"""
    total = 0
    for sym in symbols:
        df = pd.DataFrame(
            {
                "date": pd.to_datetime(SAMPLE_DATES),
                "symbol": sym,
                "close": [10.0, 11.0, 12.0][: len(SAMPLE_DATES)],
            }
        )
        data_store.write_parquet(df, directory / f"{sym}.parquet")
        total += len(df)
    return total


def _by_key(payload: dict) -> dict[str, dict]:
    return {d["key"]: d for d in payload["datasets"]}


# ======================================================================
# 定义表自身的完整性
# ======================================================================
def test_definition_table_is_well_formed() -> None:
    """维度定义不能有重复 key、不能引用未声明的分组、关键字段不能空。

    这条是给"以后加维度"用的：少写一个 group，前端那张卡片就会静默不显示。
    """
    dims = svc._definitions()
    keys = [d.key for d in dims]
    assert len(keys) == len(set(keys)), f"维度 key 重复：{keys}"

    group_keys = {g["key"] for g in svc.GROUPS}
    for d in dims:
        assert d.group in group_keys, f"{d.key} 引用了未声明的分组 {d.group}"
        assert d.origin in svc.ORIGINS, f"{d.key} 的来源 {d.origin} 未登记"
        assert d.key and d.name and d.desc and d.grain and d.producer, f"{d.key} 有字段是空的"
        assert d.sample is not None, f"{d.key} 没有配置示例取法"

    # 每个分组都要真的有维度，否则前端会渲染出一个空分组
    used = {d.group for d in dims}
    assert used == group_keys, f"空分组：{group_keys - used}"


# ======================================================================
# 磁盘真相
# ======================================================================
def test_datasets_reflects_disk(local_env: Path) -> None:
    """文件数 / 行数 / 覆盖区间全部来自实际落盘文件。"""
    rows = _write_quotes(local_env / "raw" / "quote_daily")

    payload = svc.datasets(refresh=True)
    q = _by_key(payload)["quote_daily"]

    assert q["available"] is True
    assert q["files"] == 2
    assert q["rows"] == rows
    assert q["date_start"] == SAMPLE_DATES[0]
    assert q["date_end"] == SAMPLE_DATES[-1]
    assert q["origin"] == "downloaded"
    assert q["origin_label"] == "原始下载"
    assert q["human"].endswith(("B", "KB", "MB", "GB"))
    assert q["error"] is None

    # 汇总只统计"可用"的维度
    present = [d for d in payload["datasets"] if d["available"]]
    assert payload["summary"]["available"] == len(present)
    assert payload["summary"]["files"] >= q["files"]


def test_missing_path_is_reported_not_skipped(local_env: Path) -> None:
    """没有落盘的维度照样出现在清单里，且写明原因 —— 不静默跳过。"""
    payload = svc.datasets(refresh=True)
    by = _by_key(payload)

    assert len(payload["datasets"]) == len(svc._definitions()), "维度数量必须与定义一致"
    assert payload["summary"]["missing"] >= 1

    # 目录存在但空着
    assert by["quote_daily"]["available"] is False
    assert "没有任何落盘文件" in by["quote_daily"]["error"]

    # 连目录都没有（计划内、从没回补过）
    assert by["fund_hold"]["available"] is False
    assert "没有这个目录" in by["fund_hold"]["error"]


def test_unavailable_fundamental_dataset_carries_backfill_reason(local_env: Path) -> None:
    """`fund_hold` 没落盘时，要把回补状态里的失败原因带出来（而不是写死一句）。"""
    state = local_env / "meta" / "backfill_fundamental_state.json"
    state.write_text(
        json.dumps(
            {
                "version": 1,
                "batch": {"fund_hold": {"2024-06-30": {"status": "failed", "error": "接口返回异常"}}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    payload = svc.datasets(refresh=True)
    hold = _by_key(payload)["fund_hold"]

    assert hold["available"] is False
    assert "1/1 个报告期失败" in hold["error"]
    assert "接口返回异常" in hold["error"]

    # 一旦真的落盘了，就不该再报缺
    target = local_env / "raw" / "fundamental" / "fund_hold"
    target.mkdir(parents=True, exist_ok=True)
    data_store.write_parquet(
        pd.DataFrame({"symbol": ["000001"], "notice_date": pd.to_datetime(["2024-06-30"])}),
        target / "all.parquet",
    )
    hold2 = _by_key(svc.datasets(refresh=True))["fund_hold"]
    assert hold2["available"] is True
    assert hold2["rows"] == 1


def test_factor_scale_comes_from_manifest(local_env: Path) -> None:
    """因子产物的规模以 `_manifest.json` 为准，且不把测试残留算成因子。

    ⚠️ 断言 rows == 999（manifest 里写的）而**不是** 3（真实 parquet 行数）——
       这正是"状态只有一个来源"的意思。若哪天有人改成扫磁盘，这条会红。
    """
    factor_dir = local_env / "factor" / "daily"
    for name in ("ret_5", "momentum_20"):
        data_store.write_parquet(
            pd.DataFrame(
                {
                    "date": pd.to_datetime(SAMPLE_DATES),
                    "symbol": "sh.600519",
                    "value": [1.0, 2.0, 3.0],
                }
            ),
            factor_dir / f"{name}.parquet",
        )
    # 测试残留：`_` 开头，不是数据
    data_store.write_parquet(
        pd.DataFrame({"date": pd.to_datetime(SAMPLE_DATES), "symbol": "x", "value": [0.0] * 3}),
        factor_dir / "_test_lock.parquet",
    )
    (local_env / "factor" / "_manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "factors": {
                    "ret_5": {
                        "name": "ret_5", "cname": "近5日累计收益", "category": "daily",
                        "rows": 999, "date_start": "2024-01-01", "date_end": "2024-06-30",
                    },
                    "momentum_20": {
                        "name": "momentum_20", "cname": "20日动量", "category": "daily",
                        "rows": 1, "date_start": "2024-02-01", "date_end": "2024-12-31",
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    f = _by_key(svc.datasets(refresh=True))["factor_daily"]

    assert f["available"] is True
    assert f["rows"] == 1000, "行数必须取 manifest 的和"
    assert f["files"] == 2, "`_` 开头的测试残留不能算成因子产物"
    assert f["factor_count"] == 2
    assert [x["name"] for x in f["factor_names"]] == ["momentum_20", "ret_5"]
    # 覆盖区间取各因子的**并集**
    assert f["date_start"] == "2024-01-01"
    assert f["date_end"] == "2024-12-31"


def test_rows_scope_is_exposed_when_rows_and_files_differ() -> None:
    """行数与文件数不同源时必须写出口径，否则"行数"会被误读成整个维度的行数。"""
    by = {d.key: d for d in svc._definitions()}
    assert "仅净值表" in by["backtest"].rows_scope
    assert "仅净值表" in by["paper"].rows_scope
    assert by["quote_daily"].rows_scope == ""


# ======================================================================
# 示例数据
# ======================================================================
def _wide_frame(n_extra: int = 20) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.to_datetime(SAMPLE_DATES),
            "symbol": "sh.600519",
            **{f"f{i}": [float(i)] * len(SAMPLE_DATES) for i in range(n_extra)},
        }
    )


def test_sample_uses_declared_columns_and_notes_the_trimming(local_env: Path) -> None:
    """有声明列时按声明取（顺序也按声明），裁掉的部分要写在 note 里。"""
    qdir = local_env / "raw" / "quote_daily"
    data_store.write_parquet(_wide_frame(), qdir / "sh.600519.parquet")

    s = svc.sample("quote_daily", 2)

    assert s["shown_rows"] == 2
    # 声明的 12 列里只有 date / symbol 真实存在 → 只展示这两列
    assert [c["name"] for c in s["columns"]] == ["date", "symbol"]
    assert "共 22 列" in s["note"] and "2 列" in s["note"]
    assert s["source"]["file"].endswith("sh.600519.parquet")
    assert s["source"]["symbol"] == "sh.600519", "文件名是标的名时要带出来"
    assert s["grain"] and s["dataset"]
    assert len(s["rows"][0]) == s["shown_columns"]


def test_sample_falls_back_to_leading_columns_when_none_declared(local_env: Path) -> None:
    """没声明展示列时，回落到"前 N 列"——否则 151 列的宽表铺出来没法看。"""
    qdir = local_env / "raw" / "quote_daily"
    data_store.write_parquet(_wide_frame(), qdir / "sh.600519.parquet")

    dim = svc._get_dimension("quote_daily")
    spec = replace(dim.sample, columns=())
    payload = svc._sample_parquet(dim, spec, 3)

    assert payload["shown_columns"] == svc._MAX_SAMPLE_COLUMNS
    assert payload["note"] == "" or "展示" in payload["note"]


def test_sample_prefers_named_symbol_and_skips_test_residue(local_env: Path) -> None:
    """挑文件优先用 `prefer` 指的标的，且跳过 `_` 开头的测试残留。"""
    qdir = local_env / "raw" / "quote_daily"
    _write_quotes(qdir, symbols=("sh.600000", "sh.600519"))
    data_store.write_parquet(
        pd.DataFrame({"date": pd.to_datetime(SAMPLE_DATES), "symbol": "junk", "close": [0.0] * 3}),
        qdir / "_test_lock.parquet",
    )

    s = svc.sample("quote_daily")
    assert s["source"]["symbol"] == "sh.600519"

    spec = svc._get_dimension("quote_daily").sample
    assert spec is not None
    picked = svc._pick_file(spec)
    assert picked is not None and not picked.name.startswith("_")


def test_sample_latest_picks_newest_snapshot(local_env: Path) -> None:
    """`latest=True` 的维度（股票池快照）要挑文件名最大的那一天。"""
    udir = local_env / "raw" / "universe"
    for day in ("2024-01-31", "2024-02-29", "2024-03-29"):
        data_store.write_parquet(
            pd.DataFrame(
                {
                    "date": pd.to_datetime([day]),
                    "symbol": ["sh.600519"],
                    "kept": [1],
                    "empty": [False],
                }
            ),
            udir / f"{day}.parquet",
        )

    s = svc.sample("universe")
    assert "2024-03-29" in s["source"]["file"]
    assert "2024-03-29" in s["source"]["caption"]


def test_sample_limit_is_clamped(local_env: Path) -> None:
    """limit 越界不报错，按 1~100 夹紧（服务层的最后一道闸）。"""
    _write_quotes(local_env / "raw" / "quote_daily", symbols=("sh.600519",))
    assert svc.sample("quote_daily", 0)["shown_rows"] == 1
    assert svc.sample("quote_daily", 1000)["shown_rows"] == len(SAMPLE_DATES)


def test_sample_unknown_key_raises_not_found(local_env: Path) -> None:
    with pytest.raises(NotFoundError) as ei:
        svc.sample("nope")
    assert "nope" in ei.value.message
    assert "quote_daily" in ei.value.detail.get("available", []) or ei.value.detail


def test_sample_of_unavailable_dimension_raises_app_error(local_env: Path) -> None:
    """`fund_hold` 在定义里、但磁盘上没有 —— 要给"没有可读文件"，不是 500。"""
    with pytest.raises(AppError) as ei:
        svc.sample("fund_hold")
    assert "没有可读" in ei.value.message


def test_sample_sql_dimension_uses_custom_sql(local_env: Path) -> None:
    """自定义 SQL 的维度（因子宽表）按 (date, symbol) 横向拼接。"""
    fdir = local_env / "factor" / "daily"
    for name in ("ret_5", "ret_10", "momentum_20", "turnover_20"):
        data_store.write_parquet(
            pd.DataFrame(
                {
                    "date": pd.to_datetime(SAMPLE_DATES),
                    "symbol": "sh.600519",
                    "value": [1.0, 2.0, 3.0],
                }
            ),
            fdir / f"{name}.parquet",
        )

    s = svc.sample("factor_daily", 3)
    names = [c["name"] for c in s["columns"]]
    assert names == ["date", "symbol", "ret_5", "ret_10", "momentum_20", "turnover_20"]
    assert s["shown_rows"] == 3
    assert s["source"]["symbol"] == "sh.600519"


# ======================================================================
# 缓存
# ======================================================================
def test_datasets_is_cached_until_refreshed(local_env: Path) -> None:
    """统计要扫 2.5 万个文件尾（约 12 秒），必须有缓存；refresh 才重算。"""
    first = svc.datasets()
    assert svc.datasets() is first, "未过期时应直接返回缓存对象"

    again = svc.datasets(refresh=True)
    assert again is not first, "refresh=True 必须重新扫描"
    assert again["datasets"][0]["key"] == first["datasets"][0]["key"]


def test_cache_can_be_cleared(local_env: Path) -> None:
    first = svc.datasets()
    svc.clear_cache()
    assert svc.datasets() is not first


# ======================================================================
# 单元格格式化
# ======================================================================
def test_midnight_timestamps_render_as_date() -> None:
    """零点的时间戳按日期显示（日线数据的 00:00:00 是噪音）。"""
    assert svc._cell_text(pd.Timestamp("2026-09-22")) == "2026-09-22"
    assert svc._cell_text(pd.NaT) == ""
    assert svc._cell_text(float("nan")) == ""
    assert svc._cell_text(None) == ""
    # 非零点保留时间
    assert svc._cell_text(pd.Timestamp("2026-09-22 14:30:00")) == "2026-09-22 14:30:00"
    # 普通数值/字符串原样
    assert svc._cell_text(3.14) == "3.14"
    assert svc._cell_text("sh.600519") == "sh.600519"
    # 超长文本要截断
    assert len(svc._cell_text("x" * 500)) == svc._MAX_CELL_CHARS


def test_human_helpers() -> None:
    assert svc._human_size(0) == "0.0 B"
    assert svc._human_size(2048) == "2.0 KB"
    assert svc._human_size(3 * 1024**3) == "3.0 GB"
    assert svc._human_count(999) == "999"
    assert svc._human_count(12345) == "1.2 万"
    assert svc._human_count(148_000_000) == "1.48 亿"


# ======================================================================
# 接口层（真实路由 + 真实异常处理器）
# ======================================================================
@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    """把服务层打桩，只验证路由与响应封装（不想为接口测试扫 2.5 万个文件）。"""
    captured: dict[str, int] = {}

    def fake_datasets(refresh: bool = False) -> dict:
        return {"datasets": [], "groups": [], "summary": {"total": 0}, "refresh": refresh}

    def fake_sample(key: str, limit: int = 20) -> dict:
        if key == "nope":
            raise NotFoundError("未知的本地数据维度：nope")
        captured["limit"] = limit
        return {"key": key, "rows": [], "columns": [], "source": {}}

    monkeypatch.setattr(svc, "datasets", fake_datasets)
    monkeypatch.setattr(svc, "sample", fake_sample)
    # ⚠️ 刻意**不用** `with TestClient(app)`：进入上下文会触发 lifespan，
    #    而启动补跑会去跑真实的 paper_daily / 自检 —— 单测不该有这种副作用。
    #    本用例只验证路由与响应封装，不需要 lifespan。
    c = TestClient(app)
    c.captured = captured  # type: ignore[attr-defined]
    return c


def test_local_routes_are_registered(client: TestClient) -> None:
    r = client.get("/api/v1/catalog/local/datasets")
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    assert "datasets" in body["data"]

    r2 = client.get("/api/v1/catalog/local/datasets/quote_daily/sample?limit=7")
    assert r2.status_code == 200
    assert r2.json()["data"]["key"] == "quote_daily"
    assert client.captured["limit"] == 7  # type: ignore[attr-defined]


def test_local_sample_rejects_out_of_range_limit(client: TestClient) -> None:
    """limit 上限由路由层挡（服务层还有一道夹紧，两层都不该少）。"""
    r = client.get("/api/v1/catalog/local/datasets/quote_daily/sample?limit=999")
    assert r.status_code == 400
    assert client.get("/api/v1/catalog/local/datasets/quote_daily/sample?limit=0").status_code == 400


def test_local_sample_unknown_dimension_is_404(client: TestClient) -> None:
    r = client.get("/api/v1/catalog/local/datasets/nope/sample")
    assert r.status_code == 404
    assert "nope" in r.json()["message"]
