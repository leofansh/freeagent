"""hermes API Server 的客户端与生命周期（设计文档 11.10.8）。

它是什么：**FreeAgent 当 hermes 的 HTTP 客户端**（``/v1/runs`` + SSE）。
不 fork、不二开。与 :mod:`freeagent.services.opencode_server` 是**两种不同的
生命周期** —— 那边是「起一个隔离进程、用完即弃」，这边是「**连一个早已
常驻的 gateway**」（``hermes gateway``，默认 8642），只进不出、用完不关。

为什么需要这一层：``/ask hermes`` 要把一个问题交给她，把答复拿回来。hermes
的 API Server 面形状与 opencode **同构**（建 run → 流式读 SSE → 中止），但
**传输细节不同**：

1. **run 是「提交即运行」**：prompt 在 ``POST /v1/runs`` 的请求体里，没有
   opencode 那种「先建空会话、再发 prompt」的拆分。所以这里的
   ``create_session()`` 只返回一个**占位 token**，真正的 run id 由
   ``prompt_async()`` 里那次 ``POST /v1/runs`` 返回、记在实例上。
2. **认证是 ``Authorization: Bearer <API_SERVER_KEY>``**（不是 opencode 的
   Basic）。key 从 hermes 真实 home 的 ``.env`` 读，**绝不打进日志**。
3. **事件名与载荷键不同**：hermes 的帧是 ``{"event": name, ...}``（事件名在
   JSON 的 ``event`` 字段），翻译成内部事件名的事归适配器
   （:data:`freeagent.services.executors._HERMES_0`），本模块**不内联任何**。

## 只读怎么保证（与 opencode 不是同一套）—— 且 V1.81 证伪了初稿的假设

opencode 的 ``/ask`` 靠**只读 permission 配置**（``edit``/``bash`` deny），
**真的改不了**。hermes **没有**那种「按工具名写 permission 配置」的面 ——
V1.79 初稿以为它的只读靠「``approvals.unattended_mode: deny`` + ask 遇
``permission.asked`` 即中止」双重强制。**V1.81 真机把它证伪了**：

- hermes 的审批闸是**「按模式标记」而非「每动作确认」**：``approvals.mode``
  （manual/smart/off）只决定**已被它判为危险**的动作怎么处理，**不会把更多
  动作标记为危险**（``tools/approval.py::_run_approval_gate`` 的注释明确：
  hardline / allowlist / 模式检测是调用方的事）。
- 实测：``del`` / 简单 ``write_file`` 到普通路径**都不在危险模式清单里**，
  API 面与飞书面**都会直接执行而不弹审批**——即使 ``approvals.mode: manual``
  + 重启 gateway 也一样（``file_created: True``、无 ``approval.request``）。
- 所以 hermes **做不到「根本改不了」**，只能作**知识问答**、不能当可执行器
  开放；``/ask hermes`` 的回话在 ``cli/app.py`` 常驻一条安全警示。

这个类**忽略** ``permission_config`` 参数（opencode 那份对 hermes 无意义，
见 :func:`~freeagent.services.executors._hermes_build_read_only_config`
的 docstring）。

## 绝不碰全局 current_adapter

本类**不调用** :func:`opencode_server.set_current_adapter`。理由：
``parse_sse_event`` 读的是模块级「当前适配器」，而那是 opencode 的
``OpenCodeServer`` 起进程时设置的 —— 若这里也去改，会让**下一次 /ask
opencode** 的只读配置取到 hermes 的（空）配置而失去闸门。所以本类在
``events()`` 里**直接用** hermes 适配器的 ``api.parse_event``，不经过那个
全局。
"""
from __future__ import annotations

import contextlib
import json
import os
import pathlib
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Iterator

from .executors import HERMES_API

__all__ = ["HermesClient", "ServerError", "hermes_home", "hermes_api_endpoint"]


