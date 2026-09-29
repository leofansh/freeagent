"""OpenAI 兼容族的 provider 注册表。

## 这一轮只收「兼容形状」，不收 Claude / Gemini

``deepseek.py`` 全文只有三处品牌名（``PROVIDER_NAME`` 和两个默认值），
请求体是标准 ``model``/``temperature``/``max_tokens``/``messages``，
鉴权是 ``Authorization: Bearer``，响应走 ``choices[0].message.content``。
换句话说**它已经是个 OpenAI 兼容客户端，只是默认值写死了**。所以支持
第二家不是重写，是加一张表。

而 Claude / Gemini **不是这个形状**：鉴权头不同、请求体不同、响应不同，
有的还不支持 ``response_format``。硬接进来的后果不是报错，是**静默劣化** ——
``provider.py`` 五个方法里有三个硬依赖 JSON 输出质量（``classify`` 要结构化
JSON、``draft`` 要带 ``[TODO]`` 骨架、``suggest_schedule``）。换一家 JSON
不可靠的 provider，``classify`` 会频繁返回 ``need_clarification``，
用户看到的表现是「这助手老是反问我」，而日志里一片正常。

**真要接它们，得先扩 ``LLMProvider`` 协议、加一层结构化输出能力声明**，
让 provider 自己说支持 ``json_schema`` 还是 ``json_object`` 还是「仅靠
prompt」，声明不了就**不接** —— 而不是接了在生产里悄悄降级。
这一轮的范围不含那件事。

## 凭据与环境变量的关系

每个 provider 有自己的 Key 变量名，且**所有 provider 的 Key 共存于同一个
``llm.env``**。这样在 provider 之间来回切不会丢 Key（Hermes 也是单文件）。

优先级是「环境变量优先、文件兜底」：真实环境变量是显式动作（临时换模型做
对比测试），文件是持久配置。界面上会**如实显示当前生效的是哪一个**。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

__all__ = [
    "ProviderProfile",
    "PROVIDERS",
    "DEFAULT_PROVIDER_ID",
    "get_profile",
    "all_key_env_vars",
    "discover_models",
    "DiscoveryError",
]

#: 旧配置（没有 ``llm.provider``）落到这里，行为与改动前**完全一致**。
DEFAULT_PROVIDER_ID = "deepseek"


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    """一家 provider 的静态事实。

    :param id: 写进 ``config.json`` 的稳定标识
    :param label: 界面显示名
    :param default_base_url: 选它时预填的接口地址
    :param default_model: 选它时预填的模型名
    :param key_env_var: 它的 Key 对应的环境变量名（也是 ``llm.env`` 里的键）
    :param needs_key: ``False`` = 本地服务、不用鉴权
    :param models: **提示用的精选列表，不是权威清单**
    :param note: 界面上要交代的话（比如「要自己先 pull」）

    关于 :attr:`models` 的诚实说明：模型目录各家改得很勤，写死在代码里
    很快就是错的（用户选了 → 400 → 以为是界面坏了）。所以它**只是下拉里的
    快捷项**，权威来源是 :func:`discover_models` 打的 ``/models``，两者都拿不到
    时还有手输逃生舱。留空表示「别列了，直接让用户填」。
    """

    id: str
    label: str
    default_base_url: str
    default_model: str
    key_env_var: str
    needs_key: bool = True
    models: tuple[str, ...] = ()
    note: str = ""
    #: 这个 provider 要填地址才能用（``custom`` 没有默认可用地址）
    requires_base_url: bool = False

    def with_defaults(self, model: str, base_url: str) -> "ResolvedProvider":
        """把配置里的值套到这个 profile 上，得到可直接建连接的东西。"""
        return ResolvedProvider(
            profile=self,
            model=model.strip() or self.default_model,
            base_url=(base_url.strip().rstrip("/") or self.default_base_url),
        )


@dataclass(frozen=True, slots=True)
class ResolvedProvider:
    """一个**具体的**「用哪家的哪个模型」组合。"""

    profile: ProviderProfile
    model: str
    base_url: str

    @property
    def id(self) -> str:
        return self.profile.id

    @property
    def key_env_var(self) -> str:
        return self.profile.key_env_var

    @property
    def needs_key(self) -> bool:
        return self.profile.needs_key

    @property
    def chat_endpoint(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

    @property
    def models_endpoint(self) -> str:
        return f"{self.base_url.rstrip('/')}/models"


#: 全部可选项。顺序 = 界面下拉顺序：常用的、免费的排前面。
PROVIDERS: tuple[ProviderProfile, ...] = (
    ProviderProfile(
        id="deepseek",
        label="DeepSeek",
        default_base_url="https://api.deepseek.com/v1",
        default_model="deepseek-chat",
        key_env_var="DEEPSEEK_API_KEY",
        models=("deepseek-chat", "deepseek-reasoner"),
    ),
    ProviderProfile(
        id="kimi",
        label="Kimi / 月之暗面",
        default_base_url="https://api.moonshot.cn/v1",
        default_model="moonshot-v1-32k",
        key_env_var="MOONSHOT_API_KEY",
        models=("moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"),
    ),
    ProviderProfile(
        id="qwen",
        label="通义千问 / DashScope",
        default_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        default_model="qwen-plus",
        key_env_var="DASHSCOPE_API_KEY",
        models=("qwen-turbo", "qwen-plus", "qwen-max"),
        note="用的是阿里云的「兼容模式」地址，不是 DashScope 自己的协议",
    ),
    ProviderProfile(
        id="openai",
        label="OpenAI",
        default_base_url="https://api.openai.com/v1",
        default_model="gpt-4o-mini",
        key_env_var="OPENAI_API_KEY",
        models=("gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"),
    ),
    ProviderProfile(
        id="ollama",
        label="Ollama（本机，不联网）",
        default_base_url="http://127.0.0.1:11434/v1",
        default_model="qwen3:8b",
        key_env_var="OLLAMA_API_KEY",
        needs_key=False,
        models=(),          # 本地装了什么取决于用户 pull 了什么，只能发现或手填
        note="要自己先 ollama pull；Key 留空即可",
    ),
    ProviderProfile(
        id="custom",
        label="其它 OpenAI 兼容服务",
        default_base_url="",
        default_model="",
        key_env_var="OPENAI_COMPATIBLE_API_KEY",
        models=(),
        requires_base_url=True,
        note="vLLM / LM Studio / one-api 这类；地址和模型都要自己填",
    ),
)

_BY_ID: dict[str, ProviderProfile] = {p.id: p for p in PROVIDERS}


def get_profile(provider_id: str) -> ProviderProfile | None:
    """按 id 查。**认不出返回 ``None``，不抛异常** —— 调用方要能如实显示
    「你配置里的这个 provider 我不认识」，而不是 500。"""
    return _BY_ID.get((provider_id or "").strip())


def all_key_env_vars() -> frozenset[str]:
    """所有 provider 的 Key 变量名。``llm.env`` 的白名单由此**推导**。

    推导而不是另抄一份：抄的那份会漂 —— 加了 provider 忘了加白名单，
    表现是「界面保存成功了、重启后 Key 没了」，而界面上还显示「已配置」。
    """
    return frozenset(p.key_env_var for p in PROVIDERS)


class DiscoveryError(RuntimeError):
    """列模型失败。**带上原因**，因为用户要靠它判断是没配 Key 还是地址写错。"""


def discover_models(
    base_url: str,
    *,
    api_key: str | None = None,
    timeout: float = 10.0,
) -> tuple[str, ...]:
    """打 ``GET {base_url}/models``，返回模型 id 列表。

    OpenAI 兼容的通用约定，所以**不需要每家单独实现** —— 这正是把范围限定在
    「兼容族」的回报。

    失败**不静默**：界面上要区分「没配 Key」「地址不通」「这家不支持列模型」
    「Key 无效」，因为这四种的下一步动作完全不同。
    """
    url = f"{base_url.rstrip('/')}/models"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = _http_detail(exc)
        if exc.code in (401, 403):
            raise DiscoveryError(f"Key 无效或没有权限（HTTP {exc.code}）{detail}") from exc
        raise DiscoveryError(f"HTTP {exc.code}{detail}") from exc
    except urllib.error.URLError as exc:
        raise DiscoveryError(f"连不上 {base_url}：{exc.reason}") from exc
    except TimeoutError as exc:
        raise DiscoveryError(f"列模型超时（{timeout:g}s）") from exc
    except (OSError, ValueError) as exc:
        raise DiscoveryError(f"列模型失败：{exc}") from exc

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise DiscoveryError("这家返回的不是 OpenAI 兼容的 /models 格式")
    ids = [item.get("id") for item in data if isinstance(item, dict)]
    found = tuple(sorted({i.strip() for i in ids if isinstance(i, str) and i.strip()}))
    if not found:
        raise DiscoveryError("这家没有返回任何模型 id")
    return found


def _http_detail(exc: urllib.error.HTTPError) -> str:
    """从错误响应里挖一句人话，别只丢个状态码。"""
    try:
        body = exc.read().decode("utf-8", "replace")[:300]
    except OSError:
        return ""
    for marker in ('"message"', '"error"'):
        if marker in body:
            return f"（{body[body.find(marker):][:200]}）"
    return f"（{body[:120]}）" if body.strip() else ""
