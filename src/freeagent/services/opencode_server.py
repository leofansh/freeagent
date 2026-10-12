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
import logging
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Iterable, Iterator, Mapping

from .executors import (
    ExecutorAdapter,
    ToolPermission,
    ToolQuestion,
    adapter_for,
)

__all__ = [
    "ToolPermission",
    "ToolQuestion",
    "ServerError",
    "delegation_permission_config",
    "build_isolated_config",
    "parse_sse_event",
    "permission_from_event",
    "reply_payload",
    "question_from_event",
    "question_reply_payload",
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


def read_only_permission_config() -> dict[str, Any]:
    """当前适配器的**只读** permission 配置（``/ask`` 用的那份）。

    与 :func:`delegation_permission_config` **同源**（都走适配器），但取的是
    另一个方法 —— 因为两份配置必须是**不同的东西**：委派要「改了文件问人」，
    而 ``/ask`` 必须「**改不了**」。

    为什么 ``/ask`` 需要一份单独的配置（设计文档 11.10.6 写「它不改任何东西，
    所以没有『有副作用』可拦」）：那句话是**假设**。V1 未命中规则时大多默认
    ``allow``，所以「不改任何东西」必须由这里这份配置**强制**。
    详见 :func:`freeagent.services.executors._v1_read_only_config`。
    """
    return current_adapter().build_read_only_config()


def build_isolated_config(config: dict[str, Any] | None = None) -> str:
    """把配置写成 JSON 文本。**返回文本而不写文件** —— 写文件是 IO，要可测。"""
    return json.dumps(
        config if config is not None else delegation_permission_config(),
        ensure_ascii=False, indent=2,
    )


def oc_cache_dir() -> pathlib.Path:
    """插件包的常驻缓存目录（隔离回退路径用，不随 jail 删）。

    隔离实例（``isolate=True``）的家目录是一次性 jail，关掉就删；把缓存指到
    本项目常驻目录，插件只装一次。非隔离的派发不再需要它 —— 系统 opencode
    用自己的真实缓存（离线、零重装）。
    """
    from ..paths import state_file

    return pathlib.Path(state_file(None, "oc-cache"))


log = logging.getLogger(__name__)

#: 探针给实例多长的**等待窗口**（秒）。为什么不是「问一次就知道」：
#: 插件是**首次启动时装**的，多个插件实测要几分钟。装包期间 ``/agent``
#: 就是不可用 —— 若一次判定就降级，会把**正在装包的进程杀掉**，于是下一
#: 次又从头装、又被杀，**永远装不完**。所以必须轮询等，等到装完。
_PROBE_WINDOW = 180.0

#: 探针两次尝试之间的间隔（秒）。
_PROBE_INTERVAL = 3.0


def _child_log_dir() -> pathlib.Path:
    """子进程日志目录。跟插件缓存同居（都不该随 jail 删）。"""
    return oc_cache_dir() / "logs"


def _prune_child_logs(log_dir: pathlib.Path, *, keep: int = 20) -> None:
    """只留最近 ``keep`` 个日志。日志会一直攒，不清理会占满磁盘。"""
    try:
        # ``list()`` 当场耗尽 glob 生成器：留着不关会在 GC 时抛
        # ResourceWarning（ScandirIterator），pytest 把 unraisable 当错误。
        files = sorted(list(log_dir.glob("oc-*.log")),
                       key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for old in files[:-keep]:
        try:
            old.unlink()
        except OSError:
            pass


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
    cache_dir: pathlib.Path | None = None,
) -> dict[str, str]:
    """构造子进程环境。**白名单式：不继承 :data:`os.environ`。**

    纯函数（无 IO、无 spawn）所以能直接测「子进程能看到什么」。

    :param jail: 一次性家目录。``HOME`` 与各类 XDG/缓存/临时目录**全部**指向它。
    :param cache_dir: 例外：**缓存与数据目录**指向这里而不是 jail。默认
        ``None``（= 关进 jail，旧行为）。给插件用 —— jail 随服务关闭一起
        删，插件每次重装（实测 107.7s）；指到常驻目录就是装一次、装完复用
        装配结果。它不是用户的真实目录，所以「不写回真实家目录」这条仍然
        成立；例外带来的代价见下面的注释（``opencode.db`` 跨委派留存）。
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

    # 3b) 缓存例外：插件包装在常驻目录里，不然每次委派都重装一遍。
    #
    # **光把包放常驻目录还不够**（实测踩过）：包确实没重装（packages 的
    # mtime 没变），但启动仍要 60s。查 jail 里剩下什么，发现
    # ``npm-cache/_cacache`` 与 ``opencode/node_modules`` —— **安装器的缓存**
    # 跟着 ``HOME``/``TEMP`` 落进了 jail，于是包虽在，npm 每次仍要从零建缓存：
    # 重新解析依赖、重新取元数据。Windows 上 npm 拿 ``TEMP`` 当缓存根，
    # 而 TEMP 是我们关进 jail 的那几个之一。
    #
    # 所以缓存例外必须是「包 + 安装器」两处，少一处就白搭。
    if cache_dir is not None:
        env["XDG_CACHE_HOME"] = str(cache_dir)
        env["npm_config_cache"] = str(cache_dir / "npm-cache")
        #: bun 是 opencode 实际用来装包的那个；它的缓存同样默认跟 HOME 走。
        env["BUN_INSTALL_CACHE_DIR"] = str(cache_dir / "bun-cache")
        #
        # **数据目录也在例外里**（用户 2026-10-09 拍板）。只例外缓存时，包
        # 与安装器缓存都常驻了，opencode 却仍要**每次把插件重新装配进运行时
        # 目录**（jail 里可见 ``opencode/node_modules``、``repos``、
        # ``locks``）：1 个插件 24.7s，7 个插件 >290s 仍未就绪。装配结果落在
        # ``XDG_DATA_HOME`` 下，所以它不常驻就躲不掉。
        #
        # 代价（已知且接受）：``opencode.db``（项目注册表与会话）会**跨委派
        # 留存**，实例之间不再「每次全新」。安全边界不变 —— 目录仍是我们
        # 自己的 ``~/.freeagent`` 常驻目录，不是用户真实家目录；
        # ``XDG_CONFIG_HOME``（配置）与 ``HOME``/``TEMP`` **仍关在 jail**，
        # 云凭据与 ``~/.ssh`` 的隔离没有被削弱。
        env["XDG_DATA_HOME"] = str(cache_dir / "data")

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


def question_from_event(properties: Any) -> ToolQuestion | None:
    """当前适配器对 ``question.asked`` 事件的解析。

    与 :func:`permission_from_event` 同一约定：**返回 ``None`` 表示这不是
    一条能回应的提问**，绝不用半截形状凑 —— 那会让上层把「不认识」当成
    「用户没答」，而这两件事该走的处置完全不同（前者不该发任何东西，
    后者该按超时处理）。
    """
    return current_adapter().parse_question(properties)


def question_reply_payload(answers: list[list[str]]) -> dict[str, Any]:
    """把逐轮积累的答复翻成**当前适配器**的提问答复载荷。

    V1 实测只接受**嵌套数组** ``{"answers": [["…"]]}``，扁平字符串被拒。
    形状与「空答复不许发」的校验都在适配器里，所以这里只做委托 ——
    与 :func:`reply_payload` 同一层，不在这里内联任何版本形状。
    """
    return current_adapter().build_question_reply(answers)


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
        isolate: bool = True,
        permission_config: dict[str, Any] | None = None,
        apply_gate: bool = False,
    ) -> None:
        self._project = pathlib.Path(project)
        self._exe = executable
        self._port = port or free_port()
        self._startup_timeout = startup_timeout
        self._extra_env = dict(extra_env or {})
        self._isolate = isolate
        # 权限配置可注入。默认委派那份（``edit: ask``，有人点），
        # 而 ``/ask`` 传 :func:`read_only_permission_config`（``edit: deny``）。
        #
        # 刻意**存下整个 dict 而不是一个布尔开关**：将来若还有第三种
        # （比如「只读 + 允许出网」），加一个参数比加一个 if 分支更容易看清
        # 「有几种权限形状、分别是谁在用」。
        self._permission_config = permission_config
        #: 是否把本项目审批闸门写成**项目级覆盖层**压进 opencode 的真实配置。
        #: 只给**派发 / /ask**（要强制闸门，且要借用用户原生的全局插件/MCP）；
        #: 发现（``discovery()``）不开 —— 它只读，也不该动项目目录。
        self._apply_gate = apply_gate
        #: 覆盖层落盘状态（见 :meth:`_write_overlay` 与 :meth:`close`）。
        self._overlay_path: pathlib.Path | None = None
        self._overlay_backup: bytes | None = None
        #: 是否写过覆盖层。**必须在 __init__ 初始化**：``close()`` 无条件调
        #: :meth:`_restore_overlay`，而它第一句就读这个属性。不写闸门（``apply_gate
        #: = False``，如 ``discovery()``）时 :meth:`_write_overlay` 不会被调用，
        #: 若这里不兜底初始化，``close()`` 会 ``AttributeError`` 崩溃。
        self._overlay_written = False
        #: 子进程日志落盘位置（诊断用，见 :meth:`child_log`）。
        self._log_path: pathlib.Path | None = None
        #: 日志文件句柄。自己拿着、自己关（见 :meth:`_popen`）。
        self._log_file: Any = None
        #: 探针最后一次失败的原因（含子进程日志尾部，见 :meth:`_probe_ok`）。
        self._last_probe_error: str = ""
        self._proc: Any = None
        self._home: pathlib.Path | None = None
        self._password = uuid.uuid4().hex
        # 每次起一个随机密码：服务只监听回环，但**同机别的进程能连**。
        # 固定密码等于把「谁都能开 opencode 改你代码」的门开着。
        self._auth = "Basic " + base64.b64encode(
            f"opencode:{self._password}".encode()
        ).decode()

    # -- 生命周期 -------------------------------------------------------- #
    def effective_config(self) -> dict[str, Any]:
        """本项目要强制的**审批闸门**（``permission`` 配置）。

        只返回 ``permission`` 这一项。插件 / MCP / 技能等**能力**一律不在这里
        给 —— 它们由 opencode 原生从用户的全局 / 项目配置加载（这正是终端里
        跑 opencode 的行为）。我们不去复制那份配置，否则会：① 触发 opencode
        的 ``null`` 作废整份配置（实测 502），② 每次重装插件，③ 离了网就坏。

        闸门通过**项目级覆盖层**压进真实配置（见 :meth:`_write_overlay`），
        而不是另起一个隔离世界。
        """
        # 本项目审批闸门。能力（插件/MCP/技能）一律不在这里给 —— 它们由
        # opencode 原生从用户的全局/项目配置加载，我们不去复制（那样会重装、
        # 且离了网就坏）。覆盖层机制见 :meth:`_write_overlay`。
        return dict(self._permission_config) if self._permission_config is not None \
            else dict(delegation_permission_config())

    def start(self) -> "OpenCodeServer":
        # 非隔离 = 直接跑系统 opencode（真实全局配置 → 用户原生的全局插件 /
        # MCP / 技能，**离线、零重装**）。若开了闸门，把本项目 permission 写成
        # 项目级覆盖层压进去（见 :meth:`_write_overlay`），随 close 还原。
        if not self._isolate:
            if self._apply_gate:
                self._write_overlay()
            return self._start_unisolated()
        self._spawn_isolated()
        if not self._wait_ready():
            self.close()
            raise ServerError(
                f"opencode 服务没在 {self._startup_timeout:g}s 内就绪"
                f"（端口 {self._port}）"
            )
        return self

    def _probe_ok(self, *, window: float = _PROBE_WINDOW) -> bool:
        """实例是否**真的能用**，不只是「起来了」。

        用 ``/agent`` 当探针：它是最先被继承内容搞坏的端点（实测 502），
        而派发第一步就要查它 —— 拿它试，坏在试的时候就暴露，而不是等
        真正派发时才炸。

        失败原因（含子进程日志尾部）存进 :attr:`_last_probe_error` —— 502
        本身不含任何信息，原因只写在子进程的 stderr 里。

        轮询等而不是问一次：插件首次启动要装几分钟（窗口见
        :data:`_PROBE_WINDOW`）。进程自己退了就别再等。
        """
        deadline = time.monotonic() + window
        while True:
            if self._proc is not None and self._proc.poll() is not None:
                self._last_probe_error = "子进程已退出"
                return False
            try:
                code, _ = self.call("/agent", timeout=10.0)
            except Exception as exc:  # noqa: BLE001 - 探针自己不该抛出去
                self._last_probe_error = f"{type(exc).__name__}: {exc}"
            else:
                if code == 200:
                    self._last_probe_error = ""
                    return True
                self._last_probe_error = f"GET /agent → HTTP {code}"
            if time.monotonic() >= deadline:
                tail = self.child_log(max_chars=1200)
                if tail:
                    self._last_probe_error += f"\n子进程日志尾部:\n{tail}"
                return False
            time.sleep(_PROBE_INTERVAL)

    def _spawn_isolated(self) -> None:
        """起一个隔离实例（写配置 → 起进程）。**不**等就绪。"""
        self._home = pathlib.Path(tempfile.mkdtemp(prefix="freeagent-oc-"))
        cfg_dir = self._home / "opencode"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        # 配置**在启动前**写好，不走 PATCH —— 少一次往返，也少踩
        # 「PATCH 返回 200 但没落盘」那个坑（实测踩过）。
        (cfg_dir / "opencode.json").write_text(
            build_isolated_config(self.effective_config()), encoding="utf-8"
        )

        env = build_child_env(
            jail=self._home,
            exe=shutil.which(self._exe) or self._exe,
            cache_dir=oc_cache_dir(),
            extra={
                "OPENCODE_SERVER_USERNAME": "opencode",
                "OPENCODE_SERVER_PASSWORD": self._password,
                **self._extra_env,
            },
        )

        # 进程组参数与 ``--hostname`` 的理由见 :meth:`_popen`（两个入口共用它，
        # 免得「回环 + 独立进程组」这两条安全属性在一个入口里漏掉）。
        self._proc = self._popen(env)

    def _write_overlay(self) -> None:
        """把本项目审批闸门写成**项目级覆盖层**压进 opencode 的真实配置。

        机制（实测确认）：opencode 加载「全局配置 + 项目级 ``opencode.jsonc``」
        并**项目层优先**。所以只写一份**只有 ``permission``** 的项目配置，
        就能在不碰用户全局配置的前提下强制闸门，同时保留用户原生的全部插件 /
        MCP / 技能（合并而非替换）。这比「另起隔离世界 + 复制用户配置」简单得多，
        且离线、零重装。

        覆盖层是临时文件：退出时由 :meth:`_restore_overlay` 还原，不污染项目。
        """
        # 尊重已有配置文件名（``.jsonc`` 或 ``.json``），写同名的覆盖层；
        # 关闭时原样还原。若原本没有，则我们创建的那份删掉。
        cfg_path: pathlib.Path | None = None
        for cand in ("opencode.jsonc", "opencode.json"):
            p = self._project / cand
            if p.exists():
                cfg_path = p
                break
        if cfg_path is None:
            cfg_path = self._project / "opencode.jsonc"
        self._overlay_backup = cfg_path.read_bytes() if cfg_path.exists() else None
        self._overlay_path = cfg_path
        # ``effective_config()`` 返回的是**完整配置**对象（含 ``$schema`` 与
        # ``permission`` 两个键，见 :meth:`effective_config` 的文档）—— 它给
        # 隔离实例当整份配置用。但项目级覆盖层只想压 ``permission`` 一项，
        # **不能把整份对象再套一层 ``permission``**，否则会变成
        # ``{"permission": {"$schema": …, "permission": {…}}}`` —— 双层
        # ``permission`` 让 opencode 把这份项目配置当成非法、整份丢弃，回退到
        # 内置默认值（插件 / MCP 全空、权限 ``{}``）。这正是冒烟里「7 插件
        # 变 0、permission 变 {}」的根因。所以这里只取内层规则。
        full = self.effective_config()
        rules = full.get("permission", full) if isinstance(full, dict) else full
        cfg_path.write_text(
            json.dumps({"permission": rules}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._overlay_written = True

    def _restore_overlay(self) -> None:
        """还原项目级覆盖层（见 :meth:`_write_overlay`）。"""
        if not self._overlay_written:
            return
        path = self._overlay_path
        self._overlay_written = False
        self._overlay_path = None
        if path is not None:
            try:
                if self._overlay_backup is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(self._overlay_backup)
            except OSError:
                pass
        self._overlay_backup = None

    def _start_unisolated(self) -> "OpenCodeServer":
        """起一个**看得见真实配置**的服务。只给「发现」用（见 :meth:`discovery`）。

        ## 为什么需要这一条

        :func:`freeagent.services.opencode_projects.list_projects` 与四段选择
        都要问 OpenCode「有哪些项目 / 哪些模型 / 哪些 Agent」，而那些答案
        **只存在于你真实的配置里**：

        - 项目注册表在 ``%USERPROFILE%\\.local\\share\\opencode\\opencode.db``
        - provider 的登录状态在真实配置目录的 auth 文件里

        隔离实例把 ``HOME`` / ``XDG_*`` 全指向一次性目录，于是它**看到的是
        一个空世界**：``/project`` 返回空数组、``connected`` 为空。症状是
        「明明有 3 个项目，界面说一个都没有」—— 而那被误读成
        「白名单没配」，于是往错的方向查（实测踩过：
        ``tests/test_opencode_projects_live.py`` 就是这么红的）。

        ## 边界：只读，且不写任何配置

        这一条**只用于 GET（发现）**。绝不能拿它发 prompt —— 那样委派就会
        读到你的真实配置（插件、模型、权限），而 :mod:`services.delegate` 的
        全部安全声明都建立在「闸门已被强制」之上。

        派生用法（派发 / ``/ask``）也复用本方法，但会在调用前用
        :meth:`_write_overlay` 把审批闸门写成项目级覆盖层 —— 那是唯一的
        新增文件，且 :meth:`close` 会原样还原。除此之外本方法不写任何配置、
        不 ``PATCH /config``：唯一新增的是随机密码，只存在于这个对象里。
        """
        env = dict(os.environ)
        env.update({
            "OPENCODE_SERVER_USERNAME": "opencode",
            "OPENCODE_SERVER_PASSWORD": self._password,
            **self._extra_env,
        })
        self._proc = self._popen(env)
        if not self._wait_ready():
            self.close()
            raise ServerError(
                f"opencode 发现服务没在 {self._startup_timeout:g}s 内就绪"
                f"（端口 {self._port}）"
            )
        return self

    def _popen(self, env: dict[str, str]) -> Any:
        """起进程。进程组参数**两个平台不同**（Windows 要 creationflags、
        POSIX 要 ``start_new_session``）。刻意用显式分支而不是
        ``**({"creationflags": f} if win else {"start_new_session": True})``：
        后者两个分支的**值类型不同**，类型检查器会把整个 dict 推成
        ``int | bool``，于是 Popen 的每个参数都报「int 不能赋给 bool」——
        12 条假错误淹掉真错误。踩过，故留此注记。

        子进程的两个流**落盘而不是丢掉**：插件加载失败时 opencode 只往
        stderr 写一句，``GET /agent`` 就变成 502，而我们这边只能看见 502。
        丢流等于把唯一的线索扔了（实测为此二分了半天也没定位到是哪个插件）。
        """
        popen_kwargs: dict[str, Any] = {}
        if os.name == "nt":
            # CREATE_NO_WINDOW 隐藏子进程控制台窗口：Windows 上 opencode 是控制台
            # 程序，不加这个，每次拉起都会弹一个黑框、随进程退出自动关闭（用户点
            # 「编程」时一连串黑框闪过的真因）。与 CREATE_NEW_PROCESS_GROUP 位或：
            # 后者用于向进程组广播 Ctrl+C 杀进程，二者位不冲突。
            popen_kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            )
        else:
            popen_kwargs["start_new_session"] = True

        # 一个实例一个文件：并发实例不会互相抢写锁（Windows 上共享写会失败）。
        # 句柄**自己拿着并在 close() 里关**：交给 GC 会抛 ResourceWarning，
        # 而 pytest 把 unraisable 当错误 —— 一批无关测试因此变红（踩过）。
        stream: Any = subprocess.DEVNULL
        handle: Any = None
        try:
            log_dir = _child_log_dir()
            log_dir.mkdir(parents=True, exist_ok=True)
            self._log_path = log_dir / f"oc-{self._port}-{int(time.time())}.log"
            handle = self._log_path.open("w", encoding="utf-8", errors="replace")
            stream = handle
            _prune_child_logs(log_dir)
        except OSError:
            if handle is not None:
                handle.close()
                handle = None
            stream = subprocess.DEVNULL  # 日志是增益，写不了照样得起服务
            self._log_path = None
        self._log_file = handle
        try:
            return subprocess.Popen(
                # ``--hostname`` **显式给回环**，不靠默认值。随机密码已经在了，
                # 但「只在回环监听」是更靠前的一层 —— 它决定同机别的进程
                # 能不能看见这个端口。参照 qm 的 ``--hostname=127.0.0.1``。
                [shutil.which(self._exe) or self._exe,
                 "serve", "--hostname", "127.0.0.1",
                 "--port", str(self._port)],
                stdout=stream, stderr=stream,
                cwd=str(self._project), env=env, shell=False,
                **popen_kwargs,
            )
        except OSError as exc:
            self._close_log()
            self._cleanup_home()
            raise ServerError(f"起不了 opencode 服务：{exc}") from exc

    def _close_log(self) -> None:
        """关掉子进程日志句柄。见 :meth:`_popen`：交给 GC 会污染别人的测试。"""
        handle = getattr(self, "_log_file", None)
        self._log_file = None
        if handle is not None:
            with contextlib.suppress(Exception):
                handle.close()

    def child_log(self, *, max_chars: int = 4000) -> str:
        """最近一次子进程的 stdout+stderr 尾巴。**诊断用**。

        排查「派发 502 / 插件没生效」时先读这个：opencode 把插件加载失败
        的原因只写在这里，HTTP 层只能看到一个 502。
        """
        path = getattr(self, "_log_path", None)
        if path is None or not pathlib.Path(path).exists():
            return ""
        try:
            text = pathlib.Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return text[-max_chars:]

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
        self._restore_overlay()  # 先还原项目级覆盖层，别污染项目目录
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True, timeout=60, check=False,
                    # taskkill 也是控制台程序，不加这个每次收口都弹黑框
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(os.getpgid(proc.pid), 9)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=10)
            if proc.poll() is None:  # pragma: no cover - 兜底
                with contextlib.suppress(OSError):
                    proc.kill()
        self._close_log()
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

    @classmethod
    def discovery(cls, *, executable: str = "opencode",
                  cwd: pathlib.Path | None = None) -> "OpenCodeServer":
        """一个**只读发现**客户端：看得见你的真实配置。

        用来问「有哪些项目 / 哪些模型 / 哪些 Agent」—— 那些答案只在真实
        配置里。**不要**用它发 prompt（见 :meth:`_start_unisolated` 的边界）。

        :param cwd: 服务的工作目录。默认临时目录 —— 发现用的服务不碰任何
            项目目录，也就不会在项目里留下文件。
        """
        return cls(project=cwd or pathlib.Path(tempfile.gettempdir()),
                   executable=executable, isolate=False)

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

    def list_projects(self) -> list[dict[str, Any]]:
        r"""``GET /project`` —— 项目列表。

        ## 为什么不用 ``/config/projects``

        没有那个端点。``/project`` 是官方的（OpenAPI 里 ``/project`` 直接返回
        ``Project[]``）。

        **注意 worktree 的斜杠方向**：实测 HTTP 返回 ``D:\PycharmProjects\openmos``
        （反斜杠），而旧的 ``opencode db`` 查询返回 ``D:/PycharmProjects/openmos``。
        两种都出现过，所以 :mod:`opencode_projects` 的解析必须两种都能处理。
        """
        raw = self._must("/project")
        if isinstance(raw, list):
            return [x for x in raw if isinstance(x, dict)]
        raise ServerError(f"/project 返回了预期外的形状：{type(raw).__name__}")

    def list_agents(self) -> list[dict[str, Any]]:
        """``GET /agent`` —— 可选 Agent（工作模式）。

        实测 17 个，含 ``Sisyphus - ultraworker``（primary）、
        ``Prometheus - Plan Builder``（primary，自带 variant=xhigh）、
        ``explore`` / ``oracle`` 等 subagent。

        每个元素的 ``model`` 是 ``{providerID, modelID}``，``variant`` 可选 ——
        也就是说 **Agent 自带的默认 variant 会盖过会话级的选择**，这与
        ``UserMessage.model.variant`` 的关系需要在真机上验一次（见
        :meth:`prompt_async` 的注释）。
        """
        raw = self._must("/agent")
        if isinstance(raw, list):
            return [x for x in raw if isinstance(x, dict)]
        raise ServerError(f"/agent 返回了预期外的形状：{type(raw).__name__}")

    def list_models(self) -> dict[str, Any]:
        """``GET /provider`` —— 可选 provider / 模型 / 推理档位。

        ## 为什么用 ``/provider`` 而**不是** ``/config/providers``

        实测：``/config/providers`` 里 **2 个 provider 带明文 ``key``**。
        那个端点只配拿来做调试输出；接进界面就是把 API Key 送到浏览器里。

        ``/provider`` 是干净的（``all`` / ``default`` / ``connected``）。

        ## 返回形状

        - ``all``          所有 provider，每个带 ``models``（dict）
        - ``connected``    **已配凭据的** provider id（实测 ``['deepseek','opencode']``）
        - ``default``      provider -> 默认模型

        每个模型带 ``capabilities.reasoning``（能否推理）与
        ``variants``（可用档位，如 ``{"high": {"reasoningEffort": "high"}}``）。
        **只有 4300/8475 个模型有 variants，且没有一个带 ``default`` 档** ——
        所以「不指定推理级别」= 不施加 variant，用模型基线。
        """
        raw = self._must("/provider")
        if isinstance(raw, dict) and isinstance(raw.get("all"), list):
            return raw
        raise ServerError(f"/provider 返回了预期外的形状：{type(raw).__name__}")

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
        """发指令，**立即返回**。等的是事件流，不是这个调用。

        ## ``model`` / ``agent`` / ``variant`` 三个字段的权威形状

        取自 OpenCode 自带的 OpenAPI（``GET /doc``，实测 1.18.34）——
        ``UserMessage`` 的定义就是请求体：

            agent: string
            model: { providerID: string, modelID: string, variant?: string }
            additionalProperties: false

两条由此推出的硬事实：

        1. **推理级别叫 ``variant``，而且是 ``UserMessage`` 的**顶层**字段**
           （不是 ``model`` 对象的成员 —— 那是个真踩过的坑，见下面构造处）。
           整个规范里 ``reasoningEffort`` / ``reasoning_effort`` /
           ``thinking`` 出现 **0** 次，且 ``additionalProperties: false``
           —— 传它们会被直接拒绝。所以「High」= ``variant: "high"``。
        2. **``variant`` 不在 ``required`` 里** ⇒ **不指定也能工作**，
           用模型基线。实测 4300/8475 个模型带 variants，且**没有一个带
           ``default`` 档**，所以「省略」= 不施加 variant，不是「落到某个默认档」。

        ## ``model`` 这个参数的形状是 ``provider/model``

        沿用既有约定（``--model opencode/big-pickle`` 与
        ``DelegationPolicy.model`` 都是这个形状），这里只是把它拆回
        ``providerID`` / ``modelID``。所以
        ``model="opencode/fledge-alpha-free", variant="high"`` 会变成
        ``{"model": {"providerID": "opencode", "modelID": "fledge-alpha-free"},
        "variant": "high", "parts": [...]}``。
        """
        path = f"/session/{session_id}/prompt_async"
        if directory:
            path += f"?directory={urllib.parse.quote(directory, safe='')}"
        payload: dict[str, Any] = {"parts": [{"type": "text", "text": brief}]}
        if model:
            provider, _, name = model.partition("/")
            # **``variant`` 不在这里** —— 见下面「variant 在顶层」那段。
            payload["model"] = {
                "providerID": provider,
                "modelID": name or provider,
            }
        if variant:
            # **variant 是 ``UserMessage`` 的顶层字段**，不是 ``model`` 的成员。
            #
            # 踩过的坑（实测 2026-10-07，opencode 1.18.34）：第一版把它塞进
            # ``model`` 里，依据是 OpenAPI 里 ``Session.model`` 的形状——
            # 而那**不是** ``UserMessage.model``。后果极其隐蔽：
            #   - 服务端**不报错**（HTTP 204，与乱传档名表现完全一样）
            #   - ``GET /session/{id}`` 回显 ``variant: "default"``
            #   - 而同一个模型的 ``opencode run --variant high`` 回显 ``high``
            # 也就是说「选了 High 却没生效」，且**没有任何报错可查**。
            #
            # 正确形状由抓 CLI 的真实请求确认：
            #   ``{"model": {"providerID","modelID"}, "variant": "high", "parts": [...]}``
            #
            # 只在真的指定时才带 —— 省略与带空串**不是一回事**（空串会被
            # 当成 variant 名去查，然后查不到）。
            payload["variant"] = variant
        if agent:
            payload["agent"] = agent
        # **这一行曾经丢过**，而 prompt_async 因此变成静默的 no-op：
        # 它把 payload 构造好就返回，什么都没发。断言落在假的 server 上，
        # 于是测试全绿而真机不工作。构造完**必须**发出去。
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

    def pending_questions(
        self, *, directory: str | None = None
    ) -> list[Any]:
        """列出**当前挂起**的提问。

        与 :meth:`pending_permissions` 同一个理由：**恢复现场**。执行器重启后
        靠它把「上次问过但没人答」的那些捞出来，而不是让 opencode 一直
        等一个不会来的答复。

        刻意**不轮询它**当主路径 —— 主路径走事件流，轮询只是重启后的兜底。
        """
        path = "/question"
        if directory:
            path += f"?directory={urllib.parse.quote(directory, safe='')}"
        got = self._must(path)
        return list(got) if isinstance(got, list) else []

    def reply_question(
        self,
        request_id: str,
        answers: list[list[str]],
        *,
        directory: str | None = None,
    ) -> None:
        """把人的答复送回 opencode（**一次问一个**攒起来的那份）。

        端点是 ``POST /question/{id}/reply``，与 permission 那条**不是同一个**
        —— 实测过，别把两条路径合并成一个方法：形状不同、失败含义也不同。

        ``request_id`` 必须 URL 转义：它是执行器给的串，我们不保证它不含
        斜杠之类需要转义的字符，而未转义的路径会被 ``/`` 切成多段，
        打到**别的端点**上 —— 那种失败看起来像「服务端说不行」。
        """
        path = f"/question/{urllib.parse.quote(request_id, safe='')}/reply"
        if directory:
            path += f"?directory={urllib.parse.quote(directory, safe='')}"
        self._must(path, "POST", question_reply_payload(answers))

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
