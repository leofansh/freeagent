"""目录确认闸门的守卫。

核心命题只有一条：**没确认过就不许碰磁盘**，以及**沉默一律当拒绝**。

所以这里大量用「替身」而不是真发卡 —— 因为要验的是闸门的判断，
不是飞书。真发卡那条链路由 e2e 那一步验。
"""
from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from freeagent.services.approval import ApprovalStore
from freeagent.services.localops import DirectoryAccess, DirectoryRefused
from freeagent.storage.db import connect, init_schema


class FakeClock:
    def __init__(self, t: datetime.datetime) -> None:
        self.now = t

    def __call__(self) -> datetime.datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += datetime.timedelta(**kw)


class FakeSender:
    """记录发了什么卡；**不真的联网**。"""

    def __init__(self, message_id: str = "om_fake", boom: Exception | None = None):
        self.calls: list[dict] = []
        self.message_id = message_id
        self.boom = boom

    def send_approval_card(self, *, open_id, subject, detail, credential, ttl_seconds):
        if self.boom is not None:
            raise self.boom
        self.calls.append(dict(open_id=open_id, subject=subject, detail=detail,
                               credential=credential, ttl_seconds=ttl_seconds))
        return self.message_id


@pytest.fixture
def env(tmp_path):
    conn = connect(Path(tmp_path) / "a.db")
    init_schema(conn)
    clock = FakeClock(datetime.datetime(2026, 9, 29, 12, 0))
    store = ApprovalStore(conn, clock=clock)
    return store, clock, conn


def _silent(clock):
    """「没人点」的 sleep：**必须推进时钟**。

    闸门按注入的 clock 判超时，所以不推进就是无限等待 ——
    写测试时这会表现为套件卡死，而不是测试失败。
    """
    def sleep(seconds: float) -> None:
        clock.advance(seconds=seconds)
    return sleep


def _auto_allow(store, clock, sender, *, ttl=600):
    """构造一个「有人点允许」的闸门：在 sleep 里替用户作答。

    刻意**让 sleep 同时推进时钟** —— 闸门按注入的 clock 判超时，
    只作答不推进的话，允许的那条会被判成「时钟没动 → 还没到 deadline」
    然后一直等下去。
    """
    done: list[str] = []

    def sleep(seconds: float) -> None:
        clock.advance(seconds=seconds)
        if done:
            return
        cred = sender.calls[-1]["credential"] if sender.calls else ""
        if cred:
            store.resolve(cred, "allow", decided_by="ou_张三")
            done.append(cred)

    return DirectoryAccess(store=store, sender=sender, approver_id="ou_张三",
                           ttl_seconds=ttl, clock=clock, sleep=sleep)


# =============================================================================
class TestAllow:
    def test_allowed_then_lists(self, env, tmp_path):
        store, clock, _ = env
        target = tmp_path / "docs"
        target.mkdir()
        (target / "设计.md").write_text("x", encoding="utf-8")
        (target / "说明.md").write_text("x", encoding="utf-8")
        sender = FakeSender()
        access = _auto_allow(store, clock, sender)
        # 断言**集合**，不写死中文排序。写死过一次 ["说明.md","设计.md"]，
        # 在这台 Windows 上实测是反的 —— 那是文件系统/区域设置的顺序，
        # 与「有没有真的列出来」无关，钉它只会得到一条脆断言。
        assert set(access.list_dir(target)) == {"说明.md", "设计.md"}
        assert len(sender.calls) == 1, "应该只发一次卡"

    def test_card_records_message_id(self, env, tmp_path):
        """发卡之后才知道 message_id，所以要能补记 ——
        没有它就无法在事后把那张卡标成「已处理」。"""
        store, clock, _ = env
        sender = FakeSender(message_id="om_xyz")
        access = _auto_allow(store, clock, sender)
        access.list_dir(tmp_path)
        cred = sender.calls[0]["credential"]
        assert store.get(cred).open_message_id == "om_xyz"

    def test_card_carries_credential_and_ttl(self, env, tmp_path):
        store, clock, _ = env
        sender = FakeSender()
        _auto_allow(store, clock, sender, ttl=300).list_dir(tmp_path)
        call = sender.calls[0]
        assert call["credential"].startswith("ap-")
        assert call["ttl_seconds"] == 300
        assert call["open_id"] == "ou_张三"

    def test_card_states_scope_and_why(self, env, tmp_path):
        """卡上必须写明「做什么/影响哪/多久」——少了就只能靠信任。"""
        store, clock, _ = env
        sender = FakeSender()
        _auto_allow(store, clock, sender).list_dir(tmp_path)
        detail = sender.calls[0]["detail"]
        assert "只读" in detail and "不写" in detail
        assert "仅这一次" in detail


