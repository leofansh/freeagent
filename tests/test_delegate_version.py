"""版本闸门的回归测试。

为什么必须锁：V1→V2 的 permission 字段名全变，且**旧字段被静默忽略**。
没有这道断言，一次 opencode 升级就会让 11.8.1 的接缝**无声失效** ——
测试全绿、闸门形同虚设。这类「静默」回归是最难靠肉眼发现的。

覆盖面（每条都是一次真实会发生的失败方式）：
  1. 解析：正常 / 带前缀噪声 / 只有主版本 / 解析不了 / 空
  2. 判定：V1 通过 / V2 拒绝 / 探不到拒绝（**不放过**）
  3. 拒绝理由必须**可照着做**（含「静默忽略」「11.8.1」这两个关键词）
  4. 端到端：main() 遇 V2 返回 2 且**不派发**
  5. 控制组：V1 正常放行（防「永远拒绝」这种过度拒守）
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from freeagent import delegate as dl  # noqa: E402


class TestParseMajorVersion:
    @pytest.mark.parametrize("text,expected", [
        ("1.18.31", 1),
        ("2.0.6", 2),
        ("1.18.31\n", 1),
        ("opencode 1.18.31", 1),
        ("  1.18.31  ", 1),
        ("v1.2.3", 1),
        ("1.18", 1),                  # 两段
        ("2.0.6-beta.3", 2),          # 带预发布后缀
    ])
    def test_parses(self, text: str, expected: int) -> None:
        assert dl.parse_major_version(text) == expected

    @pytest.mark.parametrize("text", [
        "", None, "no version here", "未知",
        "2",            # 裸主版本：现实中不出现
    ])
    def test_unparseable_is_none(self, text) -> None:
        """完全认不出的形状**不放行** —— 宁可停下来问，不猜一个版本出来。

        刻意**不接受裸数字**：``opencode --version`` 实测输出是
        ``1.18.31``（V2 是 ``2.0.6``），裸主版本从不出现。
        """
        assert dl.parse_major_version(text) is None

    @pytest.mark.parametrize("text", [
        "2026.09",      # 像日期
        "2026.1.1",     # 像日期的三段
        "99.99.99",
    ])
    def test_odd_shapes_never_pass_the_gate(self, text) -> None:
        """**真正要保证的不是「解析成 None」，而是「绝不当作受支持的版本」**。

        刻意按这个不变量写，而不是按解析细节写 ——
        ``2026.09`` 确实会被解析成主版本 2026（形状对、语义不对），
        但它在 :func:`version_mismatch_reason` 那里照样被拒。
        断言解析结果只会把「安全」和「某个正则长什么样」绑在一起。
        """
        major = dl.parse_major_version(text)
        assert dl.version_mismatch_reason(major) is not None, (
            f"{text!r} 解析成 {major}，不该通过版本闸门"
        )


class TestVersionMismatchReason:
    def test_v1_passes(self) -> None:
        assert dl.version_mismatch_reason(1) is None

    def test_v2_is_rejected(self) -> None:
        reason = dl.version_mismatch_reason(2)
        assert reason is not None, "V2 必须拒绝派发"

    def test_v2_reason_is_actionable(self) -> None:
        """理由要能照着做：说清「静默失效」与「去哪看」。"""
        reason = dl.version_mismatch_reason(2) or ""
        assert "静默忽略" in reason, "必须说明失效是静默的"
        assert "11.8.1" in reason, "必须指向设计文档的版本陷阱表"
        assert "2" in reason, "必须报出实际检测到的版本"

    def test_unknown_version_is_also_rejected(self) -> None:
        """探不到版本 ≠ 放行。"""
        assert dl.version_mismatch_reason(None) is not None

    def test_unknown_version_reason_mentions_expected(self) -> None:
        reason = dl.version_mismatch_reason(None) or ""
        assert str(dl.SUPPORTED_OPENCODE_MAJOR) in reason

    def test_supported_major_is_one(self) -> None:
        """本仓只按 V1 接缝实现（实测 opencode 1.18.31）。"""
        assert dl.SUPPORTED_OPENCODE_MAJOR == 1


class TestMainVersionGate:
    """端到端：main() 必须在派发**之前**挡住，且不产生任何派发。"""

    def test_rejects_v2_without_dispatching(self, tmp_path, capsys, monkeypatch) -> None:
        monkeypatch.setattr(dl, "detect_opencode_version", lambda command="opencode": 2)
        calls: list[str] = []

        def _boom(*a, **k):  # 若真派发了，这里会炸
            calls.append("dispatched")
            raise AssertionError("版本不对时绝不能派发")

        monkeypatch.setattr(dl, "run_once", _boom)
        code = dl.main(["--db", str(tmp_path / "a.db")])
        assert code == 2
        assert not calls, "不该有任何派发"
        out = capsys.readouterr().out
        assert "不派发" in out

    def test_unknown_version_also_stops(self, tmp_path, capsys, monkeypatch) -> None:
        monkeypatch.setattr(dl, "detect_opencode_version", lambda command="opencode": None)
        monkeypatch.setattr(
            dl, "run_once",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该派发")))
        assert dl.main(["--db", str(tmp_path / "a.db")]) == 2
        assert "不派发" in capsys.readouterr().out

    def test_dry_run_also_gated(self, tmp_path, capsys, monkeypatch) -> None:
        """预演也过这关 —— 版本不对时预演结果没有意义。"""
        monkeypatch.setattr(dl, "detect_opencode_version", lambda command="opencode": 2)
        assert dl.main(["--db", str(tmp_path / "a.db"), "--dry-run"]) == 2
        assert "不派发" in capsys.readouterr().out


class TestControlGroup:
    """控制组：版本对的时候**必须放行**。

    没有这条，上面那些测试全过也可能是因为「永远拒绝」——
    那同样是坏的（委派就永远没法用了）。
    """

    def test_v1_is_not_blocked(self, tmp_path, capsys, monkeypatch) -> None:
        monkeypatch.setattr(dl, "detect_opencode_version", lambda command="opencode": 1)
        monkeypatch.setattr(dl, "run_once", lambda **k: dl.DispatchReport(0, 0, 0, 0))
        monkeypatch.setattr(dl, "build_real_gate", None, raising=False)
        code = dl.main(["--db", str(tmp_path / "a.db"), "--dry-run"])
        out = capsys.readouterr().out
        assert "版本不可用" not in out, f"V1 不该被版本闸门挡住，却看到：{out!r}"
        assert code == 0

    def test_detect_reads_stdout(self, monkeypatch) -> None:
        """真实探测一次（打桩 subprocess），确认走的是 --version 且能解析。"""
        import subprocess

        class _P:
            stdout = "1.18.31"
            stderr = ""

        seen: list[list[str]] = []

        def _run(argv, **kw):
            seen.append(argv)
            return _P()

        monkeypatch.setattr(subprocess, "run", _run)
        assert dl.detect_opencode_version("opencode") == 1
        assert seen and seen[0][-1] == "--version", f"应探 --version，实际 {seen}"

    def test_detect_survives_missing_binary(self, monkeypatch) -> None:
        """探不到就返回 None（由判定层决定拒不拒），不抛。"""
        import subprocess

        def _run(argv, **kw):
            raise FileNotFoundError("opencode")

        monkeypatch.setattr(subprocess, "run", _run)
        assert dl.detect_opencode_version("opencode") is None
