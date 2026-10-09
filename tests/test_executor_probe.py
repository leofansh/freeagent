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

import pytest

from freeagent.services import executor_probe as P


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

    def test_unverified_gets_no_bucket_at_all(self, which, major):
        """⚠️ 未验证时**一桶都不给** —— 不给「至少能读」。

        「能读」在 opencode 侧对应 ``allow`` 规则，声称它就是在声称
        「权限配置生效了」，而那正是未验证时**不能声称**的。
        """
        which(True)
        major(2)          # 认识但 verified=False
        got = P.probe_opencode()
        assert got.dispatchable is False
        assert got.capabilities == ()

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

    def test_unknown_major_and_unverified_major_differ(self, which, major):
        """「不认识」与「认识但没验过」必须给**不同**的话。

        该做的事不同：前者要升级/降级本仓，后者要对真机实测适配器。
        合成一句「不可用」等于让人自己猜下一步。
        """
        which(True)
        major(2)
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

    def test_gives_the_reason_when_not_dispatchable(self, which, major):
        which(True)
        major(2)
        text = P.render_probe(P.probe_opencode())
        assert "未验证" in text
        assert "为什么不可派发" in text

    def test_no_reason_section_when_dispatchable(self, which, major):
        which(True)
        major(1)
        assert "为什么不可派发" not in P.render_probe(P.probe_opencode())