"""执行器能力探针的守卫（设计文档 11.10.2 / 11.10.3）。

## 为什么这些断言写得这么死

探针的唯一价值是**说真话**。它一旦说谎，用户就会照着错的前提操作 ——
而本项目 V1.16 记过的最大一类事故正是「文档/声明比事实强」。

所以这里锁三条纪律：

1. **不静默回落** —— 探不到就说探不到，绝不默认挑一个装着的
2. **「不认识」与「认识但没验过」给不同的理由** —— 该做的事不同
3. **探不到版本是 ``None`` 而不是 ``0``** —— 两者都能比较，但 ``0``
   会被当成「版本 0」进入别的分支，然后说出一句莫名其妙的话
"""

from __future__ import annotations

from typing import Any

import pytest

from freeagent.services import executor_probe as P


def _synthetic_unverified(major: int = 7, executor: str = "opencode"):
    """造一个**认识但没验过**的适配器（协议函数全抛）。

    与 ``tests/test_executors.py`` 的同名助手同构 —— 各自持有，避免跨测试
    文件的脆弱导入。见那里的 docstring：hermes 已转正，不再是「未验证」载体，
    所以这条纪律改用合成适配器承载。
    """
    import dataclasses

    from freeagent.services import executors as ex

    _PIECES = {
        "build_permission_config": "permission 配置形状",
        "build_read_only_config": "只读配置形状",
        "parse_request": "授权请求事件形状",
        "build_reply": "答复载荷形状",
        "parse_question": "提问事件形状",
        "build_question_reply": "提问答复载荷形状",
    }

    def _make_raiser(piece: str):
        def _raise(*_a: object, **_k: object) -> Any:
            raise ex.UnverifiedExecutorError(
                f"{executor} 主版本 {major} 的 {piece}尚未验证。"
                "要接它：对着真实实例实测，填 "
                "`freeagent/services/executors.py` 里对应适配器的形状函数，"
                "再把 `verified` 改成 True。"
            )
        return _raise

    base = ex.adapter_for(1)
    assert base is not None
    return dataclasses.replace(
        base,
        executor=executor,
        major=major,
        verified=False,
        dispatchable=False,
        **{method: _make_raiser(piece) for method, piece in _PIECES.items()},
    )


@pytest.fixture()
def which(monkeypatch):
    """控制「装没装」。"""

    def _set(found: bool) -> None:
        monkeypatch.setattr(P.shutil, "which", lambda _cmd: "C:/x/opencode" if found else None)

    return _set


@pytest.fixture()
def major(monkeypatch):
    """控制「探到哪个主版本」。"""

    def _set(value: int | None) -> None:
        import freeagent.delegate as D

        monkeypatch.setattr(D, "detect_opencode_version", lambda _cmd="opencode": value)

    return _set


@pytest.fixture()
def unverified_major(monkeypatch):
    """造一个**认识但没验过**的 opencode 主版本（合成的未验证适配器）。

    为什么需要合成：V1 与 V2 现在**都** ``verified=True``，hermes 也对着真机
    验过 /ask 那条线转正了 —— 拿任何一个真适配器当「未验证」的反例都不成立，
    那会让「未验证时一桶都不给」这条纪律失去载体。于是测试自己造一个
    ``verified=False`` 的适配器注入注册表（协议函数全抛）。
    """
    from freeagent.services import executors as ex

    fake = _synthetic_unverified(major=7)
    monkeypatch.setattr(ex, "_ADAPTERS", (*ex._ADAPTERS, fake))
    return 7


# --------------------------------------------------------------------------- #
# 三桶（11.10.3）
# --------------------------------------------------------------------------- #

class TestBuckets:
    def test_verified_gets_all_three_in_order(self, which, major):
        """穷尽且**互斥有序**：read ⊂ write，approve 覆盖 write。"""
        which(True)
        major(1)
        got = P.probe_opencode()
        assert got.dispatchable is True
        assert got.capabilities == (P.READ, P.WRITE, P.APPROVE)

    def test_unverified_gets_no_bucket_at_all(self, which, major, unverified_major):
        """⚠️ 未验证时**一桶都不给** —— 不给「至少能读」。

        「能读」在 opencode 侧对应 ``allow`` 规则，声称它就是在声称
        「权限配置生效了」，而那正是未验证时**不能声称**的。

        用夹具造的 V7 当反例：V1/V2 都已 ``verified=True``，
        拿它们当「未验证」的例子会让这条纪律失效。
        """
        which(True)
        major(unverified_major)
        got = P.probe_opencode()
        assert got.dispatchable is False
        assert got.capabilities == (), "未验证时一桶都不给，连 read 也不给"
        assert "未验证" in got.unavailable_reason

    def test_there_are_exactly_three_buckets(self):
        """桶**只**这三个 —— 多一个就等于「flag 多到没人能读」。"""
        assert set(P.__all__) >= {"READ", "WRITE", "APPROVE"}
        assert len({P.READ, P.WRITE, P.APPROVE}) == 3


# --------------------------------------------------------------------------- #
# 三条纪律
# --------------------------------------------------------------------------- #

