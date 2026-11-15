"""判断层的规矩：一句话要不要交给引擎（21.2 / V1.114）。

这几条测的都是**失效方向**，不是「识别得准不准」：

* 问自己的记录 → 留在本地（交出去等于让它去翻你的库）；
* 有路径 / 动作词 → 交出去；
* 没有目录 / 引擎不可用 → 不交，并且**说清缺什么**；
* 判断依据必须随结论一起回来 —— 它要能被人一句话推翻。
"""

from __future__ import annotations

from freeagent.services import agent_router as router


def _decide(text: str, *, directory: str = "D:\\PycharmProjects\\freeagent") -> router.EngineDecision:
    return router.decide(text, directory=directory, engine_available=True)


class TestDecide:
    def test_view_question_stays_local(self) -> None:
        d = _decide("今天有什么安排？")
        assert d.needs_engine is False
        assert "记录" in d.reason

    def test_path_is_evidence(self) -> None:
        d = _decide("D:\\p\\src\\app.py 里那个函数换掉")
        assert d.needs_engine is True
        assert "路径" in d.reason

    def test_verb_is_evidence(self) -> None:
        d = _decide("帮我把这个项目里的测试补上")
        assert d.needs_engine is True

    def test_slash_command_is_not_delegated(self) -> None:
        d = _decide("/today")
        assert d.needs_engine is False
        assert "/" in d.reason

    def test_no_directory_is_not_delegated(self) -> None:
        d = _decide("改一下 app.py", directory="")
        assert d.needs_engine is False
        assert "目录" in d.reason

    def test_engine_unavailable_is_not_delegated(self) -> None:
        d = router.decide("改一下 app.py", directory="D:\\p", engine_available=False)
        assert d.needs_engine is False
        assert "引擎" in d.reason

    def test_empty_text(self) -> None:
        d = _decide("   ")
        assert d.needs_engine is False
        assert d.brief == ""

    def test_brief_folds_newlines_without_losing_words(self) -> None:
        d = _decide("改 app.py\n还有把重命名做完")
        assert "\n" not in d.brief
        assert "重命名" in d.brief

    def test_weak_evidence_says_it_is_weak(self) -> None:
        # 「看看这个项目」没有明确动作词：仍然交出去，但理由必须
        # 说清这是弱证据 —— 强弱混着报，人就没法判断该不该推翻。
        d = _decide("看看这个项目")
        assert d.needs_engine is True
        assert "弱证据" in d.reason

    def test_hits_are_returned_for_override(self) -> None:
        d = _decide("把 D:\\p\\app.py 的入口改掉")
        assert d.hits
        assert any("app.py" in h for h in d.hits)
