"""因子实现集合。

每个模块放一类因子：
    daily.py       日度行情因子
    （minute.py / fundamental.py 待数据源接入后再建）

实现契约见 `factors/registry.yaml` 头部注释，由 `factors/loader.py` 强制校验。
"""
