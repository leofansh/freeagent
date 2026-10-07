"""四段选择卡的**结构**与点击路由。

## 为什么结构也要测

这一族卡与 :func:`sender.menu_card` 有同一个致命约束（官方 200830）：
**2.0 卡不能更新成 1.0，反之亦然**。桥接的应答走同一条更新回写路径，
所以版本写错的后果是「点一下直接失败」，而**发出去时完全正常**。

第一版就踩过这个：批准卡发的是 1.0、菜单卡写成 2.0，症状是
「点按钮报出错了」而日志里看不出结构问题。

## 为什么「值必须校验」这条要测

载荷来自飞书，而**飞书载荷用户可以伪造**（转发卡片、手改 JSON）。
不校验回传值就等于「任何人都能让机器人往任意目录委派」——
那正好绕过了项目白名单这唯一一道闸门。
"""

from __future__ import annotations

import pytest

from freeagent.feishu import bridge
from freeagent.feishu.sender import (
    MENU_ACTION,
    PLAN_CONFIRM_ACTION,
    SELECT_ACTION,
    SELECT_STAGES,
    select_card,
)
from freeagent.services import oc_discovery as discovery
from freeagent.services import oc_selection as sel


class FakeSender:
    """只记下发了什么。不碰网络。"""

    def __init__(self) -> None:
        self.cards: list[tuple[str, dict]] = []
        self.texts: list[tuple[str, str]] = []

    def _receive_id_type(self, open_id: str) -> str:
        return "open_id"

    class config:
        base_url = "https://open.feishu.cn"

    timeout = 5
    token = staticmethod(lambda: "t")

    def _transport(self, url, payload, headers, timeout):
        card = __import__("json").loads(payload["content"])
        self.cards.append((url, card))
        return b'{"code":0,"data":{"message_id":"om_test"}}'

    def send_text(self, chat_id, text, **kw):
        self.texts.append((chat_id, text))


@pytest.fixture
def opts():
    return [
        sel.Option(value="opencode/big-pickle", label="Big Pickle", hint="OpenCode Zen"),
        sel.Option(value="opencode/fledge-alpha-free", label="Fledge Alpha Free",
                   hint="OpenCode Zen · 3 档"),
    ]


# ── 卡结构 ────────────────────────────────────────────────────────────── #

def test_select_card_is_json_1_0(opts):
    """1.0 = 顶层 ``elements`` + ``tag:"action"``。2.0 是 ``schema``/``body``。"""
    card = select_card("model", opts, chat_id="oc_1")
    assert "elements" in card
    assert "body" not in card and "schema" not in card
    assert card["config"]["wide_screen_mode"] is True


def test_select_card_has_no_expiry(opts):
    """选择没有「过期即拒」语义 —— 不发 credential 字段。

    与 :data:`VIEW_CHOICE_ACTION` / :data:`MENU_ACTION` 同一族。
    """
    card = select_card("model", opts, chat_id="oc_1")
    for el in card["elements"]:
        for btn in el.get("actions", []):
            assert "id" not in btn["value"], "选择卡不该带凭据"


def test_select_card_payload_carries_stage_and_chat(opts):
    card = select_card("project", opts, chat_id="oc_1")
    value = card["elements"][-1]["actions"][0]["value"]
    assert value["action"] == SELECT_ACTION
    assert value["stage"] == "project"
    assert value["chat"] == "oc_1"


def test_current_selection_is_highlighted(opts):
    """替代下拉框「框里那个值」的方式 —— 当前项 primary 高亮。"""
    card = select_card("model", opts, chat_id="oc_1",
                       selected={"model": "opencode/big-pickle"})
    buttons = card["elements"][-1]["actions"]
    assert buttons[0]["type"] == "primary"
    assert buttons[1]["type"] == "default"


def test_unknown_stage_rejected(opts):
    with pytest.raises(ValueError):
        select_card("nonsense", opts, chat_id="oc_1")


