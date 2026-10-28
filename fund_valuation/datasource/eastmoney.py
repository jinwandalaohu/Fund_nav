"""东方财富持仓源（主源）。

实测结论（2026-10 验证）：
- PC 端 F10 持仓页 `ccmx_{code}.html` 已改为 JS 渲染，HTML 里没有表格，纯静态解析拿不到数据；
- F10 的数据接口 `api.fund.eastmoney.com/f10/ccmx` 已返回 404；
- 移动端 `FundMNInverstPosition` 仍可用，但必须带移动端 UA + H5 Referer，否则返回
  `ErrCode=61136403 网络繁忙`。

因此本源采用移动端 JSON 接口（字段结构化，最稳），静态 HTML 解析路径由天天基金源的
`FundArchivesDatas` 承担 —— 两者是不同端点，互为备份。
"""

from __future__ import annotations

from datetime import datetime

from ..models import AssetType, Holding, HoldingsSnapshot, WeightUnit
from ..utils.logger import get_logger
from .base import DataSourceError, HoldingsSource

LOGGER = get_logger(__name__)

# 必须伪装成移动端浏览器：接口会按 UA 分流，PC UA 会拿到错误码而非数据。
MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)


class EastmoneyHoldingsSource(HoldingsSource):
    """持仓主源：东财移动端 JSON 接口。

    接口一次返回前十大重仓股（GPDM 代码 / GPJC 名称 / JZBL 占净值比例）
    与报告期（Expansion 字段），比解析 HTML 表格稳定得多。
    """

    name = "eastmoney"
    MOBILE_API = "https://fundmobapi.eastmoney.com/FundMNewApi/FundMNInverstPosition"
    # JZBL 是百分数：普通基金 "6.37" 表示 6.37%，联接基金的残余持仓则是 "0.52" = 0.52%。
    # 显式声明后，小于 1 的百分数不会再被误判成小数（详见 models.WeightUnit）。
    weight_unit = WeightUnit.PERCENT

    def fetch_holdings(self, fund_code: str) -> HoldingsSnapshot:
        """请求 + 解析 + 组装快照。任何环节失败都抛 DataSourceError 以便换源。"""
        payload = self._request(fund_code)
        holdings = self._parse(payload, fund_code)
        # 报告期直接来自接口的 Expansion 字段（形如 2026-09-30），无需从文本里猜。
        report_date = str(payload.get("Expansion") or "").strip() or None

        return HoldingsSnapshot(
            fund_code=fund_code,
            # 只保留前十大：季报披露口径就是前十，多取也无意义。
            holdings=tuple(holdings[:10]),
            # 秒级时间戳，作为 holdings_snapshot 的幂等键之一（与 fund_code 联合）。
            snapshot_time=datetime.now().replace(microsecond=0).isoformat(),
            report_date=report_date,
            source=f"{self.name}:mobile",
        )

    def _request(self, fund_code: str) -> dict:
        """发起请求并做业务错误判定（HTTP 200 也可能是业务失败）。"""
        params = {
            "FCODE": fund_code,
            # 以下四个参数是移动端约定值，缺任何一个都可能拿到"网络繁忙"。
            "deviceid": "Wap",
            "plat": "WAP",
            "product": "EFund",
            "version": "2.0.0",
        }
        headers = {
            "User-Agent": MOBILE_USER_AGENT,
            "Referer": "https://h5.1234567.com.cn/",
        }
        try:
            payload = self._http.get_json(self.MOBILE_API, params=params, headers=headers)
        except Exception as exc:
            raise DataSourceError(self.name, f"{fund_code} 移动端接口请求失败: {exc}") from exc

        if not isinstance(payload, dict):
            raise DataSourceError(self.name, f"{fund_code} 移动端接口返回格式异常")
        # 业务错误码：ErrCode 为 0 或缺失表示成功，Success=False 亦视为失败。
        if payload.get("Success") is False or payload.get("ErrCode") not in (0, None):
            message = payload.get("ErrMsg") or payload.get("ErrorMessage") or "未知错误"
            raise DataSourceError(
                self.name, f"{fund_code} 移动端接口返回错误({payload.get('ErrCode')}): {message}"
            )
        return payload

    def _parse(self, payload: dict, fund_code: str) -> list[Holding]:
        """把 `Datas.fundStocks[]` 映射为 Holding 列表，并按权重降序。"""
        datas = payload.get("Datas") or {}
        stocks = datas.get("fundStocks") if isinstance(datas, dict) else None
        if not stocks:
            # 指数/债券基金/联接基金的持仓不在 fundStocks 里，属于正常的"无股票持仓"，
            # 但本源无法表达，只能报错让上层换源或走 fallback_etf。
            raise DataSourceError(self.name, f"{fund_code} 移动端接口无 fundStocks 数据")

        holdings: list[Holding] = []
        for item in stocks:
            if not isinstance(item, dict):
                continue
            # GPDM = 股票代码，可能带交易所后缀（如 00700），原样保存，
            # 后续由 synthetic 的别名映射负责与行情键对齐。
            code = str(item.get("GPDM") or "").strip()
            if not code:
                continue
            # JZBL 的口径由类属性 weight_unit 声明（百分数），这里不自行除以 100，
            # 保证"单位"只在一处定义。
            weight = self._weight_to_decimal(item.get("JZBL"))
            if weight <= 0:
                # 权重缺失的持仓无法参与加权，计入分母只会引入噪声。
                continue
            holdings.append(
                Holding(
                    asset_code=code,
                    asset_name=str(item.get("GPJC") or "").strip(),
                    weight=weight,
                    asset_type=AssetType.STOCK,
                )
            )

        # 排序保证"前十大"语义，也便于人工核对。
        holdings.sort(key=lambda h: h.weight, reverse=True)
        # 空列表会被 _require_holdings 升级为异常，触发换源。
        return self._require_holdings(fund_code, holdings, self.name)
