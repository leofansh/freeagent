"""设置页的「智能层」配置：provider 列表 + Key 落盘。

为什么单独一个模块：``endpoints_read.py`` 已经 231 行，而 web 层有
**250 行的硬上限**（``TestWebPackageStaysSmall`` 守着）。新逻辑塞进去会顶破，
而顶破之后没人会去拆 —— 于是下一个人接着往里塞。所以：先拆，再加。

连通性测试/列模型在 :mod:`llm_probe` —— 那是「探」，这是「配」，两件事的
失败方式、对用户的下一步动作都不一样，混在一起两边都会变得难读。

三条纪律：

1. **Key 只落 ``llm.env``，``config.json`` 结构上碰不到它。**
   :class:`~freeagent.config.Config` 压根没有 ``api_key`` 字段，它是现读的
   property，所以「Key 被写进配置文件」不是靠约定防住的，是靠类型防住的。

2. **响应里绝不回传明文。** 只回「配没配」「从哪来的」「掩码」。
   测试会逐字检查这一条。

3. **空串 = 不改，不是清空。** 界面上那个输入框每次加载都空着（明文不可能
   回显），用户只改模型不碰它是很正常的。如果空串当成清空，那**保存一次
   其它设置就会把 Key 抹掉** —— 而用户要等到下次启动才发现模型层没了。
   真要清空必须显式 ``clear_api_key``。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..domain import FreeAgentError
from ..llm_env import mask_secret, read_env, resolve_key, write_env
from ..services.llm.providers import PROVIDERS

__all__ = [
    "PendingKeyWrite",
    "provider_choices",
    "split_key_fields",
]


def provider_choices(config) -> list[dict[str, Any]]:
    """全部 provider 的界面元数据，**每家都带上自己的 Key 状态**。

    逐家显示「配没配 / 从哪来」是有意的：所有 Key 共存于同一个 ``llm.env``，
    来回切 provider 时用户需要看到「这家其实配过了」而不是重新输一遍。
    只显示当前那家的话，切回去就变成一个空框。
    """
    home = getattr(config, "home", None)
    stored = read_env(home)
    out: list[dict[str, Any]] = []
    for profile in PROVIDERS:
        key, source = resolve_key(profile.key_env_var, home=home)
        out.append(
            {
                "id": profile.id,
                "label": profile.label,
                "default_base_url": profile.default_base_url,
                "default_model": profile.default_model,
                "models": list(profile.models),
                "needs_key": profile.needs_key,
                "key_env_var": profile.key_env_var,
                "note": profile.note,
                "requires_base_url": profile.requires_base_url,
                "key_configured": key is not None,
                # 掩码**只在**真的配了的时候给。掩码一个空串没意义。
                "key_masked": mask_secret(key.use()) if key else "",
                "key_source": source.label,
                # ``llm.env`` 里存着一份、但环境变量把它压住了。必须提示：
                # 这种情况下在界面上重填 Key **不会生效**（环境变量优先），
                # 用户会看见「保存成功」却发现用的还是旧的。
                "key_shadowed_by_env": bool(stored.get(profile.key_env_var))
                and source.origin == "env",
            }
        )
    return out


@dataclass(frozen=True, slots=True)
class PendingKeyWrite:
    """一次**还没落盘**的 Key 写入。校验通过后调 :meth:`commit`。"""

    key_env_var: str
    #: 空串 = 删除这一行（``write_env`` 的语义）
    value: str
    home: str | Path | None = None

    def commit(self) -> Path:
        # ``write_env`` 对空串是「删除这一行」，非空是「写入」。
        return write_env({self.key_env_var: self.value}, home=self.home)


def split_key_fields(
    body: Mapping[str, Any], base_config
) -> tuple[dict[str, Any], "PendingKeyWrite | None"]:
    """把 ``api_key`` 从设置里摘出来，返回 ``(干净的设置, 待写的 Key)``。

    **为什么拆成两步**：调用方有一条既有纪律 —— 先校验、再落盘。如果在这里
    就把 Key 写进 ``llm.env``，而随后的配置校验没过，就会留下「配置没存上、
    Key 却改了」的半吊子状态。拆开之后，Key 的真正落盘发生在校验成功之后。

    返回的设置里**没有** ``api_key``，所以 :class:`~freeagent.config.Config`
    结构上就拿不到明文。
    :func:`~freeagent.config.config_from_settings` 仍然会拒绝 ``api_key``，
    那是「端点漏摘了」时的安全网，不是日常路径。
    """
    settings = {
        k: v for k, v in body.items() if k not in ("api_key", "clear_api_key")
    }
    raw = body.get("api_key")
    clear = bool(body.get("clear_api_key"))

    if raw is not None and not isinstance(raw, str):
        raise FreeAgentError("API Key 必须是文本")
    if clear and (raw or "").strip():
        raise FreeAgentError("「清空」和「新填」不能同时用")

    if not clear and not (raw or "").strip():
        return settings, None        # 空串 = 不动（见模块说明第 3 条）

    # 写到**这次选的**那家的槽位：换了 provider 又填了 Key，
    # 就该落到那家的变量名下，否则切回去会发现没配。
    from ..config import resolve_provider

    profile_id = resolve_provider(
        str(body.get("provider") or getattr(base_config, "provider", ""))
    )
    profile = next(p for p in PROVIDERS if p.id == profile_id)
    return settings, PendingKeyWrite(
        key_env_var=profile.key_env_var,
        value="" if clear else (raw or "").strip(),
        home=getattr(base_config, "home", None),
    )
