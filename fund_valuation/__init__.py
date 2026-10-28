"""基金盘中实时估值系统。

代码分层与依赖方向（箭头表示"依赖/调用"）：

    main ──► scheduler ──► valuation.engine ──► valuation.direct
      │                        │                valuation.synthetic
      │                        ├──► datasource.*（只依赖 base 抽象）
      │                        └──► db（只有 engine 触碰）
      └──► config / utils

约束：datasource 与 valuation 不导入 db 与 config，
因此加权、解析、降级这些核心逻辑可以脱离文件系统与网络单测。

对外入口：`python -m fund_valuation.main`（用法见 main.py 模块文档）。
"""

__version__ = "1.0.0"
