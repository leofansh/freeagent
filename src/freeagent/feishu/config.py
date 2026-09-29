"""飞书通道配置：**环境变量优先，``feishu.env`` 兜底**。

⚠️ 这里改过一次语义。原先写的是「密钥只从环境变量读，**绝不落盘**」，
那是 DeepSeek Key 那套规矩直接套过来的结果。12.7 让界面能改飞书配置，
于是密钥**必须**能落盘（明文，见 :mod:`freeagent.feishu.secret_store` 的
决策说明：DPAPI 对「同用户进程」这个真正的威胁毫无作用，防它的是会话令牌）。

现在的读取顺序（**先环境变量，后文件**）：

1. ``os.environ`` / 显式传入的 ``env``；
2. 上面没有的键，才用 ``<state_home>/feishu.env`` 里的值补。

**为什么文件只兜底、不覆盖环境变量**：环境变量是「这次运行我明确要什么」，
文件是「平时用的默认值」。反过来会让 ``set FEISHU_APP_ID=...`` 这类
一次性覆盖失效 —— 而那正是 doctor、测试、CI 依赖的机制。

命名沿用 Hermes Agent 的 ``FEISHU_*`` 约定，方便对照排查。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ..config import config_path
from ..secrets import SecretStr, as_secret

__all__ = ["FeishuConfig", "ConfigError", "load_config", "state_home", "merged_env"]

#: 环境变量名。集中在这里，别散在各处 ``os.environ``。
ENV_APP_ID = "FEISHU_APP_ID"
ENV_APP_SECRET = "FEISHU_APP_SECRET"
ENV_ALLOWED = "FEISHU_ALLOWED_USERS"
ENV_DOMAIN = "FEISHU_DOMAIN"
ENV_POLL = "FEISHU_REMINDER_POLL_SECONDS"

#: 允许的发送者上限。白名单是「谁能指挥本机执行代码」的边界，
#: 空 = 谁都不许；给个上限是为了让配错时**报错**而不是默默放行一堆人。
MAX_ALLOWED = 200

#: 合法的域名。提成常量是因为**校验必须只有一处** ——
#: :meth:`FeishuConfig.check_ready` 和配置界面的表单都要用同一个来源。
#: 各写一份的话，界面就会接受一个桥接启动时才拒绝的值，
#: 表现是「显示已保存，桥接却起不来」（12.7 要防的正是这个）。
VALID_DOMAINS: frozenset[str] = frozenset({"feishu", "lark"})


class ConfigError(RuntimeError):
    """配置不完整或不合法。**启动时就要炸**，不能等到连上再出问题。"""


@dataclass(frozen=True, slots=True)
class FeishuConfig:
    """飞书通道的运行参数。"""

    app_id: str
    #: **不要用裸 ``str``**。类型本身就是防线：``json.dumps`` / SDK 调用会拒绝
    #: 它，迫使调用方写 ``.use()``，于是「密钥流向了哪里」在代码里看得见。
    #: 见 :mod:`freeagent.secrets`。
    app_secret: SecretStr
    #: 授权指挥本机的 ``open_id`` 集合。**空 = 全部拒绝**（刻意的默认值）。
    allowed_users: frozenset[str]
    #: ``feishu``（中国）或 ``lark``（国际）。
    domain: str
    #: 提醒轮询秒数。0 = 不推提醒。
    reminder_poll: float

    def __post_init__(self) -> None:
        """把裸字符串收敛成 ``SecretStr``。

        刻意接受 ``str`` 作为入参：调用方（测试、将来可能的配置页）不该被迫
        知道这个包装类型。但**存进去的一定是 ``SecretStr``**，所以漏写 ``.use()``
        会在真正发请求的地方炸出来，而不是静默把明文写进 payload 或日志。
        """
        if not isinstance(self.app_secret, SecretStr):
            object.__setattr__(
                self,
                "app_secret",
                SecretStr(self.app_secret or "", label=ENV_APP_SECRET),
            )

    @property
    def base_url(self) -> str:
        """OpenAPI 域名。国际版和国内版不同，连错会一直 404。"""
        if self.domain == "lark":
            return "https://open.larksuite.com"
        return "https://open.feishu.cn"

    def __repr__(self) -> str:
        """**遮掉 app_secret。**

        踩过的坑：这是个 dataclass，默认 ``repr`` 会把 ``app_secret`` 原文带出来。
        而 ``print(cfg)``、``log.info("%s", cfg)``、异常里带 cfg 都是极常见的写法 ——
        于是「密钥只从环境变量读、不落盘」这条承诺，会被一次日志输出破掉。
        调试时看个遮蔽预览完全够用。

        真正的防线在 ``SecretStr`` 那一层：``app_secret`` 这个值本身就没法被
        顺手打印出来，所以「日志里带了 config」不再等于「泄露了密钥」。
        """
        return (
            f"{type(self).__name__}(app_id={self.app_id!r}, "
            f"app_secret={self.app_secret.preview()!r}, "
            f"allowed_users={len(self.allowed_users)} 人, "
            f"domain={self.domain!r}, reminder_poll={self.reminder_poll!r})"
        )

    def check_ready(self) -> None:
        """启动前自检。**缺东西就在这里炸**，别连一半才失败。"""
        if not self.app_id:
            raise ConfigError(f"缺少 {ENV_APP_ID}")
        if not self.app_secret:
            raise ConfigError(f"缺少 {ENV_APP_SECRET}")
        if self.domain not in VALID_DOMAINS:
            raise ConfigError(
                f"{ENV_DOMAIN} 只能是 {' 或 '.join(sorted(VALID_DOMAINS))}，"
                f"收到 {self.domain!r}"
            )
        if not self.allowed_users:
            raise ConfigError(
                f"{ENV_ALLOWED} 为空 —— 没有任何人被授权指挥本机。"
                "这是有意的默认：宁可不许，也不能默认放行。"
                f"例：{ENV_ALLOWED}=ou_xxx,ou_yyy"
            )


def _int_env(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} 必须是整数，收到 {raw!r}") from exc


def state_home(home: str | Path | None = None) -> Path:
    """飞书通道的状态文件放哪：身份缓存、事件去重表。

    解析规则与 :func:`freeagent.config.config_path` **同一套**（``FREEAGENT_HOME``
    → ``~/.freeagent``）—— 直接复用它，不另写一遍：复制粘贴的路径解析迟早
    会漂移，而漂移的表现是「缓存写在 A 目录、读取去 B 目录找」，于是缓存
    永远命中不了，而且没有任何报错。

    状态文件跟着库走是有意的：``--db`` 指到哪儿，缓存和去重表就跟到哪儿，
    不会出现「换了一套数据却还在记着旧的事件 id」。
    """
    return config_path(home).parent


def merged_env(
    env: Mapping[str, str] | None = None,
    *,
    home: str | Path | None = None,
) -> dict[str, str]:
    """环境变量与 ``feishu.env`` 合并，**环境变量优先**。

    规则只有一条，很好记：**环境里有的键，环境说了算；环境里没有的，文件补。**

    按「键是否存在」判断，而不是「值是否为空」—— 否则
    ``set FEISHU_APP_SECRET=`` 这种「我故意清空」会被文件里的值悄悄填回来，
    而那正好是最不该被悄悄推翻的一种意图。

    **显式传入 ``env`` 时完全不读文件**。传了就是「我就是要用这一份」，
    测试、doctor、CI 依赖这个封闭性；顺带也让本函数没有隐形的磁盘 IO。
    """
    from .secret_store import read_env

    base = dict(os.environ) if env is None else dict(env)
    if env is not None:
        return base
    for key, value in read_env(home).items():
        base.setdefault(key, value)
    return base


def load_config(env: dict[str, str] | None = None) -> FeishuConfig:
    """读配置：环境变量优先，``feishu.env`` 兜底（见 :func:`merged_env`）。"""
    src = merged_env(env)
    users = frozenset(
        part.strip()
        for part in src.get(ENV_ALLOWED, "").split(",")
        if part.strip()
    )
    if len(users) > MAX_ALLOWED:
        raise ConfigError(
            f"{ENV_ALLOWED} 有 {len(users)} 个，超过上限 {MAX_ALLOWED}。"
            "本机个人助手不该授权这么多人，请检查是不是误填。"
        )
    return FeishuConfig(
        app_id=src.get(ENV_APP_ID, "").strip(),
        app_secret=as_secret(src.get(ENV_APP_SECRET, ""), label=ENV_APP_SECRET)
        or SecretStr("", label=ENV_APP_SECRET),
        allowed_users=users,
        domain=src.get(ENV_DOMAIN, "feishu").strip().lower() or "feishu",
        reminder_poll=float(_int_env(src, ENV_POLL, 60)),
    )
