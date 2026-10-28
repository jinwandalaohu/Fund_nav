"""数据源层：抽象基类、各数据源实现与工厂。

    base.py       四个抽象角色 + 统一异常 DataSourceError
    tiantian.py   批量估值（主）/ 持仓档案（备）/ 基准净值
    eastmoney.py  持仓移动端接口（主）
    tencent.py    个股行情（主，覆盖 A/港/美）
    sina.py       个股行情（备，仅 A 股）/ 基金估值曲线（备）
    registry.py   把配置里的字符串映射到实例，并决定源链顺序

所有实现只依赖 utils.http.HttpClient，不触碰数据库与配置文件。
"""
