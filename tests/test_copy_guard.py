"""文案守卫的棘轮语义（12.12 / V1.94）。

**这个文件守的不是「文案对不对」，是「守卫本身会不会被绕过」。**

守卫的价值全在四条性质上，而它们每一条都能被一个善意的改动悄悄废掉：

* 挪代码不该要求刷基线（否则没人愿意重构，最后基线变成手工维护的清单）；
* 删掉违规永远不用刷基线（只准变少）；
* 在同一个文件里再复制一份同样的违规要红（否则「复制粘贴」就是后门）；
* 基线不是批准书 —— 它的唯一用途是记欠账。

仓库当前状态单独一条断言（:func:`test_repo_has_no_new_violation`）：
它红了先看是**新写了违规**，还是**基线该收账了**。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "copy_guard.py"
_spec = importlib.util.spec_from_file_location("copy_guard", _MODULE_PATH)
copy_guard = importlib.util.module_from_spec(_spec)
# 必须先登记进 sys.modules 再执行：``@dataclass`` 会去
# ``sys.modules[cls.__module__]`` 里找自己，找不到就 AttributeError。
sys.modules["copy_guard"] = copy_guard
_spec.loader.exec_module(copy_guard)

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE = REPO_ROOT / "tools" / "copy-baseline.json"

BAD = 'class X:\n    def f(self):\n        self._say("这条有**星号**")\n'
GOOD = 'class X:\n    def f(self):\n        self._say("这条干净")\n'


def _write(root: Path, name: str, body: str, *, pad: int = 0) -> Path:
    pkg = root / "src" / "freeagent"
    pkg.mkdir(parents=True, exist_ok=True)
    path = pkg / name
    path.write_text("\n" * pad + body, encoding="utf-8")
    return path


class TestRepoIsClean:
    def test_repo_has_no_new_violation(self):
        """全仓扫描：不许有基线之外的新增。

        **红了要看清楚是哪一种**：新写的违规（改文案），还是基线里那条
        已经修掉了（那就 ``--update-baseline`` 收账，只会变少）。
        """
        violations = copy_guard.scan(REPO_ROOT)
        baseline = copy_guard.load_baseline(BASELINE)
        fresh, _stale, _removed = copy_guard.compare(violations, baseline)
        assert not fresh, "\n".join(
            f"{v.file}:{v.line} {v.rule} {v.sample}" for v in fresh
        )


class TestRatchet:
    def test_moving_lines_is_harmless(self, tmp_path):
        """**挪行不该要求刷基线。** 见证串不含行号，正是为了这一条。"""
        _write(tmp_path, "a.py", BAD)
        first = copy_guard.scan(tmp_path)
        baseline = copy_guard.counts(first)

        # 同一份代码，前面垫 5 行空行 —— 行号变了，见证串没变。
        _write(tmp_path, "a.py", BAD, pad=5)
        again = copy_guard.scan(tmp_path)
        fresh, _stale, _removed = copy_guard.compare(again, baseline)
        assert not fresh, "挪了行号就要求刷基线的守卫，没人愿意用"
        assert again[0].line != first[0].line, "这条测试得真的挪了行"

    def test_removing_never_requires_baseline_update(self, tmp_path):
        """删违规永远不用刷基线 —— 只准变少。"""
        _write(tmp_path, "a.py", BAD)
        baseline = copy_guard.counts(copy_guard.scan(tmp_path))

        _write(tmp_path, "a.py", GOOD)
        fresh, stale, removed = copy_guard.compare(
            copy_guard.scan(tmp_path), baseline
        )
        assert not fresh
        assert removed == 1
        assert stale, "修干净了的条目该被提示收账"

    def test_copying_within_same_file_fails(self, tmp_path):
        """**在同一文件里再复制一份同样的违规 = 新增。**

        按「见证串在不在基线里」记账的守卫会放过这一手：见证串一样，
        所以「已经批准过」。按次数记账才堵得住 —— 复制粘贴是真实世界里
        最常见的扩散方式。
        """
        _write(tmp_path, "a.py", BAD)
        baseline = copy_guard.counts(copy_guard.scan(tmp_path))

        _write(
            tmp_path, "a.py",
            'class X:\n    def f(self):\n'
            '        self._say("这条有**星号**")\n'
            '        self._say("这条有**星号**")\n',
        )
        fresh, _stale, _removed = copy_guard.compare(
            copy_guard.scan(tmp_path), baseline
        )
        assert len(fresh) == 1

    def test_copying_to_new_file_fails(self, tmp_path):
        """复制到新文件也是新增 —— 换了文件，账就是另一笔。"""
        _write(tmp_path, "a.py", BAD)
        baseline = copy_guard.counts(copy_guard.scan(tmp_path))

        _write(tmp_path, "b.py", BAD)
        fresh, _stale, _removed = copy_guard.compare(
            copy_guard.scan(tmp_path), baseline
        )
        assert len(fresh) == 1
        assert fresh[0].file.endswith("b.py")

    def test_baseline_roundtrip(self, tmp_path):
        """基线是给 diff 看的人工产物：要有样本、要能原样读回来。"""
        _write(tmp_path, "a.py", BAD)
        violations = copy_guard.scan(tmp_path)
        text = copy_guard.dump_baseline(violations)
        path = tmp_path / "base.json"
        path.write_text(text, encoding="utf-8")

        assert "这条有**星号**" in text, "基线要带样本，否则 diff 里看不出欠了什么"
        assert copy_guard.load_baseline(path) == copy_guard.counts(violations)

    def test_newline_in_plain_text_body_is_flagged(self, tmp_path):
        """``plain_text`` 的正文里换行 —— 飞书不会渲染成换行，会挤成一坨。"""
        _write(
            tmp_path, "a.py",
            'card = {"tag": "plain_text", "content": "第一行\\n第二行"}\n',
        )
        rules = {v.rule for v in copy_guard.scan(tmp_path)}
        assert rules == {"plain-text-newline"}
