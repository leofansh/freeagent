"""OpenCode 四段选择的过滤与状态（设计文档 12.7.1 补充）。

## 为什么这些断言要锁

三条过滤规则**每一条都对应一个真实踩过的坑**，而症状全都表现为
「选项少了一个 / 多了奇怪的东西 / 选不中」，极难从界面反推：

- 少了 ``global`` 项目 —— 它的 worktree 是 ``/``，不是目录
- 多了 ``compaction`` / ``summary`` / ``title`` —— OpenCode 内部件
- 85 个还是 8475 个 —— 取决于有没有按 ``connected`` 过滤

## 为什么「模型名里有 free」不是判据

实测 ``space-bunny-free`` 名字带 free，而 ``big-pickle`` / ``kimi-k3``
同样 ``cost == 0`` 却没带。按名字判会漏掉一大半，而「哪些真不要钱」
只有 ``cost`` 说了算。:func:`_is_free` 因此只读 ``cost``。
"""

from __future__ import annotations

import json

import pytest

from freeagent.services import oc_selection as sel


# ── 项目 ──────────────────────────────────────────────────────────────── #

def test_global_project_is_filtered():
    """``global`` 的 worktree 是 ``/``，不是目录 —— 委派进去不可能成功。"""
    rows = [
        {"name": "global", "worktree": "/"},
        {"name": "FreeAgent", "worktree": "D:\\PycharmProjects\\freeagent"},
        {"name": "", "worktree": "D:\\PycharmProjects\\openmos"},
    ]
    got = sel.list_project_options(rows)
    assert [o.label for o in got] == ["FreeAgent", "openmos"]


def test_project_backslash_is_normalized_and_deduped():
    """HTTP 返回反斜杠，DB 返回正斜杠 —— 两种都要归一，否则白名单匹配不上。"""
    rows = [
        {"name": "FreeAgent", "worktree": "D:\\PycharmProjects\\freeagent"},
        {"name": "FreeAgent", "worktree": "D:/PycharmProjects/freeagent"},
    ]
    got = sel.list_project_options(rows)
    assert len(got) == 1, "同一目录不该出两项（斜杠方向不同）"
    assert got[0].value == "D:/PycharmProjects/freeagent"


def test_project_unnamed_falls_back_to_dirname():
    rows = [{"name": "", "worktree": "D:\\PycharmProjects\\xiaoyuan"}]
    assert sel.list_project_options(rows)[0].label == "xiaoyuan"


# ── 工作模式 ──────────────────────────────────────────────────────────── #

def test_internal_agents_are_filtered():
    """``compaction`` / ``summary`` / ``title`` 是 OpenCode 内部件。

    Desktop 的下拉框里没有它们，所以飞书里也不该有 —— 两边不一致时
    用户会问「为什么这里有 summary」。
    """
    rows = [
        {"name": "Sisyphus - ultraworker", "mode": "primary",
         "model": {"providerID": "opencode", "modelID": "big-pickle"}},
        {"name": "compaction", "mode": "primary", "model": None},
        {"name": "summary", "mode": "primary", "model": None},
        {"name": "title", "mode": "primary", "model": None},
        {"name": "explore", "mode": "subagent",
         "model": {"providerID": "opencode", "modelID": "big-pickle"}},
    ]
    got = sel.list_agent_options(rows)
    assert [o.value for o in got] == ["Sisyphus - ultraworker"]


def test_agent_without_model_is_filtered_even_if_named_normally():
    """``model is None`` 是真判据 —— 不只挡那三个已知名字。"""
    rows = [{"name": "mystery", "mode": "primary", "model": None}]
    assert sel.list_agent_options(rows) == []


def test_internal_agent_rejected_even_if_it_gains_a_model():
    """名字那道闸门是独立的：哪天 OpenCode 给 summary 配了模型，也挡住。"""
    rows = [{"name": "summary", "mode": "primary",
             "model": {"providerID": "opencode", "modelID": "x"}}]
    assert sel.list_agent_options(rows) == []


def test_agent_hint_shows_its_default_model():
    rows = [{"name": "Prometheus - Plan Builder", "mode": "primary",
             "model": {"providerID": "opencode", "modelID": "claude-fable-5"}}]
    got = sel.list_agent_options(rows)[0]
    assert got.hint == "opencode/claude-fable-5"
    assert got.value == "Prometheus - Plan Builder", "回传必须用精确名"


