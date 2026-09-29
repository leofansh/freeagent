"""opencode 本地服务的客户端与生命周期（设计文档 11.8.1）。

它是什么：**FreeAgent 当 opencode 的 HTTP 客户端**。不 fork、不二开，
闸门判定全在 FreeAgent 这侧 —— 下层只提供「会不会问」与「收到了没有」。

为什么需要这一层，而不是在 :mod:`freeagent.delegate` 里直接写
``urllib`` 调用：委派是**无人值守**的，它碰的是一个**会改你本机文件**的
子进程。所以这一层把三件容易出错的事收在**一处**：

1. **配置隔离** —— 绝不能写用户的 ``~/.config/opencode/opencode.jsonc``。
   实测两种机制：``OPENCODE_CONFIG`` 是**合并**语义（未指定的键沿用用户的，
   那是 fail-open），``XDG_CONFIG_HOME`` 是**完全替换**。所以只用后者。
2. **进程收口** —— Windows 上 ``terminate()`` 只杀掉 ``opencode.CMD``
   批处理包装器，原生 ``opencode.exe`` 子进程**不会跟着死**（实测 14 次探针
   各漏一个，累计约 12.5 GB）。所以用**进程组**整组收口，并且**验端口**。
3. **载荷形状** —— ``reply`` 必须是对象 ``{"reply": "once"}``；
   裸字符串会被拒（实测 ``400 Expected object``）。

**这一层不做审批判定。** 它只把 opencode 的问题原样带回来、把人的回答
原样送回去。判定在 :class:`~freeagent.services.approval.ApprovalStore` 一处。
"""
from __future__ import annotations

import base64
import contextlib
import dataclasses
import json
import os
import pathlib
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Iterator

__all__ = [
    "ToolPermission",
    "ServerError",
    "delegation_permission_config",
    "build_isolated_config",
    "parse_sse_event",
    "permission_from_event",
    "reply_payload",
    "OpenCodeServer",
    "free_port",
]


# ── 纯函数（可测，无 IO）─────────────────────────────────────────────── #

def delegation_permission_config() -> dict[str, Any]:
    """委派用的 permission 配置（**V1 字段名**）。

    ⚠️ **键序有意义**：V1 是 ``last matching rule wins``，所以通配 ``*``
    必须**放最前**，具体规则放后面。deny 放最后才不会被 ``*: allow`` 覆盖。
    （Python 的 ``dict`` 保序，``json.dumps`` 也保序，所以这个形状能原样落盘。）

    逐条的理由：

    - ``*: allow`` —— 底子。``read``/``grep``/``glob`` 是干活必需的，
      全设 ask 会让 agent 一直问、卡片刷屏，**问到最后就是无脑点**。
    - ``edit: ask`` —— 改文件。这是执行期闸门**真正要拦的东西**。
    - ``bash: ask`` —— 跑命令。与 edit 同级；官方 V1 文档明说 shell
      带宿主机的文件/进程/网络权限。
    - ``webfetch`` / ``websearch: ask`` —— 出网。委派的需求通常不需要，
      需要时再放。
    - ``task: deny`` —— 不让 agent 拉子代理。与 FreeAgent 的分层一致
      （「派什么由代码决定，不由模型决定」），也与本机既有配置一致。
    - ``external_directory: deny`` —— **永不越界**。这是 11.8 第 1 道闸门在
      opencode 侧的落点；FreeAgent 侧只拒相对路径，两侧都拒才算闸门。

    为什么不给 ``bash`` 配窄白名单：官方文档明说目录推断是 best effort，
    **不要试图用规则枚举所有危险命令**。所以这里走「全 ask + 人判断」，
    靠闸门而不是靠正则。
    """
    return {
        "$schema": "https://opencode.ai/config.json",
        "permission": {
            "*": "allow",
            "edit": "ask",
            "bash": "ask",
            "webfetch": "ask",
            "websearch": "ask",
            "task": "deny",
            "external_directory": "deny",
        },
    }


