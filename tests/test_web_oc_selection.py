"""``/api/oc/*`` 端点 —— 尤其**伪造载荷**与**清单**这两条。

## 为什么这些断言重要

编程页签的四个下拉框，其值最终决定「让 opencode 去哪个目录、用哪个模型」。
而那个值的来源是**浏览器** —— 浏览器载荷用户可以随便改（改 JS、重发请求）。

所以两条硬要求：

1. 值必须**对着真实选项校验** —— 不校验就等于任何本地页面都能让机器人
   把活派到任意目录，那正好绕过项目白名单这唯一一道准入闸门。
2. 清单里**只能放现在真能用的模型** —— 否则用户会勾上一个 401 的模型，
   然后在「为什么它跑不起来」上排查半天。
"""

from __future__ import annotations

import json

import pytest

from freeagent.domain import FreeAgentError
from freeagent.services import oc_discovery as discovery
from freeagent.services import oc_selection as sel
from freeagent.web import endpoints_oc_dispatch as ep_dispatch
from freeagent.web import endpoints_oc_selection as ep
from freeagent.web import endpoints_oc_selection_write as ep_write

#: 编程页签的端点**曾经**是一个文件，2026-10 按「读/ 写 / 建事务」拆成三个
#: （那条 250 行守卫逼出来的）。下面几条测试是**扫源码**的，所以它们必须
#: 跟着扫全部三个 —— 只扫其中一个等于「断言自己没持有的状态」，
#: 搬走的那半边从此没人管。
_EP_MODULES = ("endpoints_oc_selection", "endpoints_oc_selection_write",
               "endpoints_oc_dispatch")

#: 同理，JS 也拆成了「四段下拉 + 渲染」与「两张动作卡」两块。
_JS_MODULES = ("js_oc_selection", "js_oc_cards")


def _ep_source(name: str) -> str:
    return (__import__("pathlib").Path("src/freeagent/web") / f"{name}.py") \
        .read_text(encoding="utf-8")


def _js_body(name: str) -> str:
    """某个 JS 块里**真正上屏 / 真正执行**的那段字符串（剥掉 Python 那层壳）。"""
    raw = (__import__("pathlib").Path("src/freeagent/web") / f"{name}.py") \
        .read_text(encoding="utf-8")
    const = "JS_OC_SELECTION" if "selection" in name else "JS_OC_CARDS"
    return raw.split(f'{const} = r"""')[1]


PROJECT_ROWS = [{"worktree": "D:/p/freeagent", "name": "FreeAgent"},
                {"worktree": "D:/p/openmos", "name": "OpenMOS"}]

AGENT_ROWS = [
    {"name": "Sisyphus - ultraworker", "mode": "primary",
     "model": {"providerID": "opencode", "modelID": "big-pickle"}},
    {"name": "summary", "mode": "primary", "model": None},
]

PROVIDERS = {
    "connected": ["opencode"],
    "all": [{"id": "opencode", "name": "OpenCode Zen", "models": {
        "big-pickle": {"name": "Big Pickle", "variants": {},
                       "cost": {"input": 0, "output": 0}},
        "fledge-alpha-free": {"name": "Fledge Alpha Free",
                              "variants": {"high": {}, "low": {}},
                              "cost": {"input": 0, "output": 0}}}}],
}


class _FakeClient:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def list_projects(self):
        return list(PROJECT_ROWS)

    def list_agents(self):
        return list(AGENT_ROWS)

    def list_models(self):
        return json.loads(json.dumps(PROVIDERS))


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """端点 + 假 OpenCode + 隔离的 home。"""
    monkeypatch.setattr(discovery, "_SNAPSHOT", None)
    monkeypatch.setattr(discovery, "_discovery_client", lambda: _FakeClient())
    #三个模块各自 import 了一份 ``state_home_for``，所以要逐个打 ——
    # 漏掉哪一个，那个模块就会去读真实的 home。
    for name in _EP_MODULES:
        monkeypatch.setattr(f"freeagent.web.{name}.state_home_for",
                            lambda app: tmp_path)
    return tmp_path


class _App:
    """端点只用到 ``state_home_for(app)``，所以这里什么都能不实现。"""


# ── GET /api/oc/options ──────────────────────────────────────────────── #