# ── 工作模式的失效校验 ────────────────────────────────────────────────── #

_AGENT_ROWS = [
    {"name": "Sisyphus - ultraworker", "mode": "primary",
     "model": {"providerID": "opencode", "modelID": "big-pickle"}},
    {"name": "Prometheus - Plan Builder", "mode": "primary",
     "model": {"providerID": "opencode", "modelID": "claude-fable-5"}},
    # 下面三条都会被 list_agent_options 过滤掉，但名字是真实存在的 ——
    # 它们正是「列表里看不见、校验却说认识」这种错位的来源。
    {"name": "explore", "mode": "subagent",
     "model": {"providerID": "opencode", "modelID": "big-pickle"}},
    {"name": "summary", "mode": "primary", "model": None},
    {"name": "mystery", "mode": "primary", "model": None},
]


def test_unspecified_agent_is_always_valid():
    """不指定 = 用 OpenCode 默认，这不是失效。"""
    assert sel.current_agent_is_valid(_AGENT_ROWS, "")


def test_agent_still_present_is_valid():
    assert sel.current_agent_is_valid(_AGENT_ROWS, "Sisyphus - ultraworker")


def test_agent_that_vanished_is_invalid():
    """核心场景：昨天选的，今天这份清单里没有了。"""
    assert not sel.current_agent_is_valid(_AGENT_ROWS, "Oracle - reviewer")


def test_agent_filtered_out_by_the_list_is_not_valid():
    """**最要紧的一条**：名字在原始数据里，但过滤规则不要它。

    若校验另写一套过滤，这里就会通过 —— 于是界面下拉里看不到它，
    派发时却又认它，两边漂移。
    """
    for name in ("explore", "summary", "mystery"):
        assert not sel.current_agent_is_valid(_AGENT_ROWS, name), name


def test_switching_project_can_invalidate_the_same_name():
    """agent 定义可以是项目级的，而选择是全局一份 —— 换项目即失效。"""
    project_b = [
        {"name": "build", "mode": "primary",
         "model": {"providerID": "opencode", "modelID": "big-pickle"}},
    ]
    assert sel.current_agent_is_valid(_AGENT_ROWS, "Sisyphus - ultraworker")
    assert not sel.current_agent_is_valid(project_b, "Sisyphus - ultraworker")


# ── 执行期校验（隔离执行世界，规则与展示校验有意分叉）──────────────────── #

#: 隔离执行世界的真实形状（实测）：只有内置 agent，**全部** model=None。
_ISOLATED_ROWS = [
    {"name": "build", "mode": "primary", "model": None},
    {"name": "plan", "mode": "primary", "model": None},
    {"name": "compaction", "mode": "primary", "model": None},
    {"name": "explore", "mode": "subagent",
     "model": {"providerID": "opencode", "modelID": "big-pickle"}},
]


def test_builtin_with_null_model_is_executable():
    """执行世界 model 全是 None（模型显式传）—— 按「model 非空」判会错杀一切。"""
    assert sel.agent_is_executable(_ISOLATED_ROWS, "build")
    assert sel.agent_is_executable(_ISOLATED_ROWS, "plan")


def test_subagent_is_not_executable_even_if_present():
    assert not sel.agent_is_executable(_ISOLATED_ROWS, "explore")


def test_internal_agents_are_never_executable():
    for name in ("compaction", "summary", "title"):
        rows = _ISOLATED_ROWS + [{"name": name, "mode": "primary", "model": None}]
        assert not sel.agent_is_executable(rows, name), name


def test_missing_agent_is_not_executable():
    """执行世界没有 Prometheus —— 选择世界选的它，执行世界必须拒。"""
    assert not sel.agent_is_executable(_ISOLATED_ROWS, "Prometheus - Plan Builder")


def test_unspecified_agent_is_always_executable():
    assert sel.agent_is_executable(_ISOLATED_ROWS, "")


def test_two_rules_diverge_on_the_same_rows():
    """同一份隔离世界清单：展示校验拒 build（model=None），执行校验放行。

    这条分叉是**有意的**：展示答「目录里有没有」，执行答「跑不跑得起来」。
    钉住它，防止将来有人好心把它们「统一」回去。
    """
    assert not sel.current_agent_is_valid(_ISOLATED_ROWS, "build")
    assert sel.agent_is_executable(_ISOLATED_ROWS, "build")


