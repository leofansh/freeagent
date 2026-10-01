"""把「实际用的模型」记进产物的回归测试。

## 为什么这条要测

委派**不继承**用户 ``~/.config/opencode/`` 的 provider 配置（设计文档 11.8.1），
所以「用哪个模型」由 ``--model`` 显式决定、否则 opencode 自己挑 ——
**两者都会静默变化**。实测：加了子进程环境白名单之后，委派从
``deepseek/deepseek-v4-pro`` 悄悄换成了 ``opencode/big-pickle``，
而产物里**一个字都没留**，我差点没发现。

所以判据不是「字段存在」，而是：**换掉事件里的模型，产物里的那一行就跟着变。**
"""
from __future__ import annotations

import json

import pytest

from freeagent.app import build_app
from freeagent.delegate import _record_result
from freeagent.services.delegate import DispatchOutcome, parse_opencode_output


@pytest.fixture
def app_with_task(tmp_path):
    """一个真 app + 一条真事务。产物落库那层一起验，不 mock。"""
    app = build_app(tmp_path / "a.db")
    role = app.roles.create("工作")
    task = app.tasks.create("活儿", [role.id], project_path=str(tmp_path))
    app.tasks.start(task.id)
    try:
        yield app, task
    finally:
        app.close()


def _event(**kw) -> str:
    return json.dumps(kw, ensure_ascii=False)


def test_model_is_captured_from_events():
    out = "\n".join([
        _event(type="message.updated", sessionID="ses_1",
               info={"model": {"providerID": "deepseek", "modelID": "deepseek-v4-pro"}}),
        _event(type="text", sessionID="ses_1", text="干完了"),
    ])
    assert parse_opencode_output(out).model == "deepseek/deepseek-v4-pro"


def test_model_absent_stays_empty_not_guessed():
    """事件里没有 model 就**留空**，不许拿 modelID 单独拼一个 ——
    缺一半的模型名比没有更坏（看起来像真的）。"""
    out = "\n".join([
        _event(type="message.updated", sessionID="ses_1",
               info={"model": {"modelID": "big-pickle"}}),
        _event(type="text", sessionID="ses_1", text="x"),
    ])
    assert parse_opencode_output(out).model == ""


def test_first_model_wins_not_last():
    """取**第一个**带 model 的事件，不追最后一个。

    中途换模型时要能回答「它是怎么开跑的」—— 那才是「我以为我配的是哪个」。
    """
    out = "\n".join([
        _event(type="message.updated", sessionID="ses_1",
               info={"model": {"providerID": "openai", "modelID": "gpt-x"}}),
        _event(type="message.updated", sessionID="ses_1",
               info={"model": {"providerID": "anthropic", "modelID": "claude-y"}}),
        _event(type="text", sessionID="ses_1", text="x"),
    ])
    assert parse_opencode_output(out).model == "openai/gpt-x"


def test_dirty_lines_do_not_break_model_capture():
    out = "\n".join([
        "not json at all",
        '{"broken": ',
        _event(type="message.updated", sessionID="ses_1",
               info={"model": {"providerID": "opencode", "modelID": "big-pickle"}}),
        _event(type="text", sessionID="ses_1", text="x"),
    ])
    assert parse_opencode_output(out).model == "opencode/big-pickle"


def test_model_lands_in_the_artifact(app_with_task):
    """落到产物正文里，`/artifact` 才看得见。"""
    app, task = app_with_task
    _record_result(app, task, DispatchOutcome(
        ok=True, summary="干完了", session_id="ses_1",
        model="deepseek/deepseek-v4-pro", tool_calls=("write",),
    ))
    body = app.artifacts.current(task.id).content
    assert "deepseek/deepseek-v4-pro" in body


def test_no_model_no_line(app_with_task):
    """没有模型信息时**不加**那一行 —— 宁可少一句，不要一个空的「模型：」。"""
    app, task = app_with_task
    _record_result(app, task, DispatchOutcome(ok=True, summary="干完了"))
    body = app.artifacts.current(task.id).content
    assert "模型：" not in body
