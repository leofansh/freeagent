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
from typing import Any, Iterable, Iterator, Mapping

from .executors import (
    ExecutorAdapter,
    ToolPermission,
    adapter_for,
)

__all__ = [
    "ToolPermission",
    "ServerError",
    "delegation_permission_config",
    "build_isolated_config",
    "parse_sse_event",
    "permission_from_event",
    "reply_payload",
    "build_child_env",
    "child_env_visibility",
    "OpenCodeServer",
    "free_port",
    "current_adapter",
    "set_current_adapter",
]


# ── 执行器接缝 ────────────────────────────────────────────────────────── #
#
# 下面三个函数曾经**直接硬编码** opencode V1 的字段名与载荷形状。那意味着
# 协议变更要改三处，而漏掉一处的后果不是「跑不起来」，是**闸门静默失效**
# （旧字段被忽略，权限回到默认 allow）—— 失效方向恰好是最危险的那侧。
#
# 现在它们是对 :mod:`freeagent.services.executors` 里那个**带版本号的适配器**
# 的委托。V1 的字段名、事件形状、答复载荷全都住在适配器里，协议变更收敛到
# 一个文件。
#
# 这三个名字**保留**：delegate.py 与 tests 都从这里导入它们。适配器换版本时
# 调用方不需要改。

#: 当前使用的适配器。**默认 V1** —— 本仓实测并据以接线的那个版本。
#:
#: 刻意做成模块级可替换而不是每次构造传参：委派是**独立进程**里跑的短命
#: 流程，没有多处需要注入；而测试要能换掉它来验证 V2 不会被误用。
_CURRENT_ADAPTER = adapter_for(1)
assert _CURRENT_ADAPTER is not None, "V1 适配器必须存在"


def current_adapter() -> ExecutorAdapter:
    """当前适配器。带断言，因为「取不到」意味着注册表被改坏了。"""
    assert _CURRENT_ADAPTER is not None
    return _CURRENT_ADAPTER


def set_current_adapter(adapter: ExecutorAdapter | None) -> None:
    """换掉当前适配器。**只给测试用** —— 换完记得换回来。

    存在的理由：V2 适配器是 :class:`~freeagent.services.executors.UnverifiedExecutorError`
    的来源，必须能验证「用它会响」而不是「没人调用它所以看起来没事」。
    """
    global _CURRENT_ADAPTER
    if adapter is None:
        adapter = adapter_for(1)
        assert adapter is not None
    _CURRENT_ADAPTER = adapter


# ── 纯函数（可测，无 IO）─────────────────────────────────────────────── #

def delegation_permission_config() -> dict[str, Any]:
    """当前适配器的 permission 配置。

    字段名、键序、每条规则**为什么**这么设，全部在
    :func:`freeagent.services.executors._v1_permission_config` 里 ——
    那是执行器特有的知识，**只该有一个地方有**。

    为什么这里保留一个转发函数：`delegate.py` 与 tests 都从本模块导入它，
    而适配器版本是会变的（见 :func:`set_current_adapter`）。转发让调用方
    不必知道适配器存在。
    """
    return current_adapter().build_permission_config()


def build_isolated_config(config: dict[str, Any] | None = None) -> str:
    """把配置写成 JSON 文本。**返回文本而不写文件** —— 写文件是 IO，要可测。"""
    return json.dumps(
        config if config is not None else delegation_permission_config(),
        ensure_ascii=False, indent=2,
    )


# ── 子进程环境白名单（规范性）───────────────────────────────────────────── #
#
# 原来这里是 ``env = dict(os.environ)`` —— **全量继承**。改的理由不是「更干净」，
# 而是它与本项目自己的安全声明矛盾：
#
# - 11.8.1 说未命中规则时 V1 默认 ``allow``，所以本项目靠**显式配置**当闸门；
# - SAFETY.md 说「不要把本项目当成不可信负载的唯一安全控制」。
#
# 而全量继承意味着：**委派出去的 opencode 子进程看得到本机全部环境变量** ——
# 云凭据、CI token、``SSH_AUTH_SOCK``、其它项目的 API Key 全在里面。它跑着
# 完整 LLM 工具链，而默认配置里读与检索是放行的。
#
# 参照 ``D:\GitHub\qm`` 的 ``cleanEnv``：**9 键白名单、从零构造、什么都不继承**。
# 但不能照抄它那份 —— 它把真实 provider key 塞进 opencode 配置，
# 那一点上本项目更严（配置里只有 permission，见 :func:`build_isolated_config`）。