def test_options_returns_all_four_stages(wired):
    got = ep.oc_options(_App())
    for key in ("project", "agent", "model", "variant"):
        assert key in got, f"缺 {key}"
        assert "options" in got[key] and "note" in got[key]
    assert [o["value"] for o in got["project"]["options"]] == [
        "D:/p/freeagent", "D:/p/openmos"]


def test_options_filters_internal_agents(wired):
    """``summary`` 是 OpenCode 内部件 —— 与飞书那份必须一致。"""
    values = [o["value"] for o in ep.oc_options(_App())["agent"]["options"]]
    assert values == ["Sisyphus - ultraworker"]


def test_options_reports_selection_and_curation(wired):
    sel.save_selection(sel.Selection(model="opencode/big-pickle"), wired)
    sel.save_curated_models(["opencode/big-pickle"], wired)
    got = ep.oc_options(_App())
    assert got["selection"]["model"] == "opencode/big-pickle"
    assert got["curated"]["configured"] is True
    assert got["curated"]["models"] == ["opencode/big-pickle"]
    # 「全部可用」也要给：界面要列**没勾**的那些让人勾
    assert len(got["curated"]["available"]) == 2


def test_unconfigured_curation_is_flagged_false(wired):
    """空清单 = **未挑过**，界面该说「全部可用」而不是「一个都没配」。"""
    got = ep.oc_options(_App())
    assert got["curated"]["configured"] is False
    assert len(got["model"]["options"]) == 2, "没挑过 = 给全部"


def test_option_shape_is_exactly_three_fields(wired):
    """只给三个字段 —— Option 将来加字段不该自动变成对外契约。"""
    opt = ep.oc_options(_App())["model"]["options"][0]
    assert set(opt) == {"value", "label", "hint"}


def test_discovery_failure_is_reported_not_hidden(wired, monkeypatch):
    """OpenCode 起不来时要**说清**，不能给一张空页让人以为没模型。"""
    monkeypatch.setattr(discovery, "_SNAPSHOT", None)
    monkeypatch.setattr(
        discovery, "_discovery_client",
        lambda: (_ for _ in ()).throw(RuntimeError("起不来")))
    got = ep.oc_options(_App())
    assert got["discovery_ok"] is False
    assert got["model"]["options"] == []
    assert "查不到" in got["model"]["note"]


# ── POST /api/oc/selection ───────────────────────────────────────────── #

def test_selection_save_roundtrip(wired):
    ep_write.oc_selection_save(_App(), {"selection": {
        "project": "D:/p/freeagent", "agent": "Sisyphus - ultraworker",
        "model": "opencode/fledge-alpha-free", "variant": "high"}})
    got = sel.load_selection(wired)
    assert got.project == "D:/p/freeagent"
    assert got.variant == "high"


def test_forged_project_is_rejected(wired):
    """**这条是闸门**：不校验就等于任何本地页面都能派到任意目录。"""
    with pytest.raises(FreeAgentError) as exc:
        ep_write.oc_selection_save(_App(), {"selection": {
            "project": "C:/Windows/System32"}})
    assert "不可选" in str(exc.value)
    assert sel.load_selection(wired).is_empty()


def test_forged_model_is_rejected(wired):
    with pytest.raises(FreeAgentError):
        ep_write.oc_selection_save(_App(), {"selection": {
            "model": "opencode/never-existed"}})


def test_empty_value_is_legal(wired):
    """清空下拉框 = 「用默认」，**不是**非法输入。

    ``variant`` 的空串还有另一层意思：「用模型基线」。若这里报错，
    用户就永远无法回到「不指定」。
    """
    ep_write.oc_selection_save(_App(), {"selection": {"model": "opencode/big-pickle"}})
    ep_write.oc_selection_save(_App(), {"selection": {"model": ""}})
    assert sel.load_selection(wired).model == ""


def test_unknown_stage_is_rejected(wired):
    with pytest.raises(FreeAgentError) as exc:
        ep_write.oc_selection_save(_App(), {"selection": {"__class__": "x"}})
    assert "不认识的段" in str(exc.value)


def test_changing_model_clears_variant(wired):
    """Big Pickle 没有档 —— 留着旧的 high 会让用户以为「选了却没生效」。"""
    ep_write.oc_selection_save(_App(), {"selection": {
        "model": "opencode/fledge-alpha-free", "variant": "high"}})
    ep_write.oc_selection_save(_App(), {"selection": {"model": "opencode/big-pickle"}})
    assert sel.load_selection(wired).variant == ""