# ── 模型 ──────────────────────────────────────────────────────────────── #

def _provider_payload() -> dict:
    return {
        "connected": ["opencode", "deepseek"],
        "all": [
            {"id": "opencode", "name": "OpenCode Zen", "models": {
                "big-pickle": {"name": "Big Pickle", "variants": {},
                               "cost": {"input": 0, "output": 0}},
                "fledge-alpha-free": {
                    "name": "Fledge Alpha Free",
                    "variants": {"high": {}, "low": {}, "max": {}},
                    "cost": {"input": 0, "output": 0}},
                "claude-opus-5": {
                    "name": "Claude Opus 5",
                    "variants": {"high": {}, "low": {}, "medium": {},
                                 "xhigh": {}, "max": {}},
                    "cost": {"input": 5, "output": 25}},
            }},
            {"id": "nomodel", "name": "No creds", "models": {
                "x": {"name": "X", "cost": {"input": 0, "output": 0}}}},
        ],
    }


def test_models_are_filtered_to_connected_providers():
    """8475 → 85 的关键就在这一行：不按 connected 过滤的全是没凭据的。"""
    got = sel.list_model_options(
        _provider_payload(), provider_filter=sel.connected_providers(_provider_payload()))
    values = [o.value for o in got]
    assert "nomodel/x" not in values
    assert "opencode/big-pickle" in values


def test_free_models_sorted_first():
    got = sel.list_model_options(_provider_payload(),
                                 provider_filter=["opencode"])
    assert got[0].value == "opencode/big-pickle"
    assert got[-1].value == "opencode/claude-opus-5"


def test_free_detection_uses_cost_not_name():
    """``big-pickle`` 名字里没有 free，但 cost 为 0 —— 必须算免费。"""
    got = sel.list_model_options(_provider_payload(), provider_filter=["opencode"])
    free = [o for o in got if o.value in
            ("opencode/big-pickle", "opencode/fledge-alpha-free")]
    assert len(free) == 2


def test_model_hint_counts_variants():
    got = {o.value: o.hint for o in
           sel.list_model_options(_provider_payload(), provider_filter=["opencode"])}
    assert got["opencode/fledge-alpha-free"] == "OpenCode Zen · 3 档"
    assert got["opencode/big-pickle"] == "OpenCode Zen", "无档就别写档数"


def test_connected_providers_reads_the_field():
    assert sel.connected_providers(_provider_payload()) == ["opencode", "deepseek"]


def test_connected_providers_survives_junk():
    assert sel.connected_providers({"nope": 1}) == []
    assert sel.connected_providers({}) == []


# ── 推理档 ────────────────────────────────────────────────────────────── #

def test_variant_first_option_is_always_unspecified():
    """「不指定」= 不施加 variant，用模型基线（实测没有模型带 default 档）。"""
    got = sel.variant_options(_provider_payload(), "opencode/fledge-alpha-free")
    assert got[0].value == ""
    assert "不指定" in got[0].label


def test_variants_sorted_by_strength_not_dict_order():
    """dict 键序不保证强弱顺序；用户预期「越高越靠后」。"""
    payload = {"all": [{"id": "opencode", "name": "OC", "models": {"m": {
        "name": "M", "variants": {"max": {}, "low": {}, "high": {}, "medium": {}},
        "cost": {}}}}]}
    got = [o.value for o in sel.variant_options(payload, "opencode/m")]
    assert got == ["", "low", "medium", "high", "max"]


def test_unknown_variant_is_listed_not_dropped():
    """不认识的名字不能静默丢掉 —— 「我明明有却选不到」无从解释。"""
    payload = {"all": [{"id": "opencode", "name": "OC", "models": {"m": {
        "name": "M", "variants": {"high": {}, "ultra-turbo": {}},
        "cost": {}}}}]}
    values = [o.value for o in sel.variant_options(payload, "opencode/m")]
    assert "ultra-turbo" in values
    assert values.index("high") < values.index("ultra-turbo")


def test_model_without_variants_still_offers_unspecified():
    """只有一项也要给 —— 那正是「这个模型没有档可选」的信息。"""
    got = sel.variant_options(_provider_payload(), "opencode/big-pickle")
    assert [o.value for o in got] == [""]


