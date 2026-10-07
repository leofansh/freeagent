"""飞书选出的 agent / model / variant 真能落到委派上。

## 为什么这组测试是**集成**测试而不是单元测试

因为最容易坏的地方**不在任何一个单元里**，而在**接缝**上：
选择存进 ``oc_selection.json``，而 ``prompt_async`` 要的是
``model="provider/id"`` + ``variant="high"``。中间隔着
``Config.delegate_policy()`` 与 :class:`DelegationPolicy`。

第一版就是接缝断了：``prompt_async`` 的参数加好了、选择也存好了，
而 policy **不读它** —— 于是功能看起来全在，真跑却用不上。
症状是「我明明选了 fledge High，它跑的是默认模型」。
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from freeagent.config import Config, load_config, save_config
from freeagent.delegate import DelegationPolicy, _argv
from freeagent.services import oc_selection as sel


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FREEAGENT_HOME", str(tmp_path))
    return tmp_path


def _write_config(home, **delegate):
    (home / "config.json").write_text(
        json.dumps({"delegate": {"projects": ["D:/p"], **delegate}},
                   ensure_ascii=False),
        encoding="utf-8",
    )


# ── 选择 → policy ─────────────────────────────────────────────────────── #

def test_selection_feeds_policy_agent_model_variant(home):
    """选了什么，policy 里就是什么 —— 这是整个功能的接线点。"""
    _write_config(home)
    sel.save_selection(sel.Selection(
        agent="Sisyphus - ultraworker",
        model="opencode/fledge-alpha-free",
        variant="high",
    ), home)
    policy = load_config(home).delegate_policy()
    assert policy.agent == "Sisyphus - ultraworker"
    assert policy.model == "opencode/fledge-alpha-free"
    assert policy.variant == "high"


def test_selection_project_is_not_a_whitelist_entry(home):
    """选择的项目**不进** ``projects``。

    因为它来自飞书载荷（可伪造），而白名单是配置里写死的。把两者混起来
    等于开第二个准入口子 —— 而闸门只需被绕过一次。
    """
    _write_config(home, projects=["D:/allowed"])
    sel.save_selection(sel.Selection(project="C:/Windows/System32"), home)
    policy = load_config(home).delegate_policy()
    # 断言**归一化后**的形状：``load_config`` 会把项目路径转成反斜杠
    # （Windows 惯例），所以这里比 ``D:\\allowed`` 而不是原样写的正斜杠。
    assert policy.projects == ("D:\\allowed",)
    assert "System32" not in " ".join(policy.projects)


def test_config_model_wins_over_selection_when_set(home):
    """``config.json`` 里写了 model 就用它 —— 显式配置不该被界面选择盖掉。

    反过来（配置为空）才用选择。这样「命令行/配置是权威，界面是便捷」
    这条关系在两个方向上都不含糊。
    """
    _write_config(home, model="opencode/big-pickle")
    sel.save_selection(sel.Selection(model="opencode/fledge-alpha-free"), home)
    assert load_config(home).delegate_policy().model == "opencode/big-pickle"


def test_empty_selection_leaves_policy_defaults(home):
    """没选过 = 用默认，不报错。"""
    _write_config(home)
    policy = load_config(home).delegate_policy()
    assert (policy.agent, policy.variant) == ("", "")
    assert policy.projects == ("D:\\p",), "路径被归一化成本机分隔符"


def test_policy_still_defaults_when_no_selection_file(home):
    _write_config(home)
    assert not (home / "oc_selection.json").exists()
    assert load_config(home).delegate_policy().agent == ""


# ── policy → 命令行 / HTTP 载荷 ────────────────────────────────────────── #

def test_argv_carries_all_three():
    """``opencode run`` 实测支持 ``--model`` / ``--agent`` / ``--variant``。"""
    policy = DelegationPolicy(projects=("D:/p",),
                              model="opencode/fledge-alpha-free",
                              agent="Sisyphus - ultraworker", variant="high")
    argv = _argv(policy, "brief", "title")
    assert "--model" in argv and "opencode/fledge-alpha-free" in argv
    assert "--agent" in argv and "Sisyphus - ultraworker" in argv
    assert "--variant" in argv and "high" in argv


def test_argv_omits_absent_options():
    """没选就不传旗标 —— 空串会被当成「用名叫空串的东西」。"""
    argv = _argv(DelegationPolicy(projects=("D:/p",)), "b", "t")
    for flag in ("--model", "--agent", "--variant"):
        assert flag not in argv


def test_argv_never_passes_auto():
    """``--auto`` 是 opencode 自己标 dangerous 的开关，绝不能由我们加。"""
    policy = DelegationPolicy(projects=("D:/p",), model="opencode/m",
                              agent="A", variant="high")
    assert "--auto" not in _argv(policy, "b", "t")


# ── policy → prompt_async 载荷 ─────────────────────────────────────────── #

class _Recorder:
    """记下 ``prompt_async`` 收到的参数。不碰网络。"""

    def __init__(self) -> None:
        self.seen: list[dict] = []

    def create_session(self) -> str:
        return "ses_test"

    def prompt_async(self, sid, brief, *, model="", agent="", variant="",
                     directory=None):
        self.seen.append({"sid": sid, "brief": brief, "model": model,
                          "agent": agent, "variant": variant})

    def events(self, **kw):
        return iter([("session.idle", {})])

    def abort(self, sid) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_policy_fields_reach_prompt_async():
    """端到端：policy 的三个字段 → ``prompt_async`` 的三个参数。

    刻意读源码而不是跑一遍执行链路：真跑要起 opencode、连事件流，
    而这条断言要防的是**接缝漏传**。漏传的症状最恶劣 ——
    「选了 High 却没生效」，而且**没有任何报错**（opencode 对错档也返回 204），
    所以只能靠这条静态断言守住。

    跨行匹配：那个调用为了塞进 80 列折成了两行，所以按「从调用起点到下一个
    独立的 ``oc.`` 调用」取一段，而不是单行。
    """
    from pathlib import Path

    src = Path("src/freeagent/delegate.py").read_text(encoding="utf-8")
    start = src.find("oc.prompt_async(session_id")
    assert start > 0, "找不到 prompt_async 调用 —— 是不是被挪走了？"
    tail = src[start:]
    end = tail.find("oc.", len("oc.prompt_async(session_id"))
    call = tail[:end if end > 0 else len(tail)]
    for name in ("model=policy.model", "agent=policy.agent",
                 "variant=policy.variant"):
        assert name in call, f"prompt_async 调用缺 {name}"


def test_selection_roundtrip_through_disk(home):
    """写进去再读出来，三个字段都在 —— JSON 层不能丢字段。"""
    s = sel.Selection(project="D:/p/freeagent",
                      agent="Prometheus - Plan Builder",
                      model="opencode/space-bunny-free", variant="xhigh")
    sel.save_selection(s, home)
    got = sel.load_selection(home)
    assert got.agent == "Prometheus - Plan Builder"
    assert got.variant == "xhigh"


def test_save_config_does_not_wipe_selection(home):
    """``save_config`` 会**整体重写** ``config.json``，但它碰不到
    ``oc_selection.json``（独立文件）—— 所以选择不会被界面设置抹掉。

    第一版如果把选择存进 ``config.json``，那么在界面上改一次 LLM 设置
    就会静默清掉用户选的模型与推理档，而那两件事毫无关系。
    """
    _write_config(home)
    sel.save_selection(sel.Selection(model="opencode/fledge-alpha-free",
                                     variant="high"), home)
    cfg = load_config(home)
    save_config(cfg, home)
    got = sel.load_selection(home)
    assert got.model == "opencode/fledge-alpha-free"
    assert got.variant == "high"