"""锁住「日志是 UTF-8，且路径跟着库走」。

这个测试的存在理由是**一次真实误判**：日志里 9826 行合法 UTF-8 混着 4 行
GBK，而那 4 行恰好是两次真实点击。后果是排障时用 UTF-8 grep「卡片动作」
一条都搜不到，于是反过来断定「实机证据是假的」。

原来的实现（``basicConfig`` 只建 stderr handler，文件靠启动器重定向）
**任何测试都抓不住** —— 因为错不在函数返回值里，而在「谁把这个进程的标准
错误重定向到哪里、用什么编码写」这个**进程之外**的事实里。

所以守卫刻意分两层：

1. **结构层**（便宜、直接）：handler 里有 FileHandler、编码是 utf-8、
   路径跟着 ``--db`` 走。
2. **字节层**（真判据）：真往 handler 里写一条中文，再按 UTF-8 严格解码
   读回来 —— 读得通才算数。结构对了但字节错了（比如有人把 ``encoding``
   改成 ``locale``），只有这一层能抓到。
"""
import argparse
import contextlib
import locale
import logging

from freeagent.feishu import bridge as bridge_mod


def _args(db: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(db=db, verbose=False)


@contextlib.contextmanager
def _handlers(tmp_path, monkeypatch, db: str | None = None):
    """建 handler 并保证关掉。

    ``FileHandler`` 持有真实文件对象，测试里不关就是 ``ResourceWarning``，
    而本仓库把这类告警当失败（那是**对的** —— 资源泄漏就该拦住）。
    """
    monkeypatch.setenv("FREEAGENT_HOME", str(tmp_path))
    handlers = bridge_mod._log_handlers(_args(db))
    try:
        yield handlers
    finally:
        for h in handlers:
            h.close()


def _file_handlers(handlers):
    return [h for h in handlers if isinstance(h, logging.FileHandler)]


def _log_through(handlers, lines) -> None:
    """让这些行真的经过 handler 落盘。"""
    log = logging.getLogger("freeagent.test.编码")
    log.setLevel(logging.INFO)
    log.propagate = False
    for h in handlers:
        log.addHandler(h)
    try:
        for line in lines:
            log.info(line)
        for h in handlers:
            h.flush()
    finally:
        for h in list(log.handlers):
            log.removeHandler(h)


class TestLogPathFollowsDb:
    """路径与身份缓存、事件去重表同源（设计文档 11.9.5）。"""

    def test_default_uses_state_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FREEAGENT_HOME", str(tmp_path))
        assert bridge_mod._log_path(None) == tmp_path / "feishu.log"

    def test_follows_db_directory(self, tmp_path, monkeypatch):
        """``--db`` 指到哪儿，日志就跟到哪儿 —— 不然换了一套数据，日志却在别处。"""
        monkeypatch.setenv("FREEAGENT_HOME", str(tmp_path / "unused"))
        db_dir = tmp_path / "elsewhere"
        assert bridge_mod._log_path(str(db_dir / "agent.db")) == db_dir / "feishu.log"

    def test_is_not_reimplemented_path_parsing(self, tmp_path, monkeypatch):
        """必须是 ``state_home`` 那一套，不是自己拼 ``~/.freeagent``。

        复制粘贴的路径解析迟早漂移，而漂移的表现是「缓存写在 A、读取去 B 找」，
        永远命中不了且**没有任何报错**（见 ``state_home`` 的 docstring）。
        """
        monkeypatch.setenv("FREEAGENT_HOME", str(tmp_path))
        assert bridge_mod._log_path(None) == bridge_mod.state_home(None) / "feishu.log"


class TestHandlersAreUtf8:
    def test_includes_a_file_handler(self, tmp_path, monkeypatch):
        with _handlers(tmp_path, monkeypatch) as hs:
            assert _file_handlers(hs), "没有 FileHandler —— 又退回重定向了？"

    def test_file_handler_encoding_is_utf8(self, tmp_path, monkeypatch):
        """**显式** utf-8，不是 locale、不是 ``None``。

        写成 ``None`` 或 ``locale.getpreferredencoding()`` 都等于把这个 bug
        换个地方复现：换台机器、换个终端，字节又不一样了。
        """
        with _handlers(tmp_path, monkeypatch) as hs:
            files = _file_handlers(hs)
            assert files, "没有 FileHandler"
            enc = (files[0].encoding or "").lower()
            assert "utf" in enc, f"文件句柄编码是 {enc!r}，不是 utf-8"

    def test_stderr_kept(self, tmp_path, monkeypatch):
        """stderr 要留：控制台仍要看得到日志，文件是为了**事后**能查。"""
        with _handlers(tmp_path, monkeypatch) as hs:
            assert any(isinstance(h, logging.StreamHandler) for h in hs)


class TestChineseSurvivesAsUtf8Bytes:
    """字节层：真写一条中文，按 UTF-8 严格解码读回来。"""

    def test_round_trip_is_utf8(self, tmp_path, monkeypatch):
        with _handlers(tmp_path, monkeypatch) as hs:
            # 混一条纯 ASCII：只写中文可能侥幸全解得通，
            # 混入 ASCII 才更接近真实字节流。
            _log_through(
                hs,
                [
                    "【卡片动作】允许 凭据=ap-test 主题='只读列出目录 D:/x'",
                    "plain ascii line for good measure",
                ],
            )

        # 严格解码：解不了就是错，不接受 errors="replace" 蒙过去
        text = (tmp_path / "feishu.log").read_bytes().decode("utf-8")
        assert "【卡片动作】允许" in text, "中文按 UTF-8 读不出来"
        assert "ap-test" in text
        assert "plain ascii line" in text

    def test_every_line_is_valid_utf8(self, tmp_path, monkeypatch):
        """整份文件必须**每一行**都能严格 UTF-8 解码。

        对应真实的坑：4 行 GBK 混在 9826 行 UTF-8 里。**逐行**解（而不是
        整份解一次）才能定位到哪一行坏；整份解会直接抛，看不出是哪行。
        """
        with _handlers(tmp_path, monkeypatch) as hs:
            _log_through(
                hs,
                [f"第 {i} 行：【卡片动作】拒绝 没有访问任何东西。" for i in range(20)],
            )

        bad = []
        for i, line in enumerate((tmp_path / "feishu.log").read_bytes().split(b"\n"), 1):
            if not line:
                continue
            try:
                line.decode("utf-8")
            except UnicodeDecodeError:
                bad.append((i, line[:60]))
        assert not bad, f"有 {len(bad)} 行不是合法 UTF-8：{bad[:3]}"

    def test_locale_independent(self, tmp_path, monkeypatch):
        """**换 locale 也不变** —— 这正是原 bug 的形状。

        原实现靠 stderr 重定向，编码跟着启动 shell 的代码页走；中文
        Windows 的 cmd(936) 与 UTF-8 终端产出不同字节。这里逐个切 locale
        各写一次，锁住「与运行环境无关」。
        """
        applied = []
        for loc in ("Chinese (Simplified)_China.936", "C", ""):
            try:
                locale.setlocale(locale.LC_ALL, loc)
            except locale.Error:
                continue  # 这台机器没装该 locale，跳过
            applied.append(loc)
            with _handlers(tmp_path, monkeypatch) as hs:
                _log_through(hs, [f"locale={loc or 'C'} 卡片动作 允许"])
        assert applied, "一个 locale 都切不了，这台机器没法验这条"

        text = (tmp_path / "feishu.log").read_bytes().decode("utf-8")
        for loc in applied:
            assert f"locale={loc or 'C'} 卡片动作 允许" in text


class TestSupervisorDoesNotFightTheLogFile:
    """supervisor **不许**再把子进程输出重定向进桥接自己的日志文件。

    这是 11.9.5 那个 bug 的**另一半**，而且更容易被改回来 —— 因为删掉
    ``stdout=handle, stderr=STDOUT`` 看着像「少了个功能」。

    后果：``open(path, "ab")`` 是**字节原样落盘、不做编码转换**，写进去的
    是子进程 stderr 的原始编码（中文 Windows 的 cmd = GBK），与桥接自己
    写的 UTF-8 混在同一份文件里 —— 于是又变回「严格解码过不了、
    grep「卡片动作」搜不到」。

    判据不能是「读源码里没有某行字」，那太脆。真正要锁的是
    **Popen 实际收到了什么参数**。
    """

    def test_popen_does_not_redirect_into_log_file(self, tmp_path, monkeypatch):
        from freeagent.feishu import supervisor

        monkeypatch.setenv("FREEAGENT_HOME", str(tmp_path))
        monkeypatch.setattr(supervisor, "_preflight", lambda home=None: None)

        captured = {}

        class _FakePopen:
            """只提供 ``_do_start`` 与用例回收会用到的接口。

            ``kill`` / ``wait`` 不能少：``test_feishu_supervisor`` 的
            ``_clean_child`` 夹具会在 teardown 对 ``supervisor._child``
            调 ``poll()`` / ``kill()`` / ``wait()``。少一个就是
            ``AttributeError``，而且报错点在**别的**测试文件里，
            很容易误以为是自己的实现坏了。
            """

            def __init__(self, argv, **kwargs):
                captured["argv"] = argv
                captured["kwargs"] = kwargs

            def poll(self):
                return None  # 假装还活着，跳过「启动后立刻退出」那条分支

            def kill(self):
                return None

            def wait(self, timeout=None):
                return 0

        monkeypatch.setattr(supervisor.subprocess, "Popen", _FakePopen)
        monkeypatch.setattr(supervisor.time, "sleep", lambda _s: None)

        supervisor._do_start()

        # 先证明 Popen 真的被调到了 —— 否则下面「stdout 不在 kwargs 里」
        # 可能是「压根没走到那一步」而空转通过。
        assert "argv" in captured, "Popen 没被调用，这条守卫在空转"
        assert any("feishu.bridge" in str(a) for a in captured["argv"]), (
            f"抓到的 argv 不像桥接：{captured['argv']}"
        )

        kwargs = captured["kwargs"]
        for stream in ("stdout", "stderr"):
            assert stream not in kwargs, (
                f"Popen 收了 {stream}=... —— supervisor 又在抢桥接的日志文件了"
            )

    def test_bridge_still_logs_to_stderr(self, tmp_path, monkeypatch):
        """去掉重定向**不能**等于「日志没人看」。

        桥接的 ``basicConfig`` 仍然保留 ``StreamHandler()``，所以控制台 /
        调用方照样看得到全部日志 —— 有 ``TestHandlersAreUtf8::test_stderr_kept``
        钉住。这里只确认 supervisor 确实不再插手。
        """
        from freeagent.feishu import supervisor

        monkeypatch.setenv("FREEAGENT_HOME", str(tmp_path))
        monkeypatch.setattr(supervisor, "_preflight", lambda home=None: None)
        assert supervisor._log_path() == tmp_path / "feishu.log"


class TestUnwritableLogDoesNotKillBridge:
    """建不出日志文件**不许炸桥接** —— 为了写日志而拒绝启动是本末倒置。"""

    @staticmethod
    def _blocked(tmp_path, monkeypatch) -> str:
        """造一个「路径存在但不是目录」的场景 -> mkdir/FileHandler 必然失败。"""
        blocker = tmp_path / "blocked"
        blocker.write_text("not a directory", encoding="utf-8")
        monkeypatch.setenv("FREEAGENT_HOME", str(blocker))
        return str(blocker / "sub" / "agent.db")

    def test_falls_back_to_stderr_only(self, tmp_path, monkeypatch, capsys):
        db = self._blocked(tmp_path, monkeypatch)
        with _handlers(tmp_path, monkeypatch, db=db) as hs:
            assert hs, "至少要留 stderr"
            assert not _file_handlers(hs)
        assert "日志" in capsys.readouterr().err

    def test_never_raises(self, tmp_path, monkeypatch):
        db = self._blocked(tmp_path, monkeypatch)
        # 不该抛 —— 抛了就意味着「日志开不了 ⇒ 桥接起不来」
        with _handlers(tmp_path, monkeypatch, db=db) as hs:
            assert hs
