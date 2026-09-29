"""飞书通道的凭据落盘：读写 ``<state_home>/feishu.env``。

**这个模块只声明「飞书要哪些键」**，读 / 洗 / 写 / 掩码的实现在
:mod:`freeagent.secrets_file`（通用引擎，与 DeepSeek 等凭据共用）。

共用而不是各写一份的理由在那边：清洗粘连行、识别 ``***`` 占位符、
容忍 BOM、保权限原子写 —— 这些是**踩过坑才长出来的**，而它们跟
「哪个产品的哪个键」毫无关系。复制一份的代价不是多几行，而是**两份实现
从此各自演化**：一边修了「值里恰好含 ``API_KEY=`` 片段被误切」，
另一边没有，那个 bug 就在那儿等着。

三条硬约束（设计方案 12.7），由引擎保证，少一条就出事：

1. **变量名是固定白名单**。这不是「友好性」问题，是安全边界 ——
   接受任意变量名等于开一个 RCE 入口：往 ``PYTHONPATH`` /
   ``LD_PRELOAD`` / ``EDITOR`` 里塞一个路径，下次起子进程就在
   ``main()`` 之前加载攻击者的代码。而 ``^[A-Za-z_]\\w*$`` 这类正则
   **挡不住这些**，因为它们本来就合规。
2. **读盘容忍 BOM**。PowerShell 写出的 ``.env`` 带 BOM，用 ``utf-8`` 读
   会让第一个变量名变成 ``\\ufeffFEISHU_APP_ID`` 而**静默失效** ——
   配置看起来写了，桥接却当没有。
3. **每次读都清洗**。已知的两种损坏形态必须自愈：两对 ``KEY=VALUE``
   粘在一行（中间少了换行）、以及被打断留下的 ``KEY=***`` 占位符。

为什么是明文而不是 DPAPI：见设计方案 12.7 的决策表 ——
DPAPI 只多防「其他 Windows 用户」，而**同用户的进程照样能
``CryptUnprotectData`` 解开**，所以它对「本机进程」这个真正的威胁毫无作用。
防那个威胁的是会话令牌，不在本模块。
"""

from __future__ import annotations

from pathlib import Path

from ..secrets_file import SecretFile, mask_secret

__all__ = [
    "ENV_FILE_NAME",
    "ALLOWED_KEYS",
    "SECRET_KEYS",
    "env_path",
    "read_env",
    "write_env",
    "mask_secret",
    "describe_id",
    "UnknownKeyError",
]

#: 与身份缓存、去重表、状态文件同目录。
ENV_FILE_NAME = "feishu.env"

#: 唯一允许写入的变量名。**不接受自定义 key** —— 见模块说明第 1 条。
ALLOWED_KEYS: frozenset[str] = frozenset({
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "FEISHU_ALLOWED_USERS",
    "FEISHU_DOMAIN",
    "FEISHU_LOCK_PORT",
})

#: 其中哪些是秘密（界面上必须掩码，且不许回传明文）。
SECRET_KEYS: frozenset[str] = frozenset({"FEISHU_APP_SECRET"})

#: 引擎自己抛的异常类型，原样透出去，调用方的 except 不用改。
from ..secrets_file import UnknownKeyError  # noqa: E402 - 放这里只为 __all__ 好读


def describe_id(entry: str) -> str:
    """这个白名单条目**大概**是什么。

    刻意叫「大概」：飞书不给接口反查「这个串是哪一种 id」，只能靠前缀判。
    所以**判错不许拦住保存** —— 拦住就是「本来能用的配置被工具拒绝」。

    真正的形状校验在两处，各有各的理由：
    - :meth:`FeishuSender.send_to_allowlist_entry` 拒绝 ``oc_``（会话 id），
      因为那会让提醒**永远发不出去**，且报错发生在很久之后。
    - 匹配阶段不认任何东西，纯集合求交（见
      :meth:`ChannelService.is_allowed`）—— 不认就等于不匹配，不是错误。
    """
    entry = entry.strip()
    if entry.startswith("ou_"):
        return "open_id（应用级；换飞书应用会变）"
    if entry.startswith("oc_"):
        return "⚠ 会话 id（oc_ 开头）—— 这不是人的 id，提醒会发不出去"
    return "像是租户级 user_id（跨应用稳定；需应用申请对应权限）"


def env_path(home: str | Path | None = None) -> Path:
    """``.env`` 放哪。与其它状态文件**同一套路径解析** ——
    复制粘贴的路径解析迟早漂移，而漂移的表现是「界面写 A 目录、
    桥接读 B 目录」，于是配置永远不生效、且**没有任何报错**。

    走 ``config_path(home).parent`` 而不是 ``feishu.config.state_home``：
    两者是同一行代码，但后者会形成
    ``config -> secret_store -> config`` 的导入环 —— ``merged_env`` 要读
    这个文件，而 ``config`` 又要读 ``merged_env``。直接用 ``config_path``
    把那条边去掉，行为一字不差。
    """
    from ..config import config_path

    return config_path(home).parent / ENV_FILE_NAME


def _file(home: str | Path | None = None) -> SecretFile:
    return SecretFile(env_path(home), ALLOWED_KEYS)


def read_env(home: str | Path | None = None) -> dict[str, str]:
    """读配置。**读不到不是错误** —— 返回空字典，让界面显示「未配置」。"""
    return _file(home).read()


def write_env(values: dict[str, str], home: str | Path | None = None) -> Path:
    """写入配置。**只接受白名单里的 key**。

    未知 key 直接抛 :class:`UnknownKeyError` —— 不静默忽略。静默忽略的话，
    调用方以为自己写成功了，实际没写，而界面上会显示「已保存」。
    """
    return _file(home).write(values)