def test_variant_of_unknown_model_is_just_unspecified():
    got = sel.variant_options(_provider_payload(), "nope/zzz")
    assert [o.value for o in got] == [""]


def test_current_variant_validity():
    payload = _provider_payload()
    assert sel.current_variant_is_valid(payload, "opencode/fledge-alpha-free", "high")
    # Big Pickle 没有档 —— 之前选的高不该被当成有效
    assert not sel.current_variant_is_valid(payload, "opencode/big-pickle", "high")
    assert sel.current_variant_is_valid(payload, "opencode/big-pickle", "")


# ── 状态 ──────────────────────────────────────────────────────────────── #

def test_selection_clear_from_wipes_downstream():
    """改了模型就清档 —— 否则给 Big Pickle 发一个它没有的 ``high``。

    而 OpenCode **不校验**（实测错误档也返回 204），症状是
    「推理档明明选了 High，实际没生效」，极难排查。
    """
    s = sel.Selection(project="D:/p", agent="Sisyphus",
                      model="opencode/fledge-alpha-free", variant="high")
    s.clear_from("model")
    s.model = "opencode/big-pickle"
    assert s.variant == "", "档位必须被清掉"
    assert s.agent == "Sisyphus", "上游不该被动"


def test_selection_clear_from_keeps_upstream():
    """被改的那一段**自己**由调用方赋值，所以这里只清它**下游**。"""
    s = sel.Selection(project="D:/p", agent="Sisyphus",
                      model="opencode/m", variant="high")
    s.clear_from("agent")
    assert s.model == "", "下游要清"
    assert s.variant == "", "更下游也要清"
    assert s.project == "D:/p", "上游不动"


def test_describe_shows_all_four_even_when_empty():
    """少一行用户会以为程序没听见 —— 所以显示成「（默认）」而不是消失。"""
    lines = sel.Selection().describe()
    assert len(lines) == 4
    assert "（默认）" in lines[0]


def test_selection_roundtrip(tmp_path):
    s = sel.Selection(project="D:/p", agent="Sisyphus",
                      model="opencode/m", variant="high")
    sel.save_selection(s, tmp_path)
    got = sel.load_selection(tmp_path)
    assert got == s


def test_missing_selection_file_is_not_an_error(tmp_path):
    """用户还没选过 —— 那是正常状态，不该让助手起不来。"""
    assert sel.load_selection(tmp_path).is_empty()


def test_corrupt_selection_file_is_not_an_error(tmp_path):
    (tmp_path / "oc_selection.json").write_text("{ not json", encoding="utf-8")
    assert sel.load_selection(tmp_path).is_empty()


def test_selection_is_not_a_secret(tmp_path):
    """选择存 ``config.json`` 同级而非 ``llm.env`` —— 后者是秘密的存放处。

    agent 名 / 模型 id / 档位名**不是秘密**（``/config/providers`` 那种
    明文 key 才是）。混进去会让「哪些文件碰不得」这条规矩失效。
    """
    sel.save_selection(sel.Selection(model="opencode/fledge-alpha-free"), tmp_path)
    raw = json.loads((tmp_path / "oc_selection.json").read_text(encoding="utf-8"))
    assert raw["model"] == "opencode/fledge-alpha-free"
    assert not (tmp_path / "llm.env").exists()


# ── 日常可选清单（curation）──────────────────────────────────────────── #
#
# 为什么这一层存在：连了凭据的 provider 下**有 85 个模型**（实测
# 2026-10-07：deepseek 2 + opencode 83）。85 个按钮放不进飞书一张卡，
# 也超出「识别优于回忆」能承载的量。而「日常要用的」通常不到十个。


def test_empty_curation_falls_back_to_all_connected(tmp_path):
    """没挑过 = **给全部**，不是给空。

    「没挑过」是正常状态（刚装好的人还没配）。给空列表会让人以为
    「没模型可用」，而实际上有 85 个 —— 于是去查凭据、查网络，
    真实原因只是「还没挑」。

    ``home`` 必须给 tmp_path：第一版传 ``None``，而那读的是**真的**
    ``~/.freeagent/oc_models.json`` —— 于是本机上勾过哪个模型，
    这条断言就红。测试不该依赖用户当前配置（症状是「昨天还好好的」）。
    """
    got = sel.daily_model_options(_provider_payload(), home=tmp_path)
    assert len(got) == len(
        sel.list_model_options(_provider_payload(), provider_filter=["opencode"]))


