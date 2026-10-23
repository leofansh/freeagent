"""显式触发边界清单（11.22）的契约测试。

守三件事：
1. ``domain/enums.py`` 的 ``TriggerBoundary`` + ``CAPABILITY_BOUNDARIES`` 是
   单一真源，且分类自洽（每个能力都归类、建状态能力都在 EXPLICIT_ONLY、
   NO_SEND 只有被点名的两个）。
2. 设计文档 §11.22 必须列出三级边界名与 fail-closed 默认 —— 字典与文档不能漂。
3. fail-closed 不变量：NudgeService 结构上没有发送能力（不持有 channel/send/client）。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from freeagent.app import build_app
from freeagent.domain.enums import (
    CAPABILITY_BOUNDARIES,
    TRIGGER_BOUNDARY_LABELS,
    TriggerBoundary,
)
from freeagent.services.clock import FrozenClock
from freeagent.services.sorting import EnergyWindows

_DESIGN_DOC = (
    Path(__file__).resolve().parent.parent
    / "docs"
    / "个人事务助手设计方案.md"
)


def test_enums_importable():
    assert TriggerBoundary is not None
    assert CAPABILITY_BOUNDARIES
    # 每个枚举值都该有可读标签（防止加边界忘了写中文名）。
    for boundary in TriggerBoundary:
        assert boundary in TRIGGER_BOUNDARY_LABELS


def test_every_capability_is_classified():
    """字典非空，且每个值都是合法的边界枚举成员。"""
    assert CAPABILITY_BOUNDARIES, "能力边界字典不能为空"
    for key, boundary in CAPABILITY_BOUNDARIES.items():
        assert isinstance(boundary, TriggerBoundary), (
            f"能力 {key!r} 没有归入合法边界：{boundary!r}"
        )


def test_explicit_only_covers_state_creating_commands():
    """所有会建 / 改持久状态的能力，必须落在 EXPLICIT_ONLY。

    这是 fail-closed 的硬要求：状态一旦落地就难撤回，所以建状态永远只能由
    用户显式命令触发。漏一个就是一种「静默改动状态」的隐患。
    """
    explicit = {
        key
        for key, b in CAPABILITY_BOUNDARIES.items()
        if b is TriggerBoundary.EXPLICIT_ONLY
    }
    must_be_explicit = {
        "new_task",        # 建事务
        "clarify_pending",  # 把收集器澄清成事务
        "promise",         # 记「我答应别人」
        "memory",          # 记记忆
        "wait",            # 挂等候
        "status_change",   # 状态流转
    }
    missing = must_be_explicit - explicit
    assert not missing, f"这些能力必须在 EXPLICIT_ONLY：{sorted(missing)}"


def test_no_send_tier_is_small_and_named():
    """NO_SEND 只有被点名的两个 —— 主动能力想扩必须显式写进这张表。

    不靠「反正它也没发」的默契：任何新的「主动」入口都必须显式登记并说清
    为什么不越界，而不是悄悄多一个能对外动的口子。
    """
    no_send = {
        key
        for key, b in CAPABILITY_BOUNDARIES.items()
        if b is TriggerBoundary.NO_SEND
    }
    assert no_send == {"nudge", "daily_digest"}, (
        f"NO_SEND 只能是 nudge / daily_digest，现在却是：{sorted(no_send)}"
    )


def test_doc_lists_boundary_tiers_and_fail_closed():
    """设计文档 §11.22 必须点名三级边界 + fail-closed 默认。

    字典在代码、分类表在文档，两套不能漂 —— 谁改了边界名或默认，这里先红。
    """
    text = _DESIGN_DOC.read_text(encoding="utf-8")
    # 锚定到 §11.22 那一节，避免正文别处恰好出现同名词造成假绿。
    section = text[text.index("### 11.22"):]
    assert "EXPLICIT_ONLY" in section and "显式触发" in section
    assert "READ_ONLY" in section and ("只读" in section or "不可变" in section)
    assert "NO_SEND" in section and "无发送能力" in section
    # fail-closed 默认：未列入的能力禁止静默改动状态。
    assert "fail-closed" in section.lower()
    assert "禁止静默改动状态" in section


def test_nudge_has_no_send_capability(tmp_path: Path):
    """NudgeService 结构上没有发送能力 —— fail-closed 在代码层也是真的。

    11.20.2 / 11.22.2 的纪律不能只停在文档：想「顺手发出去」的人在代码里
    也找不到 channel / send / client。这与 test_nudge::test_says_it_will_not_send
    （草稿带「不会发」字样）互为表里。
    """
    app = build_app(
        tmp_path / "agent.db",
        clock=FrozenClock(datetime(2026, 1, 1, 9, 0)),
        energy_windows=EnergyWindows(),
    )
    assert not hasattr(app.nudge, "channel")
    assert not hasattr(app.nudge, "send")
    assert not hasattr(app.nudge, "client")
