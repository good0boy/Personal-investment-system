"""数据源能力矩阵测试。

★ 本文件盯的是一个**静默失效**（2026-09-18 发现的配置缺陷）：

`config/datasource_capabilities/_capabilities.yaml` 的 `groups[]` 原本只写了
`key/label/note`，**没写 `items`**。而 `datasource_matrix._capability_keys_in_order()`
恰恰靠 `g["items"]` 决定能力项顺序 —— 于是"人工按重要性排序"的分支一次都没进过，
静默退化成 `sorted(available)` 的字母序（`adjust_factor` 排第一，而不是
注释里明确要求的 `daily_kline`）。全链路不报错、不告警，页面上只是顺序变了。

同时前端靠 `groups[].items` 把能力项映射到分组标签，缺 items 时所有行都落到"其他"。
"""
from __future__ import annotations

import pytest

from app.services import datasource_matrix as dm


@pytest.fixture(autouse=True)
def _fresh_cache():
    dm.clear_cache()
    yield
    dm.clear_cache()


def _grouped_keys(data: dict) -> list[str]:
    return [k for g in data["capability_meta"]["groups"] for k in (g.get("items") or [])]


def test_groups_declare_items():
    """groups[] 必须写全 items —— 空列表就是上面说的静默失效。"""
    data = dm.load_matrix(force=True)
    grouped = _grouped_keys(data)
    assert grouped, "groups[].items 全为空：人工排序会退化成字母序，且前端分组标签全变「其他」"


def test_capability_keys_follow_manual_order():
    """展示顺序必须按 YAML 分组顺序，而不是字母序。

    关键能力（历史日线 / 逐日 ST / 停牌 / 退市股…）排在前面才符合设计意图。
    """
    data = dm.load_matrix(force=True)
    keys = data["capability_keys"]
    grouped = _grouped_keys(data)

    assert keys[: len(grouped)] == grouped, (
        f"能力项顺序未按 YAML 分组走：\n  期望 {grouped}\n  实际 {keys[:len(grouped)]}"
    )
    assert keys != sorted(keys), "顺序与字母序相同 —— 说明人工排序分支没生效"


def test_every_capability_is_assigned_to_a_group():
    """每个能力项都要能落到某个分组，否则前端只能显示「其他」。"""
    data = dm.load_matrix(force=True)
    grouped = set(_grouped_keys(data))
    missing = [k for k in data["capability_keys"] if k not in grouped]
    assert not missing, f"这些能力项没归入任何分组：{missing}"


def test_no_unknown_key_in_groups():
    """反向：分组里不能声明不存在的能力项（拼错 key 会静默丢掉那一项）。"""
    data = dm.load_matrix(force=True)
    known = set(data["capability_meta"]["capabilities"])
    unknown = [k for k in _grouped_keys(data) if k not in known]
    assert not unknown, f"groups[].items 里有未定义的能力项：{unknown}"


def test_meta_file_is_not_listed_as_datasource():
    """`_` 前缀文件是能力项元数据，不能被当成一个数据源列出来。"""
    data = dm.load_matrix(force=True)
    names = {s["name"] for s in data["sources"]}
    assert "_capabilities" not in names
    # 2026-10-01 起是 6 个（新增 fuyao）。断言写成"与 sources 长度一致"
    # 是为了防止"漏加一个源导致某列空白"的静默退化。
    assert data["source_count"] == len(data["sources"])
    assert data["source_count"] >= 6
