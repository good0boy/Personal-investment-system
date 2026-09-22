"""生成 akshare 接口目录骨架（raw dump）。

做什么
------
遍历 akshare 暴露的 `stock_` / `index_` / `fund_` 函数，用 `inspect` 解析
函数签名与 docstring，产出一份**结构化**的原始清单。

为什么要有这一步
----------------
手写 API 文档一定会过时。这里的做法是「目录即数据」：
    脚本负责"**不漏**"（把 555 个函数全扫出来）
    人工负责"**精选**"（从中挑 45~50 个写进 config/catalog/*.yaml）

两者产出**分开存放**，本脚本不碰人工维护的 YAML。

实测的 docstring 格式（akshare 1.18.94，格式高度统一）
----------------------------------------------------
    东方财富网-行情首页-沪深京 A 股-每日行情        ← 第 1 行：中文标题
    https://quote.eastmoney.com/concept/...        ← 第 2 行：官方文档链接
    :param symbol: 股票代码                        ← 参数说明
    :type symbol: str                              ← 参数类型
    :param period: choice of {'daily', 'weekly'}   ← 枚举候选（可解析成下拉选项）
    :return: 每日行情                              ← 返回值说明
    :rtype: pandas.DataFrame                       ← 返回容器类型

用法
----
    cd backend
    python -m tools.gen_catalog                    # 生成 config/catalog/_raw_dump.yaml
    python -m tools.gen_catalog --check            # 只比较不写入；不一致退出码 1（CI 用）
    python -m tools.gen_catalog --grep 股东        # 按关键词过滤，便于人工精选
    python -m tools.gen_catalog --prefix stock_    # 限定函数名前缀
    python -m tools.gen_catalog --show 财务        # 打印匹配项的签名，不写文件

可重复性
--------
- 输出按 id 排序；字段顺序固定；**不写时间戳**（时间戳只打日志）→ 输出可逐行 diff
- akshare 版本变化会体现在 `meta.source_version` 与具体条目上，正好用来审查升级影响
"""
from __future__ import annotations

import argparse
import inspect
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.logging import logger, setup_logging
from app.core.paths import CATALOG_DIR, CATALOG_RAW_DUMP

#: 默认扫描的函数名前缀
DEFAULT_PREFIXES = ("stock_", "index_", "fund_", "tool_")

#: docstring 里的字段标记
_RE_PARAM = re.compile(r"^:param\s+(\w+)\s*:\s*(.*)$")
_RE_TYPE = re.compile(r"^:type\s+(\w+)\s*:\s*(.*)$")
_RE_RETURN = re.compile(r"^:return\s*:\s*(.*)$")
_RE_RTYPE = re.compile(r"^:rtype\s*:\s*(.*)$")
_RE_URL = re.compile(r"^https?://\S+$")
#: `choice of {'daily', 'weekly', 'monthly'}` / `choice of {"a", "b"}`
_RE_CHOICE = re.compile(r"choice of\s*[\{\[]([^\}\]]*)[\}\]]", re.IGNORECASE)
#: 从 choice 里逐项抓引号内容
_RE_QUOTED = re.compile(r"[\"'`]([^\"'`]*)[\"'`]")

#: 类型标注里需要还原成 JSON 友好形式的
_SIMPLE_TYPES = {"str", "int", "float", "bool"}


def _parse_docstring(doc: str) -> dict[str, Any]:
    """把 docstring 拆成 标题 / 文档链接 / 参数说明 / 返回说明。"""
    title = ""
    doc_url = ""
    params: dict[str, str] = {}
    param_types: dict[str, str] = {}
    returns_note = ""
    returns_type = ""

    for raw_line in doc.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if not title:
            title = line
            continue
        if not doc_url and _RE_URL.match(line):
            doc_url = line
            continue

        m = _RE_PARAM.match(line)
        if m:
            params[m.group(1)] = m.group(2).strip()
            continue
        m = _RE_TYPE.match(line)
        if m:
            param_types[m.group(1)] = m.group(2).strip()
            continue
        m = _RE_RETURN.match(line)
        if m:
            returns_note = m.group(1).strip()
            continue
        m = _RE_RTYPE.match(line)
        if m:
            returns_type = m.group(1).strip()
            continue

    return {
        "title": title,
        "doc_url": doc_url,
        "param_desc": params,
        "param_types": param_types,
        "returns_note": returns_note,
        "returns_type": returns_type,
    }