#: OS 必需：**缺任何一个都可能让服务起不来**，不是「不安全」而是「不工作」。
#: ``SYSTEMROOT`` 尤其关键 —— 缺了它 Winsock 都初始化不了，监听端口直接失败。
_OS_REQUIRED_ENV: frozenset[str] = frozenset({
    "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "OS",
    "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "PROCESSOR_IDENTIFIER",
    # shell 与编码：opencode 的 bash 工具起子进程要 COMSPEC/PATHEXT
    "LANG", "LC_ALL", "LC_CTYPE", "TZ",
    # Windows 上 Python 与多数库仍会读这两个，置空会让部分调用炸在
    # 「找不到临时目录」而不是失败在真正的原因上
    "TEMP", "TMP",
})

#: 会被**重定向进 jail** 的家目录 / 配置目录类变量。
#:
#: 原来只改了 ``XDG_CONFIG_HOME``，于是配置隔开了但**数据与缓存仍写回真实家目录**，
#: 而 ``HOME``/``USERPROFILE`` 更是让 opencode 能读 ``~/.ssh``、``~/.aws``。
#: 全部指向一次性目录，随 close 一起删掉。
_JAILLED_ENV: frozenset[str] = frozenset({
    "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME",
    "TMPDIR", "TEMP", "TMP",
})


def _model_credential_env_names() -> frozenset[str]:
    """允许透传的**模型凭据**变量名。

    为什么这类必须放行：:func:`build_isolated_config` **只写 permission 配置**，
    不写 provider 也不写 key —— 所以模型凭据唯一的来路就是继承的环境。
    一刀切掉，委派会直接 401/402（实测：探针里 ``deepseek-v4-pro`` 就是靠
    环境里的 key 跑通的）。

    基取本项目 LLM 注册表 :func:`freeagent.services.llm.providers.all_key_env_vars`
    —— 那是仓库内唯一的权威列表，不另编一份（另编必然漂移）。
    再补 opencode 自己常用、而本项目 LLM 层没有的那几个。

    刻意**只放行模型凭据**：像 ``GITHUB_TOKEN``、``AWS_*``、``SSH_AUTH_SOCK``
    这类与「让 opencode 调模型」无关的**一律不传**。
    """
    names = {
        # opencode 常用、本项目 LLM 层没有的
        "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "GOOGLE_API_KEY",
        "GEMINI_API_KEY", "GROQ_API_KEY", "XAI_API_KEY",
        "MISTRAL_API_KEY", "COHERE_API_KEY", "DEEPSEEK_API_KEY",
        "AZURE_OPENAI_API_KEY", "CEREBRAS_API_KEY", "OPENCODE_API_KEY",
    }
    try:
        from .llm.providers import all_key_env_vars
    except Exception:                               # noqa: BLE001 - 缺了也能跑
        pass
    else:
        names.update(all_key_env_vars())
    return frozenset(names)


