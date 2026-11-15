"""在线检索可选腿（3.7.9 / V1.105）的测试。

分四组：
1. 开关与配置（默认关、端点模板、数字读不出来要报）；
2. 响应形状（只认结构化；认不出就说认不出，不猜）；
3. 失败自报（连不上 / HTTP 错 / 空结果，三种话说清）；
4. 披露（发往哪个 host 必须有值）。

**这一组测试永不联网**：所有用例要么用未启用的配置，要么注入 transport。
这是刻意的 —— 一个会联网的测试在 CI 上是「有时红有时绿」，在本机是「悄悄
用了你的网络」，两种都不该有。
"""

from __future__ import annotations

import json

import pytest

from freeagent.services.online_leg import (
    DEFAULT_MAX_ITEMS,
    DEFAULT_TIMEOUT,
    MAX_SNIPPET_CHARS,
    NOT_ENABLED_REASON,
    ONLINE_MAX_ENV,
    ONLINE_TIMEOUT_ENV,
    ONLINE_URL_ENV,
    OnlineFetchError,
    OnlineSearchConfig,
    online_search,
    parse_response,
)

URL = "https://search.example.com/s?q={query}&format=json"


def _cfg(**kw) -> OnlineSearchConfig:
    return OnlineSearchConfig(url_template=URL, **kw)


def _body(rows) -> str:
    return json.dumps({"results": rows}, ensure_ascii=False)


# -- 1. 开关与配置 ------------------------------------------------------------ #

class TestConfig:
    """默认关，且**没有默认端点** —— 这一刀的全部前提。"""

    def test_default_is_off(self):
        cfg = OnlineSearchConfig.from_env({})
        assert cfg.enabled is False
        assert cfg.timeout == DEFAULT_TIMEOUT
        assert cfg.max_items == DEFAULT_MAX_ITEMS
        assert cfg.error == ""

    def test_off_means_no_request_and_a_reason(self):
        """没启用**不是失败**，但也要能说出为什么没走这条路。"""
        leg = online_search("周报", config=OnlineSearchConfig.from_env({}))
        assert leg.enabled is False
        assert leg.attempted is False
        assert leg.reason == NOT_ENABLED_REASON
        assert leg.used is False

    def test_url_from_env(self):
        cfg = OnlineSearchConfig.from_env({ONLINE_URL_ENV: URL})
        assert cfg.enabled is True
        assert cfg.host == "search.example.com"

    def test_missing_placeholder_is_a_config_error(self):
        """缺 ``{query}`` 时不发请求 —— 猜「该拼在哪」的后果是永远搜不相干的东西。"""
        cfg = OnlineSearchConfig.from_env(
            {ONLINE_URL_ENV: "https://search.example.com/s"}
        )
        assert cfg.error
        assert "{query}" in cfg.error
        leg = online_search("周报", config=cfg)
        assert leg.attempted is False
        assert "配置有问题" in leg.reason

    def test_non_numeric_timeout_is_reported_not_swallowed(self):
        cfg = OnlineSearchConfig.from_env(
            {ONLINE_URL_ENV: URL, ONLINE_TIMEOUT_ENV: "八秒"}
        )
        assert cfg.timeout == DEFAULT_TIMEOUT
        assert ONLINE_TIMEOUT_ENV in cfg.error

    def test_negative_max_is_reported(self):
        cfg = OnlineSearchConfig.from_env({ONLINE_URL_ENV: URL, ONLINE_MAX_ENV: "-1"})
        assert ONLINE_MAX_ENV in cfg.error

    def test_query_is_url_encoded(self):
        url = _cfg().build_url("销售 周报&amp")
        assert " " not in url
        assert "销售" not in url       # 中文必须被百分号编码
        assert "%26amp" in url

    def test_host_has_no_credentials(self):
        cfg = OnlineSearchConfig(url_template="https://u:p@h.example.com/s?q={query}")
        assert cfg.host == "h.example.com"


# -- 2. 响应形状 -------------------------------------------------------------- #