def test_empty_options_rejected():
    """空卡点不动，用户只会以为「机器人坏了」。"""
    with pytest.raises(ValueError):
        select_card("model", [], chat_id="oc_1")


# ── 分页 ──────────────────────────────────────────────────────────────── #

def _many(n: int) -> list[sel.Option]:
    return [sel.Option(value=f"p/{i}", label=f"Model {i}") for i in range(n)]


def test_pagination_carries_page_in_payload():
    """翻页**无状态**：页码进按钮载荷，桥接不必记「谁翻到第几页」。

    落状态的做法在桥接重启、多端同时点、同会话两人操作时会错位，
    而错位的表现是「点了 A 结果选了 B」—— 最坏的一种。
    """
    card = select_card("model", _many(30), chat_id="oc_1", page=1, page_size=12)
    nav = [el for el in card["elements"]
           if el["tag"] == "action" and el["actions"][0]["value"].get("nav")][0]
    assert nav["actions"][0]["value"]["page"] == 0      # 上一页
    assert nav["actions"][1]["value"]["page"] == 2      # 下一页
    assert nav["actions"][1]["value"]["nav"] == 1


def _option_buttons(card):
    """取「选项那一组」按钮。刻意不写死 ``elements[1]``。

    页数变了 note 就会多插一个元素，写死下标就成了「改一处红三处」。
    """
    for el in card["elements"]:
        if el["tag"] == "action" and not el["actions"][0]["value"].get("nav"):
            return el["actions"]
    return []


def test_pagination_splits_at_page_size():
    card = select_card("model", _many(30), chat_id="oc_1", page=0, page_size=12)
    assert len(_option_buttons(card)) == 12


def test_pagination_shown_only_when_needed(opts):
    single = select_card("agent", opts, chat_id="oc_1", page_size=12)
    assert all(el["tag"] != "note" for el in single["elements"])


def test_pagination_boundary_is_page_size():
    """清单 ≤ 每页数时**一次看完** —— 分页按钮与页码提示都不出现。

    ## 这条为什么存在

    「飞书模型卡要不要去掉分页」曾被我列成待办，依据是读了代码觉得
    「清单大了用不着分页了」。但读代码不算证据：万一它是**无条件**渲染
    的呢？而条件渲染这件事很容易在改动时被破坏（有人把 ``total_pages > 1``
    去掉，或者换了个默认 page_size）。

    实测确认了它本来就是条件渲染（1/5/12 个 → 无翻页，13 个 → 有），
    所以**没有代码要改** —— 但边界值值得钉住，因为它正是「勾到第 13 个
    模型那一刻」的体验变化点。
    """
    for n, want_nav in ((1, False), (12, False), (13, True), (85, True)):
        many = [sel.Option(value=f"opencode/m{i}", label=f"M{i}")
                for i in range(n)]
        card = select_card("model", many, chat_id="oc_1",
                           page_size=sel.MODEL_PAGE_SIZE)
        has_nav = any(el["tag"] == "action"
                      and el["actions"][0]["value"].get("nav")
                      for el in card["elements"])
        assert has_nav is want_nav, f"{n} 个模型时翻页={has_nav}，期望 {want_nav}"


def test_out_of_range_page_is_clamped(opts):
    """页码来自飞书载荷，可伪造 —— 越界不能变成 IndexError。"""
    card = select_card("model", _many(30), chat_id="oc_1", page=999, page_size=12)
    assert card  # 不抛


# ── 路由 ──────────────────────────────────────────────────────────────── #

