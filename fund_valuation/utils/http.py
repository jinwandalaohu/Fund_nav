"""HTTP 访问层：集中管理 UA、超时、重试、编码与代码归一化。

所有数据源必须通过本模块发请求，禁止各自调用 requests.get —— 否则超时/重试/UA
策略会散落各处，接口口径一变就要改 N 个文件。

本模块对外提供四类能力：
1. HttpClient     带重试与线程隔离 Session 的 GET 封装（只出文本/JSON，不解析业务）
2. strip_jsonp    剥离 JSONP / JS 变量包装，把响应还原成纯 JSON
3. normalize_code 代码归一化为 `sh600519` 形式（行情接口的通用键）
4. safe_float     容错的数值转换（接口普遍用 "" / "--" 表示空值）
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Mapping, Sequence

import requests

from .logger import get_logger

LOGGER = get_logger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 天天基金 / 东方财富 / 新浪部分接口会校验来源页，缺失时返回空内容或 403。
# 这里只放通用头，Referer 因接口而异，由各数据源在调用时覆盖。
DEFAULT_HEADERS: dict[str, str] = {
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
}

# JSONP 形如 `callback({"a":1});`，取括号内的内容。
_JSONP_PATTERN = re.compile(r"^[^(]*\((.*)\)\s*;?\s*$", re.DOTALL)
# 代码拆解：可选市场前缀 + 剩余部分（剩余部分后续再按长度/字符类型判断市场）。
_CODE_PATTERN = re.compile(r"^(?P<prefix>sh|sz|bj|hk|us)?(?P<body>.+)$", re.IGNORECASE)


class HttpError(RuntimeError):
    """网络层错误（连接失败、超时、非 2xx、内容为空）。

    继承 RuntimeError 而非 Exception 的裸用，是为了让"可重试的临时故障"与
    "程序缺陷"在 except 子句上容易区分。
    """

    def __init__(self, url: str, message: str, status_code: int | None = None) -> None:
        """把 URL 与状态码一并带上，否则日志里只剩"请求失败"无法定位是哪个接口。"""
        super().__init__(f"{message} [url={url}]")
        self.url = url
        self.status_code = status_code


class HttpClient:
    """带重试的轻量 HTTP 客户端。

    - Session 按线程隔离：requests.Session 并非线程安全，估值阶段会并发抓行情。
    - 仅对连接错误/超时/5xx 重试；4xx 属于请求本身有问题，重试只会放大故障。
    """

    def __init__(
        self,
        timeout: float = 8.0,
        retries: int = 2,
        user_agent: str = DEFAULT_USER_AGENT,
        backoff: float = 0.4,
    ) -> None:
        """所有参数都应来自 RuntimeConfig，不要在数据源里另设一套默认值。"""
        self.timeout = timeout
        # 负数无意义，统一夹到 0，避免下方 range(self.retries + 1) 变成空循环。
        self.retries = max(0, retries)
        self.backoff = backoff
        self._headers = {**DEFAULT_HEADERS, "User-Agent": user_agent or DEFAULT_USER_AGENT}
        # threading.local 让每个线程各自持有一个 Session，见 _session()。
        self._local = threading.local()

    def _session(self) -> requests.Session:
        """取当前线程的 Session，不存在则创建。

        复用 Session 的目的是连接池与 Cookie 保持，减少盘中高频轮询的握手开销；
        按线程隔离则是因为 Session 内部的连接池不保证线程安全。
        """
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(self._headers)
            self._local.session = session
        return session

    def get_text(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        encoding: str | None = None,
    ) -> str:
        """获取文本响应。

        encoding 显式指定时优先使用（新浪/腾讯为 GBK，靠 apparent_encoding 猜会乱码）；
        未指定时对 ISO-8859-1 这类默认值用内容探测结果覆盖，避免中文变问号。
        """
        # _request 负责重试与状态码判定，这里只关心"拿到文本之后怎么解码"。
        response = self._request(url, params=params, headers=headers)
        if encoding:
            # 已知编码的接口（GBK）必须显式指定：requests 对无 charset 的响应
            # 默认按 ISO-8859-1 解码，中文会整段变成乱码。
            response.encoding = encoding
        elif not response.encoding or response.encoding.lower() == "iso-8859-1":
            # 未指定且是默认值时用探测结果；探测失败兜底 utf-8。
            response.encoding = response.apparent_encoding or "utf-8"
        text = response.text
        if not text or not text.strip():
            # 空响应在业务上等于"该接口没有数据"，必须显式报错，否则下游会当成
            # 解析失败，错误信息反而更难定位。
            raise HttpError(url, "响应内容为空")
        return text

    def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        encoding: str | None = "utf-8",
    ) -> Any:
        """获取 JSON 响应，自动剥离 JSONP 包装。"""
        text = self.get_text(url, params=params, headers=headers, encoding=encoding)
        payload = strip_jsonp(text)
        try:
            return __import__("json").loads(payload)
        except ValueError as exc:
            # 带上内容片段，接口返回 HTML 报错页时能立刻看出来。
            preview = payload[:120].replace("\n", " ")
            raise HttpError(url, f"JSON 解析失败: {exc}; 内容片段: {preview}") from exc

    def _request(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None,
        headers: Mapping[str, str] | None,
    ) -> requests.Response:
        """真正的请求 + 重试循环。

        重试判定标准：
        - 连接错误 / 超时 → 重试（网络抖动，重试大概率成功）
        - 5xx            → 重试（服务端临时故障）
        - 4xx            → 不重试，直接抛（参数/鉴权写错了，重试无意义）
        """
        merged_headers = dict(headers) if headers else None
        last_error: Exception | None = None

        # attempt 从 0 开始，总共尝试 retries + 1 次（首次 + N 次重试）。
        for attempt in range(self.retries + 1):
            try:
                response = self._session().get(
                    url,
                    params=params,
                    headers=merged_headers,
                    timeout=self.timeout,
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                # 这两类是典型的瞬时故障，记下来继续下一轮。
                last_error = exc
                LOGGER.debug("请求失败(第 %d 次): %s %s", attempt + 1, type(exc).__name__, url)
            except requests.RequestException as exc:  # 其余 requests 异常不重试
                # 例如 URL 非法、SSL 证书错误：重试不会改变结果。
                raise HttpError(url, f"请求异常: {exc}") from exc
            else:
                if response.status_code >= 500:
                    last_error = HttpError(url, "服务端错误", response.status_code)
                    LOGGER.debug("服务端 %s(第 %d 次): %s", response.status_code, attempt + 1, url)
                elif response.status_code >= 400:
                    raise HttpError(url, "客户端错误", response.status_code)
                else:
                    return response

            if attempt < self.retries:
                # 指数退避：0.4s、0.8s、1.6s... 避免被限流时叠加压力。
                time.sleep(self.backoff * (2**attempt))

        raise HttpError(url, f"重试 {self.retries} 次后仍失败: {last_error}")


def strip_jsonp(text: str) -> str:
    """去掉 `callback({...})` 或 `var x = {...}` 包装，返回纯 JSON 文本。"""
    stripped = text.strip()
    match = _JSONP_PATTERN.match(stripped)
    if match:
        return match.group(1).strip()
    # 形如 `var apidata={...};` 的 JS 赋值语句，取等号右侧并去掉结尾分号。
    if stripped.startswith("var ") and "=" in stripped:
        return stripped.split("=", 1)[1].strip().rstrip(";")
    return stripped


def normalize_code(raw: str) -> str:
    """把基金/股票代码归一化为带市场前缀的形式（腾讯、新浪行情接口均可用）。

    规则（与主流财经接口一致）：
    - 6 位纯数字：6/9 开头为沪市 sh，4/8 开头为北交所 bj，其余（0/3）为深市 sz
    - 5 位纯数字：港股 hk
    - 已带 sh/sz/bj/hk/us 前缀：原样返回（前缀小写）
    - 字母代码：美股 us
    - 700.HK / 600519.SH 这类后缀式：剥离后缀后按上述规则处理

    这个函数是"持仓代码 ↔ 行情键"之间唯一的桥。持仓表里存的是 `00700`，
    行情字典的键是 `hk00700`，少了这层转换，查表会全部落空。
    """
    code = str(raw).strip()
    if not code:
        raise ValueError("代码不能为空")

    # 先剥离 `.SH` / `.OF` 之类的后缀写法，后面统一按前缀规则处理。
    upper = code.upper()
    suffix = ""
    for candidate in (".SH", ".SZ", ".BJ", ".HK", ".US", ".OF"):
        if upper.endswith(candidate):
            code = code[: -len(candidate)]
            suffix = candidate
            break

    # 港股常用 700.HK / 0700.HK 这类写法，统一补齐 5 位——
    # 腾讯/新浪的港股键都是 hk00700 形式，位数不对会查不到。
    if suffix == ".HK" and code.strip().isdigit():
        return f"hk{code.strip().zfill(5)}"

    match = _CODE_PATTERN.match(code)
    if not match:
        raise ValueError(f"无法识别的代码: {raw!r}")
    prefix = (match.group("prefix") or "").lower()
    body = match.group("body").strip()

    if prefix:
        # 已带前缀，信任调用方；美股代码统一大写（usaapl → usAAPL）。
        return f"{prefix}{body.upper() if prefix == 'us' else body}"

    if body.isdigit():
        if len(body) == 6:
            if body[0] in "569":
                # 沪市：600/601/603/605/688 股票、5xx 场内基金与 ETF、900 B 股
                return f"sh{body}"
            if body[0] in "48":
                # 北交所：4xx/8xx（新股 920 开头后续如遇到可在此扩展）
                return f"bj{body}"
            if body[0] in "0123":
                # 深市：000/001/002/003 股票、1xx 基金与可转债、300 创业板
                return f"sz{body}"
            raise ValueError(f"无法识别的市场前缀: {raw!r}")
        if len(body) == 5:
            # 5 位纯数字只可能是港股（A 股均为 6 位）。
            return f"hk{body}"
        raise ValueError(f"无法识别的代码长度: {raw!r}")

    if body.isalpha():
        # 纯字母视为美股代码。
        return f"us{body.upper()}"

    raise ValueError(f"无法识别的代码: {raw!r}")


def split_code(code: str) -> tuple[str, str]:
    """拆分 `sh600519` 为 (市场, 数字代码)。"""
    normalized = normalize_code(code)
    # 美股前缀是两位，与其它市场一致，这里写成显式判断是为了说明意图。
    prefix = "us" if normalized.startswith("us") else normalized[:2]
    return prefix, normalized[len(prefix) :]


def chunked(items: Sequence[str], size: int) -> list[list[str]]:
    """按固定大小切分序列，size <= 0 时整体返回。

    批量接口都有单次请求上限（天天基金 50、腾讯 60），超出会静默截断——
    比报错更危险，因此必须由调用方显式分批。
    """
    if size <= 0:
        return [list(items)]
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


def safe_float(value: Any) -> float | None:
    """把接口返回的字符串/数字安全转 float。

    注意：`float(None)` 会抛异常而 `float('')` 也抛异常，但很多接口用 "" 表示空值，
    这里统一返回 None，交由调用方判断"空值"与"0"的区别（GSZ=0 与 GSZ 缺失语义不同）。
    """
    if value is None:
        return None
    # bool 是 int 的子类，若不显式拦截，True 会变成 1.0，属于静默错值。
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    # 接口常带 "9.53%" 或 "1,234" 这类装饰，去掉后再转。
    text = str(value).strip().replace("%", "").replace(",", "")
    if not text or text in {"-", "--", "null", "None", "NaN"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None