def build_isolated_config(config: dict[str, Any] | None = None) -> str:
    """把配置写成 JSON 文本。**返回文本而不写文件** —— 写文件是 IO，要可测。"""
    return json.dumps(
        config if config is not None else delegation_permission_config(),
        ensure_ascii=False, indent=2,
    )


@dataclasses.dataclass(frozen=True, slots=True)
class ToolPermission:
    """一次「opencode 想动手」的请求（实测载荷的形状）。

    刻意**只保留渲染卡片真正需要的字段**，其余原样丢掉：载荷里还有
    ``tool.messageID`` / ``callID`` 之类，它们是 opencode 的内部坐标，
    留着容易让人误以为该拿它们做点什么。
    """

    request_id: str
    permission: str
    paths: tuple[str, ...] = ()
    diff: str | None = None
    suggested_always: tuple[str, ...] = ()

    @property
    def summary(self) -> str:
        """一行摘要，给日志用。"""
        where = self.paths[0] if self.paths else "(无路径)"
        return f"{self.permission} → {where}"


def permission_from_event(properties: Any) -> ToolPermission | None:
    """从 ``permission.asked`` 事件的 properties 里取出请求。

    **返回 ``None`` 表示这不是一条可回应的请求**（缺 id 或缺动作名），
    绝不用空字符串凑一个 —— 那会让上层把「无法回应」当成「已拒绝」，
    两种错误的处理方式完全不同。
    """
    if not isinstance(properties, dict):
        return None
    rid = properties.get("id")
    perm = properties.get("permission")
    if not isinstance(rid, str) or not rid:
        return None
    if not isinstance(perm, str) or not perm:
        return None
    meta = properties.get("metadata")
    meta = meta if isinstance(meta, dict) else {}
    raw_paths = properties.get("patterns")
    paths = tuple(p for p in raw_paths if isinstance(p, str)) \
        if isinstance(raw_paths, list) else ()
    filepath = meta.get("filepath")
    if isinstance(filepath, str) and filepath and filepath not in paths:
        paths = paths + (filepath,)
    diff = meta.get("diff")
    raw_always = properties.get("always")
    always = tuple(a for a in raw_always if isinstance(a, str)) \
        if isinstance(raw_always, list) else ()
    return ToolPermission(
        request_id=rid, permission=perm, paths=paths,
        diff=diff if isinstance(diff, str) and diff else None,
        suggested_always=always,
    )


def parse_sse_event(line: str) -> tuple[str, Any] | None:
    """解析一行 SSE。返回 ``(type, properties)``，不是事件则 ``None``。

    SSE 每条消息是多行的 ``event:``/``data:``，但 opencode 实测只发
    ``data: {...}``（类型在 JSON 里的 ``type`` 字段）。所以只认 ``data:`` 前缀，
    其余（``:`` 心跳、空行、注释）一律忽略。
    """
    if not line or not line.startswith("data:"):
        return None
    try:
        obj = json.loads(line[5:].strip())
    except (ValueError, TypeError):
        # 半包 / 非法 JSON：丢弃这一行，**不要**炸掉整个流。
        return None
    if not isinstance(obj, dict):
        return None
    kind = obj.get("type")
    if not isinstance(kind, str) or not kind:
        return None
    return kind, obj.get("properties")


def reply_payload(decision: str) -> dict[str, str]:
    """把本地结论翻成 opencode 的 ``reply`` 载荷。

    ⚠️ **必须带 ``reply`` 键**（实测：裸字符串 → ``400 Expected object``；
    ``{"action": ...}`` → ``400 Missing key ["reply"]``）。

    映射：本地 ``allow`` → opencode ``once``。
    **刻意不映射到 ``always``** —— V1 的 ``always`` 是会话级授权，
    而本项目明确不做永久授权（设计文档 11.9.7）；而且每次都问一次
    本来就是这个闸门的意义。
    """
    if decision not in ("allow", "deny"):
        raise ValueError(f"未知结论：{decision!r}")
    return {"reply": "once" if decision == "allow" else "reject"}


