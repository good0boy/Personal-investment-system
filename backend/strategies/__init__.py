"""策略包（阶段 2 · 模块 3）。

为什么放在 `backend/strategies/` 而不是仓库根
------------------------------------------
`技术架构.md` 的目录树把它画在仓库根（`stock-system/strategies/`），
但那样 `python -m strategies.run` 需要额外把 `backend/` 加进 `sys.path`
才能 `from app.*` 导入。本项目已经把 `tools/` 放在 `backend/` 下，
策略与工具同源，放这里可以开箱即用：

    cd backend
    python -m strategies.run --strategy momentum

（`策略规格说明书.md` 是策略规则的唯一依据；本包的代码只实现它，不改它。）
"""