@pytest.fixture
def wired(monkeypatch, tmp_path):
    """把 bridge 的模块级依赖接上假实现。返回 ``(sender, 读取函数)``。

    返回读取函数而不让测试直接 ``sel.load_selection()``：那样会读**真的**
    ``~/.freeagent/oc_selection.json`` —— 于是测试既污染用户真实偏好，
    又会因机器上已有选择而随机红。这类「测试写到用户目录」的问题在第一版
    就踩了：断言读到的是上一次跑测试留下的值。
    """
    sender = FakeSender()
    monkeypatch.setattr(bridge, "_card_sender", sender)
    monkeypatch.setattr(bridge, "_card_channel",
                        type("C", (), {"is_allowed": lambda s, w: True,
                                       "app": type("A", (), {"lock": None})()})())
    monkeypatch.setattr(bridge, "_oc_load_selection",
                        lambda: sel.load_selection(tmp_path))
    monkeypatch.setattr(bridge, "_oc_store",
                        lambda s: sel.save_selection(s, tmp_path))
    # 直接给 ``_STAGE_OPTS[stage]``（它**已经是列表**）—— 包成 ``([...], "")``
    # 会得到「列表的列表」，于是校验时 ``getattr(item, "value", "")`` 恒为
    # ``""``，所有回传值都被判成伪造。第一版就是这么红的：症状是
    # 「合法值也说不可用」，看着像生产代码的 bug。
    monkeypatch.setattr(bridge, "_oc_options",
                        lambda stage: (_STAGE_OPTS[stage], ""))
    return sender, lambda: sel.load_selection(tmp_path)


_STAGE_OPTS = {
    "project": [sel.Option(value="D:/p/freeagent", label="FreeAgent")],
    "agent": [sel.Option(value="Sisyphus - ultraworker", label="Sisyphus")],
    # 两个模型：``big-pickle`` 没有 variants，测「改模型要清档」要用它。
    "model": [sel.Option(value="opencode/fledge-alpha-free", label="Fledge"),
              sel.Option(value="opencode/big-pickle", label="Big Pickle")],
    "variant": [sel.Option(value="", label="不指定"), sel.Option(value="high", label="high")],
}


def _click(**value):
    value.setdefault("chat", "oc_1")
    return bridge._run_oc_select(value, who="ou_me")


def test_first_click_stores_and_advances(wired):
    sender, read = wired
    _click(stage="project", value="D:/p/freeagent", page=0)
    assert read().project == "D:/p/freeagent"
    assert len(sender.cards) == 1, "只发下一段，不发摘要"


def test_four_stages_then_summary(wired):
    sender, read = wired
    _click(stage="project", value="D:/p/freeagent", page=0)
    _click(stage="agent", value="Sisyphus - ultraworker", page=0)
    _click(stage="model", value="opencode/fledge-alpha-free", page=0)
    _click(stage="variant", value="high", page=0)
    got = read()
    assert (got.agent, got.variant) == ("Sisyphus - ultraworker", "high")
    assert sender.texts and "选好了" in sender.texts[-1][1], "第四段后要给摘要"


def test_forged_value_is_rejected(wired):
    """回传值不在选项里 —— 载荷可伪造，不校验就绕过了项目白名单。"""
    _, read = wired
    _click(stage="project", value="C:/Windows/System32", page=0)
    assert read().is_empty()


def test_nav_does_not_change_selection(wired):
    sender, read = wired
    _click(stage="project", value="D:/p/freeagent", page=0)
    _click(stage="model", value="", page=1, nav=1)
    assert read().model == "", "翻页不该动选择"
    assert sender.cards, "但要重发那一页"


def test_empty_value_clears_stage(wired):
    _, read = wired
    _click(stage="project", value="D:/p/freeagent", page=0)
    _click(stage="project", value="", page=0)
    assert read().project == ""


def test_empty_variant_is_legal(wired):
    """variant 段的空值 = 「不指定，用模型基线」，不是「清掉」。"""
    _, read = wired
    _click(stage="model", value="opencode/fledge-alpha-free", page=0)
    _click(stage="variant", value="", page=0)
    assert read().variant == ""


