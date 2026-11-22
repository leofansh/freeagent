"""对话式起手（V1.141）：一句话 → 认出项目 + 要做的事 → 预填工作台表单。

借鉴飞书插件的「一句话起手」交互：拿不准就追问，下一条消息即回答。
本仓的落点在工作台（不是对话页）—— 工作台本来就有「选目录 + 写一句话 +
交给它」这道表单，缺的只是「一句话把前两项一起填上」。

## 为什么复用 ``parse_delegation``（用户拍板）

AI 承接用**已配的文本模型**（与能力路由、表达层同一套接缝），不是规则：
规则「剥动词猜需求」实测必然出错（「帮我改一下 README」剥出空的），
而猜错项目的代价是**在错误的仓库里动手**。``parse_delegation`` 已经把
「抽不出就空串」写死在契约里，这里只消费它的结构，不重造一套抽取。

## 追问为什么是规则的

模型只在**首次**抽一次；追问的「下一句 = 答案」是确定性的：要 brief 就把
下一句原样当 brief，要 project 就拿下一句去授权清单里**精确比对**。
追问再调模型既多花一次计费，又把「人的回答」变成模型可自由发挥的字段 ——
那是把一次安全交互交给不确定性，不划算。
"""

from __future__ import annotations

from dataclasses import dataclass

from .capability import CapabilityView

__all__ = ["IntakePlan", "plan"]


@dataclass(frozen=True, slots=True)
class IntakePlan:
    """一句话起手的解析结果。字段都可能为空 —— 空不是失败，是要追问。"""

    #: 认出的项目名（原样取自授权清单的 labels）。
    project: str = ""
    #: 该项目的授权目录（给工作台 ``#wb-dir`` 预填用；认不出时为空）。
    directory: str = ""
    #: 要做的事（给 ``#wb-text`` 预填用）。
    brief: str = ""
    #: 还缺什么：``""`` 齐了 / ``"project"`` / ``"brief"``。
    need: str = ""
    #: ``need`` 非空时的追问文案。
    question: str = ""
    #: 这是不是一句委派请求（False 时上面几项无意义）。
    recognized: bool = False
    #: 没认出来的原因：``no_projects`` / ``no_model`` / ``model_error`` /
    #: ``not_delegation`` / ``""``。原样回给界面，不翻译成「没有」。
    reason: str = ""


def _resolve_directory(view: CapabilityView, name: str) -> str:
    """项目名 → 授权目录。``labels()`` 与 ``projects`` 建时按下标对齐
    （:func:`freeagent.app._capability_view`），所以按下标回查是确定的。
    """
    if not name:
        return ""
    low = name.casefold()
    for i, label in enumerate(view.labels()):
        if label.casefold() == low and i < len(view.projects):
            return view.projects[i]
    return ""


def _need(project: str, brief: str, view: CapabilityView) -> tuple[str, str]:
    """按「先定项目、再定做什么」的顺序决定下一问。"""
    if not project:
        named = "、".join(view.labels()[:8])
        return "project", f"要改哪个项目？（已有：{named}）"
    if not brief:
        return "brief", f"在「{project}」里具体要做什么？"
    return "", ""


def plan(
    text: str,
    view: CapabilityView | None,
    llm: object | None,
    *,
    carry: dict | None = None,
) -> IntakePlan:
    """把一句话（或追问的一句回答）变成 :class:`IntakePlan`。

    ``carry`` 是上一轮追问带回来的 ``{need, project, brief}``；有它时**不再
    问模型**——那是人对追问的回答，按确定规则消费（见模块 docstring）。
    """
    text = (text or "").strip()
    carry = carry or {}
    if not text:
        return IntakePlan(reason="empty", question="先说一句要做什么。")

    need = str(carry.get("need") or "")
    if need == "brief":
        # 人要 brief：这一句**原样**就是 brief，不抽、不改、不补全。
        brief = text
        project = str(carry.get("project") or "")
        nxt, question = _need(project, brief, view) if view else ("", "")
        if not brief:
            return IntakePlan(reason="empty", question="没听清要做什么，再说一次？")
        return IntakePlan(project, _resolve_directory(view, project) if view else "",
                          brief, nxt, question, True, "")
    if need == "project":
        # 人要 project：只认授权清单里的名字（大小写不敏感的子串匹配，
        # 容忍「小袁」对「XiaoYuan」这类简称；认不出就再问，不猜一个最像的）。
        hit = ""
        if view:
            low = text.casefold()
            for label in view.labels():
                if label and (label.casefold() in low or low in label.casefold()):
                    hit = label
                    break
        if not hit:
            named = "、".join(view.labels()[:8]) if view else ""
            return IntakePlan(need="project", question=f"没认出这个项目，再说说？（已有：{named}）",
                              reason="unmatched_project")
        brief = str(carry.get("brief") or "")
        nxt, question = _need(hit, brief, view)
        return IntakePlan(hit, _resolve_directory(view, hit), brief, nxt, question, True, "")

    # 首次解析：下面是唯一会花模型一次请求的地方。
    if view is None or not view.enabled:
        return IntakePlan(reason="no_projects",
                          question="还没授权项目 —— 先在「项目」里选一个本地文件夹。")
    if llm is None:
        return IntakePlan(reason="no_model",
                          question="没连模型，认不出这句话 —— 请手动选项目并写要做的事。")
    try:
        intent = llm.parse_delegation(text, view.labels())
    except Exception:
        return IntakePlan(reason="model_error",
                          question="模型没能认出这句话 —— 请手动选项目并写要做的事。")
    if not intent or not getattr(intent, "is_delegation", False):
        return IntakePlan(reason="not_delegation",
                          question="这句话不像是让我改代码 —— 要说「让它改 <项目> 做 <什么>」。")
    project = intent.project if intent.project in view.labels() else ""
    brief = (intent.brief or "").strip()
    nxt, question = _need(project, brief, view)
    return IntakePlan(project, _resolve_directory(view, project), brief,
                      nxt, question, True, "")
