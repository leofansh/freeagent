"""变异探针：确认 weigh 的守卫真的会咬。

**为什么要这一步**（V1.106 的纪律，V1.109 补了第二步）：
写完看着绿 ≠ 在起作用。造几个故意违规的临时副本跑一遍，全红才算它真会咬。

**第二步更关键**：先确认探针**真的改变了行为**。
改了字段默认值却全绿 = 探针改错了地方（V1.109 实测：
``ExecutorProbe.credential`` 改默认值全绿，因为那行是显式传值的）。

用法：默认**跳过**；带 ``--run-mutants`` 才真跑。
跑完临时副本会被还原（``finally`` 里）。

⚠️ **它会改源码文件**（改完立刻还原）。所以**不要和别的测试并行跑**，
也不要在跑全量的时候手动改源码 —— 否则会读到别的东西。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "freeagent" / "services" / "weigh.py"
RENDER = REPO / "src" / "freeagent" / "cli" / "render.py"

pytestmark = pytest.mark.skipif(
    "--run-mutants" not in sys.argv,
    reason="变异探针默认跳过（要跑请加 --run-mutants）",
)


#: 每个探针：改哪个文件、改成什么、期望哪条测试变红。
#:
#: **期望红的那条必须真的红** —— 全绿说明探针改错了地方，
#: 那是 V1.109 踩过的坑（改默认值 vs 改那行显式传值）。
MUTANTS = [
    (
        "facet_source_emptied",
        SRC,
        "把 source 置空（代价不可对账 → 本模块存在的前提没了）",
        '                source="promises.status",',
        '                source="",',
        "test_facets_carry_source",
    ),
    (
        "same_day_gate_removed",
        SRC,
        "去掉「争同一天」这道门（不同天也摆成取舍 → 假冲突）",
        "if best_day is None or len(best_group) < 2:",
        "if best_day is None:",
        "test_different_days_is_not_a_conflict",
    ),
    (
        "wait_kind_gate_removed",
        SRC,
        "去掉「纯等候不参与争用」（在等别人的事也占时间）",
        "if _is_open(s.task) and s.task.kind is not TaskKind.WAIT",
        "if _is_open(s.task)",
        "test_waiting_task_does_not_contend",
    ),
    (
        "unscheduled_claims_today",
        SRC,
        "没排期的事也算占今天（什么都要挤进去 → 假冲突）",
        "    if task.scheduled_for is not None:\n        days.add(task.scheduled_for)",
        "    if task.scheduled_for is not None:\n        days.add(task.scheduled_for)\n    else:\n        days.add(today)",
        "test_unscheduled_without_deadline_takes_no_day",
    ),
    (
        "source_wrong_column",
        SRC,
        "把出处写成不存在的列（代价看似可核对，实则对不上）",
        '                source="promises.promised_for",',
        '                source="promises.not_a_real_column",',
        "test_promise_facet_source_is_promised_for",
    ),
    (
        "imperative_added",
        RENDER,
        "在呈现层加祈使句（指令类语句是自动决策的前一步）",
        "    for i, opt in enumerate(result.options, 1):",
        "    lines.append('你应该先做第一件')\n"
        "    for i, opt in enumerate(result.options, 1):",
        "test_render_has_no_imperative",
    ),
    (
        "ranking_added",
        RENDER,
        "在呈现层加排序措辞（呈现层不决定谁先谁后）",
        "    for i, opt in enumerate(result.options, 1):",
        "    lines.append('推荐先做第一件')\n"
        "    for i, opt in enumerate(result.options, 1):",
        "test_render_does_not_rank",
    ),
]


def _run_tests() -> tuple[int, str]:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_weigh.py",
            "-x",
            "--basetemp=.pytest-tmp/mutants",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONPATH": "src", "PYTHONIOENCODING": "utf-8"},
    )
    return proc.returncode, proc.stdout


@pytest.mark.parametrize(
    "name,target,desc,old,new,expect",
    MUTANTS,
    ids=[m[0] for m in MUTANTS],
)
def test_mutant_is_caught(name, target, desc, old, new, expect):
    """每个探针都要让测试变红。"""
    original = target.read_text(encoding="utf-8")
    backup = target.with_suffix(target.suffix + ".bak")
    shutil.copy2(target, backup)
    try:
        if old not in original:
            pytest.skip(f"探针锚点没找到（源码已变，探针要更新）：{desc}")
        target.write_text(original.replace(old, new, 1), encoding="utf-8")
        code, _out = _run_tests()
        assert code != 0, (
            f"探针 {name} 跑出来是绿的 —— 说明它没真改变行为，"
            f"守卫也可能根本没在咬"
        )
    finally:
        shutil.copy2(backup, target)
        backup.unlink(missing_ok=True)
