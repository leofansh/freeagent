"""LLM 凭据落盘：读写 ``<state_home>/llm.env``。

和 ``feishu/secret_store.py`` 是**同一套引擎、同一套约束**，只是白名单和
文件名不同。共用理由见 :mod:`freeagent.secrets_file`。

## 优先级：环境变量优先、文件兜底

这条和 Hermes 不同，值得说清为什么。

Hermes 把 ``.env`` 载入 ``os.environ``，于是**只有一个来源**——在容器里
够用。但本项目要同时支持「多家 provider」和「本机不联网的本地模型」，那
就需要区分：

- **环境变量 = 显式动作**。临时 ``set DEEPSEEK_API_KEY=...`` 做一次对比
  测试，跑完就消失。它压过文件，是因为它是**当场**做的决定。
- **文件 = 持久配置**。界面上填的、写进 ``llm.env`` 的那份。��会话令牌
  保护，所以它是「配置」而不是「随手的输入」。

界面上会**如实显示当前生效的是哪一个**（:func:`resolve_key` 的
:attr:`KeySource.label`）。不显示的话，用户改完界面却发现还是旧 Key
在生效，会以为设置坏了。

## 一个刻意的例外

``ollama`` 的 ``needs_key`` 是 ``False`` —— 本机服务不鉴权。此时
``llm.env`` 里**可以**有 ``OLLAMA_API_KEY``（有人用反代套一层鉴权），
但没有它也完全能用，所以界面不给这个 provider 显示 Key 输入框。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .paths import state_file
from .secrets import SecretStr, as_secret
from .secrets_file import SecretFile, mask_secret, UnknownKeyError
from .services.llm.providers import all_key_env_vars

__all__ = [
    "ENV_FILE_NAME",
    "ALLOWED_KEYS",
    "env_path",
    "read_env",
    "write_env",
    "mask_secret",
    "resolve_key",
    "KeySource",
    "UnknownKeyError",
]

#: 与 ``config.json`` 同目录（``:func:`env_path` 里由 config_path 推出来）。
ENV_FILE_NAME = "llm.env"

#: 允许写入的变量名，**从 provider 注册表推导**。
#:
#: 推导而不是另抄一份：抄的那份会漂 —— 加了 provider 忘了加白名单，
#: 表现是「界面保存成功了、重启后 Key 没了」，而界面上还显示「已配置」。
#:
#: 这**不构成导入环**（``services.llm`` 只 import ``...domain`` 这个叶子，
#: 不碰 ``config``），所以可以模块级导入。密钥**全都是**秘密，
#: 所以掩码规则对全部键一致。
ALLOWED_KEYS: frozenset[str] = all_key_env_vars()

# ``UnknownKeyError`` 是从 :mod:`freeagent.secrets_file` 直接**导入**的，
# 不在这里另包一层同名类 —— 包一层只会让调用方的 ``except`` 记错名字，
# 而两处 catch 同一个类却以为是两个，是很难查的问题。


def env_path(home: str | Path | None = None) -> Path:
    """``.env`` 放哪。

    与 ``config.json`` **同一套路径解析**（走 :mod:`freeagent.paths`）——
    复制粘贴的路径解析迟早漂移，而漂移的表现是「界面写 A 目录、运行时读
    B 目录」，于是配置永远不生效、且**没有任何报错**。

    刻意**不**用 ``from .config import config_path``：``Config.api_key`` 要读
    这个文件，反向 import 就成了 ``config -> llm_env -> config`` 的环。运行时
    它是惰性的、不会炸，但静态检查会报，而且**会诱导下一个人写出真环** ——
    哪天某个模块在模块级 import 了 config，整条链就在 import 期炸掉，
    报错还指向一个看起来无辜的模块。
    """
    return state_file(home, ENV_FILE_NAME)


def _file(home: str | Path | None = None) -> SecretFile:
    return SecretFile(env_path(home), ALLOWED_KEYS)


def read_env(home: str | Path | None = None) -> dict[str, str]:
    """读全部 provider 的 Key。**读不到不是错误** —— 返回空字典。"""
    return _file(home).read()


def write_env(values: dict[str, str], home: str | Path | None = None) -> Path:
    """写入。**只接受白名单里的 key**，未知 key 抛 :class:`UnknownKeyError`。"""
    return _file(home).write(values)


@dataclass(frozen=True, slots=True)
class KeySource:
    """Key 从哪来。**界面上要显示**，因为它决定用户改哪里才生效。"""

    #: ``"env"`` / ``"file"`` / ``None``（没配）
    origin: str | None
    #: 变量名（origin 为 ``env``/``file`` 时有意义）
    name: str = ""

    @property
    def label(self) -> str:
        if self.origin == "env":
            return f"环境变量 {self.name}"
        if self.origin == "file":
            return f"文件 {ENV_FILE_NAME} 的 {self.name}"
        return "未配置"

    @property
    def is_env(self) -> bool:
        return self.origin == "env"


def resolve_key(
    key_env_var: str, *, home: str | Path | None = None
) -> tuple[SecretStr | None, KeySource]:
    """拿某 provider 的 Key，**并说清它从哪来**。

    返回 ``(None, KeySource(None))`` 表示没配 —— 那是**合法状态**，不是错误：
    规则层兜底，不需要 Key 也能用。

    环境变量优先于文件。理由见模块说明。
    """
    from_env = os.environ.get(key_env_var, "")
    if from_env.strip():
        return (
            as_secret(from_env, label=key_env_var),
            KeySource("env", key_env_var),
        )
    from_file = _file(home).read().get(key_env_var, "")
    if from_file.strip():
        return (
            as_secret(from_file, label=key_env_var),
            KeySource("file", key_env_var),
        )
    return None, KeySource(None, key_env_var)