def test_changing_model_clears_variant(wired):
    """改了模型要清档 —— Big Pickle 没有 high。

    而 OpenCode 不校验（错误档也返回 204），症状是「选了 High 却没生效」。
    """
    _, read = wired
    _click(stage="model", value="opencode/fledge-alpha-free", page=0)
    _click(stage="variant", value="high", page=0)
    _click(stage="model", value="opencode/big-pickle", page=0)
    assert read().variant == ""


def test_unknown_stage_rejected(wired):
    assert bridge._run_oc_select({"stage": "x", "value": "y"},
                                 who="ou_me")["toast"]["type"] == "error"


def test_non_whitelisted_user_gets_nothing(monkeypatch, tmp_path):
    """白名单外点选择 —— 不发卡、不回话、不落状态。

    选择卡在群里也是全员可见的，不判就会向白名单外确认「这里有个 bot」。
    """
    sender = FakeSender()
    monkeypatch.setattr(bridge, "_card_sender", sender)
    monkeypatch.setattr(bridge, "_card_channel",
                        type("C", (), {"is_allowed": lambda s, w: False})())
    monkeypatch.setattr(bridge, "_oc_options",
                        lambda stage: (_STAGE_OPTS[stage], ""))
    bridge._run_oc_select({"stage": "project", "value": "D:/p/freeagent",
                           "chat": "oc_1"}, who="ou_stranger")
    assert not sender.cards and not sender.texts
    assert sel.load_selection(tmp_path).is_empty()


def test_select_is_dispatched_before_credentials(wired):
    """选择必须排在凭据流程之前 —— 它无状态、不查库。

    混进凭据流程是错配：那套机制（凭据 uuid4、「过期即拒」）是为**授权**
    设计的，凭空加在选择上只会多出「凭据过期」这类困惑。
    """
    sender, read = wired
    data = {"event": {"action": {"value": {
        "action": SELECT_ACTION, "stage": "project",
        "value": "D:/p/freeagent", "chat": "oc_1"}},
        "operator": {"open_id": "ou_me"}}}
    resp = bridge._card_action(data)
    assert resp.get("card") is not None, "应回写旧卡"
    assert read().project == "D:/p/freeagent"
    assert sender.cards, "并发出下一段卡"


def test_menu_and_select_actions_are_distinct():
    """两个 action 不能同名 —— 否则「哪个 choice 能当命令执行」失去单一判据。"""
    assert SELECT_ACTION not in (MENU_ACTION, PLAN_CONFIRM_ACTION)


def test_stage_order_is_dependency_order():
    """模型依赖 connected provider，档位依赖模型 —— 顺序不能改。"""
    assert SELECT_STAGES == ("project", "agent", "model", "variant")


# ── 快照：三段只起一个进程 ────────────────────────────────────────────── #

class _FakeServer:
    """假 discovery 客户端。数着被开了几次。"""

    def __init__(self):
        type(self).opened += 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def list_projects(self):
        return [{"worktree": "D:/p/freeagent", "name": "FreeAgent"}]

    def list_agents(self):
        return [{"name": "Sisyphus - ultraworker", "mode": "primary",
                 "model": {"providerID": "opencode", "modelID": "big-pickle"}}]

    def list_models(self):
        return {"connected": ["opencode"], "all": [
            {"id": "opencode", "name": "OpenCode Zen", "models": {
                "fledge-alpha-free": {"name": "Fledge Alpha Free",
                                      "variants": {"high": {}},
                                      "cost": {"input": 0, "output": 0}}}}]}


