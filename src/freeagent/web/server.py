"""本地 Web UI：stdlib ``http.server``，零框架零依赖。

安全边界（刻意保守）
--------------------
* **只绑 ``127.0.0.1``** —— 局域网/公网不可达。:func:`create_server` 直接拒绝。
* **接口要会话令牌** —— 令牌见 :mod:`session`（进程内生成、注入页面，退出即失效）。
  12.6 曾写「无鉴权成立」，那是「只听回环」的推论；12.7 让接口能**写 App Secret**
  之后就不再成立 —— 无令牌的本机 HTTP 接口等于把凭据敞开。
* **写操作是白名单** —— 新建事务、状态迁移、排期/移出排期、改非秘密设置、对话记账。
  角色归属、完成标准、角色管理、草稿采纳仍只在终端。

这个文件只做三件事：**读请求、鉴权、写响应**。
「哪个路径对应哪个函数」在 :mod:`routes` / :mod:`routes_write`，
「给我 App、我回 payload」全在 :mod:`endpoints`，业务规则全在 ``services``。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..app import App
from ..domain import FreeAgentError
from . import endpoints_read
from .body import discard_body, read_json
from .page import INDEX_HTML
from .routes import READ_ROUTES as _READ_ROUTES
from .routes_write import dispatch_post
from .session import (
    PUBLIC_API_PATHS,
    SESSION_HEADER,
    inject_token,
    token_matches,
)

__all__ = [
    "create_server", "WebRequestHandler",
    "DEFAULT_HOST", "DEFAULT_PORT",
]

#: 回环地址。不提供 0.0.0.0 选项。
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8770


@dataclass(frozen=True, slots=True)
class _Route:
    status: int
    body: bytes
    content_type: str


class WebRequestHandler(BaseHTTPRequestHandler):
    """只负责 HTTP 机制。**不重写任何业务规则。**"""

    server_version = "FreeAgent/1.0"

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        sys.stderr.write("[web] " + (fmt % args) + "\n")

    def _send(self, route: _Route) -> None:
        self.send_response(route.status)
        self.send_header("Content-Type", route.content_type)
        self.send_header("Content-Length", str(len(route.body)))
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; connect-src 'self'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(route.body)

    def _json(self, payload: dict, status: int = HTTPStatus.OK) -> _Route:
        return _Route(
            status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _error(self, message: str, status: int) -> _Route:
        return self._json({"error": message}, status)

    # -- 锁 ------------------------------------------------------------------ #
    # SQLite 连接不能被两个请求线程同时使用（``check_same_thread=False`` 只解除
    # 线程归属检查，不提供并发安全）。所以每个请求整段持有这把锁。
    def do_GET(self) -> None:  # noqa: N802
        with self.app.lock:
            self._get()

    def do_HEAD(self) -> None:  # noqa: N802
        with self.app.lock:
            self._get()

    def do_POST(self) -> None:  # noqa: N802
        with self.app.lock:
            self._post()

    # -- 鉴权 --------------------------------------------------------------- #
    # 顺序要紧：令牌校验**必须在**路由之前，且在 host 检查之后。
    # 放在路由之后的话，攻击者只要猜到路径就能直接打。
    def _authorized(self, path: str) -> bool:
        """**GET** 的判定：白名单只覆盖只读端点。"""
        if not path.startswith("/api/"):
            return True                  # 页面本身要带令牌，静态资源不必
        if path in PUBLIC_API_PATHS:
            return True
        return self._has_token()

    def _post_authorized(self) -> bool:
        """**POST** 的判定：只认令牌，**不查白名单**。

        刻意不复用 :meth:`_authorized`：白名单是给只读端点的，套到写操作上
        等于开了扇「因为它只读所以放过」的门。``/api/health`` 正在名单里，
        复用的话 ``POST /api/health`` 就会免鉴权通过 —— 今天它还没有副作用，
        可那只是巧合，不是设计。哪天它有了写副作用，免鉴权就跟着漏进来。
        """
        return self._has_token()

    def _has_token(self) -> bool:
        return token_matches(self.headers.get(SESSION_HEADER))

    def _reject_unauthorized(self) -> None:
        # ⚠️ **必须先丢弃请求体再回响应**（见 :func:`.body.discard_body`）。
        # 不丢的话接收缓冲区里还留着数据，``close()`` 变成 abort ——
        # 客户端**连这个 401 都读不到**，只看到 ConnectionAbortedError。
        # 而 POST 一律带体，所以这条在写接口上是常态而不是边角。
        discard_body(self.rfile, self.headers)
        self._send(self._error("需要会话令牌", HTTPStatus.UNAUTHORIZED))

    # -- 路由 ---------------------------------------------------------------- #
    def _get(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if not self._authorized(path):
            self._reject_unauthorized()
            return
        try:
            if path in ("/", "/index.html"):
                # 令牌注入页面，界面自己带上 —— 不要求用户手输。
                # 手输的话用户得从某处抄到它，而那个「某处」是新的泄露面。
                self._send(
                    _Route(200, inject_token(INDEX_HTML).encode("utf-8"),
                           "text/html; charset=utf-8")
                )
                return
            handler = _READ_ROUTES.get(path)
            if handler is not None:
                self._send(self._json(_call(handler, self.app, query)))
                return
            if path.startswith("/api/task/"):
                self._send(
                    self._json(
                        endpoints_read.task(self.app, path.rsplit("/", 1)[-1])
                    )
                )
                return
            self._send(self._error("没有这个接口", HTTPStatus.NOT_FOUND))
        except FreeAgentError as exc:
            self._send(self._error(str(exc), HTTPStatus.BAD_REQUEST))
        except Exception as exc:  # noqa: BLE001 - 兜底，别让服务整个挂掉
            # 必须带上异常信息：只回类型名的话，线上出一个 NameError
            # 只能看到「服务端出错：NameError」，完全无从查起。
            self._send(self._error(f"服务端出错：{exc!r}", 500))

    def _post(self) -> None:
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        # POST 一律要令牌，且不查只读白名单（理由见 _post_authorized）。
        # 写成只挡 GET 的话，最危险的写接口反而成了唯一不需要令牌的那个。
        if not self._post_authorized():
            self._reject_unauthorized()
            return
        try:
            found = dispatch_post(self.app, parts, self._read_json)
            if found is None:
                # ⚠️ 同 :meth:`_reject_unauthorized`：**先丢弃请求体**。
                # 路径不认识时 ``dispatch_post`` 压根不碰请求体，于是缓冲区
                # 里还留着数据，``close()`` 变成 abort —— 客户端连这个 404
                # 都读不到。实测会表现为随机的 ConnectionAbortedError，
                # 而且会**连累之后无关的连接**，凶手不在现场（见
                # :func:`.body.discard_body`）。
                discard_body(self.rfile, self.headers)
                self._send(self._error("没有这个接口", HTTPStatus.NOT_FOUND))
                return
            status, payload = found
            self._send(self._json(payload, status))
        except FreeAgentError as exc:
            self._send(self._error(str(exc), HTTPStatus.BAD_REQUEST))
        except ValueError as exc:
            self._send(self._error(str(exc), HTTPStatus.BAD_REQUEST))
        except Exception as exc:  # noqa: BLE001
            self._send(self._error(f"服务端出错：{exc!r}", 500))

    def _read_json(
        self, allow_empty: bool = False, limit: int = 0
    ) -> dict[str, Any]:
        """薄封装：真正的读法在 :mod:`body`（含超限时丢弃未读数据）。"""
        return read_json(self.rfile, self.headers, allow_empty=allow_empty, limit=limit)


def _call(handler, app: App, query: dict):
    """GET 端点统一签名：有的要 ``query``，有的不要。"""
    return handler(app, query) if handler.__code__.co_argcount > 1 else handler(app)


def create_server(
    app: App, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT
) -> ThreadingHTTPServer:
    """建服务器。**非回环地址直接拒绝**。

    回环之外的个人事务库不能暴露：令牌只在进程内生成、注入页面，
    它挡的是「网页里的脚本乱发请求」，**不是**挡网络上的攻击者。
    真正的防线是「局域网根本到不了」。
    """
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(
            f"拒绝绑定 {host}：个人数据只允许回环地址，会话令牌不是网络防线"
        )
    server = ThreadingHTTPServer((host, port), WebRequestHandler)
    server.app = app  # type: ignore[attr-defined]
    return server
