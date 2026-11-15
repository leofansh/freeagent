"""在线检索可选腿（3.7.9 / V1.105）：**本地真的空手时，才允许问一句外面**。

## 为什么是「可选腿」而不是「联网检索」

本仓的硬性质之一是**断网照常能用**（README；现状速读第 3 条）。向量那条腿
（``VECTOR_UNAVAILABLE``）就是被这条挡掉的：embedding 要联网。所以在线检索
不能是一条「自动的」腿 —— 那样每次检索都可能在网络上卡到超时，断网时更是
每次都白等一遍，于是「断网可用」从一句话变成了一个愿望。

因此：**默认关闭**，只有用户显式配了端点才启用。默认安装与本版之前**行为
完全相同**（多的只是这段代码的存在），这是这一刀唯一能接受的形态 ——
九刀里它排最后，压它的正是这条。

## 什么时候才发出去

只有**本地关键词一条都没命中**（``bm25_hits == 0``）才问外部。

判据必须是可观察的事实，不是「感觉本地不够」：「本地有，只是没排上前几名」
不该出去，「本地一条都没有」才是真的空手。这条边界同时让留痕（3.7.7）与
外部调用对齐 —— ``legs='online'`` 的语义就是「这次本地空手，外面补上了」，
而不是「这次用了在线检索」。

## 发出去的东西必须说出来

查询原文离开本机是**一次不可撤回的披露**。所以：

- 每次真的发出去，呈现层必须写明**发往哪个主机**（:attr:`OnlineLeg.endpoint_host`）；
- 端点由用户配，本仓**不内置任何搜索服务**（不选边、不代持 key、不留默认值）；
- 回来的东西**单列一块**，标「外部材料，未核实」，**不与本地条目混排**。
  本地条目是「你记过的」，外部材料是「网上有个人这么说」—— 两类证据的可信度
  不同，混在一起就是让 V1.104 刚分开的东西重新糊上。

## 不自动喂给模型（本版刻意的边界）

只做到「摆到人眼前」，不做「替人塞进 ``/ask`` 的上下文」。两条理由：

1. **注入面**：外部文本进 prompt 就是 V1.93 那个问题 —— 「引文是数据不是指令」
   只解决了标记，不解决内容；
2. **会让凭据档说谎**：模型读到一段网上文字，不是「查过本机」，但人在
   V1.104 的档位旁边看到「确定」两个字时不会想这么多。

要做也该单开一刀：带标记、带档位联动、带「这一段来自外部」的显式声明。
**能在这一步之前做完的事，就不要在这一步顺手做掉。**

## 响应形状：只认结构化，不解析散文

与 11.10.2（能力探针）同一条立场：**解析自由文本就是在猜**。所以端点必须
返回 JSON，本模块只认容器里的列表与几个常见字段别名（``title``/``url``/
``snippet`` 一族，见 :data:`RESULT_KEYS` 等），JSON 里 SearXNG 那种
``{"results": [...]}`` 开箱即用。

**认不出就如实说认不出**（「返回的 JSON 里找不到 results/items 这类列表」），
不猜、不硬凑、更不把整页 HTML 当摘要塞给人看 —— 那种「有总比没有好」的
做法，人只会得到一堆噪声，然后不再相信这个功能。
"""

from __future__ import annotations

import dataclasses
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Mapping

__all__ = [
    "DEFAULT_MAX_ITEMS",
    "DEFAULT_TIMEOUT",
    "MAX_BODY_BYTES",
    "MAX_SNIPPET_CHARS",
    "NOT_ENABLED_REASON",
    "ONLINE_MAX_ENV",
    "ONLINE_TIMEOUT_ENV",
    "ONLINE_URL_ENV",
    "OnlineFetchError",
    "OnlineLeg",
    "OnlineSearchConfig",
    "OnlineSnippet",
    "OnlineTransport",
    "online_search",
    "urllib_transport",
]

#: 端点模板。必须含 ``{query}`` 占位符 —— 缺了就报配置错，不替用户猜该把
#: 查询拼在哪（拼错的表现是「永远搜到不相干的东西」，最难查的那类）。
ONLINE_URL_ENV = "FREEAGENT_ONLINE_SEARCH_URL"
ONLINE_TIMEOUT_ENV = "FREEAGENT_ONLINE_SEARCH_TIMEOUT"
ONLINE_MAX_ENV = "FREEAGENT_ONLINE_SEARCH_MAX"

#: 没配端点时的说明。与 ``VECTOR_UNAVAILABLE`` 同一条纪律：没走的路要能
#: 说出为什么，而不是留个空字段让人以为「没搜到」。
NOT_ENABLED_REASON = "未启用（默认关闭：本仓默认不联网）"

DEFAULT_TIMEOUT = 8.0
DEFAULT_MAX_ITEMS = 5

#: 单条摘要的展示上限。外部摘要动辄几千字，照抄进终端会把人的视线冲掉；
#: 截断**留标记**（与 3.6 的 ``truncated`` 同一条纪律）。
MAX_SNIPPET_CHARS = 300