# =============================================================================
# 沉默即拒绝 —— 这条比什么都重要
# =============================================================================
class TestSilenceIsDenial:
    def test_never_answered_raises(self, env, tmp_path):
        store, clock, _ = env
        sender = FakeSender()
        access = DirectoryAccess(store=store, sender=sender, approver_id="ou_张三",
                                 ttl_seconds=0, clock=clock, sleep=_silent(clock))
        with pytest.raises(DirectoryRefused):
            access.list_dir(tmp_path)

    def test_denied_never_touches_disk(self, env, tmp_path):
        """拒绝时**一个字节都不许读** —— 靠「先问后读」的顺序保证。"""
        store, clock, _ = env
        target = tmp_path / "secret"
        target.mkdir()
        (target / "a.txt").write_text("x", encoding="utf-8")
        sender = FakeSender()
        done: list[str] = []

        def sleep(_s):
            if not done and sender.calls:
                store.resolve(sender.calls[-1]["credential"], "deny",
                              decided_by="ou_张三")
                done.append("x")

        access = DirectoryAccess(store=store, sender=sender, approver_id="ou_张三",
                                 ttl_seconds=600, clock=clock, sleep=sleep)
        with pytest.raises(DirectoryRefused):
            access.list_dir(target)
        assert done, "测试自己没走完，拒绝分支没被验到"

    def test_expiry_is_also_refusal(self, env, tmp_path):
        store, clock, _ = env
        sender = FakeSender()
        access = DirectoryAccess(store=store, sender=sender, approver_id="ou_张三",
                                 ttl_seconds=1, clock=clock, sleep=_silent(clock))
        clock.advance(seconds=5)
        with pytest.raises(DirectoryRefused):
            access.list_dir(tmp_path)

    def test_deny_and_timeout_same_exception(self, env, tmp_path):
        """两者**刻意不区分** —— 调用方不该有机会把「没答」当「答了」。"""
        store, clock, _ = env
        sender = FakeSender()
        access = DirectoryAccess(store=store, sender=sender, approver_id="ou_张三",
                                 ttl_seconds=0, clock=clock, sleep=_silent(clock))
        with pytest.raises(DirectoryRefused):
            access.list_dir(tmp_path)
        assert not issubclass(type("x", (DirectoryRefused,), {}), TimeoutError), (
            "别引入 TimeoutError —— 那会诱导调用方去区分「拒绝」和「没答」"
        )


# =============================================================================
class TestFailClosed:
    def test_send_failure_is_refusal(self, env, tmp_path):
        """发卡失败 → 拒绝。**绝不能「没问成就当允许」。**"""
        store, clock, _ = env
        sender = FakeSender(boom=RuntimeError("网络断了"))
        access = DirectoryAccess(store=store, sender=sender, approver_id="ou_张三",
                                 ttl_seconds=600, clock=clock, sleep=_silent(clock))
        with pytest.raises(DirectoryRefused):
            access.list_dir(tmp_path)

    def test_unreadable_dir_after_allow_raises_not_empty(self, env, tmp_path):
        """允许了但读不了 → 抛。**不能返回空列表** ——
        「被拒绝」和「目录真是空的」长得一样，后果完全相反。"""
        store, clock, _ = env
        sender = FakeSender()
        missing = tmp_path / "不存在"
        access = _auto_allow(store, clock, sender)
        with pytest.raises(DirectoryRefused):
            access.list_dir(missing)


# =============================================================================
class TestRealDirectoryIsNotReadBeforeApproval:
    def test_nothing_read_when_refused(self, env, tmp_path):
        """最强的断言：拒绝时目录内容**根本没被读**。

        用一个不存在但「能列出东西」的替身来证明 —— 真正的守卫是顺序，
        而顺序只能靠「不通过就不调 iterdir」来保证。
        """
        store, clock, conn = env
        target = tmp_path / "x"
        target.mkdir()
        (target / "f").write_text("y", encoding="utf-8")

        calls: list[str] = []
        real_iterdir = Path.iterdir

        def spy(self, *a, **kw):
            calls.append(str(self))
            return real_iterdir(self, *a, **kw)

        sender = FakeSender()
        access = DirectoryAccess(store=store, sender=sender, approver_id="ou_张三",
                                 ttl_seconds=0, clock=clock, sleep=_silent(clock))
        original = Path.iterdir
        Path.iterdir = spy            # type: ignore[method-assign]
        try:
            with pytest.raises(DirectoryRefused):
                access.list_dir(target)
        finally:
            Path.iterdir = original    # type: ignore[method-assign]
        assert calls == [], f"被拒绝了却还是读了磁盘：{calls}"