class ServerError(RuntimeError):
    """opencode 服务起不来 / 调用失败。**必须让派发失败，不能当成功。**"""


def free_port() -> int:
    """借操作系统一个空闲端口。

    刻意用「bind 后立刻关」的经典做法而不是「猜一个高位端口」：
    猜的端口可能正好被 opencode 自己（默认 4096）或别的服务占着。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ── 服务生命周期（IO）────────────────────────────────────────────────── #

class OpenCodeServer:
    """一次委派 = 一个**隔离的** opencode 服务。用完即弃。

    上下文管理器：``with OpenCodeServer() as oc:`` —— 退出时**保证**进程组
    被收口、临时配置目录被删。异常路径同样收口（不然又会漏进程）。
    """

    def __init__(
        self,
        *,
        project: pathlib.Path,
        executable: str = "opencode",
        port: int | None = None,
        startup_timeout: float = 60.0,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self._project = pathlib.Path(project)
        self._exe = executable
        self._port = port or free_port()
        self._startup_timeout = startup_timeout
        self._extra_env = dict(extra_env or {})
        self._proc: Any = None
        self._home: pathlib.Path | None = None
        self._password = uuid.uuid4().hex
        # 每次起一个随机密码：服务只监听回环，但**同机别的进程能连**。
        # 固定密码等于把「谁都能开 opencode 改你代码」的门开着。
        self._auth = "Basic " + base64.b64encode(
            f"opencode:{self._password}".encode()
        ).decode()

    # -- 生命周期 -------------------------------------------------------- #
    def start(self) -> "OpenCodeServer":
        self._home = pathlib.Path(tempfile.mkdtemp(prefix="freeagent-oc-"))
        cfg_dir = self._home / "opencode"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        # 配置**在启动前**写好，不走 PATCH —— 少一次往返，也少踩
        # 「PATCH 返回 200 但没落盘」那个坑（实测踩过）。
        (cfg_dir / "opencode.json").write_text(
            build_isolated_config(), encoding="utf-8"
        )

        env = dict(os.environ)
        env.update({
            "OPENCODE_SERVER_USERNAME": "opencode",
            "OPENCODE_SERVER_PASSWORD": self._password,
            # 完全替换，不是合并（实测：OPENCODE_CONFIG 会漏进用户配置）
            "XDG_CONFIG_HOME": str(self._home),
        })
        env.update(self._extra_env)

        # 进程组参数**两个平台不同**（Windows 要 creationflags、POSIX 要
        # start_new_session）。刻意用显式分支而不是
        # ``**({"creationflags": f} if win else {"start_new_session": True})``：
        # 后者两个分支的**值类型不同**，类型检查器会把整个 dict 推成
        # ``int | bool``，于是 Popen 的每个参数都报「int 不能赋给 bool」——
        # 12 条假错误淹掉真错误。踩过，故留此注记。
        popen_kwargs: dict[str, Any] = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        try:
            self._proc = subprocess.Popen(
                [shutil.which(self._exe) or self._exe,
                 "serve", "--port", str(self._port)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                cwd=str(self._project), env=env, shell=False,
                **popen_kwargs,
            )
        except OSError as exc:
            self._cleanup_home()
            raise ServerError(f"起不了 opencode 服务：{exc}") from exc

        if not self._wait_ready():
            self.close()
            raise ServerError(
                f"opencode 服务没在 {self._startup_timeout:g}s 内就绪"
                f"（端口 {self._port}）"
            )
        return self

    def _wait_ready(self) -> bool:
        deadline = time.monotonic() + self._startup_timeout
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                return False
            code, _ = self.call("/config")
            if code != -1:
                return True
            time.sleep(0.5)
        return False

    def close(self) -> None:
        """收口。**必须整组杀**，否则原生子进程会留着占端口和内存。"""
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True, timeout=60, check=False,
                )
            else:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(os.getpgid(proc.pid), 9)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=10)
            if proc.poll() is None:  # pragma: no cover - 兜底
                with contextlib.suppress(OSError):
                    proc.kill()
        self._cleanup_home()

    def _cleanup_home(self) -> None:
        home, self._home = self._home, None
        if home is not None:
            shutil.rmtree(home, ignore_errors=True)

    def __enter__(self) -> "OpenCodeServer":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- HTTP ------------------------------------------------------------ #
    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def call(
        self, path: str, method: str = "GET", body: Any = None, *, timeout: float = 30.0
    ) -> tuple[int, bytes]:
        """调一次。返回 ``(状态码, 原始响应体)``；连不上返回 ``(-1, 原因)``。

        返回码而不是抛异常：调用点要区分「服务没响应」与「服务说不行」，
        前者是本地环境问题、后者是业务拒绝，处置方式不同。
        """
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        req.add_header("Authorization", self._auth)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
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
            raise ServerError(f"{method} {path} 连不上：{raw.decode('utf-8', 'replace')}")
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

    # -- 委派要用到的四件事 ------------------------------------------------ #
    def create_session(self) -> str:
        """建会话。

        刻意**不给** ``directory`` 参数：opencode 的会话归属于启动时的 cwd，
        而本类已经在启动时把 cwd 设成委派项目了。再加一个「这次要用哪个目录」
        的入口，就多一条「传的目录和跑的目录不是同一个」的路 ——
        闸门判定的目录必须**只有一个**来源。
        """
        body = self._must("/session", "POST", {})
        sid = body.get("id") if isinstance(body, dict) else None
        if not isinstance(sid, str) or not sid:
            raise ServerError(f"建会话没拿到 sessionID：{body!r}")
        return sid

    def prompt_async(
        self, session_id: str, brief: str, *,
        model: str = "", directory: str | None = None,
    ) -> None:
        """发指令，**立即返回**。等的是事件流，不是这个调用。"""
        path = f"/session/{session_id}/prompt_async"
        if directory:
            path += f"?directory={urllib.parse.quote(directory, safe='')}"
        payload: dict[str, Any] = {"parts": [{"type": "text", "text": brief}]}
        if model:
            provider, _, name = model.partition("/")
            payload["model"] = {"providerID": provider, "modelID": name or provider}
        self._must(path, "POST", payload)

    def pending_permissions(self, *, directory: str | None = None) -> list[Any]:
        """列出**当前挂起**的授权请求。

        用途不是「轮询主路径」（主路径走事件流），而是**恢复现场**：
        执行器重启后，靠它把「上次问过但没人答」的那些捞出来按拒绝处理。
        """
        path = "/permission"
        if directory:
            path += f"?directory={urllib.parse.quote(directory, safe='')}"
        got = self._must(path)
        return list(got) if isinstance(got, list) else []

    def reply_permission(
        self, request_id: str, decision: str, *, directory: str | None = None
    ) -> None:
        """把人的结论送回 opencode。"""
        path = f"/permission/{urllib.parse.quote(request_id, safe='')}/reply"
        if directory:
            path += f"?directory={urllib.parse.quote(directory, safe='')}"
        self._must(path, "POST", reply_payload(decision))

    def abort(self, session_id: str) -> None:
        """叫停。失败只记不抛 —— 叫停是尽力而为，不是必须成功。"""
        with contextlib.suppress(ServerError):
            self._must(f"/session/{session_id}/abort", "POST", {})

    def events(self, *, directory: str | None = None,
               timeout: float = 600.0) -> Iterator[tuple[str, Any]]:
        """订阅事件流（SSE），逐条 yield ``(type, properties)``。

        用 ``urlopen`` 直接迭代行：SSE 就是「一行一条 data:」，
        上第三方 SSE 库反而多一个依赖。
        """
        url = f"{self.base_url}/event"
        if directory:
            url += f"?directory={urllib.parse.quote(directory, safe='')}"
        req = urllib.request.Request(url)
        req.add_header("Authorization", self._auth)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:
                parsed = parse_sse_event(raw.decode("utf-8", "replace").strip())
                if parsed is not None:
                    yield parsed
