"""第 5 步的**真链路**：走 ``_card_action`` 与 ``_process``，落到真 sqlite。

## 这个文件为什么存在

前面几个文件都停在「函数的输入输出」这一层：

- ``test_brief_flow.py`` 用**假 channel**，只断言「路由成了哪条命令」；
- ``test_brief_flow_real.py`` 用真 channel，但**直接调用**
  ``_run_delegate_confirm`` —— 绕过了 ``_card_action`` 的载荷拆解。

于是有一整段从来没被任何测试走过：**飞书点击的真载荷 →
``_card_action`` 拆解 → 派发 → 四段选型；真 ``IncomingMessage`` →
``_process`` 的顺序 → ``_route_brief`` → 建委派**。

这正是「真机上点了一下」才会走的路径。这里把它补上，且仍然只把
**飞书传输**换成假的（网络没法在测试里发）。

## 两条必须先确认、不能猜的事实

1. **``IncomingMessage.sender_ids`` 是 property**（``events.py:96``），把
   open_id / user_id / union_id 合成 ``frozenset``。所以测试里**必须**用真
   ``IncomingMessage``，不能拿 dict 手搓 —— 手搓的没有这个 property，
   ``is_allowed`` 会拿到错形状，白名单判定就成了假绿。
2. **``dedup_key`` 也是 property**（``events.py:123``），取 ``event_id``
   或 ``message_id``。重投测试靠它。

## 为什么必须把「选型」那一层整个换掉

``bridge._oc_store`` 调 ``save_selection(selection)`` 时**不传 home**，
而 ``oc_selection.save_selection`` 在 ``home`` 为 None 时写
``Path.home()/".freeagent"/"oc_selection.json"``（``oc_selection.py:591-593``）
—— **真机上那份选型文件**。它不读 ``FREEAGENT_HOME``，只认显式参数。

所以让四段点击走真 ``_oc_store`` 的测试，跑一次就把用户机器上的选型覆盖了。
这不是测试该付的代价，故而 fixture 里把 ``_oc_options`` /
``_oc_load_selection`` / ``_oc_store`` 三者一并换成假的：要验的是
``_card_action`` 的派发与 ``_process`` 的顺序，不是真 opencode 发现层。
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
from freeagent.feishu.config import ENV_APP_SECRET, FeishuConfig  # noqa: E402
from freeagent.feishu.events import IncomingMessage  # noqa: E402
from freeagent.feishu.sender import (  # noqa: E402
    BRIEF_CONFIRM_ACTION,
    SELECT_ACTION,
)
from freeagent.secrets import SecretStr, as_secret  # noqa: E402
from freeagent.services import oc_selection as sel  # noqa: E402
from freeagent.services.channel import ChannelService  # noqa: E402

CHAT = "oc_chat"
WHO = "ou_owner"
BRIEF = "把登录页在窄屏下的按钮错位修掉"


class _Sender:
    """只实现点击与简报路径用到的几个方法（照既有真库测试的写法）。"""

    def __init__(self) -> None:
        self.cards: list[dict] = []
        self.texts: list[tuple[str, str]] = []

    def _receive_id_type(self, open_id: str) -> str:
        return "open_id"

    class config:
        base_url = "https://open.feishu.cn"

    timeout = 5
    token = staticmethod(lambda: "t")

    def _transport(self, url, payload, headers, timeout):
        self.cards.append(json.loads(payload["content"]))
        return b'{"code":0,"data":{"message_id":"om_test"}}'

    def send_text(self, chat_id, text, **kw):
        self.texts.append((chat_id, text))

    @property
    def last(self) -> str:
        return self.texts[-1][1] if self.texts else ""


class _Opt:
    """一个最小 Option：``_run_oc_select`` 只读 ``value``。"""

    def __init__(self, value: str) -> None:
        self.value = value


def _click_payload(value: dict, *, who: str = WHO) -> dict:
    """**飞书点击的真载荷形状**。

    ``_card_action`` 的 ``_plain`` 逐层拆 ``__dict__`` 与 dict，所以嵌套
    dict 就能过。刻意用 dict 而不是造 SDK 对象：这里要验的是**载荷形状**
    （``event.action.value`` / ``operator`` / ``header``），不是 SDK 模型。
    """
    return {
        "event": {
            "action": {"value": dict(value)},
            "operator": {"open_id": who},
            "context": {"open_chat_id": CHAT},
        },
        "header": {"event_id": f"ev_{value.get('stage') or value.get('choice')}"},
    }


def _msg(text: str, *, event_id: str = "ev_msg_1") -> IncomingMessage:
    """真 ``IncomingMessage`` —— 因为 ``sender_ids`` / ``dedup_key`` 是 property。"""
    return IncomingMessage(
        event_id=event_id, chat_id=CHAT, sender_open_id=WHO, text=text,
        is_group=False, mentioned=True,
    )


@pytest.fixture(autouse=True)
def _clear_pending():
    brief_pending._PENDING.clear()
    yield
    brief_pending._PENDING.clear()


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    """真 app + 真 channel + **真桥接对象**；只把飞书传输换成假的。"""
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

    cfg = FeishuConfig(
        app_id="cli_test",
        # 与 config.py 同一套兜底：as_secret 对空串返回 None。
        app_secret=as_secret("t", label=ENV_APP_SECRET)
        or SecretStr("", label=ENV_APP_SECRET),
        allowed_users=frozenset({WHO}),
        domain="feishu",
        reminder_poll=60.0,
    )
    bridge = B.FeishuBridge(channel, sender, cfg, bot_open_id="ou_bot")

    # 卡片路径读的模块级状态切成这一套
    monkeypatch.setattr(B, "_card_channel", channel, raising=False)
    monkeypatch.setattr(B, "_card_sender", sender, raising=False)

    # 选型那一层整层换掉 —— 理由见模块 docstring 末段：
    # 真 ``_oc_store`` 会写真机上的 oc_selection.json。
    #
    # ⚠️ ``_oc_load_selection`` **必须每次返回新副本**。``_run_oc_select``
    # 点完一段会 ``clear_from(stage)`` 把下游清掉；若返回同一个可变对象，
    # 第一次点 project 就把 agent/model 冲掉了，后面几段拿到空值静默清空 —
    # 症状是「工作模式没进快照」，而那是**测试自己的坑**，不是代码的。
    values = {
        "project": str(project),
        "agent": "Sisyphus - ultraworker",
        "model": "opencode/big-pickle",
        "variant": "",
    }

    def _fake_options(stage: str):
        return [_Opt(values[stage])], ""

    monkeypatch.setattr(B, "_oc_options", _fake_options)
    monkeypatch.setattr(
        B, "_oc_load_selection", lambda: sel.Selection(**values))
    monkeypatch.setattr(B, "_oc_store", lambda selection: None)

    yield app, channel, sender, bridge, values
    app.close()


def _click_four_stages(sender: _Sender, values: dict) -> None:
    """走**真** ``_card_action`` 点完四段。

    值来自 fixture 的 ``values``（常量），**不是**从 state 上现读 ——
    上面那条注意说明了为什么不能现读。
    """
    for stage in ("project", "agent", "model", "variant"):
        B._card_action(_click_payload({
            "action": SELECT_ACTION, "stage": stage,
            "value": values[stage], "page": 0, "chat": CHAT,
        }))


# --------------------------------------------------------------------------- #
# 真链路：四段 → 简报 → 确认卡 → 建委派
# --------------------------------------------------------------------------- #
def test_full_chain_through_card_action_and_process(wired):
    """从飞书点击一路走到真库里出现委派事务。"""
    app, _channel, sender, bridge, values = wired

    _click_four_stages(sender, values)

    # 四段点完 → 快照已 arm，简报还空着；且发的是**提示卡**而不是纯文本
    armed = brief_pending.peek(WHO)
    assert armed is not None, "四段点完应当 arm 出快照"
    assert armed.brief == "", "还没交简报，brief 必须为空"
    assert armed.project == values["project"]
    assert sender.cards, "应当发过卡片"

    # 交简报：走**真** _process
    bridge._process(_msg(BRIEF))

    filled = brief_pending.peek(WHO)
    assert filled is not None and filled.brief == BRIEF, "简报没被填进快照"

    # 交简报**不许**建事务 —— 这一步只回显
    assert app.tasks.list_all() == [], "交简报就建事务 = 确认卡形同虚设"
    assert sender.cards, "交简报应当发出确认卡"

    # 点「建这条委派」—— 又走一次真 _card_action
    B._card_action(_click_payload(
        {"action": BRIEF_CONFIRM_ACTION, "choice": "create", "chat": CHAT}))

    tasks = app.tasks.list_all()
    assert len(tasks) == 1, f"应当建出一条委派，实际 {len(tasks)}"
    task = tasks[0]
    assert task.project_path == values["project"]
    assert task.delegate_requested_by == WHO
    assert task.state is not TaskState.ACTIVE, "刚建的不许是 active"
    assert "/start" in sender.last, f"回话没告诉用户 /start：{sender.last!r}"


def test_clicking_four_stages_arms_without_touching_real_selection(wired):
    """四段点完 arm 的是**这一份**快照，且没有写真盘。

    这是上面那条安全顾虑的守门测试：``_oc_store`` 被换成 no-op，所以真机
    的 ``oc_selection.json`` 一个字节都不该动。
    """
    _app, _channel, _sender, _bridge, values = wired

    _click_four_stages(_sender, values)

    armed = brief_pending.peek(WHO)
    assert armed is not None
    assert armed.project == values["project"]
    assert armed.agent == "Sisyphus - ultraworker", "工作模式没进快照"
    assert armed.model == "opencode/big-pickle", "模型没进快照"


# --------------------------------------------------------------------------- #
# 去重重投 —— 本链路最容易漏的坑
# --------------------------------------------------------------------------- #
def test_replayed_brief_message_creates_only_one(wired):
    """同一条简报消息被飞书重投 → 委派仍只有一条，且不蹦出普通事务。

    ``_process`` 自己**不去重**（只在 ``channel.handle`` 里做），而简报路径
    绕过了 ``handle``。所以第一次已 ``fill_brief`` 并发卡，第二次若不再拦，
    就会掉进 ``handle`` 拿简报正文**建出一条普通事务**。
    """
    app, _channel, _sender, bridge, _values = wired
    _click_four_stages(_sender, _values)

    bridge._process(_msg(BRIEF, event_id="ev_replay"))
    filled = brief_pending.peek(WHO)
    assert filled is not None and filled.brief, "简报没填进快照"

    # 第一次点到建
    B._card_action(_click_payload(
        {"action": BRIEF_CONFIRM_ACTION, "choice": "create", "chat": CHAT}))
    first = len(app.tasks.list_all())
    assert first == 1, f"第一次点就该建出来，实际 {first}"

    # 飞书重投：同一条消息、同一个 event_id
    bridge._process(_msg(BRIEF, event_id="ev_replay"))

    assert len(app.tasks.list_all()) == 1, (
        "重投建出了第二条 —— 简报路径的去重没生效"
    )


# --------------------------------------------------------------------------- #
# 命令打断
# --------------------------------------------------------------------------- #
def test_slash_command_clears_pending_state(wired):
    """待填态下发 ``/today`` → 待填态清掉，且走真 ``channel.handle``。

    ``/`` 开头的不是简报。若不顺手清掉，用户改主意去打命令之后，下一条
    普通文字还会被当成简报吞掉。
    """
    app, _channel, _sender, bridge, _values = wired
    _click_four_stages(_sender, _values)
    brief_pending.fill_brief(WHO, "上一句")

    bridge._process(_msg("/today"))

    assert brief_pending.peek(WHO) is None, "命令没把待填态清掉"
    assert app.tasks.list_all() == [], "/today 是只读的，却建出了东西"


# --------------------------------------------------------------------------- #
# 问候语仍归菜单
# --------------------------------------------------------------------------- #
def test_greeting_is_not_taken_as_brief(wired):
    """待填态下发「你好」→ 不该被当成简报。

    ``_route_brief`` 插在 ``_route_menu`` **之后**，所以问候语先被菜单拦下。
    这里证明插入点没排错：菜单仍然优先。
    """
    _app, _channel, sender, bridge, _values = wired
    _click_four_stages(sender, _values)

    bridge._process(_msg("你好"))

    armed = brief_pending.peek(WHO)
    assert armed is not None and armed.brief == "", "「你好」被当成简报吞了"
    assert sender.cards, "菜单应当回了入口卡"