class TestParse:
    def test_searxng_shape(self):
        snippets, problem = parse_response(
            _body([{"title": "标题", "url": "https://a/1", "content": "摘要"}])
        )
        assert problem == ""
        assert snippets[0].title == "标题"
        assert snippets[0].url == "https://a/1"
        assert snippets[0].snippet == "摘要"

    def test_field_aliases(self):
        snippets, problem = parse_response(
            _body([{"name": "n", "link": "https://a/2", "description": "d"}])
        )
        assert problem == ""
        assert (snippets[0].title, snippets[0].url, snippets[0].snippet) == (
            "n", "https://a/2", "d",
        )

    def test_top_level_list(self):
        snippets, problem = parse_response('[{"title": "t", "url": "u"}]')
        assert problem == ""
        assert len(snippets) == 1

    def test_not_json_says_so(self):
        snippets, problem = parse_response("<html>404</html>")
        assert snippets == []
        assert "不是 JSON" in problem

    def test_unknown_container_says_so(self):
        snippets, problem = parse_response('{"foo": 1}')
        assert snippets == []
        assert "results" in problem

    def test_rows_without_readable_fields_is_not_an_empty_result(self):
        """「有 3 条但读不出来」与「一条都没有」要人做的事不同 —— 不许合并。"""
        snippets, problem = parse_response(_body([{"a": 1}, {"b": 2}]))
        assert snippets == []
        assert "读不出" in problem

    def test_empty_list_is_a_legit_empty_result(self):
        snippets, problem = parse_response(_body([]))
        assert snippets == []
        assert problem == ""

    def test_long_snippet_is_clipped_with_a_marker(self):
        snippets, _ = parse_response(_body([{"title": "t", "snippet": "啊" * 500}]))
        assert snippets[0].snippet.endswith("…")
        assert len(snippets[0].snippet) == MAX_SNIPPET_CHARS + 1

    def test_limit_is_respected(self):
        rows = [{"title": f"t{i}"} for i in range(10)]
        snippets, _ = parse_response(_body(rows), limit=2)
        assert len(snippets) == 2


# -- 3. 失败自报 -------------------------------------------------------------- #

class TestFailures:
    def test_network_error_is_reported_with_host(self):
        def boom(url: str, timeout: float) -> str:
            raise OSError("timed out")

        leg = online_search("周报", config=_cfg(), transport=boom)
        assert leg.attempted is True
        assert leg.endpoint_host == "search.example.com"
        assert "没取回" in leg.reason

    def test_http_error_is_reported(self):
        def denied(url: str, timeout: float) -> str:
            raise OnlineFetchError("HTTP 403")

        leg = online_search("周报", config=_cfg(), transport=denied)
        assert "HTTP 403" in leg.reason

    def test_empty_external_result_is_stated(self):
        leg = online_search("周报", config=_cfg(), transport=lambda u, t: _body([]))
        assert leg.used is False
        assert "它也没找到" in leg.reason

    def test_blank_query_is_not_sent(self):
        """空查询发出去只会白暴露「这台机器在用这个端点」。"""
        def never(url: str, timeout: float) -> str:  # pragma: no cover
            raise AssertionError("不该发出去")

        leg = online_search("   ", config=_cfg(), transport=never)
        assert leg.attempted is False
        assert "没有查询词" in leg.reason

    def test_transport_receives_timeout(self):
        seen: list[float] = []

        def spy(url: str, timeout: float) -> str:
            seen.append(timeout)
            return _body([])

        online_search("周报", config=_cfg(timeout=3.5), transport=spy)
        assert seen == [3.5]


# -- 4. 成功路径 -------------------------------------------------------------- #

class TestSuccess:
    def test_used_means_got_something(self):
        leg = online_search(
            "周报", config=_cfg(), transport=lambda u, t: _body([{"title": "t"}])
        )
        assert leg.used is True
        assert leg.attempted is True
        assert leg.reason == ""
        assert leg.endpoint_host == "search.example.com"

    def test_query_reaches_the_transport(self):
        urls: list[str] = []

        def spy(url: str, timeout: float) -> str:
            urls.append(url)
            return _body([])

        online_search("销售周报", config=_cfg(), transport=spy)
        assert urls and urls[0].startswith("https://search.example.com/s?q=")

    def test_max_items_caps_snippets(self):
        rows = [{"title": f"t{i}"} for i in range(10)]
        leg = online_search(
            "周报", config=_cfg(max_items=2), transport=lambda u, t: _body(rows)
        )
        assert len(leg.snippets) == 2


@pytest.mark.parametrize("bad", ["", "   "])
def test_blank_url_template_is_off(bad):
    assert OnlineSearchConfig.from_env({ONLINE_URL_ENV: bad}).enabled is False
