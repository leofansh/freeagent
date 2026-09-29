"""探测 **bot 自己**的 ``open_id`` —— 群聊 @ 门控的唯一依据。

## 为什么需要这个

飞书群聊里，@ 人这件事在事件里体现为：正文里留一个占位符（``@_user_1``），
外加 ``event.message.mentions`` 列出被 @ 的对象（含各自的 ``id.open_id``）。

所以「@ 的是不是机器人自己」是**判得出来的**。而这必须判，因为
群里的 ``@张三 你看下这个`` 不该让 bot 插嘴 —— 它对 @ 的人类说话，
且 bot 一旦插嘴，拿到的是一句被剥掉占位符的残缺文本，意图识别必然跑偏。
详见设计方案 11.9.1。

## 为什么不写死 open_id

``open_id`` 随「应用 × 用户」生成，同一个应用换个人类用户 open_id 就变，
所以既不能从文档抄也猜不出来，只能问飞书 —— 这也是用户第一次配置时
卡住的地方（先有 app 还是先有 open_id）。本模块把这个动作变成自动的，
并且把结果缓存下来，让它只需要成功一次。

传输层不自己实现：复用 :class:`~freeagent.feishu.sender.FeishuSender`
的 token 缓存与错误校验，所以本模块**不发网络请求也能测**。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Mapping

from .config import state_home

if TYPE_CHECKING:  # 只为类型标注，运行期不导入 —— 少一条依赖边
    from .sender import FeishuSender

__all__ = [
    "BotIdentity",
    "IdentityError",
    "BOT_INFO_PATH",
    "CACHE_FILE_NAME",
    "CACHE_TTL_SECONDS",
    "parse_bot_identity",
    "fetch_identity",
    "identity_path",
    "load_cached_identity",
    "save_identity",
    "resolve_identity",
]

#: 飞书「获取机器人信息」。用 tenant token 鉴权，不需要额外申请权限。
BOT_INFO_PATH = "/open-apis/bot/v3/info"

CACHE_FILE_NAME = "feishu_bot.json"

#: 缓存多久重探一次。``open_id`` 对同一个应用是不变的，所以这只是
#: 「万一探错了还能自愈」的时间窗，不是新鲜度要求。
CACHE_TTL_SECONDS = 7 * 24 * 3600


class IdentityError(RuntimeError):
    """探测 bot 身份失败。**不含任何凭据**。"""


@dataclass(frozen=True, slots=True)
class BotIdentity:
    """bot 自己的身份。"""

    open_id: str
    app_name: str = ""
    #: 飞书原样返回的启用状态，**不在这里下结论**（不同版本含义可能不同），
    #: 由 doctor 如实展示给用户看。
    activate_status: str = ""


def parse_bot_identity(body: Mapping[str, object]) -> BotIdentity:
    """从 ``bot/v3/info`` 的响应里取出身份。

    坑：字段在 **``body["bot"]`` 里**，不在顶层。只读顶层会得到空
    ``open_id`` —— 而空 ``open_id`` 恰好会让 @ 门控「看起来能跑」
    （谁都不等于它，于是群里没人能叫醒 bot），所以必须在这里就炸掉。
    """
    bot = body.get("bot")
    if not isinstance(bot, Mapping):
        raise IdentityError("响应里没有 bot 字段（结构不对）")
    open_id = bot.get("open_id")
    if not isinstance(open_id, str) or not open_id.strip():
        raise IdentityError("响应里没有 bot.open_id")
    name = bot.get("app_name")
    status = bot.get("activate_status")
    return BotIdentity(
        open_id=open_id.strip(),
        app_name=name.strip() if isinstance(name, str) else "",
        activate_status="" if status is None else str(status),
    )


def fetch_identity(sender: FeishuSender) -> BotIdentity:
    """联网探测。失败抛 :class:`IdentityError`。"""
    try:
        body = sender.get_json(BOT_INFO_PATH)
    except Exception as exc:  # sender 抛的是 FeishuError
        raise IdentityError(f"探测 bot 身份失败：{exc}") from exc
    return parse_bot_identity(body)


# -- 缓存 -------------------------------------------------------------------- #

def identity_path(home: str | Path | None = None) -> Path:
    """缓存路径。目录不存在则返回预期路径（不创建）。"""
    return state_home(home) / CACHE_FILE_NAME


def load_cached_identity(
    home: str | Path | None = None, *, now: float | None = None
) -> BotIdentity | None:
    """读缓存。**没有、过期、损坏都返回 ``None``** —— 缓存永远不该让人启动失败。

    损坏时把坏文件改名留现场，而不是删掉：出问题时得能看出「当时写进去了什么」。
    """
    path = identity_path(home)
    if not path.is_file():
        return None
    current = time.time() if now is None else now
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        _quarantine(path)
        return None
    if not raw.strip():
        # 空文件当「没有缓存」，**不**算损坏。写盘走原子替换，本来不该出现
        # 空文件；但 ``touch`` 一个、或编辑器建了空壳都很常见。此时没有内容
        # 值得留现场，改名只会白吓一跳。与 feishu/dedup.py 保持同一判断 ——
        # 两处对「空」的理解若不一致，doctor 和桥接就会给出矛盾结论。
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        _quarantine(path)
        return None
    if not isinstance(payload, dict):
        _quarantine(path)
        return None
    fetched = payload.get("fetched_at")
    if not isinstance(fetched, (int, float)):
        _quarantine(path)
        return None
    if current - float(fetched) > CACHE_TTL_SECONDS:
        return None                      # 过期不是损坏，静静重探即可
    try:
        return parse_bot_identity(payload)
    except IdentityError:
        _quarantine(path)
        return None


def save_identity(
    identity: BotIdentity, home: str | Path | None = None, *, now: float | None = None
) -> Path:
    """写缓存。返回写入的路径。原子写，避免半截文件。

    刻意把身份存在 **``bot`` 这层嵌套里**，和 ``bot/v3/info`` 的响应同构，
    这样读缓存和读响应走**同一个** :func:`parse_bot_identity`。

    踩过的坑：早期这里写的是扁平结构（``open_id`` 在顶层），读的时候却又
    去调解析响应的那个函数 —— 于是**每写一次缓存，下一次启动就把它当损坏
    文件改名留现场**，身份永远命中不了缓存，每次启动都重新联网探一遍。
    「写一个形状、读另一个形状」这种错在测试里极容易漏掉，因为写完立刻
    断言文件存在是过的；要等到重启才暴露。所以这里只留一个形状。
    """
    path = identity_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "bot": {
            "open_id": identity.open_id,
            "app_name": identity.app_name,
            "activate_status": identity.activate_status,
        },
        "fetched_at": time.time() if now is None else now,
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    tmp.replace(path)                   # 原子：要么是旧的完整内容，要么是新的
    return path


def _quarantine(path: Path) -> None:
    try:
        path.replace(path.with_suffix(".json.corrupt"))
    except OSError:
        pass                            # 挪不动就算了，不能因为它起不来


# -- 组合入口 ---------------------------------------------------------------- #

def resolve_identity(
    sender: FeishuSender,
    *,
    home: str | Path | None = None,
    now: float | None = None,
) -> tuple[BotIdentity | None, str]:
    """拿 bot 身份，返回 ``(身份, 人话说明)``。

    顺序是「缓存 → 联网 → 放弃」，**放弃时返回 ``None`` 而不是抛异常**：
    探测失败不该让桥接起不来，但必须让调用方知道 @ 门控要失败关闭了。
    那句人话说明会一路带到启动日志和 doctor 输出里，所以它得说清后果，
    不能只说「失败」。
    """
    cached = load_cached_identity(home, now=now)
    if cached is not None:
        return cached, f"用缓存（{CACHE_FILE_NAME}）"
    try:
        identity = fetch_identity(sender)
    except IdentityError as exc:
        return None, f"{exc}；群聊 @ 门控将失败关闭（群里 @ 它也不会回）"
    try:
        save_identity(identity, home, now=now)
    except OSError as exc:
        # 探到了但存不下：这次仍可用，只是下次要重新探。不该因此失败。
        return identity, f"已联网探测，但写缓存失败（{exc.strerror}）"
    return identity, "已联网探测并缓存"
