"""会话令牌（设计方案 12.7）的守卫。

这组测试存在的理由很具体：**没有它，鉴权是可以被悄悄拆掉的。**
12.7 让接口能写 App Secret，令牌是那道门。如果哪天有人为了「让测试变绿」
把 ``_authorized`` 放宽，测试套件会一片绿 —— 除非这里挡着。

所以覆盖的是**行为**（不带令牌打不进去），不是实现细节：
断言「401」而不是断言「调了某个函数」。
"""

from __future__ import annotations

import json
import re
import threading
import time
from http.client import HTTPConnection, HTTPException
from typing import Any

import pytest

from freeagent.app import App
from freeagent.web.server import create_server
from freeagent.web.session import (
    PUBLIC_API_PATHS,
    SESSION_HEADER,
    SESSION_TOKEN,
    inject_token,
    make_token,
    token_matches,
)

#: 页面里令牌被注入成这一行（见 session.inject_token）
_TOKEN_IN_PAGE = re.compile(r'window\.__FREEAGENT_SESSION__="([^"]*)"')


def injected_value(html: str) -> str:
    """取出页面里注入的令牌值。

    找不到就**明确失败**。直接 ``.search(...).group(1)`` 的话，注入一旦失效
    这里会抛 ``AttributeError: 'NoneType'``，报错信息完全指不到真正的原因。
    """
    found = _TOKEN_IN_PAGE.search(html)
    assert found is not None, "页面里没有注入令牌"
    return found.group(1)


