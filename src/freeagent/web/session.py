"""会话令牌：保护会读写凭据的接口。

抄 Hermes 的做法（``hermes_cli/web_server.py:195``），它踩过的坑一并带过来：

- ``secrets.token_urlsafe(32)`` —— 不用 ``random``，前者走 CSPRNG。
- **进程退出即失效**：不落盘、不长期有效。落盘的令牌会变成「磁盘上的秘密」。
- **注入页面**（:func:`inject_token`）：令牌给界面自己用，不要求用户手输。
  手输的话用户得从某处抄到它，而那个「某处」就是新的泄露面。
- **``hmac.compare_digest``**：恒定时间比较。``==`` 会在第一个不同字节处
  提前返回，攻击者能靠响应时间差逐字节试出令牌。
- **独立的 header 名**（``X-FreeAgent-Session``）而不是复用 ``Authorization``：
  后者常被反向代理占用（Caddy 的 basic_auth 就用它），撞名会导致两边都失效。

⚠️ 这一节推翻 12.6 的「鉴权无」：那是「只监听回环」的推论，而本节让接口
能**写** App Secret —— 无令牌的本机 HTTP 接口等于把凭据敞开。
"""

from __future__ import annotations

import hmac
import secrets

__all__ = [
    "SESSION_TOKEN",
    "SESSION_HEADER",
    "make_token",
    "token_matches",
    "inject_token",
    "PUBLIC_API_PATHS",
]

#: 32 字节熵，urlsafe 编码后 43 字符。
_TOKEN_BYTES = 32

#: 独立 header 名，理由见模块说明。
SESSION_HEADER = "X-FreeAgent-Session"

#: 进程启动时生成一次。**刻意不做「可注入」**：一旦能从环境变量传进来，
#: 就会变成「谁启动它谁就能拿到」，而自动生成的版本谁都拿不到 ——
#: 除非它从页面泄漏，而页面本身就是攻击面。
SESSION_TOKEN: str = secrets.token_urlsafe(_TOKEN_BYTES)

#: 免鉴权的只读端点。**刻意保持最小**：只有真正不敏感、不含用户数据的
#: 端点才配进来。
#:
#: 单独成模块是为了防漂移：Hermes 踩过「同一个端点在旧门下公开、
#: 在新门下 401」的坑（通配符子域回归，破坏了 liveness probe）。
#: 两道门共用这一份清单，就不会有两份。
PUBLIC_API_PATHS: frozenset[str] = frozenset({
    "/api/health",       # 探活：不含用户数据
})


def make_token() -> str:
    """新令牌。测试用（生产走模块级的 :data:`SESSION_TOKEN`）。"""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def token_matches(supplied: str | None, expected: str | None = None) -> bool:
    """校验令牌。**恒定时间**。

    ``supplied`` 为空一律判否 —— 不做「空值放行」，那是把
    「忘了传令牌」和「令牌正确」当成同一件事。
    """
    want = expected if expected is not None else SESSION_TOKEN
    if not supplied or not want:
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), want.encode("utf-8"))


def inject_token(html: str, token: str | None = None) -> str:
    """把令牌塞进页面，界面自己带上。

    刻意**放在 ``<script>`` 里而不是 meta 标签**：meta 会被某些扩展读走。
    """
    tok = token if token is not None else SESSION_TOKEN
    # 令牌是 urlsafe base64，不含引号/尖括号，但仍做一次转义 ——
    # 「当前值恰好安全」不能作为「永远安全」的理由。
    safe = (
        tok.replace("&", "&amp;").replace("<", "&lt;")
        .replace(">", "&gt;").replace('"', "&quot;")
    )
    inject = "window.__FREEAGENT_SESSION__=\"" + safe + "\";\n"
    # 插进**页面已有的那块脚本**里，不另开 ``<script>``：
    # 界面的守卫要求「脚本恰好一块」（tests/test_web.py 的
    # ``test_script_appears_exactly_once``）—— 单页界面就该是单块脚本，
    # 拆成两块会让"页面到底有几段逻辑"这种问题变得难答。
    # 放在块首（而不是块尾）：令牌必须早于任何可能发请求的代码就位。
    marker = "<script>"
    if marker in html:
        return html.replace(marker, marker + "\n" + inject, 1)
    # 万一将来页面结构变了，退回塞在 </head> 前，总比没有强。
    marker = "</head>"
    if marker in html:
        return html.replace(
            marker, '<script>\n' + inject + "</script>\n" + marker, 1
        )
    return '<script>\n' + inject + "</script>\n" + html