#: 响应体读取上限。端点返回一页 HTML 时不该把内存吃掉。
MAX_BODY_BYTES = 512 * 1024

#: 查询在 URL 里的占位符。
QUERY_PLACEHOLDER = "{query}"

#: 结果列表可能挂的键名（SearXNG 是 ``results``，别家常见的还有几个）。
LIST_KEYS: tuple[str, ...] = ("results", "items", "data", "organic", "hits")
TITLE_KEYS: tuple[str, ...] = ("title", "name", "heading")
URL_KEYS: tuple[str, ...] = ("url", "link", "href")
SNIPPET_KEYS: tuple[str, ...] = (
    "snippet", "content", "description", "summary", "body", "text",
)

USER_AGENT = "FreeAgent/1.0 (+personal agent, opt-in online lookup)"


class OnlineFetchError(RuntimeError):
    """取不回来（HTTP 非 2xx / 响应读中断）。

    网络层的连接失败按 ``OSError`` 抛（``urllib`` 的既有约定），本类只收
    「连上了但没拿到」那类 —— 两者的自报文案不同，人要做的事也不同
    （一个是查网络/端点，一个是查端点本身）。
    """


@dataclasses.dataclass(frozen=True, slots=True)
class OnlineSnippet:
    """一条外部结果。**不是** :class:`~freeagent.services.knowledge.KnowledgeHit`
    —— 它没有条目 id、没有记进任何表，也不该长得像本地条目。"""

    title: str = ""
    url: str = ""
    snippet: str = ""

    @property
    def headline(self) -> str:
        """一行标题：有标题用标题，没有就用 URL，两者都没有就没得显示。"""
        return self.title or self.url


@dataclasses.dataclass(frozen=True, slots=True)
class OnlineLeg:
    """这一次在线腿的**完整交代**：走没走、发到哪、拿回了什么、为什么没拿到。

    默认值就是「未启用」—— 于是调用方不需要判空，也不需要知道配置长什么样。
    """

    #: 用户配了端点没有（``False`` = 这条路压根没开，不是失败）。
    enabled: bool = False
    #: 这次真的把查询发出去了没有。**发出去就必须被披露**。
    attempted: bool = False
    snippets: tuple[OnlineSnippet, ...] = ()
    #: 没拿到结果时的原因；拿到了就是空串。
    reason: str = NOT_ENABLED_REASON
    #: 查询发往的主机（只有真的发出去时才有值）。
    endpoint_host: str = ""

    @property
    def used(self) -> bool:
        """这次是否**靠它拿到了东西**。留痕的 ``legs='online'`` 用它判。"""
        return bool(self.snippets)


@dataclasses.dataclass(frozen=True, slots=True)
class OnlineSearchConfig:
    """端点配置。**没有默认端点** —— 这是刻意的，见模块说明。"""

    url_template: str = ""
    timeout: float = DEFAULT_TIMEOUT
    max_items: int = DEFAULT_MAX_ITEMS
    #: 配了但配错了（缺占位符 / 数字不是数字）。非空即不发出任何请求。
    error: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.url_template.strip())

    @property
    def host(self) -> str:
        """端点主机。展示「话会发到哪」用 —— 不含账号密码，不含端口。"""
        return _host_of(self.url_template)

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None
    ) -> "OnlineSearchConfig":
        """从环境变量读（每次调用现读，不缓存）。

        现读而不是构造时读一次：改配置不用重启，而且**测试与实跑看的是同一
        份判断**。显式 ``env`` 形参让测试不必 monkeypatch 进程环境
        （同 ``feishu.config.load_feishu_config``）。
        """
        source = os.environ if env is None else env
        url = (source.get(ONLINE_URL_ENV) or "").strip()
        timeout, timeout_error = _read_number(
            source.get(ONLINE_TIMEOUT_ENV), DEFAULT_TIMEOUT, float,
            ONLINE_TIMEOUT_ENV,
        )
        max_items, max_error = _read_number(
            source.get(ONLINE_MAX_ENV), DEFAULT_MAX_ITEMS, int, ONLINE_MAX_ENV,
        )
        errors = [e for e in (timeout_error, max_error) if e]
        if url and QUERY_PLACEHOLDER not in url:
            errors.append(
                f"端点模板里没有 {QUERY_PLACEHOLDER} 占位符，不知道该把查询放哪"
            )
        return cls(
            url_template=url,
            timeout=timeout,
            max_items=max(1, int(max_items)),
            error="；".join(errors),
        )

    def build_url(self, query: str) -> str:
        return self.url_template.replace(
            QUERY_PLACEHOLDER, urllib.parse.quote(query, safe="")
        )


OnlineTransport = Callable[[str, float], str]
"""``(url, timeout) -> 响应体文本``。可注入 —— 测试因此**永不联网**。"""


