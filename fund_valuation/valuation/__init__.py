"""估值层：直接取值、持仓加权合成、调度引擎。

    direct.py     消费第三方 GSZ/GSZZL，产出 source=api 的结果
    synthetic.py  重仓股加权合成（含代理 ETF fallback），产出 source=synthetic/fallback
    engine.py     按 mode 做双轨分派、降级、落库与汇总

两条路径互不替代：direct 是权威值，synthetic 是方向性近似，
engine 负责在"接口无估值"时把前者平滑降级为后者。
"""
