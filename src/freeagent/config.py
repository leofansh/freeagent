"""配置加载。

**秘密与非秘密分开处理**（安全上的硬要求）：

* **API Key 只从环境变量 ``DEEPSEEK_API_KEY`` 读，永不落盘、永不进配置文件、
  永不进日志与错误信息。**
* model / base_url / timeout / 精力档位这类非秘密项可以写进
  ``~/.freeagent/config.json``，环境变量优先。

配置来源优先级（后者覆盖前者）：

1. 内置默认值
2. ``~/.freeagent/config.json``（``FREEAGENT_HOME`` 可改目录）
3. 环境变量
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from .domain import ValidationError
from .paths import state_file
from .secrets import SecretStr
from .services.llm.deepseek_vision import DEFAULT_VISION_MODEL
from .services.sorting import EnergyWindows

__all__ = [
    "Config",
    "load_config",
    "save_config",
    "config_from_settings",
    "overridden_by_env",
    "config_path",
    "API_KEY_ENV",
    "MODEL_ENV",
    "BASE_URL_ENV",
    "TIMEOUT_ENV",
    "ENERGY_ENV",
    "DISCLAIM_PROVIDER",
    "PROVIDER_ENV",
    "DEFAULT_PROVIDER",
    "resolve_provider",
]

API_KEY_ENV = "DEEPSEEK_API_KEY"
MODEL_ENV = "DEEPSEEK_MODEL"
BASE_URL_ENV = "DEEPSEEK_BASE_URL"
TIMEOUT_ENV = "DEEPSEEK_TIMEOUT"
#: 视觉模型**独立**于文本模型：文本可能配 deepseek-pro，但视觉只有
#: deepseek-flash 能用（2026-09 起该模型原生多模态）。
VISION_MODEL_ENV = "DEEPSEEK_VISION_MODEL"

#: 开启后，即使有 key 也不用真实模型（用于演示与离线排查）。
DISCLAIM_PROVIDER = "FREEAGENT_RULES_ONLY"

#: 有 key 也没有网络时，是否自动降级到规则层。
_ALLOW_FALLBACK_ENV = "FREEAGENT_ALLOW_FALLBACK"

ENV_FILE_NAME = "config.json"

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_TIMEOUT = 20.0

#: 没写 ``llm.provider`` 的旧配置落到这里。
#:
#: 刻意写成**字面量**而不是从 ``services.llm.providers`` 导入：
#: ``config`` 在模块级不依赖 ``services``（见 :meth:`Config.delegate_policy`
#: 的说明），而这个值**本来就不该跟着注册表变** —— 它表示「这份配置产生于
#: 多 provider 之前」，语义上永远是 DeepSeek。哪天把 ``deepseek`` 从注册表
#: 删掉，正确做法是**迁移旧配置**，而不是让旧配置指到别家去。
DEFAULT_PROVIDER = "deepseek"

#: provider 的环境变量覆盖名。与 MODEL_ENV / BASE_URL_ENV 同一套优先级语义。
PROVIDER_ENV = "FREEAGENT_LLM_PROVIDER"


def config_path(home: str | Path | None = None) -> Path:
    """配置文件路径。目录不存在则返回预期路径（不创建）。

    解析逻辑**不在这里** —— 下沉到 :mod:`freeagent.paths`，因为 ``llm.env``
    要落同一个目录，而本模块又要读 ``llm.env``。解析放在这里的话
    ``llm_env`` 就得反向 import 本模块，形成环。
    """
    return state_file(home, ENV_FILE_NAME)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"配置文件不是合法 JSON：{path}（{exc.msg}）") from exc
    except OSError as exc:
        raise ValidationError(f"读不了配置文件：{path}（{exc.strerror}）") from exc
    if not isinstance(raw, dict):
        raise ValidationError(f"配置文件顶层必须是对象：{path}")
    return raw


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValidationError(f"环境变量 {name} 必须是数字，收到 {raw!r}") from exc
    if value <= 0:
        raise ValidationError(f"环境变量 {name} 必须大于 0，收到 {value}")
    return value


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _provider_profile(provider: str):
    """按 id 取 provider 档案。**``config`` 在模块级不依赖 ``services``**，
    所以这层惰性导入是刻意的（理由同 :meth:`Config.delegate_policy`）。"""
    from .services.llm.providers import get_profile

    return get_profile(provider)


def resolve_provider(raw: Any) -> str:
    """把配置里的 ``llm.provider`` 收敛成一个**认识的** id。

    没写（老配置）→ :data:`DEFAULT_PROVIDER`。写了但不认识 → **报错**，
    不静默回落。理由：静默回落的表现是「我明明选了 Kimi，跑起来还是
    DeepSeek」，而界面上 provider 下拉显示着 Kimi、请求却打到了别家 ——
    这类「界面说一套、实际做另一套」的问题排查成本极高。

    公开而不是私有：web 层落 Key 时也要用它决定「写进哪个变量名」，
    两处各自校验一遍必然漂。
    """
    from .services.llm.providers import PROVIDERS, get_profile

    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return DEFAULT_PROVIDER
    text = str(raw).strip()
    if get_profile(text) is not None:
        return text
    known = "、".join(p.id for p in PROVIDERS)
    raise ValidationError(
        f"不认识的服务商 {text!r}。可选：{known}。"
        "（这一版只支持 OpenAI 兼容的那几家；Claude / Gemini 还不行，"
        "原因见 services/llm/providers.py 的模块说明。）"
    )


def _energy_from(payload: Mapping[str, Any]) -> EnergyWindows | None:
    raw = payload.get("energy_windows")
    if raw is None or raw is False:
        return None
    if raw is True:
        return EnergyWindows()
    if not isinstance(raw, dict):
        raise ValidationError("config.energy_windows 必须是 true/false 或对象")
    fields = {}
    for band in ("morning", "afternoon", "evening"):
        value = raw.get(band)
        if value is None:
            continue
        if not (isinstance(value, list) and len(value) == 2):
            raise ValidationError(f"config.energy_windows.{band} 必须是 [起始小时, 结束小时]")
        try:
            fields[band] = (int(value[0]), int(value[1]))
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                f"config.energy_windows.{band} 的小时必须是整数"
            ) from exc
    return EnergyWindows(**fields) if fields else EnergyWindows()


@dataclass(frozen=True, slots=True)
class Config:
    """非秘密配置。``api_key`` 单独持有，且**不会被序列化**。"""

    model: str = DEFAULT_MODEL
    vision_model: str = DEFAULT_VISION_MODEL
    base_url: str = DEFAULT_BASE_URL
    timeout: float = DEFAULT_TIMEOUT
    #: 用哪家的模型。见 :mod:`freeagent.services.llm.providers`。
    provider: str = DEFAULT_PROVIDER
    energy_windows: EnergyWindows | None = None
    llm_enabled: bool = True
    allow_fallback: bool = True
    #: 让**模型**挑只读视图（``LLMProvider.select_view``）。**默认关。**
    #:
    #: 默认关不是因为这条路没实现 —— 它有协议、有实现、有 13 条接缝测试。
    #: 是因为**实测没赢**（2026-09-29，黄金语料 15 条，真 DeepSeek）：
    #:
    #:   关键词表  路由 14/15 = 93%   自报 13/15 = 87%
    #:   真模型    路由 13/15 = 87%   自报 11/15 = 73%
    #:
    #: 输在更重的那一项上：模型**一次都没返回「不匹配」**。提示里明确要求
    #: 挑不出就返回 null，它仍每次都自信地挑一个 —— 于是「自报猜的」这条
    #: 防线被悄悄拆掉，而那正是为了「宁可承认不知道」才建的。
    #:
    #: 也就是说：照现状打开它，系统会**更自信也更不诚实**。等 null 行为
    #: 做对、语料再大一些（现在一条样本就是 7%，n=15 不足以下结论），
    #: 再把它默认打开。**量出来的结论就照着量，别凭「架构上更该这样」。**
    llm_view_routing: bool = False
    #: 允许委派到的项目目录（绝对路径）。**空 = 禁用委派**。
    #:
    #: 这是整个委派链路最硬的安全约束：链路的终点是「在你的机器上执行代码」，
    #: 所以能去哪些目录必须是**你预先批准的清单**，不能由消息内容决定。
    delegate_projects: tuple[str, ...] = ()
    #: 委派用哪个 opencode 模型。**空 = 用 opencode 自己的默认**。
    #:
    #: 刻意不给收费的默认值：账户余额不足时收费模型会直接 402 失败。
    #: 要指定就用实测不需要余额的那批（名字带 ``-free``）。
    delegate_model: str = ""
    home: str | None = None

    @property
    def provider_profile(self):
        """当前 provider 的档案。**方法式的惰性导入**：见 :meth:`delegate_policy`。"""
        return _provider_profile(self.provider)

    @property
    def key_env_var(self) -> str:
        """当前 provider 的 Key 变量名。

        认不出的 provider 退回 :data:`API_KEY_ENV` —— 因为这个值要拿去**查凭据**，
        猜错的后果是「读了个不相干的变量」，而那会显示成「已配置」却发不出请求。
        """
        profile = self.provider_profile
        return profile.key_env_var if profile is not None else API_KEY_ENV

    @property
    def api_key(self) -> SecretStr | None:
        """拿 Key：��**环境变量优先、``llm.env`` 兜底**。未配置返回 ``None``。

        刻意用 :class:`~freeagent.secrets.SecretStr` 而不是裸 ``str``：
        这样 ``log.info("%s", config.api_key)`` 之类的手写法在**类型层面**
        就漏不出去，而 ``describe()`` 仍然能判空。参见 12.3 分层纪律。

        每次都**现读文件**，不做缓存 —— 缓存会在界面上改完 Key 之后继续返回
        旧值，而那个 bug 的表现是「保存成功但没生效」，极难查。
        """
        from .llm_env import resolve_key

        key, _source = resolve_key(self.key_env_var, home=self.home)
        return key

    @property
    def key_source(self) -> str:
        """Key 当前从哪来（人话）。界面上要显示，否则用户改错地方。"""
        from .llm_env import resolve_key

        _key, source = resolve_key(self.key_env_var, home=self.home)
        return source.label

    @property
    def has_credentials(self) -> bool:
        """能不能用真实模型。

        本机服务（``ollama``）**不需要 Key** 也算「有凭据」—— 否则界面里选
        了本地模型却永远退回规则层，而且日志上完全看不出原因。
        """
        if not self.llm_enabled:
            return False
        profile = self.provider_profile
        if profile is not None and not profile.needs_key:
            return True
        return self.api_key is not None

    def describe(self) -> dict[str, object]:
        """给 ``/llm`` 看的状态。**刻意不含 key**。"""
        profile = self.provider_profile
        return {
            "服务商": profile.label if profile is not None else f"（未知 {self.provider}）",
            "模型": self.model if self.llm_enabled else "（已停用）",
            "接口": self.base_url,
            "超时": f"{self.timeout:g}s",
            "Key": "已配置" if self.api_key else "未配置",
            "Key 来源": self.key_source,
            "允许降级": "是" if self.allow_fallback else "否",
            "精力档位": (
                "未启用"
                if self.energy_windows is None
                else "已启用（早晨/下午/晚上）"
            ),
            "配置文件": str(config_path(self.home)),
        }

    def with_energy(self, windows: EnergyWindows | None) -> Config:
        return replace(self, energy_windows=windows)

    def delegate_policy(self):
        """委派策略。**方法而不是字段**：避免 ``config`` 依赖 ``services``。

        ``services.delegate`` 才是那个策略类型的定义处。
        """
        from .services.delegate import DelegationPolicy
        from .services.oc_selection import load_selection

        # 飞书四段选择（项目/工作模式/模型/推理档）的结果。
        #
        # 刻意**只取模型与档位，不取项目** —— 项目仍走 ``projects`` 白名单
        # 那一道闸门。把选择的项目直接当准入依据等于开第二个口子：
        # 那个值来自飞书载荷（可伪造），而白名单是配置里写死的。
        # 项目已经在 :func:`services.delegate.check_project_allowed` 里被
        # 白名单校验过一遍，这里再放一次没有意义，只有风险。
        selection = load_selection(self.home)
        return DelegationPolicy(
            projects=self.delegate_projects,
            model=self.delegate_model or selection.model,
            agent=selection.agent,
            variant=selection.variant,
        )


def load_config(home: str | Path | None = None) -> Config:
    """按「默认 → 文件 → 环境变量」的顺序合并配置。

    ``home`` 为空时用 ``FREEAGENT_HOME`` 或 ``~/.freeagent``。
    显式传入时（例如 ``build_app`` 用数据库所在目录）以它为准 ——
    库和配置放在一起，避免「改了 --db 就找不到配置」。
    """
    resolved_home = str(home) if home is not None else None
    payload = _read_json(config_path(resolved_home))

    llm = payload.get("llm", {})
    if not isinstance(llm, dict):
        raise ValidationError("config.llm 必须是对象")

    energy_raw: Any = payload.get("energy_windows")
    energy_windows = _energy_from({"energy_windows": energy_raw})

    delegate_raw = payload.get("delegate") or {}
    if not isinstance(delegate_raw, dict):
        raise ValidationError("config.delegate 必须是对象")
    projects_raw = delegate_raw.get("projects") or []
    if not isinstance(projects_raw, list):
        raise ValidationError("config.delegate.projects 必须是数组")
    # 相对路径在这里就拒掉 —— 不给「以后再补全」留口子
    delegate_projects: list[str] = []
    for item in projects_raw:
        if not isinstance(item, str) or not item.strip():
            raise ValidationError("config.delegate.projects 只能是非空字符串")
        text = item.strip()
        if not Path(text).expanduser().is_absolute():
            raise ValidationError(
                f"config.delegate.projects 里必须是绝对路径：{text}"
            )
        delegate_projects.append(str(Path(text).expanduser()))

    delegate_model = str(delegate_raw.get("model") or "").strip()

    model = os.environ.get(MODEL_ENV) or llm.get("model") or DEFAULT_MODEL
    vision_model = (
        os.environ.get(VISION_MODEL_ENV)
        or llm.get("vision_model")
        or DEFAULT_VISION_MODEL
    )
    base_url = os.environ.get(BASE_URL_ENV) or llm.get("base_url") or DEFAULT_BASE_URL
    timeout = _env_float(TIMEOUT_ENV, float(llm.get("timeout", DEFAULT_TIMEOUT)))

    provider = resolve_provider(
        os.environ.get(PROVIDER_ENV) or llm.get("provider")
    )

    llm_enabled = not _truthy(DISCLAIM_PROVIDER)

    # 降级开关：环境变量优先于配置文件
    if os.environ.get(_ALLOW_FALLBACK_ENV) is not None:
        allow_fallback = _truthy(_ALLOW_FALLBACK_ENV)
    else:
        allow_fallback = bool(llm.get("allow_fallback", True))

    return Config(
        model=str(model),
        vision_model=str(vision_model),
        base_url=str(base_url).rstrip("/"),
        timeout=timeout,
        provider=provider,
        energy_windows=energy_windows,
        llm_enabled=llm_enabled,
        allow_fallback=allow_fallback,
        # 缺省 False：这条路上实测没赢（见字段注释），所以「没配」= 不开。
        llm_view_routing=bool(llm.get("view_routing", False)),
        delegate_projects=tuple(delegate_projects),
        delegate_model=delegate_model,
        home=resolved_home,
    )


# --- 运行期改配置（设置页用） ------------------------------------------------


def overridden_by_env(config: Config) -> list[str]:
    """当前被**环境变量压住**的字段名。

    必须如实告诉用户：环境变量优先级高于配置文件，界面上改了这些字段
    不会生效。不说的话，用户会以为设置坏了。
    """
    out: list[str] = []
    if os.environ.get(MODEL_ENV):
        out.append("model")
    if os.environ.get(BASE_URL_ENV):
        out.append("base_url")
    if os.environ.get(TIMEOUT_ENV):
        out.append("timeout")
    if os.environ.get(PROVIDER_ENV):
        out.append("provider")
    if _truthy(DISCLAIM_PROVIDER):
        out.append("llm_enabled")
    if os.environ.get(_ALLOW_FALLBACK_ENV) is not None:
        out.append("allow_fallback")
    return out


def _settings_energy(raw: Any) -> EnergyWindows | None:
    """从设置页输入解析精力档位。

    ``None``/``False`` = 不启用；``True`` = 用默认三档；对象 = 指定区间。
    """
    if raw is None or raw is False:
        return None
    if raw is True:
        return EnergyWindows()
    if not isinstance(raw, dict):
        raise ValidationError("精力档位必须是 false（不启用）、true（默认三档）或对象")
    out: dict[str, tuple[int, int]] = {}
    for band in ("morning", "afternoon", "evening"):
        value = raw.get(band)
        if value is None:
            continue
        if not (isinstance(value, list) and len(value) == 2):
            raise ValidationError(f"精力档位「{band}」必须是 [起始小时, 结束小时]")
        try:
            low, high = int(value[0]), int(value[1])
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"精力档位「{band}」的小时必须是整数") from exc
        if not 0 <= low < high <= 24:
            raise ValidationError(f"精力档位「{band}」必须满足 0 ≤ 起始 < 结束 ≤ 24")
        out[band] = (low, high)
    return EnergyWindows(**out) if out else None


def config_from_settings(
    settings: Mapping[str, Any], *, base: Config
) -> Config:
    """从**不可信的界面输入**构造 ``Config``。

    逐字段解析、给人话错误，而不是「拿到什么都往里塞」。

    传入 ``api_key`` 仍然**报错**，但这已经不再是「不能填 Key」，而是
    **安全网**：真正写 ``llm.env`` 的是 web 端点，它会**先把 api_key 摘走**
    再调这里。所以走到这个分支意味着「端点漏摘了」，此时必须炸 ——
    静默忽略的后果是用户以为 Key 存好了、实际没存，下次启动才发现。
    报错文案要说清是哪个环节出错，而不是继续说「不能填」。
    """
    unknown = set(settings) - {
        "model", "vision_model", "base_url", "timeout", "llm_enabled",
        "allow_fallback", "energy_windows", "provider",
    }
    if unknown:
        if "api_key" in unknown:
            raise ValidationError(
                "API Key 应该在落 llm.env 之前就被摘走，"
                "到这里说明写入流程漏了一步。请报告这个问题（Key 本身不会被记录）。"
            )
        raise ValidationError(f"不认识的设置项：{', '.join(sorted(unknown))}")

    provider = resolve_provider(settings.get("provider", base.provider))

    model = str(settings.get("model", base.model)).strip()
    if not model:
        raise ValidationError("模型名不能为空")

    vision_model = str(settings.get("vision_model", base.vision_model)).strip()
    if not vision_model:
        raise ValidationError("视觉模型名不能为空")

    base_url = str(settings.get("base_url", base.base_url)).strip().rstrip("/")
    if base_url and not base_url.startswith(("http://", "https://")):
        raise ValidationError("接口地址必须以 http:// 或 https:// 开头")

    # 「其它兼容服务」没有默认可用地址，漏填会让请求打到 "" 上，
    # 报错发生在**调用时**（几百毫秒后、且是网络错误），而不是保存时。
    profile = _provider_profile(provider)
    if profile is not None and profile.requires_base_url and not base_url:
        raise ValidationError(
            f"「{profile.label}」必须自己填接口地址（比如 http://127.0.0.1:8000/v1）。"
        )

    try:
        timeout = float(settings.get("timeout", base.timeout))
    except (TypeError, ValueError) as exc:
        raise ValidationError("超时必须是数字（秒）") from exc
    if not 0 < timeout <= 600:
        raise ValidationError("超时必须大于 0 且不超过 600 秒")

    energy_raw = settings.get("energy_windows", _ENERGY_TO_RAW(base.energy_windows))

    return Config(
        model=model,
        vision_model=vision_model,
        base_url=base_url,
        timeout=timeout,
        provider=provider,
        energy_windows=_settings_energy(energy_raw),
        llm_enabled=bool(settings.get("llm_enabled", base.llm_enabled)),
        allow_fallback=bool(settings.get("allow_fallback", base.allow_fallback)),
        # 实验开关，**刻意不进设置页**：实测它不如关键词表（见字段注释），
        # 把一个「量出来更差」的开关放到界面上，只会诱使人随手打开。
        # 和委派字段一样从 base 原样带走 —— 要试请直接改 config.json。
        llm_view_routing=base.llm_view_routing,
        # 委派字段**从 base 原样带上**。漏掉的后果不是「丢个偏好」：空
        # projects = 委派关闭，所以界面上存一次设置就会把用户那份
        # 「允许改哪些目录」的白名单**清空**，而它被文档称作整条委派链路
        # 最硬的安全约束。设置页本来就不该碰它，所以照搬、不接受输入。
        delegate_projects=base.delegate_projects,
        delegate_model=base.delegate_model,
        home=base.home,
    )


def _ENERGY_TO_RAW(windows: EnergyWindows | None) -> Any:
    """``EnergyWindows`` → 可 JSON 序列化的形式（用于默认值回填）。"""
    if windows is None:
        return None
    return {
        band: list(getattr(windows, band))
        for band in ("morning", "afternoon", "evening")
    }


def save_config(config: Config, home: str | Path | None = None) -> Path:
    """把**非秘密**配置写回 ``config.json``，返回写入的路径。

    这里**结构上不可能**写入 API Key：``Config`` 压根没有 ``api_key`` 字段，
    它只是个现读环境变量/``llm.env`` 的 property。所以「Key 被存进文件」不是
    靠约定防住的，是靠类型防住的 —— 少一层依赖少一个错。

    同样不写 ``llm_enabled``：它由 ``FREEAGENT_RULES_ONLY`` 单方面决定，
    写进文件只会造成「界面关了、文件里还开着」的错觉。
    """
    path = config_path(home if home is not None else config.home)
    payload: dict[str, Any] = {
        "llm": {
            "provider": config.provider,
            "model": config.model,
            "vision_model": config.vision_model,
            "base_url": config.base_url,
            "timeout": config.timeout,
            "allow_fallback": config.allow_fallback,
            "view_routing": config.llm_view_routing,
        },
        "energy_windows": _ENERGY_TO_RAW(config.energy_windows),
        "delegate": {
            "projects": list(config.delegate_projects),
            "model": config.delegate_model,
        },
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path