def _parse_options(desc: str) -> list[str]:
    """从 `choice of {...}` 里解析枚举候选（供前端渲染 el-select）。"""
    m = _RE_CHOICE.search(desc or "")
    if not m:
        return []
    inner = m.group(1)
    quoted = _RE_QUOTED.findall(inner)
    if quoted:
        return list(quoted)
    # 无引号形式：按逗号切
    return [p.strip() for p in inner.split(",") if p.strip()]


def _default_repr(value: Any) -> Any:
    """把签名默认值转成 JSON 友好的标量。"""
    if value is inspect.Parameter.empty:
        return None
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return repr(value)


def _extract_params(fn: Any, doc: dict[str, Any]) -> list[dict[str, Any]]:
    """按签名顺序提取参数，合并 docstring 说明。"""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):  # pragma: no cover —— 少数内置对象无签名
        return []

    out: list[dict[str, Any]] = []
    for name, p in sig.parameters.items():
        if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue

        default = _default_repr(p.default)
        desc = doc["param_desc"].get(name, "")
        out.append(
            {
                "name": name,
                "type": doc["param_types"].get(name, "str"),
                "required": p.default is inspect.Parameter.empty,
                "default": default,
                "desc": desc,
                "options": _parse_options(desc),
            }
        )
    return out


def _is_akshare_function(obj: Any) -> bool:
    """只保留 akshare 自己定义的函数，排除 import 进来的第三方函数。"""
    if not inspect.isfunction(obj):
        return False
    module = getattr(obj, "__module__", "") or ""
    return module.startswith("akshare")


def collect(prefixes: tuple[str, ...] = DEFAULT_PREFIXES) -> list[dict[str, Any]]:
    """扫描 akshare，返回按 id 排序的结构化记录。"""
    import akshare as ak  # noqa: PLC0415

    records: list[dict[str, Any]] = []
    for name, fn in inspect.getmembers(ak, _is_akshare_function):
        if not name.startswith(prefixes):
            continue

        doc = _parse_docstring(inspect.getdoc(fn) or "")
        try:
            signature = str(inspect.signature(fn))
        except (TypeError, ValueError):  # pragma: no cover
            signature = "()"

        records.append(
            {
                "id": name,
                "func_name": name,
                "module": getattr(fn, "__module__", ""),
                "title": doc["title"],
                "doc_url": doc["doc_url"],
                "signature": signature,
                # 源文档只有整体描述，**逐列说明必须靠真实调用获得**（见 tools/probe_returns.py）
                "returns_note": doc["returns_note"],
                "returns_type": doc["returns_type"],
                "params": _extract_params(fn, doc),
                "n_params": len(inspect.signature(fn).parameters) if signature != "()" else 0,
                "param_names": [p["name"] for p in _extract_params(fn, doc)],
            }
        )

    records.sort(key=lambda r: r["id"])
    return records


def _build_dump(
    records: list[dict[str, Any]], prefixes: tuple[str, ...], version: str
) -> dict[str, Any]:
    """组装 dump 结构。**刻意不写时间戳** —— 保证输出可逐行 diff。"""
    by_prefix: dict[str, int] = dict.fromkeys(prefixes, 0)
    for r in records:
        for p in prefixes:
            if r["id"].startswith(p):
                by_prefix[p] += 1
                break

    return {
        "meta": {
            "source": "akshare",
            "source_version": version,
            "prefixes": list(prefixes),
            "total": len(records),
            "by_prefix": by_prefix,
            "note": (
                "本文件由 tools/gen_catalog.py 生成，**不要手改**。"
                "人工精选结果写入 config/catalog/{fundamental,quote_realtime,quote_history}.yaml。"
            ),
        },
        "interfaces": records,
    }


