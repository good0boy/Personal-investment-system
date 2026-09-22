"""模块 2 · 因子库。

三层结构：

    factors/registry.yaml   ← 因子的【定义】唯一来源（人工维护）
    factors/impl/*.py       ← 因子的【实现】（纯函数，无 IO）
    factors/calc.py         ← 【计算通道】CLI：读行情 → 调实现 → 落盘 + 写清单
    app/services/factor_service.py ← 【只读】服务层，前端只看它的输出

与模块 3 完全同构的纪律：**计算走 CLI，前端只读文件**。
"""