def test_curation_narrows_the_list(tmp_path):
    sel.save_curated_models(
        ["opencode/fledge-alpha-free", "opencode/claude-opus-5"], tmp_path)
    got = sel.daily_model_options(_provider_payload(), home=tmp_path)
    assert [o.value for o in got] == [
        "opencode/fledge-alpha-free", "opencode/claude-opus-5"]


def test_curation_drops_models_this_version_lacks(tmp_path):
    """清单里勾了、但这一版 OpenCode 不提供的，跳过而不报错。

    那是「你卸载了它 / 它改名了」。症状是「我明明勾了它却选不到」——
    说清比报错有用。
    """
    sel.save_curated_models(["opencode/已经没了", "opencode/big-pickle"], tmp_path)
    got = sel.daily_model_options(_provider_payload(), home=tmp_path)
    assert [o.value for o in got] == ["opencode/big-pickle"]


def test_curated_add_and_remove(tmp_path):
    assert sel.curate("add", "opencode/big-pickle", tmp_path) == [
        "opencode/big-pickle"]
    assert sel.curate("add", "opencode/big-pickle", tmp_path) == [
        "opencode/big-pickle"], "重复添加应幂等"
    sel.curate("add", "opencode/claude-opus-5", tmp_path)
    assert sel.curate("remove", "opencode/big-pickle", tmp_path) == [
        "opencode/claude-opus-5"]


def test_curate_rejects_unknown_action_and_empty_model(tmp_path):
    """**抛 ValueError** 而不是默默加个空串进清单。

    清单是纯 JSON 文件，一个空串会让界面上出现一个点不中的空白项。
    """
    for bad_action in ("toggle", "", "ADD"):
        with pytest.raises(ValueError):
            sel.curate(bad_action, "opencode/big-pickle", tmp_path)
    with pytest.raises(ValueError):
        sel.curate("add", "  ", tmp_path)


def test_remove_drops_all_duplicates(tmp_path):
    """清单是纯 JSON，可能已被手改成有重复 —— 只删一个会留下幽灵项。

    症状是「同一个模型在界面出现两次」。
    """
    (tmp_path / "oc_models.json").write_text(
        json.dumps({"models": ["opencode/m", "opencode/m", "opencode/other"]}),
        encoding="utf-8")
    assert sel.curate("remove", "opencode/m", tmp_path) == ["opencode/other"]


def test_curation_dedupes_and_keeps_order(tmp_path):
    """**保序**是有意的：勾选顺序就是偏好顺序（常用的排前面）。"""
    sel.save_curated_models(
        ["opencode/b", "opencode/a", "opencode/b", "", "  "], tmp_path)
    assert sel.load_curated_models(tmp_path) == ["opencode/b", "opencode/a"]


def test_curation_corrupt_file_is_empty_not_crash(tmp_path):
    """坏了就是「没挑过」，不是助手起不来。"""
    (tmp_path / "oc_models.json").write_text("{ 坏", encoding="utf-8")
    assert sel.load_curated_models(tmp_path) == []
    assert sel.daily_models_configured(tmp_path) is False


def test_curation_accepts_bare_list_shape(tmp_path):
    """容许「裸数组」：手写 JSON 时最容易写成那样，而不该报错。"""
    (tmp_path / "oc_models.json").write_text(
        json.dumps(["opencode/m"]), encoding="utf-8")
    assert sel.load_curated_models(tmp_path) == ["opencode/m"]


def test_curation_is_independent_of_selection_file(tmp_path):
    """两个文件独立：``save_config`` 整体重写 ``config.json`` 不影响清单，
    而界面改一次 LLM 设置也不该顺手清掉「日常可选模型」。"""
    sel.save_selection(sel.Selection(model="opencode/x"), tmp_path)
    sel.save_curated_models(["opencode/y"], tmp_path)
    assert sel.load_selection(tmp_path).model == "opencode/x"
    assert sel.load_curated_models(tmp_path) == ["opencode/y"]


def test_daily_models_configured_distinguishes_unset(tmp_path):
    assert sel.daily_models_configured(tmp_path) is False
    sel.save_curated_models(["opencode/m"], tmp_path)
    assert sel.daily_models_configured(tmp_path) is True
    sel.save_curated_models([], tmp_path)
    assert sel.daily_models_configured(tmp_path) is False, "清空=回到未配置"