class ServerError(RuntimeError):
    """hermes API Server 起不来 / 调用失败。**必须让 /ask 失败，不能当成功。**"""


def hermes_home() -> pathlib.Path:
    """hermes 的**真实**数据目录（`hermes doctor` 确认）。

    Windows 上是 ``%LOCALAPPDATA%\\hermes``（如
    ``C:\\Users\\fanli\\AppData\\Local\\hermes``），POSIX 上是 ``~/.hermes``。
    **不要猜 ``~/.hermes``**：Windows 下 ``hermes`` 读的是
    ``LOCALAPPDATA`` 下的那份 ``.env`` / ``config.yaml``，``~/.hermes`` 是
    一条**无效路径**（本仓早期就因这个 8642 一直连不上）。
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        return pathlib.Path(base) / "hermes" if base else (
            pathlib.Path.home() / ".hermes"
        )
    return pathlib.Path.home() / ".hermes"


def _read_env(path: pathlib.Path) -> dict[str, str]:
    """读 ``.env``（KEY=VALUE 行），**不给 key 值进日志**。"""
    out: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        return out
    return out


def hermes_api_endpoint() -> dict[str, str]:
    """从 hermes 真实 home 的 ``.env`` 推导 API Server 的连接参数。

    返回 ``{"base_url", "api_key"}``。**key 只在返回值里**，不写日志。

    ## 为什么从这里读，而不是让用户另配

    8642 是 hermes gateway 自己按 ``.env`` 起的（``API_SERVER_KEY`` /
    ``API_SERVER_HOST`` / ``API_SERVER_PORT``），FreeAgent 若再单独配一份
    连接参数，迟早会漂移（一个改了另一个没改，症状是「gateway 起来了但连
    不上」）。**一份能力一份实现**：连接参数以 hermes 自己的 ``.env`` 为准，
    本仓只是去读它。

    ## 缺失 key 时**响亮失败**，不猜

    没配 ``API_SERVER_KEY`` 说明 gateway 压根没启用 API Server 面 —— 这是
    「还没起好」，不是「可以用默认」。给出能照着做的话。
    """
    home = hermes_home()
    env = _read_env(home / ".env")
    key = env.get("API_SERVER_KEY")
    if not key:
        raise ServerError(
            f"hermes 的数据目录 {home} 里没配 API_SERVER_KEY（{home}/.env）。"
            "API Server 面没启用 —— /ask hermes 连不上。"
            "要接它：先起 `hermes gateway` 并配好 API_SERVER_ENABLED=true 与 "
            "API_SERVER_KEY。"
        )
    host = env.get("API_SERVER_HOST") or "127.0.0.1"
    port = env.get("API_SERVER_PORT") or "8642"
    return {"base_url": f"http://{host}:{port}", "api_key": key}


def _hermes_parse_line(line: str) -> tuple[str, Any] | None:
    """解析一行 SSE → ``(内部事件名, 载荷)``，不是事件则 ``None``。

    hermes 的帧是 ``event: <name>\ndata: <json>\n\n`` 或只有 ``data: <json>``。
    这里**只认 ``data:`` 前缀**（与 opencode 同一手法）：事件名在 data 行的
    JSON 的 ``event`` 字段里（``_run_event`` 的 name），所以 ``event:`` 行
    可以忽略。载荷翻译归适配器（:data:`HERMES_API.parse_event`），本模块不
    内联任何 hermes 形状 —— 换协议只改一个文件。

    **刻意不经过** :func:`freeagent.services.opencode_server.parse_sse_event`：
    那个读的是模块级「当前适配器」（opencode 起进程时设的），本类不该碰它
    （见模块 docstring「绝不碰全局 current_adapter」）。
    """
    if not line or not line.startswith("data:"):
        return None
    try:
        obj = json.loads(line[5:].strip())
    except (ValueError, TypeError):
        # 半包 / 非法 JSON：丢弃这一行，不炸掉整个流。
        return None
    if not isinstance(obj, dict):
        return None
    return HERMES_API.parse_event(obj)


class HermesClient:
    """一次 ``/ask hermes`` = 连到常驻 gateway 提交一个 run、读事件流。

    上下文管理器：``with HermesClient(...) as hc:`` —— ``__enter__`` 验证
    8642 可连（GET /v1/capabilities），``__exit__`` 只清实例状态、**不关
    gateway**（它是常驻服务，不是我们起的）。

    给 ``/ask`` 的接口与 :class:`~freeagent.services.opencode_server.OpenCodeServer`
    **同构**（``create_session`` / ``prompt_async`` / ``events`` / ``abort``），
    所以 :func:`~freeagent.services.ask.ask_executor` 一行都不用改 —— 它只
    依赖这四个方法 + 上下文管理器。**但生命周期不同**：这里不起进程、不建
    jail、不写覆盖层，只是连一个早已在跑的 HTTP 服务。
    """

    def __init__(
        self,
        project,
        *,
        command: str = "hermes",
        permission_config: dict[str, Any] | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._project = pathlib.Path(project)
        self._command = command
        self._timeout = timeout
        # ``permission_config`` **忽略**：hermes 没有「按工具名写 permission
        # 配置」的面，只读也保证不了（V1.81 真机证伪，见模块 docstring）。
        self._permission_config = permission_config
        if base_url is None or api_key is None:
            found = hermes_api_endpoint()
            base_url = base_url or found["base_url"]
            api_key = api_key or found["api_key"]
        self._base_url = base_url
        self._api_key = api_key
        #: 真正跑起来的 run id（prompt_async 提交 /v1/runs 后拿到）。
        self._run_id: str = ""
        #: ``create_session`` 返回的占位 token。hermes 无独立建会话，这个
        #: token 只是 ask_executor 约定的句柄；真正的 run id 在上面。
        self._session_token: str = ""

    # -- 生命周期 -------------------------------------------------------- #
    @property
    def base_url(self) -> str:
        return self._base_url

    def start(self) -> "HermesClient":
        # 就绪探针：GET /v1/capabilities（能力面，判据 1 真机可 200）。
        code, raw = self.call("/v1/capabilities")
        if code != 200:
            raise ServerError(
                f"hermes API Server 没就绪（{self._base_url}/v1/capabilities "
                f"→ HTTP {code}）。先 `hermes gateway restart` 确认 8642 在听。"
            )
        return self

    def close(self) -> None:
        # gateway 是常驻服务，不是我们起的 —— 这里只清实例状态，**不关它**。
        self._run_id = ""
        self._session_token = ""

    def __enter__(self) -> "HermesClient":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- HTTP ------------------------------------------------------------ #
    def call(
        self, path: str, method: str = "GET", body: Any = None, *, timeout: float | None = None
    ) -> tuple[int, bytes]:
        """调一次。返回 ``(状态码, 原始响应体)``；连不上返回 ``(-1, 原因)``。

        返回码而不是抛异常：调用点要区分「服务没响应」与「服务说不行」。
        """
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self._base_url + path, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self._api_key)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self._timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            with contextlib.suppress(Exception):
                return exc.code, exc.read()
            return exc.code, b""
        except Exception as exc:  # noqa: BLE001 - 网络异常一律归为「连不上」
            return -1, str(exc).encode()

    def _must(self, path: str, method: str = "GET", body: Any = None) -> Any:
        code, raw = self.call(path, method, body)
        if code < 0:
            raise ServerError(
                f"{method} {path} 连不上 hermes：{raw.decode('utf-8', 'replace')}"
            )
        if code >= 400:
            raise ServerError(
                f"{method} {path} -> {code} {raw.decode('utf-8', 'replace')[:200]}"
            )
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return raw.decode("utf-8", "replace")

    # -- /ask 要用的四件事 ------------------------------------------------ #
    def create_session(self) -> str:
        """返回一个占位 token。

        hermes **没有** opencode 那种「先建空会话」的端点 —— run 是「提交
        即运行」，prompt 随 ``POST /v1/runs`` 一起给。所以这里只生成一个
        内部 token 当 ``ask_executor`` 的会话句柄，真正的 run id 在
        :meth:`prompt_async` 里绑定。诚实地说：这个返回值**不是**服务器给
        的 id，只是本仓内部的占位 —— 别拿它去拼任何 URL。
        """
        if not self._session_token:
            self._session_token = uuid.uuid4().hex
        return self._session_token

    def prompt_async(
        self,
        session_id: str,
        brief: str,
        *,
        model: str = "",
        agent: str = "",
        variant: str = "",
        directory: str | None = None,
    ) -> None:
        """提交一个 run（``POST /v1/runs``，问题在 ``input`` 字段里），记下 run id。

        ``input`` 是**真机实测**的字段名（判据 5）：hermes 的 run 面是
        OpenAI **Responses** 兼容，请求体是 ``{"input": ...}`` 而不是
        ``{"prompt": ...}`` —— 传 ``prompt`` 会 400（``Missing 'input' field``）。
        「建 run 就拿 run id」与「问题随 run 一起给」这两个形状，都是
        对着真机验过才写下来的。

        ## ``model`` / ``agent`` / ``variant`` 暂不接

        hermes 的 run 用**它自己配置的模型**（profile / config 里选）。
        ``POST /v1/runs`` 虽然接受 ``model``（真机看是用于路由的字符串，
        见 ``_resolve_route``），但那多半是 hermes 自己的模型名，跟 opencode
        的 ``provider/model`` 形状不是一回事 —— 把 opencode 的 model 原样
        传过去会「模型切换没生效且不报错」（V1 踩过的坑）。所以这里**不传**，
        用 hermes 默认模型。哪天验明 hermes 的 model 命名再补。
        """
        payload: dict[str, Any] = {"input": brief}
        body = self._must("/v1/runs", "POST", payload)
        # run id 在 ``run_id`` 字段（真机实测：Responses 风格，不是 ``id``）。
        rid = body.get("run_id") if isinstance(body, dict) else None
        if not isinstance(rid, str) or not rid:
            raise ServerError(f"POST /v1/runs 没拿到 run_id：{body!r}")
        self._run_id = rid

    def events(
        self, *, directory: str | None = None, timeout: float = 600.0
    ) -> Iterator[tuple[str, Any]]:
        """订阅 run 的事件流（SSE），逐条 yield ``(内部事件名, 载荷)``。

        用 ``urlopen`` 直接迭代行：SSE 就是「一行一条 data:」，上第三方库
        反而多一个依赖（与 opencode 同一手法）。事件名与载荷的差由适配器
        :data:`HERMES_API.parse_event` 翻译，所以 yield 的 ``type`` 仍是
        ``ask_executor`` 认识的内部事件名。
        """
        if not self._run_id:
            raise ServerError("还没提交 run（prompt_async 未调用）就订阅事件流")
        rid = urllib.parse.quote(self._run_id, safe="")
        url = f"{self._base_url}/v1/runs/{rid}/events"
        req = urllib.request.Request(url)
        req.add_header("Authorization", "Bearer " + self._api_key)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:
                parsed = _hermes_parse_line(raw.decode("utf-8", "replace").strip())
                if parsed is not None:
                    yield parsed

    def abort(self, session_id: str) -> None:
        """叫停（``POST /v1/runs/{id}/stop``）。失败只记不抛。

        叫停是尽力而为，不是必须成功 —— ``ask_executor`` 在中止后要报的是
        「超预算/权限」那个结论，不是「中止失败」（见
        :func:`~freeagent.services.ask._abort_quietly`）。
        """
        if not self._run_id:
            return
        with contextlib.suppress(ServerError):
            self._must(HERMES_API.abort_path(self._run_id), "POST", {})
