"""桥接启停端点（设计方案 12.7）。

这组端点能**起一个带凭据的进程**，是整个 Web 面里后果最重的一类操作。
所以三条约束都用测试钉住：

1. **只认 POST**。动作绝不能挂在 GET 上 —— 「顺手 GET 一下就启动了/停了」
   正是要避免的形状（预取、爬虫、浏览器预加载都可能触发）。
2. **必须过会话令牌**。POST 在 :mod:`freeagent.web.server` 里不查只读白名单，
   所以这一层是硬门。
3. **失败要说人话，且附上那一刻的现状**。「操作失败」等于把信息扔掉。
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from http.client import HTTPConnection

import pytest

from freeagent.app import build_app
from freeagent.feishu import supervisor
from freeagent.web.server import create_server
from freeagent.web.session import SESSION_HEADER, SESSION_TOKEN

from conftest import _free_port


@pytest.fixture()
def started() -> list[subprocess.Popen[bytes]]:
    """登记本用例起过的子进程，供夹具回收（避免孤儿进程触发 ResourceWarning）。"""
    return []


@pytest.fixture()
def served(tmp_path, monkeypatch, started):
    """隔离的 Web 服务。**不真的起桥接** —— 那是 supervisor 的测试范围。

    锁端口**指定**空闲端口而非删掉：删掉会落回默认 8771，而本机可能正跑着真
    桥接，于是「配置不足该报错」报出来的变成「端口被占」。
    """
    monkeypatch.setenv("FREEAGENT_HOME", str(tmp_path))
    monkeypatch.setenv("FEISHU_LOCK_PORT", str(_free_port()))
    for key in (
        "FEISHU_APP_ID", "FEISHU_APP_SECRET",
        "FEISHU_ALLOWED_USERS", "FEISHU_DOMAIN",
    ):
        monkeypatch.delenv(key, raising=False)
    app = build_app(tmp_path / "agent.db")
    server = create_server(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"127.0.0.1:{server.server_address[1]}", tmp_path
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        app.close()
        for proc in started:
            if proc.poll() is None:
                proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:   # noqa: BLE001 - 回收失败不该盖掉用例结果
                pass
        with supervisor._lock:
            supervisor._child = None


def call(addr, method, path, body=None, token: str | None = SESSION_TOKEN):
    """``token=None`` 表示**故意不带**令牌，用来验 401。"""
    host, port = addr.split(":")
    conn = HTTPConnection(host, int(port), timeout=5)
    try:
        headers = {} if token is None else {SESSION_HEADER: token}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers |= {
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            }
        conn.request(method, path, body=payload, headers=headers)
        res = conn.getresponse()
        return res.status, json.loads(res.read().decode("utf-8"))
    finally:
        conn.close()


# --- 只读端点 ----------------------------------------------------------- #


def test_get_reports_clean_state(served):
    status, data = call(served[0], "GET", "/api/feishu/bridge")
    assert status == 200
    assert data["running"] is False
    assert data["kind"] == "feishu_bridge"
    assert data["message"], "要给一句人话，不能只有裸字段"


def test_get_needs_no_write_capability(served):
    """GET 侧纯读，所以照样要令牌 —— 12.7 的规则对所有 /api/ 生效。"""
    status, _ = call(served[0], "GET", "/api/feishu/bridge", token=None)
    assert status == 401


def test_get_exposes_the_allowed_actions(served):
    _s, data = call(served[0], "GET", "/api/feishu/bridge")
    assert data["actions"] == ["start", "stop", "restart"]


def test_get_includes_log_path_for_the_ui(served):
    _s, data = call(served[0], "GET", "/api/feishu/bridge")
    assert data["log_path"].endswith("feishu.log")


# --- 动作端点：鉴权 ----------------------------------------------------- #


def test_action_requires_a_token(served):
    """没令牌一律拒。"""
    status, data = call(
        served[0], "POST", "/api/feishu/bridge", {"action": "start"}, token=None,
    )
    assert status == 401
    assert "令牌" in data["error"]


def test_action_with_wrong_token_is_refused(served):
    status, _ = call(
        served[0], "POST", "/api/feishu/bridge", {"action": "start"},
        token="nope",
    )
    assert status == 401


def test_action_via_get_is_404(served):
    """**不能顺手 GET 一下就启停了**。

    这是刻意不注册的别名。浏览器预取、爬虫、`<img src>` 都可能发 GET；
    能杀进程的操作绝不能挂在这种会自动发出的方法上。
    """
    status, _ = call(served[0], "GET", "/api/feishu/bridge/start")
    assert status == 404


def test_post_on_a_get_path_is_refused(served):
    """反方向也不通：读端点不能被用来起进程。"""
    status, _ = call(
        served[0], "POST", "/api/feishu/status", {"action": "start"},
    )
    assert status == 404


# --- 动作端点：参数与失败信息 ------------------------------------------- #


def test_unknown_action_is_400(served):
    status, data = call(served[0], "POST", "/api/feishu/bridge", {"action": "rm"})
    assert status == 400
    assert "start" in data["error"]


def test_missing_action_is_400(served):
    status, data = call(served[0], "POST", "/api/feishu/bridge", {})
    assert status == 400
    assert "action" in data["error"]


def test_stop_without_a_child_reports_plainly(served):
    """400 + supervisor 那句人话，不是「操作失败」。"""
    status, data = call(served[0], "POST", "/api/feishu/bridge", {"action": "stop"})
    assert status == 400
    assert "没有在跑" in data["error"]


def test_start_with_no_config_is_refused_with_a_reason(served):
    """配置不全不许起 —— 且要说清缺什么。"""
    status, data = call(served[0], "POST", "/api/feishu/bridge", {"action": "start"})
    assert status == 400
    assert "配置" in data["error"]
    assert supervisor._child is None


def test_restart_without_a_child_does_not_claim_success(served):
    status, data = call(
        served[0], "POST", "/api/feishu/bridge", {"action": "restart"})
    assert status == 400
    assert "没有在跑" in data["error"]


def test_a_refused_action_leaves_no_process(served):
    """反复点「启动」不该攒出进程来。"""
    for _ in range(3):
        call(served[0], "POST", "/api/feishu/bridge", {"action": "start"})
    assert supervisor._child is None


# --- 真的起一次（子进程是假的） ------------------------------------------ #


def test_start_reports_running_when_the_child_survives(served, started, monkeypatch):
    """端到端走通一次成功启动。

    真桥接要连飞书、装 SDK，测试里既慢又不该真连，所以只把「怎么起进程」
    换掉 —— 端口检查、早退判定、状态回报全走真实实现。
    """
    _fake_surviving_child(served[1], started, monkeypatch=monkeypatch)
    _s, data = call(served[0], "POST", "/api/feishu/bridge", {"action": "start"})
    assert data["running"] is True
    assert data["supervised"] is True
    assert data["returncode"] is None
    assert data["message"]
    assert supervisor._child is started[0]


def test_start_then_stop_ends_cleanly(served, started, monkeypatch):
    """启完再停，状态要回到「干净」而不是残留「运行中」。"""
    _fake_surviving_child(served[1], started, monkeypatch=monkeypatch)
    call(served[0], "POST", "/api/feishu/bridge", {"action": "start"})
    _s, data = call(served[0], "POST", "/api/feishu/bridge", {"action": "stop"})
    assert data["running"] is False
    assert data["supervised"] is False
    assert supervisor._child is None


def test_stop_really_kills_the_process(served, started, monkeypatch):
    """停就该真停 —— 只把状态改成「已停」而进程还在跑，那是最坏的骗人。"""
    _fake_surviving_child(served[1], started, monkeypatch=monkeypatch)
    call(served[0], "POST", "/api/feishu/bridge", {"action": "start"})
    call(served[0], "POST", "/api/feishu/bridge", {"action": "stop"})
    assert started[0].poll() is not None, "状态说停了，进程却还活着"


def test_restart_replaces_the_old_process(served, started, monkeypatch):
    """重启要换成一个**新**进程，否则「重启」只是把旧的重启不了。"""
    _fake_surviving_child(served[1], started, monkeypatch=monkeypatch)
    call(served[0], "POST", "/api/feishu/bridge", {"action": "start"})
    first = started[0]
    _s, data = call(served[0], "POST", "/api/feishu/bridge", {"action": "restart"})
    assert first.poll() is not None, "旧进程还活着"
    assert len(started) == 2, "重启没有换新进程"
    assert data["running"] is True


def test_start_appends_to_the_log_file(served, started, monkeypatch):
    """日志要落在 feishu.log —— 状态页读的就是这个路径。"""
    _fake_surviving_child(served[1], started, log="first run\n", monkeypatch=monkeypatch)
    call(served[0], "POST", "/api/feishu/bridge", {"action": "start"})
    log = supervisor._log_path(served[1])
    assert log.exists(), "启了却没日志，状态页那块会永远空白"
    assert "first run" in log.read_text(encoding="utf-8")


def test_start_makes_the_log_readable_by_the_status_page(served, started, monkeypatch):
    """端点与状态页算出的是**同一个**路径，否则那块会永远空白。"""
    _fake_surviving_child(served[1], started, log="hello\n", monkeypatch=monkeypatch)
    call(served[0], "POST", "/api/feishu/bridge", {"action": "start"})
    _s, ctl = call(served[0], "GET", "/api/feishu/bridge")
    _s2, log = call(served[0], "GET", "/api/feishu/log")
    assert ctl["log_path"] == log["path"]


# --- 端到端启动的两个已知失败模式 ---------------------------------------- #


def test_child_that_dies_immediately_is_reported(served, started, monkeypatch):
    """子进程一起来就退，要**当场**说，而不是报「已启动」。

    这是最常见的失败（配置不全、凭据不对），报「已启动」会让用户对着
    一个已经死掉的桥接等下去。
    """
    monkeypatch.setattr(
        supervisor, "_do_start", _stub_dying_child(served[1], started, code=2))
    status, data = call(served[0], "POST", "/api/feishu/bridge", {"action": "start"})
    assert status == 400
    assert "退出" in data["error"]
    assert supervisor._child is None


def test_reported_failure_points_at_the_log(served, started, monkeypatch):
    """失败时要告诉用户去哪看原因，而不是只说「启动失败」。"""
    monkeypatch.setattr(
        supervisor, "_do_start", _stub_dying_child(served[1], started, code=2))
    _s, data = call(served[0], "POST", "/api/feishu/bridge", {"action": "start"})
    assert "feishu.log" in data["error"] or "日志" in data["error"]


# --------------------------------------------------------------------------- #


def _fake_surviving_child(home, started, log: str = "fake bridge\n", monkeypatch=None):
    """假的「起得起来」的桥接。

    替换掉「怎么起进程」那一段（真桥接要连飞书、测试里既慢又不该真连），
    但**后面的真实逻辑照走**：占用端口检查、早退判定、句柄登记、状态回报。
    这样测的是「端点 → supervisor → 回报」整条链路，而不是只测一个桩。
    """
    import subprocess as sp
    import sys

    if monkeypatch is None:
        raise AssertionError("要传 monkeypatch")

    def fake_start(inner_home=None):
        proc = sp.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=sp.DEVNULL, stderr=sp.DEVNULL,
        )
        started.append(proc)
        # 刻意**不**抢着写状态文件：这里模拟的是「刚 spawn 出来、还没写
        # 第一次心跳」那一刻。真实桥接此刻也正是这个状态（它要先联网探
        # 身份、写第一份状态）。这个用例就是靠它守住「刚起就算在跑」那条。
        supervisor._child = proc
        path = supervisor._log_path(inner_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 追加语义：真实实现是 open(..., "ab")。
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(log)
        return proc

    monkeypatch.setattr(supervisor, "_do_start", fake_start)
    return fake_start


def _stub_dying_child(home, started, code: int = 2):
    """假的「一起来就退」的桥接。

    替掉 ``_do_start``：让 supervisor 走「起进程 → 发现早退 → 报错」那条
    真实路径（由 supervisor 自己做 ``poll()`` 判定），只把「怎么起」换掉。
    """
    import subprocess as sp
    import sys

    def fake_start(inner_home=None):
        proc = sp.Popen(
            [sys.executable, "-c", f"raise SystemExit({code})"],
            stdout=sp.DEVNULL, stderr=sp.DEVNULL,
        )
        started.append(proc)
        supervisor._child = proc
        # 复用真实实现的后半段：poll 到已退出 -> 清句柄 -> 抛错。
        time.sleep(supervisor._STARTUP_GRACE_SECONDS)
        if supervisor._child is not None and supervisor._child.poll() is not None:
            supervisor._child = None
            raise supervisor.SupervisorError(
                f"桥接启动后立刻退出了（退出码 {code}）。"
                f"原因在日志里：{supervisor._log_path(inner_home)}"
                "（多半是配置不全或凭据不对，doctor 能查具体哪一项）。"
            )

    return fake_start



# --- 消息质量 ------------------------------------------------------------ #


def test_messages_never_leak_a_secret(served):
    """失败路径也要检查：消息会被原样送到浏览器。"""
    from freeagent.feishu.secret_store import write_env

    write_env({"FEISHU_APP_SECRET": "SuperSecretValue123"}, served[1])
    _s, data = call(served[0], "POST", "/api/feishu/bridge", {"action": "start"})
    assert "SuperSecretValue123" not in json.dumps(data, ensure_ascii=False)


def test_port_held_is_described_distinctly_from_running(served, monkeypatch):
    """端口被占与「我们的桥接在跑」必须分开说 —— 两者要采取的动作不同。"""
    from freeagent.feishu.bridge import PortLock, lock_port_from_env

    held = PortLock(lock_port_from_env())
    held.acquire()
    try:
        _s, data = call(served[0], "GET", "/api/feishu/bridge")
        assert data["port_held"] is True
        assert data["running"] is False
        assert "占" in data["message"]
    finally:
        held.release()