# ── POST /api/oc/curation ───────────────────────────────────────────── #

def test_curation_add_and_remove(wired):
    ep_write.oc_curation_save(_App(), {"action": "add", "model": "opencode/big-pickle"})
    assert sel.load_curated_models(wired) == ["opencode/big-pickle"]
    ep_write.oc_curation_save(_App(), {"action": "add",
                                 "model": "opencode/fledge-alpha-free"})
    ep_write.oc_curation_save(_App(), {"action": "remove",
                                 "model": "opencode/big-pickle"})
    assert sel.load_curated_models(wired) == ["opencode/fledge-alpha-free"]


def test_curation_rejects_unavailable_model(wired):
    """清单里**只能有现在真能用的**。

    否则用户会勾上一个没凭据的模型，然后在「为什么它跑不起来」上
    排查半天 —— 而真实原因是它压根不在 connected 里。
    """
    with pytest.raises(FreeAgentError) as exc:
        ep_write.oc_curation_save(_App(), {"action": "add",
                                     "model": "opencode/某付费模型"})
    assert "不可用" in str(exc.value)
    assert sel.load_curated_models(wired) == []


def test_curation_rejects_bad_action(wired):
    for bad in ("toggle", "", None, "ADD"):
        with pytest.raises(FreeAgentError):
            ep_write.oc_curation_save(_App(), {"action": bad,
                                         "model": "opencode/big-pickle"})


def test_no_endpoint_accepts_credentials(wired):
    """**不提供「连接提供商」** —— 这是刻意的缺席，不是漏做。

    ``/config/providers`` 实测返回明文 API Key，而 Web 无鉴权（只靠回环）。
    让凭据流经这里等于把密钥摊在页面上。所以配凭据留在 OpenCode 里做，
    FreeAgent 只管「日常用哪些模型」。
    """
    source = "".join(_ep_source(n) for n in _EP_MODULES)
    assert "/config/providers" not in source.replace(
        "``/config/providers``", ""), "别去读那个会泄露明文 key 的端点"
    assert "api_key" not in source and "API_KEY" not in source


# ── 前端：派发接线 ──────────────────────────────────────────────────── #

def test_dispatch_view_is_actually_reachable():
    """``ocDispatchView`` 必须**被调用** —— 定义了不等于接线了。

    第一版写完这个函数就去做别的了，它一次都没被调用，于是页面上只有
    选择、没有「开始」。这类漏最安静：JS 语法合法、端点齐全、全绿。

    函数搬到了 :mod:`js_oc_cards`，而调用它的是 :mod:`js_oc_selection` 的
    ``renderOcSelection`` —— 所以这条断言**跨两个文件**：只查「定义在哪个
    文件」会漏掉「搬完之后没人调了」，那正是本条要防的那件事。
    """
    assert "function ocDispatchView(" in _js_body("js_oc_cards")
    assert "ocDispatchView(data, root)" in _js_body("js_oc_selection"), \
        "定义了却从没被调用"


def _js_onscreen_text() -> str:
    """JS 里**真正上屏**的部分：剥掉注释。

    刻意剥注释再断言，而不是对整个文件下绝对判据 —— 注释里用 ``**`` 强调
    是**合理的**（那是给人读的中文说明），而只有字符串里的会显示成星号。
    第一版就是「整个文件不许有 ``**``」，结果注释里的中文说明把自己的测试
    判红了 —— 判据错了，红的也是错的。

    剥 ``//`` 时跳过前面是 ``:`` 的那种（``https://…``），否则将来谁在
    提示里写个链接就会误报。
    """
    import re

    body = "\n".join(_js_body(n) for n in _JS_MODULES)
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    body = re.sub(r"(?<!:)//[^\n]*", "", body)
    return body


def test_hint_text_has_no_markdown_emphasis():
    """``hint`` 元素**不解析 markdown**，只认 HTML。

    所以 ``**这样**`` 会原样显示成星号 —— 而星号在界面上看起来像乱码。
    这条断言的原因是我**已经犯过两次**：第一次在说明文字里，第二次在
    派发卡的提示里，都是写的时候顺手用了 markdown 习惯。

    真需要强调就用 ``<strong>`` —— 那个确实生效（同一个页面上「配置与试验」
    就是靠它加粗的）。
    """
    onscreen = _js_onscreen_text()
    assert "**" not in onscreen, (
        "上屏文本里出现了 ** —— hint 不解析 markdown，会显示成字面星号。"
        "要强调请用 <strong>。"
    )