def build_child_env(
    *,
    jail: pathlib.Path,
    exe: str | None = None,
    extra: Mapping[str, str] | None = None,
    credential_names: Iterable[str] | None = None,
) -> dict[str, str]:
    """构造子进程环境。**白名单式：不继承 :data:`os.environ`。**

    纯函数（无 IO、无 spawn）所以能直接测「子进程能看到什么」。

    :param jail: 一次性家目录。``HOME`` 与各类 XDG/缓存/临时目录**全部**指向它。
    :param exe: opencode 可执行文件路径。把它所在目录放进 ``PATH`` ——
        替身方案里常用自造脚本，得能找得到。
    :param extra: 显式追加（``OPENCODE_SERVER_USERNAME`` / ``_PASSWORD`` 等）。
    :param credential_names: 覆盖「放行哪些模型凭据」，给测试用。
    """
    env: dict[str, str] = {}

    # 1) OS 必需
    for name in _OS_REQUIRED_ENV:
        value = os.environ.get(name)
        if value is not None:
            env[name] = value

    # 2) PATH：**白名单**，不是继承。
    #    只给系统目录 + opencode 所在目录（替身方案常见自造脚本）。
    parts = [str(pathlib.Path(exe).parent)] if exe else []
    parts += [r"C:\Windows\System32", r"C:\Windows", r"C:\Windows\System32\Wbem"]
    env["PATH"] = os.pathsep.join(parts)

    # 3) 家目录 / 缓存 / 临时目录：全部关进 jail
    for name in _JAILLED_ENV:
        env[name] = str(jail)
    env["TMPDIR"] = str(jail)
    env["TEMP"] = str(jail)
    env["TMP"] = str(jail)

    # 4) 模型凭据：功能必需，但**只放行这一类**
    names = (frozenset(credential_names) if credential_names is not None
             else _model_credential_env_names())
    for name in names:
        value = os.environ.get(name)
        if value is not None:
            env[name] = value

    # 5) 显式追加（优先级最高，会覆盖上面同名项）
    if extra:
        env.update(extra)
    return env


def child_env_visibility(
    env: Mapping[str, str], *, secret_names: Iterable[str] = ()
) -> dict[str, list[str]]:
    """给「子进程能看到什么」出一份可断言的对照（测试与诊断用）。

    只回**名字**与「是否非空」，**不回值** —— 凭据的值不进日志。
    """
    secrets = set(secret_names)
    visible = sorted(env)
    return {
        "可见变量": visible,
        "其中非空": sorted(k for k, v in env.items() if v != ""),
        "凭据类（已隐去值）": sorted(
            f"{k}{'（非空）' if env[k] else '（空）'}" for k in visible if k in secrets
        ),
    }



def permission_from_event(properties: Any) -> ToolPermission | None:
    """当前适配器对 ``permission.asked`` 事件的解析。

    **返回 ``None`` 表示这不是一条可回应的请求**（缺 id 或缺动作名），
    绝不用空字符串凑一个 —— 那会让上层把「无法回应」当成「已拒绝」，
    两种错误的处理方式完全不同。

    载荷形状随版本而变，所以形状住在适配器里（归一化后的
    :class:`~freeagent.services.executors.ToolPermission` 才是调用方要的东西）。
    """
    return current_adapter().parse_request(properties)


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
    """把本地结论翻成**当前适配器**的答复载荷。

    载荷形状随版本而变（V1 是 ``{"reply": "once"}``，且实测裸字符串会被拒
    —— ``400 Expected object``），所以形状住在适配器里。

    映射：本地 ``allow`` → 执行器的「这一次」。**刻意不映射到「永久授权」**
    —— 那与本项目明确不做永久授权冲突（设计文档 11.9.7）；而且每次都问一次
    本来就是这个闸门的意义。
    """
    return current_adapter().build_reply(decision)


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

        env = build_child_env(
            jail=self._home,
            exe=shutil.which(self._exe) or self._exe,
            extra={
                "OPENCODE_SERVER_USERNAME": "opencode",
                "OPENCODE_SERVER_PASSWORD": self._password,
                **self._extra_env,
            },
        )

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
                # ``--hostname`` **显式给回环**，不靠默认值。随机密码已经在了，
                # 但「只在回环监听」是更靠前的一层 —— 它决定同机别的进程
                # 能不能看见这个端口。参照 qm 的 ``--hostname=127.0.0.1``。
                [shutil.which(self._exe) or self._exe,
                 "serve", "--hostname", "127.0.0.1",
                 "--port", str(self._port)],
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
