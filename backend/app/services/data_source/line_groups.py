"""数据源「线路组」—— 熔断的分组粒度。

★ 为什么熔断必须按线路分组（2026-10-01 实测根因）
------------------------------------------------
东财 `push2.eastmoney.com` / `push2his.eastmoney.com` 在本机网络被**掐断**
（表现为 `RemoteDisconnected`）。`throttle.classify_limit_signal()` 把"连接被
掐断"归为 `soft` 疑似限频，连续 3 次即熔断；而熔断器原本是**每个 client 一把
锁**、不区分域名 —— 于是「push2 踩 3 次」把**整个 akshare 通道**封了 300 秒，
连本机完全正常的 `push2ex`（涨停池）与同花顺接口也一起报"数据源触发限频"。

现象上就是"越测越像全是限频"，而真相是**次生灾害**：一个域名的故障被放大成
整个数据源的故障。按线路分组后，故障边界与真实边界一致。

★ 为什么用映射表，而不是从异常里抠 host
----------------------------------------
akshare 把 `requests` / `http.client` 的异常层层包装后抛出，`RemoteDisconnected`
的异常链上**常常没有** `.request.url` —— 而 soft 信号恰恰是最需要分组的那一类，
抠不到就等于没分组。显式声明是唯一确定的手段。

★ 为什么放 Python 而不是 YAML
------------------------------
这是**运行时热路径的内部机制**，不是用户可编辑的业务配置。YAML 需要 import 期
文件 IO + 解析，且改了要重启（`datasource_matrix.py` 已有这个坑）。项目"目录即
数据"的纪律针对的是**用户可见的接口定义**，熔断线路属于实现细节。

⚠️ 缺省规则：未声明的 `func_name` → **以自身为一组**。
   好处是新接口零配置也有熔断保护，坏处是同一域名的新接口要各自踩坑。
   所以：**已知域名归属的接口请补进本表**；不确定时不要瞎猜（猜错会把两个
   无关线路绑在一起，比不分组更糟）。
"""
from __future__ import annotations

__all__ = ["LINE_GROUPS", "group_label", "line_group_of"]

#: 线路组 → 成员函数名。组名会出现在熔断状态里，请保持简短可读。
LINE_GROUPS: dict[str, tuple[str, ...]] = {
    # ---- 东方财富：push2.eastmoney.com（含 NN.push2 子域）----
    # 本机实测：全部 ConnectionError / RemoteDisconnected
    "em_push2": (
        "stock_zh_a_spot_em",
        "stock_cy_a_spot_em",
        "stock_kc_a_spot_em",
        "stock_bj_a_spot_em",
        "stock_sh_a_spot_em",
        "stock_sz_a_spot_em",
        "stock_new_a_spot_em",
        "stock_bid_ask_em",
        "stock_intraday_em",
        "stock_zh_a_hist_pre_min_em",
        "stock_zh_index_spot_em",
        "stock_board_industry_name_em",
        "stock_board_industry_spot_em",
        "stock_board_industry_cons_em",
        "stock_board_concept_name_em",
        "stock_board_concept_spot_em",
        "stock_board_concept_cons_em",
        "stock_board_change_em",
        "stock_individual_fund_flow_rank",
        "stock_sector_fund_flow_rank",
    ),
    # ---- 东方财富：push2his.eastmoney.com ----
    "em_push2his": (
        "stock_zh_a_hist",
        "stock_zh_a_hist_min_em",
        "index_zh_a_hist",
        "index_zh_a_hist_min_em",
        "stock_individual_fund_flow",
        "stock_board_industry_hist_em",
        "stock_board_industry_hist_min_em",
        "stock_board_concept_hist_em",
        "stock_board_concept_hist_min_em",
        "stock_cyq_em",
    ),
    # ---- 东方财富：push2ex.eastmoney.com ----
    # ★ 本机实测**唯一可用**的东财线路（涨停板系列，0.2~0.3s）
    "em_push2ex": (
        "stock_zt_pool_em",
        "stock_zt_pool_previous_em",
        "stock_zt_pool_strong_em",
        "stock_zt_pool_sub_new_em",
        "stock_zt_pool_zbgc_em",
        "stock_zt_pool_dtgc_em",
    ),
    # ---- 东方财富：datacenter-web.eastmoney.com ----
    "em_datacenter": (
        "stock_lhb_detail_em",
        "stock_hsgt_hist_em",
        "stock_hsgt_fund_flow_summary_em",
    ),
    # ---- 同花顺：q/d/data.10jqka.com.cn ----
    # 多数成员以 `_ths` 结尾，由 `line_group_of()` 的后缀兜底自动归组；
    # 这里显式列出**不以 _ths 结尾**的同花顺接口。
    "ths": (
        "stock_fund_flow_individual",
        "stock_fund_flow_concept",
        "stock_fund_flow_industry",
        "stock_fund_flow_big_deal",
        "stock_lh_yyb_capital",
        "stock_lh_yyb_control",
        "stock_lh_yyb_most",
    ),
    # ---- 新浪 ----
    "sina": (
        "tool_trade_date_hist_sina",
        "stock_zh_a_daily",
        "stock_zh_a_spot",
        "stock_zh_b_spot",
        "stock_zh_kcb_spot",
        "stock_zh_index_spot_sina",
        "stock_hk_index_spot_sina",
        "stock_hk_spot",
        "stock_us_spot",
        "stock_sector_spot",
        "stock_sector_detail",
    ),
    # ---- 上交所官网 ----
    "sse": (
        "stock_margin_sse",
        "stock_margin_detail_sse",
    ),
}

#: 反向索引（模块加载时构建一次）
_FUNC_TO_GROUP: dict[str, str] = {
    fn: group for group, funcs in LINE_GROUPS.items() for fn in funcs
}

#: 组名 → 给前端看的中文标签（认不出来就原样显示组名）
_GROUP_LABELS: dict[str, str] = {
    "em_push2": "东财 push2",
    "em_push2his": "东财 push2his",
    "em_push2ex": "东财 push2ex",
    "em_datacenter": "东财数据中心",
    "ths": "同花顺",
    "sina": "新浪",
    "sse": "上交所",
    "fuyao": "同花顺 REST",
    "default": "默认",
}


def line_group_of(func_name: str) -> str:
    """返回熔断分组 key。

    优先级：显式声明 → `_ths` 后缀兜底 → 以函数名自成一组。
    """
    fn = (func_name or "").strip()
    if not fn:
        return "default"
    hit = _FUNC_TO_GROUP.get(fn)
    if hit:
        return hit
    # 同花顺的接口绝大多数以 _ths 结尾（akshare 的命名约定）
    if fn.endswith("_ths"):
        return "ths"
    return fn


def group_label(group: str) -> str:
    """组名 → 展示用标签（未知组原样返回）。"""
    return _GROUP_LABELS.get(group, group)