def test_emphasis_actually_uses_strong():
    """反向确认：确实有地方用 ``<strong>``，否则上面那条可能是「因为没人强调」。"""
    assert "<strong>" in _js_onscreen_text()


def test_python_user_facing_strings_have_no_markdown():
    """Python 侧上屏文案也不许有 ``**`` —— **同一个 bug 的第三个来源**。

    界面不解析 markdown（只认 HTML），所以任何**会显示给用户**的字符串里
    写 ``**这样**``，用户看到的就是字面星号。

    这条判据用 AST 区分「docstring」与「真字符串」：docstring 与注释是给
    开发者读的、在那儿用 ``**`` 强调是**合理的**；而字面量是要上屏的。

    加上这条的原因：这个 bug 我犯了三次 —— 前两次在 JS 字符串里
    （``test_hint_text_has_no_markdown_emphasis`` 已钉住），第三次在
    Python 端点返回的 ``next`` 话术里，靠肉眼看截图才发现。
    """
    import ast

    offenders: list[str] = []
    for name in _EP_MODULES:
        tree = ast.parse(_ep_source(name))

        # 收集所有 docstring 的节点 id：它们是「说明」而不是「上屏文案」。
        docstrings: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
                body = getattr(node, "body", None)
                if (body and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    docstrings.add(id(body[0].value))

        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant):
                continue
            if not isinstance(node.value, str) or id(node) in docstrings:
                continue
            if "**" in node.value:
                offenders.append(f"{name}: {node.value[:60]}")

    assert not offenders, (
        "这些字符串会上屏但里面有 **（界面不解析 markdown）：\n  "
        + "\n  ".join(offenders)
    )


def test_brief_must_be_single_line(wired):
    """含换行的需求必须**在这里**被挡。

    实测：opencode 看到换行就判定「复杂任务」，升级到它自己的强模型并
    **无视** ``--model``，于是必然失败。那是 opencode 的行为，所以我们挡 ——
    而且要挡在**建事务之前**，否则用户会拿到一条注定失败的委派。
    """
    with pytest.raises(FreeAgentError) as exc:
        ep_dispatch.oc_dispatch(_App(), {"brief": "第一行\n第二行",
                                "project": "D:/p/freeagent"})
    assert "一行" in str(exc.value)


def test_empty_brief_is_rejected(wired):
    with pytest.raises(FreeAgentError):
        ep_dispatch.oc_dispatch(_App(), {"brief": "  ", "project": "D:/p/freeagent"})


def test_dispatch_without_project_says_so(wired):
    """没选项目要说清，**不要**默默挑一个 —— 挑错的代价是在错误的仓库动手。"""
    with pytest.raises(FreeAgentError) as exc:
        ep_dispatch.oc_dispatch(_App(), {"brief": "改点东西"})
    assert "还没选项目" in str(exc.value)

class TestDispatchStartIsClickable:
    """派发之后必须**能点**，不能叫人手敲 ``/start``。

    由来（2026-10-09）：四段选择（项目/模式/模型/档位）在网页上都是点选，
    唯独「开始」要手打 ``/start xxxx`` —— 而飞书动作卡上这个按钮一直都在
    （``sender.py`` 的 ``_btn("开始做", "/start")``）。同一件事两个渠道两种
    待遇，设计文档 11.9.8 的「点一下即可」在网页端等于没兑现。

    钉死它：按钮在，且走的是任务动作那条老路（不是新端点）。
    """

    def test_js_has_a_start_button(self):
        src = _ep_source("js_oc_cards")
        assert "开始做" in src, "派发卡要有「开始做」按钮"

    def test_button_reuses_the_task_action_endpoint(self):
        """走 ``/api/task/<id>/start`` —— ``start`` 已在动作白名单里。

        刻意**不**新开端点：白名单与「状态→动作」翻译是同一份契约的两半，
        再开一条就是两套契约。
        """
        src = _ep_source("js_oc_cards")
        assert "/api/task/" in src and "/start" in src

    def test_next_wording_no_longer_asks_for_manual_start(self):
        """``next`` 话术不该再教人手敲命令 —— 有按钮了还说「发 /start」自相矛盾。"""
        src = _ep_source("endpoints_oc_dispatch")
        assert "发 `/start" not in src
        assert "开始做" in src