@pytest.fixture()
def served(app: App):
    """起一个真实服务器 —— 鉴权只有走真 HTTP 才算数。"""
    server = create_server(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def request(
    addr: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    token: str | None = None,
) -> tuple[int, str]:
    """发一次 HTTP 请求，拿到 ``(状态码, 响应体)``。

    **为什么这里有重试，但只重试「连接」阶段。**

    这条辅助函数被 POST 用，而 POST **有副作用**（会建事务、改状态）。
    所以「传输层失败就重发」是**不安全**的：请求可能**已经被服务端处理了**
    （实测抓到的失败现场里，服务端日志明明记了 ``401``，而客户端在读状态行
    的那一层炸了）—— 那时重发就是**做两次事**。

    于是分两段：
    - **连接阶段**（还没发出任何字节）→ 失败就是「没连上」，重试**无副作用**，
      允许重试。全量跑时端口/accept 偶发慢，这是唯一安全可重试的地方。
    - **已发出之后**（读状态行/读 body 失败）→ **绝不重发**，直接抛出，
      并把方法、路径、阶段说清楚。

    之前这个函数既不区分阶段也不重试，于是那次偶发失败表现为
    ``http.client`` 里一堆栈、看不出是哪一步 —— 定位花的时间远超写这个函数。

    顺带说明为什么**不加服务端改动**：``create_server`` 已经是
    ``ThreadingHTTPServer``（多线程），所以「单线程队头阻塞」这个最常见的
    猜测不成立（我查过）。真要治本得看那次运行的负载，不是往测试里塞 sleep。
    """
    host, port = addr.split(":")
    headers: dict[str, str] = {}
    payload = None
    if body is not None:
        payload = json.dumps(body).encode()
        headers |= {
            "Content-Type": "application/json",
            "Content-Length": str(len(payload)),
        }
    if token is not None:
        headers[SESSION_HEADER] = token

    # 阶段一：连接。失败可安全重试（没发出任何字节）。
    last_connect: Exception | None = None
    for attempt in range(3):
        conn = HTTPConnection(host, int(port), timeout=5)
        try:
            conn.connect()
        except OSError as exc:
            last_connect = exc
            conn.close()
            time.sleep(0.2 * (attempt + 1))
            continue
        # 阶段二：已连上。**此后任何失败都不重发** —— 请求可能已被处理。
        try:
            conn.request(method, path, body=payload, headers=headers)
            res = conn.getresponse()
            return res.status, res.read().decode("utf-8")
        except (OSError, HTTPException) as exc:
            raise AssertionError(
                f"{method} {path} 在**已连接、已发出**之后失败："
                f"{type(exc).__name__}: {exc}。"
                "这一段不重试（请求可能已被服务端处理，重发会做两次事）。"
                "若是偶发，检查当时的机器负载 / 服务端是否有别的线程在等锁。"
            ) from exc
        finally:
            conn.close()
    raise AssertionError(
        f"{method} {path} 连不上 {addr}（重试 3 次）："
        f"{type(last_connect).__name__}: {last_connect}"
    )


# --- 令牌本身 ------------------------------------------------------------ #


def test_token_is_32_bytes_of_entropy():
    """32 字节 → urlsafe 编码 43 字符。少于此就是熵不够。"""
    assert len(SESSION_TOKEN) == 43


def test_make_token_is_not_the_module_token():
    """每次现生成 —— 复用模块级那个就等于「令牌是常量」。"""
    assert make_token() != SESSION_TOKEN
    assert len(make_token()) == 43


@pytest.mark.parametrize("supplied", [None, "", "wrong", SESSION_TOKEN + "x", SESSION_TOKEN[:-1]])
def test_token_rejects_everything_that_is_not_it(supplied):
    """空值一律判否 —— 不做「空值放行」。"""
    assert token_matches(supplied) is False


def test_token_accepts_exact_match():
    assert token_matches(SESSION_TOKEN) is True


def test_token_comparison_is_constant_time():
    """用 compare_digest，不退回 ``==``（见 session 模块说明）。"""
    import hmac
    import inspect

    src = inspect.getsource(token_matches)
    assert "compare_digest" in src
    assert "==" not in src
    # 顺带确认真的能比对（别写出一个永远返回 False 的实现）
    assert hmac.compare_digest(b"a", b"a")


def test_expected_can_be_overridden_for_tests():
    """``expected`` 参数让测试不用去改模块级全局。"""
    assert token_matches("mine", "mine") is True
    assert token_matches("mine", "yours") is False


# --- 免鉴权白名单 --------------------------------------------------------- #


def test_health_is_the_only_public_path():
    """白名单刻意最小：加东西进来得有人写下理由。"""
    assert set(PUBLIC_API_PATHS) == {"/api/health"}


def test_health_is_reachable_without_token(served):
    status, _ = request(served, "GET", "/api/health")
    assert status == 200


# --- 未授权一律拒 --------------------------------------------------------- #


@pytest.mark.parametrize(
    "path", ["/api/today", "/api/all", "/api/roles", "/api/settings", "/api/task/x"]
)
def test_read_apis_reject_missing_token(served, path):
    status, body = request(served, "GET", path)
    assert status == 401
    assert "令牌" in json.loads(body)["error"]


@pytest.mark.parametrize(
    "path", ["/api/task", "/api/settings", "/api/chat", "/api/vision", "/api/task/x/start"]
)
def test_write_apis_reject_missing_token(served, path):
    """POST 一律要令牌。"""
    status, _ = request(served, "POST", path, body={})
    assert status == 401


def test_read_apis_reject_wrong_token(served):
    status, _ = request(served, "GET", "/api/today", token="nope")
    assert status == 401


def test_read_apis_accept_right_token(served):
    status, _ = request(served, "GET", "/api/today", token=SESSION_TOKEN)
    assert status == 200


def test_write_api_accepts_right_token(served, roles):
    """带对令牌就该真的能写 —— 不然就成了一律拒绝的摆设。"""
    status, _ = request(
        served,
        "POST",
        "/api/task",
        body={"title": "带令牌才写得进去", "role_ids": [roles["work"].id]},
        token=SESSION_TOKEN,
    )
    assert status == 201


def test_empty_header_value_is_rejected(served):
    """「传了 header 但值是空」和「没传」必须同判，否则能绕过。"""
    status, _ = request(served, "GET", "/api/today", token="")
    assert status == 401


# --- 这条是本文件的核心 ---------------------------------------------------- #


def test_post_never_uses_the_read_whitelist(served):
    """**POST 不能因为路径在只读白名单里就免鉴权。**

    回归：``_post`` 曾复用 ``_authorized``，而那张名单里就有
    ``/api/health`` —— 于是 ``POST /api/health`` 会带着「只读端点」的
    身份跳过令牌校验。当时它还没有副作用，所以看不出问题；可那是巧合，
    不是设计。这条测试钉住的是「白名单只给 GET」这条规则本身。
    """
    for path in PUBLIC_API_PATHS:
        status, _ = request(served, "POST", path, body={})
        assert status == 401, f"POST {path} 竟然免了鉴权"


def test_page_itself_needs_no_token(served):
    """页面必须能取到 —— 令牌是从页面里注入的，要求它先有令牌是死循环。"""
    status, _ = request(served, "GET", "/")
    assert status == 200


# --- 注入 ---------------------------------------------------------------- #


def test_page_carries_this_process_token(served):
    """页面里是**这个进程**的令牌，不是别的进程的。"""
    status, html = request(served, "GET", "/")
    assert status == 200
    # 拿不到的话界面将发不出任何请求 —— 那时报错比静默 401 好。
    assert injected_value(html) == SESSION_TOKEN


def test_injection_keeps_a_single_script_block():
    """守卫要求「脚本恰好一块」——注入不能另开一块。"""
    html = "<html><head><title>t</title></head><body><script>\nvar a=1;\n</script>\n</body></html>"
    out = inject_token(html, "tok")
    assert out.count("<script>") == 1
    assert out.count("</script>") == 1
    assert 'window.__FREEAGENT_SESSION__="tok";' in out


def test_injection_lands_before_any_request_making_code():
    """令牌必须早于可能发请求的代码就位，否则首屏就 401。"""
    html = "<script>\napi('/api/today');\n</script>"
    out = inject_token(html, "tok")
    assert out.index("__FREEAGENT_SESSION__") < out.index("api('/api/today')")


@pytest.mark.parametrize("hostile", ['x";alert(1);"', "x</script><script>y"])
def test_injection_escapes_quotes_and_tags(hostile):
    """转义不能省：「当前值恰好安全」不是「永远安全」的理由。

    断的是**性质**（注入进去的值里不含裸引号/尖括号），不是某种转义写法 ——
    换一种同样正确的转义实现不该让这条测试变红。
    """
    out = inject_token("<script>var a=1;</script>", hostile)
    value = injected_value(out)
    for bad in ('"', "<", ">"):
        assert bad not in value, f"注入的值里混进了裸的 {bad!r}，等于让它逃出字符串"
    # 整体上也不能拼出第二个脚本块
    assert out.count("<script>") == 1


def test_injection_survives_a_page_without_head():
    """结构变了也别静默失效 —— 宁可多一块脚本。"""
    out = inject_token("<div>no head no script</div>", "tok")
    assert 'window.__FREEAGENT_SESSION__="tok";' in out


def test_static_page_template_has_no_token_value():
    """模块级的 ``INDEX_HTML`` **不含任何令牌值**。

    ``js_core`` 里出现 ``window.__FREEAGENT_SESSION__`` 是**应该的** ——
    界面要读它才能发请求。要禁的是「赋值」：令牌只在响应时注入，
    留在源码里等于「令牌是编译期常量」，全进程共用一个。
    """
    from freeagent.web.page import INDEX_HTML

    assert not re.search(r'__FREEAGENT_SESSION__\s*=\s*"', INDEX_HTML)
    assert SESSION_TOKEN not in INDEX_HTML
