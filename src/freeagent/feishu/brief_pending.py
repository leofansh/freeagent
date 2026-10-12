"""「待填委派简报」状态：按 ``open_id`` 键控、单次有效、**纯内存**。

## 这个模块解决什么

飞书里的四段选择（项目 → 工作模式 → 模型 → 推理档）选完后，原本让用户直接
用自然语言说「要做什么」。但那句话会被意图分类误判 —— 例如含「进度」二字
会被当成「查询某条事务的进展」。改成第 5 步：四段选完后进入「等你交简报」
状态，用户下一句纯文字**必然**是简报，不再过意图分类。

这里存的就是那个「等你交简报」的状态：四段选完 :func:`arm` 一份**选型快照**，
用户交来一句话 :func:`fill_brief` 填进快照，用户点确认卡 :func:`claim`
取走并建委派。四个函数对应调用方（``bridge.py``）的三处触发。

## 为什么按 ``open_id`` 键控，而不是全局单槽

确认卡发在**群**里，全员可见 —— 谁都能点。若按 ``chat_id`` 或全局单槽存，
群里的另一个白名单成员点一下「建这条委派」，取到的就是**上一个人的**简报。
而 ``tasks.delegate_requested_by`` 取的是**点击者**，于是这条委派记成了
他的名、内容却是别人的 —— 一次张冠李戴的建事务。按 ``open_id`` 键控让每个
人**只可能**取到自己的那一份。

## 为什么**刻意不落盘**

桥接一重启这份状态就该没了（**失败关闭**）：用户重选一遍即可，代价很小。
而落盘会把它拖进 :mod:`freeagent.services.oc_selection` 那条「全局单槽 +
Web 与飞书共用」的契约里 —— 那套状态是**跨进程、跨入口**的一份，语义与本
模块的「一次会话里的临时快照」根本不是一回事。混进去就会出现「Web 上选的
东西影响飞书的待填状态」这类错位。所以：只放在进程内存里，进程没了它就没了。

## 为什么 :func:`claim` 必须是「取即删」

飞书的卡片点击会被**重复投递**（长连接重连重投，见 :mod:`freeagent.feishu.dedup`），
用户也可能手快连点两下。取即删让第二次点击拿不到东西（返回 ``None``），
从而不会建出两条委派 —— **单次消费**是这里唯一的安全保证。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["PendingBrief", "arm", "fill_brief", "claim", "discard", "peek"]


@dataclass(slots=True)
class PendingBrief:
    """一份「四段选完、等用户交简报」的选型快照。

    :attr:`brief` 是**后填**的：:func:`arm` 出来时它还是空串，用户交了简报
    才有值。所以「已 arm」与「已有简报」是两个状态，靠这个字段区分 ——
    确认卡要等到有简报才该发，而 bridge 判断依据就是它非空。
    """

    open_id: str
    project: str
    model: str
    agent: str
    variant: str
    chat_id: str
    brief: str = ""


#: 唯一的存储：``open_id -> 快照``。**模块级、纯内存**，见模块 docstring。
#: 刻意不做成类实例字段 —— 调用方要的就是「全进程一份、import 即用」，
#: 多包一层反而给「谁持有它」这件事制造了不必要的选择。
_PENDING: dict[str, PendingBrief] = {}


def arm(
    open_id: str,
    *,
    project: str,
    model: str,
    agent: str,
    variant: str,
    chat_id: str,
) -> PendingBrief:
    """四段选择选完后调用：把选型**快照**存下，:attr:`PendingBrief.brief` 为空。

    同 ``open_id`` 重复 arm 是**覆盖写** —— 用户在四段里改了选型重新选完，
    新快照就该盖掉旧的。否则会留着一份更早的选型，而症状是「我明明重选了，
    建出来的还是上一版」。

    ``open_id`` 为空时抛 :class:`ValueError`：空键是**全体共享的一个槽**，
    正是本模块「按人隔离」要防的东西，绝不能悄悄放行。
    """
    if not open_id:
        raise ValueError("待填简报必须按 open_id 键控，不能为空")
    entry = PendingBrief(
        open_id=open_id,
        project=project,
        model=model,
        agent=agent,
        variant=variant,
        chat_id=chat_id,
    )
    _PENDING[open_id] = entry
    return entry


def fill_brief(open_id: str, brief: str) -> PendingBrief | None:
    """用户交来简报时调用：填进已 arm 的记录。

    **没有 arm 记录时返回 ``None``，不凭空造一条** —— 造出来就没有选型
    （project / model / agent / variant 全空），建委派时会拿一个残缺快照去
    派发，报错还指不到「其实你没选完四段」这个真因。返回 ``None`` 让调用方
    如实告诉用户「请先选项目 / 模型」。
    """
    entry = _PENDING.get(open_id)
    if entry is None:
        return None
    entry.brief = brief
    return entry


def claim(open_id: str) -> PendingBrief | None:
    """用户点「建这条委派」时调用：**取出并删除**。

    「取即删」是单次消费的保证：第二次 ``claim`` 同一个 ``open_id`` 必定
    返回 ``None``，于是重复点击 / 重投不会建出第二条委派。用 ``pop`` 一步
    完成取值与删除，不留「先查再删」那种两步之间的窗口。
    """
    return _PENDING.pop(open_id, None)


def discard(open_id: str) -> bool:
    """取消 / 清态：丢掉这个 ``open_id`` 的待填记录。

    返回**原来是否确实有东西** —— 调用方要靠它决定「要不要回一句话」。
    没有记录却回一句「已取消」是骗人（用户可能压根没在等交简报）；
    有记录才值得告知。所以这里不能只删不返回。
    """
    return _PENDING.pop(open_id, None) is not None


def peek(open_id: str) -> PendingBrief | None:
    """只读不删。给「确认卡要不要发」「现在到哪一步了」这类判断用。"""
    return _PENDING.get(open_id)