@pytest.fixture
def one_server(monkeypatch):
    """每次只放行**一个** discovery；第二次调用就失败。

    这样「四段各起一次进程」会立刻炸，而不只是慢 —— 慢在测试里看不出来。

    打桩打在 :mod:`services.oc_discovery` 上，而**不是**
    :mod:`freeagent.feishu.bridge`：快照与选项查询搬走之后（为了让 Web
    端点不必 import 飞书桥接），那才是它们所在的地方。桩留在旧位置的话，
    这几条会静默失去作用 —— 而「测试悄悄不再测任何东西」是最坏的结局：
    它们绿着，却什么也没守住。
    """
    _FakeServer.opened = 0
    monkeypatch.setattr(discovery, "_SNAPSHOT", None)

    def one_shot(**kw):
        if _FakeServer.opened >= 1:
            raise RuntimeError("又起了一个 discovery —— 快照该复用")
        return _FakeServer()
    monkeypatch.setattr(
        "freeagent.services.opencode_server.OpenCodeServer.discovery",
        classmethod(lambda cls, **kw: one_shot(**kw)))
    yield
    monkeypatch.setattr(discovery, "_SNAPSHOT", None)


def test_four_stages_use_one_server(one_server):
    """三份数据来自**同一个**进程，只该起一次。

    最初是三个各自带缓存的函数，各起各的进程：实测冷启动四段要走 20.6 秒
    （6.6 + 6.9 + 7.1），用户点一次「选项目/模型」要等 20 秒。
    合成快照后是 7.1 秒 —— 那 7 秒是 opencode 自己的冷启动，起不掉。
    """
    for stage in ("project", "agent", "model"):
        opts, _ = discovery.stage_options(stage)
        assert opts, f"{stage} 该有选项"
    assert _FakeServer.opened == 1


def test_snapshot_is_reused_across_calls(one_server):
    discovery.stage_options("project")
    for _ in range(5):
        discovery.stage_options("model")
    assert _FakeServer.opened == 1


def test_bridge_delegates_to_discovery(monkeypatch):
    """桥接必须**转手**服务层，不能自己查 —— 否则两处实现会漂移。

    这条防的是「有人在 bridge 里又写了一份查询」：那样 Web 端点与飞书
    就会给出不同的选项，而症状是「Web 上选得到，飞书里选不到」。
    """
    import ast
    from pathlib import Path

    src = Path("src/freeagent/feishu/bridge.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_oc_options")
    called = {n.func.id for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "stage_options" in called, "bridge._oc_options 不该自己查 OpenCode"
    for banned in ("list_projects", "list_agents", "list_models",
                   "list_model_options", "variant_options"):
        assert banned not in called, f"bridge._oc_options 又自己调了 {banned}"


def test_each_stage_failure_is_isolated(monkeypatch):
    """provider 查失败不该让「有哪些项目」也一起消失。

    三份数据逐项记成败：一个登录失败不该让另外两段变成空的。
    """
    class Partial(_FakeServer):
        def list_models(self):
            raise RuntimeError("登录过期")

    monkeypatch.setattr(discovery, "_SNAPSHOT", None)
    monkeypatch.setattr(
        "freeagent.services.opencode_server.OpenCodeServer.discovery",
        classmethod(lambda cls, **kw: Partial()))
    try:
        assert discovery.stage_options("project")[0], "项目不该受 provider 失败影响"
        assert discovery.stage_options("agent")[0], "工作模式也不该"
        opts, note = discovery.stage_options("model")
        assert opts == [] and "查不到" in note, "模型段要说清是查不到"
    finally:
        monkeypatch.setattr(discovery, "_SNAPSHOT", None)


def test_failure_is_distinguishable_from_empty(monkeypatch):
    """``None``（查不到）与 ``[]``（真的没有）必须能分开。

    混成一个空列表时，「OpenCode 没起来」与「你没装任何工作模式」
    长得一模一样，界面只能说同一句话 —— 于是用户往错的方向查。
    """
    monkeypatch.setattr(discovery, "_SNAPSHOT", None)
    monkeypatch.setattr(discovery, "_discovery_client",
                        lambda: (_ for _ in ()).throw(RuntimeError("起不来")))
    try:
        snap = discovery.snapshot()
        assert snap.agents is None, "起不来必须是 None"
        assert snap.projects is None and snap.providers is None
    finally:
        monkeypatch.setattr(discovery, "_SNAPSHOT", None)