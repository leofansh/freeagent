"""第 5 步的**真闭环**：点「建这条委派」之后，库里到底有没有那条委派。

## 为什么必须是真库

`test_brief_flow.py` 把 ``/delegate`` 交给一个**假 channel** —— 它只记录命令串
再返回一句预设回复，**从不真的跑** :meth:`Repl._create_delegation`。于是这条
命令被真实解析、真实落库时成不成立，完全没有覆盖。

而这里恰恰有一处**已知偏离**：脉络没有真留空（留空会让 ``parse_delegate_args``
把简报当成脉络），而是传了第一条脉络名。这处偏离对不对，只有真跑才知道。

## 这个 fixture 的一个要点：快照与当前选择**恒等**

``_run_delegate_confirm`` 在建委派前会核对 ``oc_selection.json`` 与快照是否
一致，不一致就说「选择已变」并拒绝。这是**有意的闸门**，不是 bug。

所以 fixture 用一个可变的 ``box`` 同时喂两者（:func:`_sel` 与
:func:`_arm_and_fill` 都从它读），使它们在正常用例里天然一致；只有
``test_selection_drift_blocks_create`` 故意改 ``box`` 去撞那道闸门。
第一版把两者写成不同的值，于是三条测试全红 —— 闸门拦的是**测试自己造的
矛盾**，而不是代码有问题。

## 守的四条

1. **真的建出来了**：``project_path`` 落在快照里那个项目，且是真实目录。
2. **脉络对**：挂到了那条脉络上。
3. **发起人是谁**：``delegate_requested_by`` 是**点击者**。
4. **第三道闸门还在**：建出来是**待办**，不是 active —— 执行器不碰它。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from freeagent.app import build_app  # noqa: E402
from freeagent.domain.models import TaskState  # noqa: E402
from freeagent.feishu import bridge as B  # noqa: E402
from freeagent.feishu import brief_pending  # noqa: E402
from freeagent.services import oc_selection as sel  # noqa: E402
from freeagent.services.channel import ChannelService  # noqa: E402

CHAT = "oc_chat"
WHO = "ou_owner"


class _Sender:
    """只实现确认卡点击路径用到的两个方法。"""

    def __init__(self) -> None:
        self.texts: list[tuple[str, str]] = []

    def send_text(self, chat_id: str, text: str) -> None:
        self.texts.append((chat_id, text))

    @property
    def last(self) -> str:
        return self.texts[-1][1] if self.texts else ""


@pytest.fixture(autouse=True)
def _clear_pending():
    brief_pending._PENDING.clear()
    yield
    brief_pending._PENDING.clear()


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    """真 app + 真库 + 真 channel；只把飞书传输换成假的。

    白名单要先写进 db 同目录的 ``config.json`` —— 配置跟着 db 走
    （``--db`` 指定别的位置时配置也跟着走那个目录），所以顺序是
    先落配置再建 app。
    """
    project = tmp_path / "repo"
    project.mkdir()
    (tmp_path / "config.json").write_text(
        json.dumps({"delegate": {"projects": [str(project)]}}),
        encoding="utf-8",
    )

    app = build_app(tmp_path / "a.db")
    app.roles.create("工作项目A")
    channel = ChannelService(app, allowed_senders=frozenset({WHO}))
    sender = _Sender()
    monkeypatch.setattr(B, "_card_channel", channel, raising=False)
    monkeypatch.setattr(B, "_card_sender", sender, raising=False)

    # 快照与当前选择共用一份 box —— 见模块 docstring 末尾那段。
    box = {
        "project": str(project),
        "model": "opencode/big-pickle",
        "agent": "Sisyphus - ultraworker",
        "variant": "",
    }
    monkeypatch.setattr(B, "_oc_load_selection", lambda: _sel(box))

    yield app, channel, sender, box, project
    app.close()


def _sel(box: dict):
    return sel.Selection(
        project=box["project"], agent=box["agent"],
        model=box["model"], variant=box["variant"],
    )


def _arm_and_fill(box: dict, brief: str = "改一下登录页") -> None:
    brief_pending.arm(
        WHO, project=box["project"], model=box["model"],
        agent=box["agent"], variant=box["variant"], chat_id=CHAT,
    )
    brief_pending.fill_brief(WHO, brief)


def _click(choice: str):
    return B._run_delegate_confirm({"choice": choice, "chat": CHAT}, who=WHO)


# --------------------------------------------------------------------------- #
# 真的建出来了
# --------------------------------------------------------------------------- #
def test_real_create_builds_a_real_delegate(wired):
    """点「建这条委派」→ **真库**里出现一条带项目路径的委派事务。"""
    app, _channel, sender, box, project = wired
    _arm_and_fill(box)

    resp = _click("create")

    tasks = app.tasks.list_all()
    assert len(tasks) == 1, f"应当恰好建出一条，实际 {len(tasks)}"
    task = tasks[0]

    # 1. 项目路径真的落库了 —— 这是「委派」与「普通事务」的分界
    assert task.project_path == str(project), f"项目路径没落对：{task.project_path!r}"
    assert Path(task.project_path).is_dir(), "项目路径应当是真实存在的目录"

    # 2. 脉络归属：第一条脉络（这处是已知偏离，不是留空）
    assert task.role_ids, "必须挂到一条脉络上"
    role = app.roles.get(task.role_ids[0])
    assert role is not None and role.name == "工作项目A", (
        f"脉络不对：{role.name if role else None!r}"
    )

    # 3. 发起人是点击者
    assert task.delegate_requested_by == WHO, (
        f"发起人应是点击者 {WHO}，实际 {task.delegate_requested_by!r}"
    )

    # 4. 第三道闸门：建出来是**待办**，不是 active
    assert task.state is not TaskState.ACTIVE, (
        f"刚建的不许是 active（执行器会立刻碰它），实际 {task.state}"
    )

    # 5. 用户看到的话里要有 /start 这一道门
    assert "/start" in sender.last, f"回话没告诉用户 /start：{sender.last!r}"
    # 6. 卡片是**绿头**「已建委派」—— 与下面「被拒时不许绿」那条一起把
    #    「成功才绿」夹住（只查一条等于没夹）。
    header = resp["card"]["data"]["header"]
    assert header["title"]["content"] == "已建委派", (
        f"成功时卡片标题应说已建：{header['title']['content']!r}"
    )
    assert header["template"] == "green", (
        f"成功时卡片应是绿头：{header['template']!r}"
    )


def test_second_click_builds_nothing_more(wired):
    """单次有效：第二次点「建这条委派」不再建第二条。"""
    app, _channel, _sender, box, _project = wired
    _arm_and_fill(box)

    _click("create")
    _click("create")

    assert len(app.tasks.list_all()) == 1, "重复点击建出了第二条委派"


# --------------------------------------------------------------------------- #
# 白名单这道闸门没被新入口绕过
# --------------------------------------------------------------------------- #
def test_project_outside_whitelist_is_refused(wired):
    """白名单外的项目**建不出来**。

    这是最要紧的一条：第 5 步是桥接侧新加的入口，若它在建事务前没走
    ``check_project_allowed``，等于给「往任意目录派活」开了个新口子。

    ``box`` 也指向那个目录 —— 否则先撞上的是**快照漂移**闸门，测的就不是
    白名单了。
    """
    app, _channel, sender, box, _project = wired
    outside = str(Path(_project).parent / "somewhere-else")
    box["project"] = outside
    _arm_and_fill(box)

    resp = _click("create")

    assert app.tasks.list_all() == [], "白名单外的项目竟然建出事务了"

    # 卡片抢不得功：「没建出来」时它**不许**是绿头「已建委派」。
    # `_cmd_delegate` 失败时只 `_say("[拒绝] …")`，并不置 `denied`，
    # 所以旧实现一路落到尾、无条件报绿 —— 用户瞥一眼就以为成了。
    header = resp["card"]["data"]["header"]
    assert header["title"]["content"] != "已建委派", (
        f"没建出来，卡片却说成了：{header['title']['content']!r}"
    )
    assert header["template"] != "green", (
        f"没建出来，卡片却报绿：{header['template']!r}"
    )

    # 聊天窗口必须有一句如实的说明（含拒绝原因「白名单」）。
    assert "白名单" in sender.last, f"应当说清为什么被拒：{sender.last!r}"


def test_selection_drift_blocks_create(wired):
    """arm 之后选型被别处改掉（Web 编程页）→ 提示重选，**不建**。

    确认卡上回显的模型必须是**真正会用的那个**；两者不一致却照建，
    用户点的是 A、系统干的是 B。
    """
    app, _channel, sender, box, _project = wired
    _arm_and_fill(box)

    box["model"] = "opencode/别的模型"      # 模拟 Web 上改了模型
    _click("create")

    assert app.tasks.list_all() == [], "选型漂移竟然照建了"
    # 只回写卡片是不够的 —— 用户得在聊天窗口里**看到**要他重选，
    # 而不是自己去发现「刚才那张卡变了」。
    assert sender.last, "只回写了卡片，聊天气泡没发"
    assert "重选" in sender.last, f"应当说清要重选：{sender.last!r}"