def _read_number(
    raw: str | None, default: float, kind: Callable[[str], float], env_name: str
):
    """读一个正数。**读不出来就报出来**，不悄悄用默认值假装配好了。"""
    text = (raw or "").strip()
    if not text:
        return default, ""
    try:
        value = kind(text)
    except ValueError:
        return default, f"{env_name} 不是数字（{text!r}），仍按 {default:g}"
    if value <= 0:
        return default, f"{env_name} 要正数（{text!r}），仍按 {default:g}"
    return value, ""


def urllib_transport(url: str, timeout: float) -> str:
    """默认传输：一个 GET。零依赖（``urllib`` 是标准库，同飞书发信）。"""
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT}, method="GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read(MAX_BODY_BYTES).decode(charset, errors="replace")
    except urllib.error.HTTPError as exc:
        # 端点活着但拒绝了：状态码是唯一有用的信息，别把整页错误页带出来。
        raise OnlineFetchError(f"HTTP {exc.code}") from exc


def _host_of(url: str) -> str:
    """URL 的主机名（不含账号密码、不含端口）。披露用，只取必要的。"""
    try:
        return urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return ""


def online_search(
    query: str,
    *,
    config: OnlineSearchConfig | None = None,
    transport: OnlineTransport | None = None,
) -> OnlineLeg:
    """问一次外部。**任何失败都不抛**，全部落进返回值的 ``reason``。

    检索是辅助能力（同 ``MemoryService.retrieve`` 的既有纪律）：外部端点是
    别人家的服务，它今天活着明天可能不在，不该让一条 ``/memory search`` 崩掉。
    但**失败必须自报** —— 静默失败会把「端点坏了」显示成「网上也没有」。
    """
    cfg = config if config is not None else OnlineSearchConfig.from_env()
    if not cfg.enabled:
        return OnlineLeg()
    if cfg.error:
        return OnlineLeg(enabled=True, reason=f"配置有问题：{cfg.error}")

    text = (query or "").strip()
    if not text:
        # 没有查询词还发出去，等于向外部端点发了一个空请求：既无意义，
        # 也白白暴露「这台机器正在用某个端点」。
        return OnlineLeg(enabled=True, reason="没有查询词，没发出去")

    url = cfg.build_url(text)
    host = _host_of(url)
    fetch = transport if transport is not None else urllib_transport
    try:
        body = fetch(url, cfg.timeout)
    except (OSError, OnlineFetchError) as exc:
        return OnlineLeg(
            enabled=True, attempted=True, endpoint_host=host,
            reason=f"已发往 {host}，但没取回：{exc}",
        )

    snippets, problem = parse_response(body, limit=cfg.max_items)
    if problem:
        return OnlineLeg(
            enabled=True, attempted=True, endpoint_host=host,
            reason=f"已发往 {host}，{problem}",
        )
    if not snippets:
        return OnlineLeg(
            enabled=True, attempted=True, endpoint_host=host,
            reason=f"已发往 {host}，它也没找到",
        )
    return OnlineLeg(
        enabled=True,
        attempted=True,
        snippets=tuple(snippets),
        # 成功时必须**显式清空** ``reason``：它的默认值是「未启用」，忘了清
        # 就会得到一条「拿到了 3 条结果，原因是未启用」的鬼话。测试守着这条。
        reason="",
        endpoint_host=host,
    )


def parse_response(body: str, *, limit: int = DEFAULT_MAX_ITEMS):
    """把响应体解成 ``(snippets, problem)``。``problem`` 非空即「看不懂」。

    返回二元组而不是抛：调用方需要的是「为什么看不懂」这句人话，不是栈。
    """
    try:
        data = json.loads(body)
    except (ValueError, TypeError) as exc:
        # 不把响应体前几十字贴出来：它可能是整页 HTML，贴出来只会让人更困惑。
        return [], f"返回的不是 JSON（{exc}）"

    rows = _result_rows(data)
    if rows is None:
        return [], "返回的 JSON 里找不到 results/items 这类列表"

    out: list[OnlineSnippet] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = _first_str(row, TITLE_KEYS)
        url = _first_str(row, URL_KEYS)
        snippet = _first_str(row, SNIPPET_KEYS)
        if not (title or url or snippet):
            continue
        out.append(
            OnlineSnippet(
                title=title,
                url=url,
                snippet=_clip(snippet, MAX_SNIPPET_CHARS),
            )
        )
        if len(out) >= max(1, int(limit)):
            break

    if not out and rows:
        # 有东西但读不出任何一个字段：这是**形状不认识**，不是「没找到」。
        # 两者混起来会让人去换端点，而真正该做的是看端点返回什么形状。
        return [], f"有 {len(rows)} 条结果，但读不出标题/链接/摘要"
    return out, ""


def _result_rows(data: object) -> list[object] | None:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in LIST_KEYS:
            value = data.get(key)
            if isinstance(value, list):
                return value
    return None


def _first_str(row: Mapping[str, object], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "…"
