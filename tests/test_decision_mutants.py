"""变异探针：确认决策日志（7.6）的守卫真的会咬。

**为什么要这一步**（V1.106 的纪律，V1.109 补了第二步）：
写完看着绿 ≠ 在起作用。造几个故意违规的临时副本跑一遍，全红才算它真会咬。

**第二步更关键**：先确认探针**真的改变了行为**。
改了字段默认值却全绿 = 探针改错了地方（V1.109 实测：
``ExecutorProbe.credential`` 改默认值全绿，因为那行是显式传值的）。

本轮探针盯的是**这个模块最核心的几条**：
① 快照冻结（落库后不随事实漂移）；
② 只摆事实不评价；
③ 不读人的审批决策；
④ **同一板不记两遍**（重复拍板不是两次拍板）；
⑤ **合并有边界**（改了主意、跨天都留两条；「甲→乙→甲」不并掉最后那次）。

**探针咬的必须是「期望的那条」测试**：只看「有东西红了」太弱 ——
改坏一处可能连带让别的测试红。这里查的是**期望红的那条测试的名字**
（``expect`` 字段终于从注释变成了断言）。

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
DECISION = REPO / "src" / "freeagent" / "services" / "decision.py"
RENDER = REPO / "src" / "freeagent" / "cli" / "render.py"
DB = REPO / "src" / "freeagent" / "storage" / "db.py"

pytestmark = pytest.mark.skipif(
    "--run-mutants" not in sys.argv,
    reason="变异探针默认跳过（要跑请加 --run-mutants）",
)


#: 每个探针：改哪个文件、改成什么、期望哪条测试变红。
#:
#: **期望红的那条必须真的红** —— 全绿说明探针改错了地方，
#: 那是 V1.109 踩过的坑。
MUTANTS = [
    (
        "snapshot_recomputed_from_live_facts",
        DECISION,
        "对峙时用今天的库重算代价（快照漂移 → 对着今天比今天，nothing to see）",
        "        frozen = parse_frozen_options(decision.options_json)",
        "        frozen = parse_frozen_options(decision.options_json)\n"
        "        frozen = tuple(\n"
        "            FrozenOption(\n"
        "                task_id=o.task_id,\n"
        "                title=o.title,\n"
        "                facets=tuple(\n"
        "                    (f[0] + '（现算）', f[1]) for f in o.facets\n"
        "                ),\n"
        "            )\n"
        "            for o in frozen\n"
        "        )",
        "test_confront_uses_the_frozen_facets",
    ),
    (
        "frozen_facets_dropped",
        DECISION,
        "冻结的快照里丢掉代价项（存进去的那份就不完整了）",
        "                facets=tuple(facets),",
        "                facets=(),",
        "test_record_freezes_the_facets",
    ),
    (
        "confront_verdict_added",
        RENDER,
        "对峙里加上评价（替人下结论 → 越界）",
        '    lines.append("只摆事实 —— 当初怎么想的、现在各自什么状态。你自己看。")',
        '    lines.append("你选对了，这一板很准。")',
        "test_confront_output_never_judges",
    ),
    (
        "picked_receipt_praises",
        RENDER,
        "回执里加上评价（回执不是评价位）",
        '    lines.append("记的是当时那一板 —— 7 天后会拿出来，跟这几件事现在的状态并排摆给你看。")',
        '    lines.append("好选择 —— 这板拍得准。")',
        "test_picked_receipt_shows_which_one",
    ),
    (
        "empty_state_becomes_advice",
        RENDER,
        "空态降格成建议（把没有取舍说成有建议，比不给更坏）",
        '            "没有到期的取舍记录 —— "\n'
        '            "拍过板、且到对质日的那几条才会出现在这里。"',
        '            "没有到期的取舍记录 —— 那就先做今天最要紧的那件。"',
        "test_no_due_says_so_plainly",
    ),
    (
        "out_of_range_silently_clamped",
        DECISION,
        "越界静默夹到范围内（选错了变成记了别的那个）",
        "        if not 0 <= chosen_index < len(result.options):\n"
        "            raise ValueError(\n"
        '                f"选了第 {chosen_index + 1} 个，但当时只摆了 {len(result.options)} 个"\n'
        "            )",
        "        chosen_index = max(0, min(chosen_index, len(result.options) - 1))",
        "test_out_of_range_raises",
    ),
    (
        "vanished_task_silently_skipped",
        DECISION,
        "事务被删了静默跳过（少一条就看不出当时摆了几件）",
        "            if task is None:\n"
        "                vanished.append(option.title)\n"
        "                continue",
        "            if task is None:\n"
        "                continue",
        "test_confront_reports_vanished_task",
    ),
    (
        "broken_snapshot_silently_ignored",
        DECISION,
        "快照读不出来静默跳过（让人以为没拍过板）",
        "    try:\n"
        '        raw = json.loads(options_json)\n'
        "    except (TypeError, ValueError) as exc:\n"
        '        raise ValueError(f"决策快照读不出来：{options_json[:60]!r}") from exc',
        "    try:\n"
        "        raw = json.loads(options_json)\n"
        "    except (TypeError, ValueError):\n"
        "        return ()",
        "test_broken_snapshot_raises",
    ),
    (
        "confront_always_due",
        DECISION,
        "所有快照都算到期（对峙不再是一次性的事）",
        "    def due(self, today: date) -> list[DecisionLog]:\n"
        '        """今天该拉出来对峙的快照。"""\n'
        "        return self._decisions.due_on(today)",
        "    def due(self, today: date) -> list[DecisionLog]:\n"
        '        """今天该拉出来对峙的快照。"""\n'
        "        return self._decisions.list_all()",
        "test_not_due_before_the_day",
    ),
    (
        "duplicate_board_records_twice",
        DECISION,
        "去掉幂等判定（重复拍板在日志里留两条一模一样的记录）",
        "        previous = self._decisions.latest_on(now.date())\n"
        "        if (\n"
        "            previous is not None\n"
        "            and previous.options_json == options_json\n"
        "            and previous.chosen_index == chosen_index\n"
        "        ):",
        "        previous = self._decisions.latest_on(now.date())\n"
        "        if (\n"
        "            previous is not None\n"
        "            and previous.options_json == options_json\n"
        "            and previous.chosen_index == chosen_index\n"
        "            and False\n"
        "        ):",
        "test_same_board_same_choice_records_once",
    ),
    (
        "replay_reported_as_new",
        DECISION,
        "命中已有却说成「新记了一条」（库里没多出行，回执把事实说错）",
        "            return RecordResult(decision=previous, is_new=False)",
        "            return RecordResult(decision=previous, is_new=True)",
        "test_receipt_tells_new_from_repeat",
    ),
    (
        "idempotency_searches_all_history",
        DECISION,
        "幂等判据拿「历史里任何一条」比（甲→乙→甲 的最后那次甲被并掉）",
        "        previous = self._decisions.latest_on(now.date())",
        "        previous = next(\n"
        "            (\n"
        "                d\n"
        "                for d in self._decisions.list_all()\n"
        "                if d.options_json == options_json\n"
        "                and d.chosen_index == chosen_index\n"
        "            ),\n"
        "            None,\n"
        "        )",
        "test_wavering_keeps_the_final_choice",
    ),
]


def _run_tests() -> tuple[int, str]:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_decision.py",
            "--tb=no",
            "-rf",
            "--basetemp=.pytest-tmp/mutants",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONPATH": "src", "PYTHONIOENCODING": "utf-8"},
    )
    return proc.returncode, proc.stdout


def _is_expected_failure(out: str, expect: str) -> bool:
    """输出里「期望红的那条测试」是不是真的红了。

    **只看「有东西红了」太弱**（V1.109 的纪律：先确认探针真的改变了行为）：
    改坏一处可能连带让别的测试红，而探针的意义在于「它咬的正是我们
    以为的那条」。所以这里查的是**那一条的名字**。

    **认「FAILED」也认「ERROR」**：探针改坏了 fixture 时，
    pytest 报的是 ERROR 而不是 FAILED —— 那同样说明这条没守住。
    """
    for line in out.splitlines():
        if expect in line and (
            line.startswith("FAILED") or line.startswith("ERROR")
        ):
            return True
    return False


@pytest.mark.parametrize(
    "name,target,desc,old,new,expect",
    MUTANTS,
    ids=[m[0] for m in MUTANTS],
)
def test_mutant_is_caught(name, target, desc, old, new, expect):
    """每个探针都要让**期望的那条**测试变红。"""
    original = target.read_text(encoding="utf-8")
    backup = target.with_suffix(target.suffix + ".bak")
    shutil.copy2(target, backup)
    try:
        if old not in original:
            pytest.skip(f"探针锚点没找到（源码已变，探针要更新）：{desc}")
        target.write_text(original.replace(old, new, 1), encoding="utf-8")
        code, out = _run_tests()
        assert code != 0, (
            f"探针 {name} 跑出来是绿的 —— 说明它没真改变行为，"
            f"守卫也可能根本没在咬"
        )
        assert _is_expected_failure(out, expect), (
            f"探针 {name} 确实让测试变红了，但红的不是期望的那条"
            f"（{expect}）—— 说明这条探针打歪了，守卫守的其实是别的东西"
        )
    finally:
        shutil.copy2(backup, target)
        backup.unlink(missing_ok=True)