def _dump_to_yaml(data: dict[str, Any]) -> str:
    import yaml  # noqa: PLC0415

    return yaml.safe_dump(
        data,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=120,
    )


def _filter(records: list[dict[str, Any]], grep: str | None) -> list[dict[str, Any]]:
    if not grep:
        return records
    kw = grep.lower()
    out = []
    for r in records:
        haystack = " ".join(
            [r["id"], r["title"], r["returns_note"], " ".join(r["param_names"])]
        ).lower()
        if kw in haystack:
            out.append(r)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 akshare 接口目录骨架")
    parser.add_argument("--check", action="store_true", help="只比较，不一致时退出码 1")
    parser.add_argument("--grep", default=None, help="按关键词过滤（id/标题/返回说明/参数名）")
    parser.add_argument("--prefix", nargs="*", default=list(DEFAULT_PREFIXES), help="函数名前缀")
    parser.add_argument("--show", default=None, metavar="KW", help="打印匹配项，不写文件")
    parser.add_argument("--limit", type=int, default=0, help="仅展示前 N 条（配合 --show）")
    args = parser.parse_args()

    setup_logging()
    prefixes = tuple(args.prefix)

    try:
        import akshare as ak  # noqa: PLC0415

        version = str(getattr(ak, "__version__", "unknown"))
        records = collect(prefixes)
    except ImportError:
        logger.error("akshare 未安装，请先 pip install -r requirements.txt")
        return 3

    if args.show is not None:
        picked = _filter(records, args.show)
        if args.limit:
            picked = picked[: args.limit]
        print(f"\n匹配 {len(picked)} 个函数：\n")
        for r in picked:
            print(f"  {r['id']:<48} {r['signature']}")
            if r["title"]:
                print(f"  {'':<48} └─ {r['title']}")
        print()
        return 0

    filtered = _filter(records, args.grep)
    dump = _build_dump(filtered, prefixes, version)
    text = _dump_to_yaml(dump)

    meta = dump["meta"]
    print(f"\nakshare {meta['source_version']}｜扫描到 {meta['total']} 个函数")
    for p, c in meta["by_prefix"].items():
        print(f"  {p:<10} {c}")
    if args.grep:
        print(f"  （已按关键词 {args.grep!r} 过滤）")

    if args.check:
        if not CATALOG_RAW_DUMP.exists():
            print(f"\n[FAIL] {CATALOG_RAW_DUMP} 不存在，请先运行不带 --check 的命令生成\n")
            return 1
        old = CATALOG_RAW_DUMP.read_text(encoding="utf-8")
        if old == text:
            print("\n[OK] 与磁盘上的 dump 一致\n")
            return 0
        print("\n[FAIL] dump 与磁盘不一致（akshare 可能已升级）。差异见下：\n")
        import difflib  # noqa: PLC0415

        diff = difflib.unified_diff(
            old.splitlines(), text.splitlines(),
            fromfile="on-disk", tofile="regenerated", lineterm="", n=1,
        )
        for i, line in enumerate(diff):
            if i > 60:
                print("  ... （差异过长，已截断）")
                break
            print(f"  {line}")
        print()
        return 1

    CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    CATALOG_RAW_DUMP.write_text(text, encoding="utf-8")
    size_kb = CATALOG_RAW_DUMP.stat().st_size / 1024
    print(f"\n[OK] 已写入 {CATALOG_RAW_DUMP}（{size_kb:.0f} KB）")
    print("     下一步：从中精选接口，写入 config/catalog/*.yaml\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
