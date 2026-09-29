"""飞书配置端点（设计方案 12.7）的守卫。

重点是三件「错了会出事、而且很安静」的事：

- **Secret 绝不回传明文**。GET 只给掩码。
- **空 Secret 不等于删除**。界面上一个没填过的输入框提交上来，如果当成
  清空，就会把用户正在用的凭据抹掉，而界面还会显示「已保存」。
- **只认白名单里的变量名**。透传任意 key 等于开 RCE 入口。
"""

from __future__ import annotations

import json
import tempfile
import threading
from http.client import HTTPConnection
from pathlib import Path

import pytest

from freeagent.app import App, build_app
from freeagent.feishu.secret_store import env_path, read_env
from freeagent.web.server import create_server
from freeagent.web.session import SESSION_HEADER, SESSION_TOKEN

SECRET = "S3cr3tValueThatIsLongEnough"


@pytest.fixture()
def served(tmp_path):
    app = build_app(tmp_path / "agent.db")
    server = create_server(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"127.0.0.1:{server.server_address[1]}", app
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        app.close()


def call(addr: str, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    host, port = addr.split(":")
    conn = HTTPConnection(host, int(port), timeout=5)
    try:
        headers = {SESSION_HEADER: SESSION_TOKEN}
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


def get_config(addr: str) -> dict:
    status, data = call(addr, "GET", "/api/feishu/config")
    assert status == 200, data
    return data


def save(addr: str, body: dict) -> tuple[int, dict]:
    return call(addr, "POST", "/api/feishu/config", body)


# --- 保存与读取 ---------------------------------------------------------- #


def test_save_then_read_round_trips(served):
    addr, _app = served
    status, data = save(addr, {
        "FEISHU_APP_ID": "cli_test",
        "FEISHU_APP_SECRET": SECRET,
        "FEISHU_ALLOWED_USERS": "ou_aaa,ou_bbb",
    })
    assert status == 200, data
    assert data["values"]["FEISHU_APP_ID"] == "cli_test"
    assert data["missing"] == []


def test_config_really_lands_on_disk(served):
    """界面显示「已保存」必须等于**盘上真的写了**。

    这条是整个功能的命根子：只回一句「好了」而不落盘的话，配置界面就是个
    假装能用的摆设，而用户要到桥接起不来时才发现。
    """
    addr, app = served
    save(addr, {"FEISHU_APP_ID": "cli_test", "FEISHU_APP_SECRET": SECRET})
    on_disk = read_env(app.config.home)
    assert on_disk["FEISHU_APP_ID"] == "cli_test"
    assert on_disk["FEISHU_APP_SECRET"] == SECRET


def test_saved_config_actually_reaches_the_bridge(served, monkeypatch):
    """桥接**实际走的**那条路必须读得到界面存的值。

    回归：``load_config`` 原先**只读进程环境变量**、不读 ``feishu.env``，
    于是界面存了配置、桥接却当没有 —— 一个「显示已保存但毫无作用」的假功能。

    这里用 ``FREEAGENT_HOME`` 把默认状态目录指到测试目录，然后调
    ``load_config(env=None)`` —— 和桥接启动时**完全一样**的调用方式。
    只测 ``merged_env(home=...)`` 是不够的：那验证的是合并函数，
    而桥接压根不传 home。
    """
    addr, _app = served
    status, _data = save(addr, {
        "FEISHU_APP_ID": "cli_from_ui",
        "FEISHU_APP_SECRET": SECRET,
        "FEISHU_ALLOWED_USERS": "ou_zzz",
    })
    assert status == 200
    home = Path(_app_home(addr))
    monkeypatch.setenv("FREEAGENT_HOME", str(home))

    from freeagent.feishu.config import load_config

    cfg = load_config(env=None)
    assert cfg.app_id == "cli_from_ui"
    assert cfg.app_secret.use() == SECRET
    assert cfg.allowed_users == frozenset({"ou_zzz"})


def _app_home(addr: str) -> str:
    """从 GET 响应里拿配置目录。"""
    home = get_config(addr)["env_path"]
    return str(Path(home).parent)


def test_bridge_env_wins_over_file(served, monkeypatch):
    """环境变量要**压过**文件 —— 否则 ``set FEISHU_APP_ID=...`` 这类
    一次性覆盖会失效，而 doctor、测试、CI 都依赖它。"""
    addr, _app = served
    save(addr, {"FEISHU_APP_ID": "cli_from_file"})
    monkeypatch.setenv("FREEAGENT_HOME", str(_app_home(addr)))
    monkeypatch.setenv("FEISHU_APP_ID", "cli_from_env")

    from freeagent.feishu.config import load_config

    assert load_config(env=None).app_id == "cli_from_env"


def test_empty_env_var_is_not_silently_refilled(served, monkeypatch):
    """``set FEISHU_APP_SECRET=`` 是「我故意清空」，文件**不能**悄悄填回来。

    按「键是否存在」判断而不是「值是否为空」，就是为了这条：把文件当兜底时，
    最不该被悄悄推翻的就是用户明确写出来的空值。"""
    addr, _app = served
    save(addr, {"FEISHU_APP_SECRET": SECRET})
    monkeypatch.setenv("FREEAGENT_HOME", str(_app_home(addr)))
    monkeypatch.setenv("FEISHU_APP_SECRET", "")

    from freeagent.feishu.config import merged_env

    assert merged_env().get("FEISHU_APP_SECRET") == ""


def test_domain_defaults_when_omitted(served):
    addr, _app = served
    _s, data = save(addr, {"FEISHU_APP_ID": "cli_x", "FEISHU_ALLOWED_USERS": "ou_a"})
    assert "FEISHU_DOMAIN" not in data["values"]


def test_missing_required_keys_are_reported(served):
    """**不因为缺项就拒绝保存** —— 用户可能分几次填。

    但必须如实报出「还差什么」，否则用户以为配好了，桥接却起不来。
    """
    addr, _app = served
    _s, data = save(addr, {"FEISHU_APP_ID": "cli_only"})
    assert data["missing"] == ["FEISHU_APP_SECRET", "FEISHU_ALLOWED_USERS"]


# --- Secret：绝不回传明文 ------------------------------------------------ #


def test_get_never_returns_the_secret_in_plaintext(served):
    addr, _app = served
    save(addr, {"FEISHU_APP_SECRET": SECRET, "FEISHU_APP_ID": "cli_x"})
    data = get_config(addr)
    assert SECRET not in json.dumps(data, ensure_ascii=False)
    shown = data["values"]["FEISHU_APP_SECRET"]
    assert SECRET[:4] in shown and SECRET[-4:] in shown
    assert SECRET not in shown
    assert data["secret_configured"] is True


def test_masked_secret_is_not_usable(served):
    """掩码要能确认「是那个值」，但不足以拿去用。"""
    addr, _app = served
    save(addr, {"FEISHU_APP_SECRET": SECRET})
    shown = get_config(addr)["values"]["FEISHU_APP_SECRET"]
    assert len(shown) < len(SECRET)


def test_save_response_also_masks(served):
    """保存的回显同样不能带明文 —— 它也是一条 HTTP 响应。"""
    addr, _app = served
    _s, data = save(addr, {"FEISHU_APP_SECRET": SECRET})
    assert SECRET not in json.dumps(data, ensure_ascii=False)


# --- 空 Secret 不等于删除 ------------------------------------------------ #


def test_empty_secret_leaves_the_old_one_alone(served):
    """**这条是防误删凭据的那道闸。**

    界面上一个没填过的 Secret 输入框提交上来，如果当成「清空」，
    用户正在用的凭据就没了，而界面还显示「已保存」。
    """
    addr, app = served
    save(addr, {"FEISHU_APP_SECRET": SECRET, "FEISHU_APP_ID": "cli_x"})
    _s, data = save(addr, {"FEISHU_APP_SECRET": "", "FEISHU_APP_ID": "cli_y"})
    assert data["secret_configured"] is True
    assert read_env(app.config.home)["FEISHU_APP_SECRET"] == SECRET


def test_whitespace_only_secret_also_leaves_it_alone(served):
    addr, app = served
    save(addr, {"FEISHU_APP_SECRET": SECRET})
    save(addr, {"FEISHU_APP_SECRET": "   "})
    assert read_env(app.config.home)["FEISHU_APP_SECRET"] == SECRET


def test_omitted_secret_leaves_it_alone(served):
    addr, app = served
    save(addr, {"FEISHU_APP_SECRET": SECRET})
    save(addr, {"FEISHU_ALLOWED_USERS": "ou_new"})
    assert read_env(app.config.home)["FEISHU_APP_SECRET"] == SECRET


def test_secret_is_replaced_when_actually_typed(served):
    """上一条的反面：真填了新值就得真换掉，不能一律当「没改」。"""
    addr, app = served
    save(addr, {"FEISHU_APP_SECRET": SECRET})
    save(addr, {"FEISHU_APP_SECRET": "AnotherSecretValue123"})
    assert read_env(app.config.home)["FEISHU_APP_SECRET"] == "AnotherSecretValue123"


def test_clearing_the_secret_needs_an_explicit_flag(served):
    """真要删必须显式来 —— 那个操作太危险，不该由空输入框触发。"""
    addr, app = served
    save(addr, {"FEISHU_APP_SECRET": SECRET})
    _s, data = save(addr, {"clear_secret": True})
    assert data["secret_configured"] is False
    assert "FEISHU_APP_SECRET" not in read_env(app.config.home)


# --- 白名单：挡住 RCE ---------------------------------------------------- #


@pytest.mark.parametrize("evil", ["PYTHONPATH", "LD_PRELOAD", "EDITOR", "PATH"])
def test_never_writes_an_arbitrary_variable(served, evil):
    """透传任意变量名 = 一个 RCE 入口（下次起子进程就加载攻击者的代码）。"""
    addr, app = served
    status, data = save(addr, {evil: "C:/attacker"})
    assert status == 400
    assert evil in data["error"]
    # 被拒时文件可能压根没建出来 —— 那比「建了但没这个键」更强。
    path = env_path(app.config.home)
    on_disk = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    assert evil not in on_disk
    assert "attacker" not in on_disk


def test_rejected_save_leaves_the_file_untouched(served):
    """一次坏输入不能把已有配置弄坏。"""
    addr, app = served
    save(addr, {"FEISHU_APP_ID": "cli_good", "FEISHU_APP_SECRET": SECRET})
    before = env_path(app.config.home).read_text(encoding="utf-8-sig")
    assert save(addr, {"PYTHONPATH": "x"})[0] == 400
    assert env_path(app.config.home).read_text(encoding="utf-8-sig") == before


# --- 校验：不能比桥接宽松 ---------------------------------------------- #


def test_rejects_a_domain_the_bridge_would_reject(served):
    """界面绝不能接受一个桥接启动时才拒绝的值。

    否则就是「显示已保存、桥接起不来」—— 12.7 要防的正是这个。
    """
    addr, _app = served
    status, data = save(addr, {"FEISHU_DOMAIN": "example.com"})
    assert status == 400
    assert "feishu" in data["error"]


@pytest.mark.parametrize("domain", ["feishu", "lark", "FEISHU", " Lark "])
def test_accepts_exactly_what_the_bridge_accepts(served, domain):
    addr, app = served
    assert save(addr, {"FEISHU_DOMAIN": domain})[0] == 200
    assert read_env(app.config.home)["FEISHU_DOMAIN"] == domain.strip().lower()


def test_rejects_a_non_numeric_lock_port(served):
    addr, _app = served
    assert save(addr, {"FEISHU_LOCK_PORT": "abc"})[0] == 400


@pytest.mark.parametrize("port", ["0", "70000", "-1"])
def test_rejects_out_of_range_lock_port(served, port):
    addr, _app = served
    assert save(addr, {"FEISHU_LOCK_PORT": port})[0] == 400


def test_accepts_a_normal_lock_port(served):
    addr, app = served
    assert save(addr, {"FEISHU_LOCK_PORT": "8781"})[0] == 200
    assert read_env(app.config.home)["FEISHU_LOCK_PORT"] == "8781"


def test_lock_port_from_the_file_reaches_the_bridge(served, monkeypatch):
    """界面的锁端口也要真的生效 —— 它是白名单里的变量，不是摆设。

    回归：``lock_port_from_env`` 原先直接读 ``os.environ``，不读
    ``feishu.env``，于是界面存的端口对桥接毫无影响。
    """
    addr, _app = served
    status, _data = save(addr, {"FEISHU_LOCK_PORT": "8791"})
    assert status == 200
    monkeypatch.setenv("FREEAGENT_HOME", str(_app_home(addr)))
    monkeypatch.delenv("FEISHU_LOCK_PORT", raising=False)

    from freeagent.feishu.bridge import lock_port_from_env

    assert lock_port_from_env() == 8791


def test_lock_port_still_defaults(served, monkeypatch):
    """改了读取方式之后,默认值不能被顺带弄坏。"""
    monkeypatch.setenv("FREEAGENT_HOME", str(_app_home(served[0])))
    monkeypatch.delenv("FEISHU_LOCK_PORT", raising=False)
    from freeagent.feishu.bridge import DEFAULT_LOCK_PORT, lock_port_from_env

    assert lock_port_from_env() == DEFAULT_LOCK_PORT


def test_lock_port_garbage_still_raises(served, monkeypatch):
    """环境变量里的垃圾值必须**报错**,不能静默退回默认端口。

    静默退回最坏：用户配了 8781 来避开和别的进程撞车，解析失败却悄悄用回
    8771，于是撞车报出来时他完全想不通「我明明改过端口」。

    注意这条走的是**环境变量**而不是文件：写文件那条路已被端点校验挡住
    （见 ``test_rejects_a_non_numeric_lock_port``），所以手改文件和
    ``set`` 变量是剩下两条真正可达的坏值来源，后者要照样炸。
    """
    monkeypatch.setenv("FREEAGENT_HOME", str(_app_home(served[0])))
    monkeypatch.setenv("FEISHU_LOCK_PORT", "not-a-number")

    from freeagent.feishu.bridge import lock_port_from_env
    from freeagent.feishu.config import ConfigError

    with pytest.raises(ConfigError, match="整数"):
        lock_port_from_env()


def test_rejects_too_many_allowed_users(served):
    """上限与 ``load_config`` 同源（MAX_ALLOWED），不能界面能填、桥接不认。"""
    addr, _app = served
    status, data = save(addr, {"FEISHU_ALLOWED_USERS": ",".join(
        f"ou_{i}" for i in range(201))})
    assert status == 400
    assert "200" in data["error"]


def test_whitelist_mess_is_normalised(served):
    """逗号前后空格不该变成两个「用户」—— 那会让白名单静默失真。"""
    addr, app = served
    save(addr, {"FEISHU_ALLOWED_USERS": " ou_a , ou_b ,, ou_c "})
    assert read_env(app.config.home)["FEISHU_ALLOWED_USERS"] == "ou_a,ou_b,ou_c"


def test_non_string_value_rejected(served):
    addr, _app = served
    assert save(addr, {"FEISHU_APP_ID": 123})[0] == 400


# --- 需要重启 ------------------------------------------------------------ #


def test_fresh_config_with_no_bridge_needs_no_restart(served):
    """没有桥接在跑时不该喊「需要重启」—— 界面本来就会显示「未运行」，
    再加一句是纯噪音。"""
    addr, _app = served
    _s, data = save(addr, {"FEISHU_APP_ID": "cli_x"})
    assert data["restart_required"] is False


def test_changed_config_while_bridge_runs_asks_for_restart(served):
    """**这条是「以为保存了就是生效了」的解药**（12.7）。

    桥接启动时记下它看到的配置；改盘上的配置之后，界面必须说出来。
    """
    addr, app = served
    from freeagent.feishu.status import (
        CONFIG_REVISION_FIELD,
        StatusReporter,
        status_path,
    )

    save(addr, {"FEISHU_APP_ID": "cli_x", "FEISHU_APP_SECRET": SECRET})
    reporter = StatusReporter(status_path(app.config.home))
    reporter.update(state="ready", connected=True,
                    **{CONFIG_REVISION_FIELD: _revision(app)})
    assert get_config(addr)["restart_required"] is False

    save(addr, {"FEISHU_APP_ID": "cli_changed"})
    assert get_config(addr)["restart_required"] is True


def test_bridge_without_the_field_is_not_guessed_at(served):
    """旧版桥接没记这个字段。不知道就是不知道，不猜。"""
    addr, app = served
    from freeagent.feishu.status import StatusReporter, status_path

    save(addr, {"FEISHU_APP_ID": "cli_x"})
    reporter = StatusReporter(status_path(app.config.home))
    reporter.update(state="ready", connected=True)
    assert get_config(addr)["restart_required"] is False


def _revision(app: App) -> int:
    from freeagent.feishu.status import config_revision

    return config_revision(app.config.home)


# --- 页面契约：表单存在，且 Secret 绝不进页面源码 -------------------------- #


def _page(addr: str) -> str:
    host, port = addr.split(":")
    conn = HTTPConnection(host, int(port), timeout=5)
    try:
        conn.request("GET", "/")
        return conn.getresponse().read().decode("utf-8")
    finally:
        conn.close()


@pytest.fixture()
def served_page():
    """先存好一份真 Secret，再起服务。

    单独一个夹具是因为「页面里不能有明文 Secret」这条只有在**真的存过**
    之后才测得出来 —— 空配置时页面当然没有 Secret，那测不出任何东西。
    """
    with tempfile.TemporaryDirectory() as tmp:
        app = build_app(Path(tmp) / "agent.db")
        server = create_server(app, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            addr = f"127.0.0.1:{server.server_address[1]}"
            status, _d = call(addr, "POST", "/api/feishu/config", {
                "FEISHU_APP_ID": "cli_page",
                "FEISHU_APP_SECRET": SECRET,
                "FEISHU_ALLOWED_USERS": "ou_page",
            })
            assert status == 200
            yield addr, app
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            app.close()


def test_page_has_the_config_form(served):
    """界面上得有地方填 —— 「通过界面更新配置」是这一轮的需求本身。"""
    body = _page(served[0])
    for key in (
        "FEISHU_APP_ID",
        "FEISHU_APP_SECRET",
        "FEISHU_ALLOWED_USERS",
        "FEISHU_DOMAIN",
        "FEISHU_LOCK_PORT",
    ):
        assert key in body, f"页面上没有 {key} 的输入框"


def test_page_says_secret_stays_masked(served):
    body = _page(served[0])
    assert "留空" in body, "要说明留空=不改，否则用户会填个空串把凭据抹了"
    assert "清除" in body, "要有显式清除的入口"


def test_page_explains_restart_is_needed(served):
    """保存≠生效。桥接是独立进程，这句不说用户一定会以为白改了。"""
    assert "重启" in _page(served[0])


def test_page_sends_the_token_so_save_works(served_page):
    """表单要能真的存下去 —— 依赖页面拿到会话令牌（由服务端注入）。"""
    addr, _app = served_page
    assert "__FREEAGENT_SESSION__" in _page(addr), "页面没被注入令牌，保存必然 401"


def test_saved_secret_never_appears_in_page_source(served_page):
    """**Secret 不能出现在页面源码的任何地方。**

    这是最容易出事的一条：Secret 一旦进了 HTML，它就进了浏览器缓存、
    页面截图、以及「查看源代码」—— 而掩码只在接口那层做了保护，
    前端写错一次 `innerHTML` 就全漏了。
    """
    addr, _app = served_page
    body = _page(addr)
    assert SECRET not in body


# --- 鉴权 --------------------------------------------------------------- #



def test_config_write_requires_a_token():
    """凭据是这轮最敏感的输入，没令牌一律拒。"""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        app = build_app(Path(tmp) / "a.db")
        server = create_server(app, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = "127.0.0.1", server.server_address[1]
            conn = HTTPConnection(host, port, timeout=5)
            payload = json.dumps({"FEISHU_APP_ID": "cli_x"}).encode()
            conn.request(
                "POST", "/api/feishu/config", body=payload,
                headers={"Content-Type": "application/json",
                         "Content-Length": str(len(payload))},
            )
            assert conn.getresponse().status == 401
            conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            app.close()
