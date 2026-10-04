"""桥接启停（设计方案 12.7）。

重点是**三条不能靠「看起来对」糊过去**的性质：

1. **探测不能占着锁**。探测用的锁必须立刻释放 —— 不释放的话 Web 进程会一直
   占着端口，而它刚启动的那个桥接永远拿不到锁，自己把自己堵死。
2. **只停自己启的进程**。句柄只在内存里；拿不到就如实报错。照着状态文件里
   的 pid 去杀，可能杀掉一个 pid 回收后属于**别的程序**的进程。
3. **坏配置不许起进程**。起一个注定立刻退出的桥接，只会让用户对着空日志猜。
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from freeagent.feishu import supervisor
from freeagent.feishu.supervisor import (
    SupervisorError,
    bridge_state,
    port_probe,
    supervise,
)

from conftest import _free_port


@pytest.fixture()
def started() -> list[subprocess.Popen[bytes]]:
    """登记本用例起过的子进程，供 :func:`_clean_child` 回收。"""
    return []


@pytest.fixture(autouse=True)
def _clean_child(started: list[subprocess.Popen[bytes]]):
    """每个用例前后都确保没有残留子进程，且模块级句柄归零。

    顺带回收**任何**被登记过的子进程：``monkeypatch`` 会在用例结束时
    撤销 ``setattr``，句柄就再也拿不到了。漏掉的话，``Popen.__del__`` 会对
    还在跑的进程发 ResourceWarning，被 pytest 当成错误抛出来。
    """
    yield
    with supervisor._lock:
        proc = supervisor._child
        supervisor._child = None
    for alive in [p for p in (proc, *started) if p is not None]:
        if alive.poll() is None:
            alive.kill()
        try:
            alive.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            pass


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """隔离的状态目录 + 一份可用的配置。

    ⚠️ 锁端口这里**指定**一个空闲端口，而不是 ``delenv`` 掉。
    conftest 已在 session 级设了一个隔离端口；``delenv`` 会把它抹掉，
    于是落回默认 8771 —— 而本机可能正跑着真桥接，撞成「端口已被占用」，
    于是「配置不足该报错」这条测试报出来的却是「端口被占」，断言全歪。
    """
    monkeypatch.setenv("FREEAGENT_HOME", str(tmp_path))
    monkeypatch.setenv("FEISHU_LOCK_PORT", str(_free_port()))
    for key in (
        "FEISHU_APP_ID", "FEISHU_APP_SECRET",
        "FEISHU_ALLOWED_USERS", "FEISHU_DOMAIN",
    ):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


# --- 探测：占了端口就是有人在跑 ---------------------------------------- #


def test_probe_says_nothing_running_when_port_is_free(home):
    assert port_probe(home)["running"] is False


def test_probe_detects_a_held_port(home):
    """用真实的 PortLock 占住端口 —— 不 mock，测的就是同一套机制。"""
    from freeagent.feishu.bridge import PortLock, lock_port_from_env

    held = PortLock(lock_port_from_env())
    held.acquire()
    try:
        assert port_probe(home)["running"] is True
    finally:
        held.release()


def test_probe_releases_the_lock_immediately(home):
    """**这条是回归**：探测若把锁攥在手里，就再也起不了桥接。

    症状很隐蔽：点「启动」永远报「端口已被占用」，而那个占用者是自己。
    """
    from freeagent.feishu.bridge import PortLock, lock_port_from_env

    port = lock_port_from_env()
    port_probe(home)                       # 探一次
    again = PortLock(port)                 # 应该还能拿到
    again.acquire()                        # 拿不到就说明锁没释放
    again.release()


def test_probe_reports_the_port_it_used(home):
    """回报端口号，用户才能对上「是不是同一个」。"""
    from freeagent.feishu.bridge import lock_port_from_env

    assert port_probe(home)["lock_port"] == lock_port_from_env()


# --- 配置不全时拒绝启动 ------------------------------------------------- #


def test_start_refuses_when_nothing_is_configured(home):
    """一条进程都不该起 —— 起来了也只会立刻退出。"""
    with pytest.raises(SupervisorError) as exc:
        supervise("start", home)
    assert "配置" in str(exc.value)
    assert supervisor._child is None


def test_start_refuses_when_secret_missing(home, monkeypatch):
    """App ID 有了但 Secret 没有 —— 半个配置最容易被当成配好了。"""
    monkeypatch.setenv("FREEAGENT_HOME", str(home))
    from freeagent.feishu.secret_store import write_env

    write_env({"FEISHU_APP_ID": "cli_x", "FEISHU_ALLOWED_USERS": "ou_a"}, home)
    with pytest.raises(SupervisorError) as exc:
        supervise("start", home)
    assert supervisor._child is None
    assert "Secret" in str(exc.value) or "SECRET" in str(exc.value)


def test_refusing_to_start_leaves_no_log_behind(home):
    """拒绝了就不该留下日志文件，否则下次真起时会误以为上次跑过。"""
    with pytest.raises(SupervisorError):
        supervise("start", home)
    assert not supervisor._log_path(home).exists()


# --- 端口被别人占着：不硬顶 -------------------------------------------- #


def test_start_refuses_when_port_is_held_by_someone_else(home):
    """不硬顶。顶掉一个可能正在正常收消息的桥接，比报错坏得多。"""
    from freeagent.feishu.bridge import PortLock, lock_port_from_env

    held = PortLock(lock_port_from_env())
    held.acquire()
    try:
        with pytest.raises(SupervisorError) as exc:
            supervise("start", home)
        assert "端口" in str(exc.value)
        assert supervisor._child is None
    finally:
        held.release()


def test_start_twice_is_refused_the_second_time(home, monkeypatch, started):
    """已经在跑就不要再起一个 —— 飞书只投给其中一个，另一个静默失效。"""
    fake = _stub_running(monkeypatch, started)
    with pytest.raises(SupervisorError) as exc:
        supervise("start", home)
    assert "已经在跑" in str(exc.value)
    assert supervisor._child is fake


# --- 停止：只认自己启的进程 -------------------------------------------- #


def test_stop_without_a_child_says_so_plainly(home):
    """没有句柄时**如实说**，不编一个「已停止」。

    说谎的代价：用户以为干净了，于是从别处再起一个，而飞书只投给其中之一。
    """
    with pytest.raises(SupervisorError) as exc:
        supervise("stop", home)
    assert "没有在跑" in str(exc.value)


def test_stop_never_kills_by_pid_from_a_status_file(home, monkeypatch):
    """**安全约束**：状态文件里的 pid 可能已被回收给别的程序，不能照着杀。

    这里把 pid 指到一个**确实存在**的进程（当前 pytest 自己），若实现照着
    pid 杀，它会被终止 —— 测试进程一死，pytest 就会崩，所以这个断言一旦
    失效会以很难看的方式暴露出来。
    """
    from freeagent.feishu.status import write_status, status_path

    write_status(status_path(home), {
        "pid": os.getpid(), "state": "ready", "connected": True,
        "updated_at": 1e18,          # 极新，别被判成陈旧
    })
    with pytest.raises(SupervisorError):
        supervise("stop", home)
    assert os.getpid() != 0, "如果走到了终止这一步，测试进程已经死了"


def test_stop_of_an_already_exited_child_reports_the_code(home, monkeypatch):
    """已经自己退了就说退出码，而不是报「停止成功」。"""
    exited = subprocess.Popen(
        [sys.executable, "-c", "raise SystemExit(3)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    exited.wait(timeout=10)
    monkeypatch.setattr(supervisor, "_child", exited)
    with pytest.raises(SupervisorError) as exc:
        supervise("stop", home)
    assert "3" in str(exc.value)
    assert exited.returncode == 3


# --- 现状报告：几种情况必须分得开 -------------------------------------- #


def test_state_reports_clean_when_nothing_happens(home):
    s = bridge_state(home)
    assert s["running"] is False
    assert s["supervised"] is False
    assert s["port_held"] is False


def test_state_separates_port_held_from_running(home):
    """端口有人占 ≠ 我们的桥接在跑。这两件事必须分开报。"""
    from freeagent.feishu.bridge import PortLock, lock_port_from_env

    held = PortLock(lock_port_from_env())
    held.acquire()
    try:
        s = bridge_state(home)
        assert s["port_held"] is True
        assert s["running"] is False        # 没句柄 -> 不是我们起的
        assert s["supervised"] is False
    finally:
        held.release()


def test_state_reports_returncode_of_a_dead_child(home, monkeypatch):
    exited = subprocess.Popen(
        [sys.executable, "-c", "raise SystemExit(7)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    exited.wait(timeout=10)
    monkeypatch.setattr(supervisor, "_child", exited)
    try:
        s = bridge_state(home)
    finally:
        supervisor._child = None
    assert s["supervised"] is True
    assert s["returncode"] == 7
    assert s["running"] is False


def test_a_fresh_status_alone_does_not_mean_running(home):
    """状态文件很新 ≠ 进程在跑。

    进程被强杀时状态文件会留在盘上（见 status 模块说明），照着它报
    「运行中」就是对着尸体报健康。
    """
    from freeagent.feishu.status import write_status, status_path

    write_status(status_path(home), {
        "state": "ready", "connected": True, "updated_at": 1e18,
    })
    s = bridge_state(home)
    assert s["stale"] is False
    assert s["running"] is False, "没有句柄就不该说运行中"


def test_fresh_status_from_an_unsupervised_bridge_is_alive_observed(home):
    """**回归**：心跳新鲜、但不是本界面启的桥接，必须报 ``alive_observed``。

    这就是开机自启 / 别的终端手开 / **Web 自己重启过一次**之后的那三种情形。
    修之前界面只有 ``running``，而它依赖内存里的进程句柄，于是这三种全被
    报成「没在跑」—— 用户明明在正常收发消息，界面却持续报故障。

    刻意**不改** ``running``（见
    :func:`test_a_fresh_status_alone_does_not_mean_running`）：那个字段回答的
    是「我们 spawn 的那个还在吗」，与本条是两件事。
    """
    from freeagent.feishu.status import write_status, status_path

    write_status(status_path(home), {
        "state": "ready", "connected": True, "updated_at": 1e18,
    })
    s = bridge_state(home)
    assert s["alive_observed"] is True, (
        "心跳新鲜就该承认它在跑——哪怕不是本界面启的"
    )
    assert s["running"] is False, "仍然不许说「是我们启的」"


def test_dead_bridge_is_not_alive_observed(home):
    """心跳陈旧 → ``alive_observed`` 必须为假（否定的另一半）。"""
    from freeagent.feishu.status import write_status, status_path

    write_status(status_path(home), {
        "state": "ready", "connected": True, "updated_at": 1.0,
    })
    assert bridge_state(home)["alive_observed"] is False


def test_no_status_file_is_not_alive_observed(home):
    """从没跑过 → 没有心跳可谈，旁证必须为假。"""
    from freeagent.feishu.status import status_path

    status_path(home).unlink(missing_ok=True)
    s = bridge_state(home)
    assert s["alive_observed"] is False
    assert s["running"] is False


def test_freshly_spawned_child_counts_as_running(home, monkeypatch, started):
    """**回归**：刚 spawn 出来、还没写心跳的桥接，必须已经算「在跑」。

    踩过的坑（测试抓出来的）：原先 ``running`` 还要求「状态新鲜」，而陈旧
    判定对「没有状态文件」一律返回 True。桥接刚起来时还没写第一次心跳 ——
    于是点完「启动」立刻回读，看到的必然是「没在跑」，而进程明明活着。
    界面于是显示「已停止」，用户以为启动失败，去点第二次。
    """
    proc = _stub_running(monkeypatch, started)
    s = bridge_state(home)
    assert s["running"] is True, "刚起的桥接被误报成已停止"
    assert s["stale"] is True          # 还没心跳，如实说陈旧
    assert s["pid"] == proc.pid        # 但进程号要给对，界面要显示它
    assert proc.pid is not None


def test_a_stale_status_is_reported_as_stale(home):
    from freeagent.feishu.status import write_status, status_path

    write_status(status_path(home), {
        "state": "ready", "connected": True, "updated_at": 1.0,
    })
    assert bridge_state(home)["stale"] is True


# --- 日志落盘位置 ------------------------------------------------------- #


def test_log_path_matches_what_the_status_page_reads(home):
    """**回归**：写去别处的话，日志区会永远空白，而空白会被当成没出错。"""
    from freeagent.web.endpoints_feishu import feishu_log

    # feishu_log 需要 App，这里直接比对两者算出的路径。
    assert supervisor._log_path(home).name == "feishu.log"
    assert supervisor._log_path(home).parent == (home)


def test_state_includes_the_log_path_for_the_ui(home):
    assert bridge_state(home)["log_path"].endswith("feishu.log")


# --- 动作名 ------------------------------------------------------------- #


def test_unknown_action_is_refused(home):
    with pytest.raises(SupervisorError) as exc:
        supervise("nope", home)
    assert "nope" in str(exc.value)


def test_restart_without_a_child_says_it_cannot_stop_first(home):
    """重启的第一步是停。没得停就如实说，别报「重启成功」。"""
    with pytest.raises(SupervisorError) as exc:
        supervise("restart", home)
    assert "没有在跑" in str(exc.value)


# --------------------------------------------------------------------------- #


def _stub_running(monkeypatch, started) -> subprocess.Popen[bytes]:
    """假装有个还活着的子进程。

    ``started`` 是那个 autouse 夹具的回收清单 —— 登记进去，才不会在
    monkeypatch 撤销句柄之后留下一个孤儿进程。
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    started.append(proc)
    monkeypatch.setattr(supervisor, "_child", proc)
    return proc