class TestNoSilentFallback:
    def test_not_installed_says_so_and_does_not_guess(self, which, major):
        """没装就说没装，**不许**默认挑一个装着的。

        静默挑一个的后果：用户以为在用 Claude，其实在用别的。
        """
        which(False)
        got = P.probe_opencode()
        assert got.installed is False
        assert got.dispatchable is False
        assert got.detected_major is None
        assert "找不到" in got.unavailable_reason

    def test_undetectable_version_is_none_not_zero(self, which, major):
        """⚠️ 探不到必须是 ``None``，**不是 ``0``**。

        ``0`` 能进 ``detected == expected`` 那类比较而**不报错**，
        然后给出一句关于「版本 0」的话 —— 那比说「探不到」差得多。
        """
        which(True)
        major(None)
        got = P.probe_opencode()
        assert got.detected_major is None, "探不到时不能是 0"
        assert got.dispatchable is False
        assert "探不到" in got.unavailable_reason

    def test_unknown_major_and_unverified_major_differ(self, which, major, unverified_major):
        """「不认识」与「认识但没验过」必须给**不同**的话。

        该做的事不同：前者要升级/降级本仓，后者要对真机实测适配器。
        合成一句「不可用」等于让人自己猜下一步。
        """
        which(True)
        major(unverified_major)
        unverified = P.probe_opencode().unavailable_reason
        which(True)
        major(99)
        unknown = P.probe_opencode().unavailable_reason

        assert unverified and unknown
        assert unverified != unknown, "两种拒绝给了同一句话"
        assert "未验证" in unverified
        assert "不认识" in unknown

    def test_dispatchable_has_no_reason(self, which, major):
        """能派发时 ``unavailable_reason`` 必须是空串。

        留着理由会让人以为「能派但还有问题」，而那正是它要消除的疑虑。
        """
        which(True)
        major(1)
        assert P.probe_opencode().unavailable_reason == ""


# --------------------------------------------------------------------------- #
# 探针**只看不派**
# --------------------------------------------------------------------------- #

class TestProbeHasNoSideEffects:
    def test_probe_takes_no_arguments_that_could_dispatch(self):
        """⚠️ 探针的签名里**不许**出现「派给谁」这类参数。

        设计文档 11.10.5：绝不自动触发委派。理由与第七章同构 ——
        用户看不到为什么派、也没地方反驳，且派错的代价不对称。
        一旦探针能顺手派出去，就有了「手滑就跑」的形状。
        """
        import inspect

        sig = inspect.signature(P.probe_opencode)
        assert list(sig.parameters) == ["command"], (
            f"探针不该接受别的参数，却有 {list(sig.parameters)}"
        )
        # ``command`` 只是「找哪个可执行文件」，默认值就是唯一的那个
        assert sig.parameters["command"].default == "opencode"

    def test_probe_is_read_only_on_the_database(self, tmp_path):
        """探针**不许碰库** —— 它只回答「装没装」。

        ``installed`` / ``detected_major`` 全部来自外部探测，
        所以一个纯探针不该有任何落库动作。
        """
        import inspect

        src = inspect.getsource(P.probe_opencode)
        for forbidden in ("INSERT", "UPDATE", "DELETE", "CREATE TABLE"):
            assert forbidden not in src.upper(), f"探针里出现了 {forbidden}"


# --------------------------------------------------------------------------- #
# 呈现
# --------------------------------------------------------------------------- #

class TestRender:
    def test_leads_with_whether_it_can_dispatch(self, which, major):
        """先说能不能派 —— 用户问「能不能用」。

        答「装在 D:\\...」是答非所问；而那句信息量最小的事实必须在第一行。
        """
        which(True)
        major(1)
        text = P.render_probe(P.probe_opencode())
        assert text.splitlines()[0].startswith("opencode：可以派发")

    def test_says_none_rather_than_zero(self, which, major):
        which(True)
        major(None)
        text = P.render_probe(P.probe_opencode())
        assert "探不到" in text
        assert "主版本：0" not in text

    def test_gives_the_reason_when_not_dispatchable(self, which, major, unverified_major):
        which(True)
        major(unverified_major)
        text = P.render_probe(P.probe_opencode())
        assert "未验证" in text
        assert "为什么不可派发" in text

    def test_no_reason_section_when_dispatchable(self, which, major):
        which(True)
        major(1)
        assert "为什么不可派发" not in P.render_probe(P.probe_opencode())


# --------------------------------------------------------------------------- #
# hermes 探针（11.10.8）
# --------------------------------------------------------------------------- #

class TestProbeHermes:
    def test_disabled_gets_no_buckets(self, monkeypatch):
        """hermes 已停用（V1.83）→ 一桶都不给、verified 不给。

        ``verified=False`` / ``capabilities == ()`` 是为了**不读成「能读」**：
        它不是没验过，是决定不用了（审批闸失效，做不了受控执行器）。
        """
        import freeagent.delegate as D

        monkeypatch.setattr(D, "detect_hermes_version", lambda _cmd="hermes": 0)
        monkeypatch.setattr(P.shutil, "which", lambda _cmd: "C:/x/hermes")
        got = P.probe_hermes()
        assert got.installed is True
        assert got.verified is False
        assert got.dispatchable is False
        assert got.capabilities == (), "已停用的执行器不给任何能力桶"

    def test_reason_says_disabled_not_unverified(self, monkeypatch):
        """理由必须区分「已停用」与「未验证」—— 前者是决定不用了，后者是没验。"""
        import freeagent.delegate as D

        monkeypatch.setattr(D, "detect_hermes_version", lambda _cmd="hermes": 0)
        monkeypatch.setattr(P.shutil, "which", lambda _cmd: "C:/x/hermes")
        reason = P.probe_hermes().unavailable_reason
        assert "已停用" in reason
        assert "未验证" not in reason, "hermes 不是没验过，是已停用"

    def test_probe_hermes_takes_only_command(self):
        """探针只接受 ``command``，不许有任何能派发的参数。"""
        import inspect

        sig = inspect.signature(P.probe_hermes)
        assert list(sig.parameters) == ["command"]
        assert sig.parameters["command"].default == "hermes"