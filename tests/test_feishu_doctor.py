"""飞书通道自检（doctor）。

最该锁住的三条：

1. **默认不联网** —— 「查一下配置」不该产生网络请求
2. **每项要说清「缺了会怎样」**，而不是只列变量名
3. **绝不打印 secret 内容** ——  doctor 的输出会被贴进 issue/聊天
"""

from __future__ import annotations

import pytest

from freeagent.feishu.config import (
    ENV_ALLOWED,
    ENV_APP_ID,
    ENV_APP_SECRET,
    ENV_DOMAIN,
    ENV_POLL,
)
from freeagent.feishu.doctor import (
    Check,
    check_config,
    check_database,
    check_sdk,
    format_report,
    run_checks,
)

GOOD_ENV = {
    ENV_APP_ID: "cli_x",
    # 必须是 **32 位**：doctor 现在会校验 Secret 长度（用来识别「粘贴被截断」）。
    # 踩过的坑：一开始这里写的是 19 位的值，于是「全部通过」那个用例
    # 因为长度不对被判成致命、返回 1 —— 失败的是夹具，不是被测代码。
    # 保留可辨识子串，好让「绝不打印 Secret」那两个用例仍有意义。
    ENV_APP_SECRET: "sk-NEVER-PRINT-THIS-secret-12345",
    ENV_ALLOWED: "ou_alice,ou_bob",
    ENV_DOMAIN: "feishu",
    ENV_POLL: "60",
}


def _by_name(checks):
    return {c.name: c for c in checks}


class TestSdkCheck:
    def test_reports_installed_or_not(self):
        c = check_sdk()
        assert isinstance(c, Check)
        assert c.name == "飞书 SDK"

    def test_missing_sdk_gives_install_command(self, monkeypatch):
        """没装 SDK 是最常见的首次失败，必须直接给出安装命令。"""
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *a, **kw):
            if name == "lark_oapi":
                raise ImportError("没装")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        c = check_sdk()
        assert c.ok is False
        assert c.fatal is True
        assert 'pip install ".[feishu]"' in c.detail


class TestConfigChecks:
    def test_all_present(self):
        from freeagent.feishu.config import load_config

        got = _by_name(check_config(load_config(GOOD_ENV)))
        assert got[ENV_APP_ID].ok
        assert got[ENV_APP_SECRET].ok
        assert got[ENV_ALLOWED].ok
        assert got[ENV_DOMAIN].ok

    def test_missing_secret_is_fatal_and_says_why(self):
        from freeagent.feishu.config import load_config

        env = {**GOOD_ENV}
        del env[ENV_APP_SECRET]
        c = _by_name(check_config(load_config(env)))[ENV_APP_SECRET]
        assert c.ok is False and c.fatal is True
        assert "只从环境变量读" in c.detail, "要说清正确的填法"

    def test_empty_allowlist_is_fatal(self):
        from freeagent.feishu.config import load_config

        c = _by_name(check_config(load_config({**GOOD_ENV, ENV_ALLOWED: ""})))[
            ENV_ALLOWED
        ]
        assert c.ok is False and c.fatal is True

    def test_bad_domain_is_fatal(self):
        from freeagent.feishu.config import load_config

        c = _by_name(check_config(load_config({**GOOD_ENV, ENV_DOMAIN: "slack"})))[
            ENV_DOMAIN
        ]
        assert c.ok is False and c.fatal is True
        assert "feishu" in c.detail and "lark" in c.detail

    def test_secret_never_appears_in_detail(self):
        from freeagent.feishu.config import load_config

        secret = "sk-super-secret-value"
        checks = check_config(load_config({**GOOD_ENV, ENV_APP_SECRET: secret}))
        blob = "\n".join(c.detail for c in checks)
        assert secret not in blob, "doctor 的输出会被贴进 issue，secret 不能出现"

    def test_secret_also_absent_from_full_report(self, tmp_path):
        secret = "sk-another-secret"
        report = format_report(run_checks(db_path=tmp_path / "d.db",
                                          env={**GOOD_ENV, ENV_APP_SECRET: secret}))
        assert secret not in report

    def test_secret_length_checked(self):
        """**回归**：长度不对要能立刻指出来。

        粘贴被截断时，接口只回一句没法定位的「app secret invalid」——
        用户会以为密钥过期，反复去后台重置，而真正原因是少粘了几个字符。
        长度不对是唯一能当场识别的信号。
        """
        from freeagent.feishu.config import load_config
        from freeagent.feishu.doctor import SECRET_LENGTH

        def probe(n: int):
            # 先把 env 备好，再传进去 —— 别把条件表达式塞进调用参数里，
            # 那样括号会错配（我第一版就这么写炸了）。
            env = dict(GOOD_ENV)
            if n:
                env[ENV_APP_SECRET] = "x" * n
            else:
                env.pop(ENV_APP_SECRET, None)
            got = {c.name: c for c in check_config(load_config(env))}
            return got.get("App Secret 长度")

        assert SECRET_LENGTH == 32
        good = probe(32)
        assert good is not None and good.ok and not good.fatal
        for bad_n in (20, 31, 33):
            c = probe(bad_n)
            assert c is not None and not c.ok and c.fatal, (
                f"{bad_n} 位应当被判为致命问题"
            )
            assert "粘贴" in c.detail, "要说清是粘贴问题，而非密钥过期"

    def test_length_check_never_prints_the_secret(self):
        from freeagent.feishu.config import load_config

        secret = "y" * 20
        got = {c.name: c for c in check_config(
            load_config({**GOOD_ENV, ENV_APP_SECRET: secret})
        )}
        assert secret not in got["App Secret 长度"].detail

    def test_no_length_check_when_secret_absent(self):
        """没填 secret 时不该报「长度 0 位」—— 那是另一条错误。"""
        from freeagent.feishu.config import load_config

        env = {k: v for k, v in GOOD_ENV.items() if k != ENV_APP_SECRET}
        names = {c.name for c in check_config(load_config(env))}
        assert "App Secret 长度" not in names


class TestDatabaseCheck:
    def test_reports_schema_and_count(self, tmp_path):
        c = check_database(tmp_path / "d.db")
        assert c.ok, f"新库应正常：{c.detail}"
        assert "schema" in c.detail

    def test_unopenable_db_is_fatal(self, tmp_path):
        # 用**普通文件**当目录组件，mkdir 才会失败。
        # 踩过的坑：一开始拿一个已存在的目录当障碍，可 build_app 会
        # ``parent.mkdir(parents=True, exist_ok=True)`` —— 目录本来就在，
        # 于是照样建成了库，测试却以为「打不开」。
        blocker = tmp_path / "blocker.txt"
        blocker.write_text("我是个文件", encoding="utf-8")
        c = check_database(blocker / "x.db")
        assert c.ok is False
        assert c.fatal is True


class TestRunChecksOffline:
    def test_default_does_not_touch_network(self, tmp_path, monkeypatch):
        """**回归**：「查一下配置」不该发网络请求。

        踩过的坑：doctor 一开始就顺手换 token 验凭据 —— 于是每次自检
        都会打飞书接口，既可能撞限流，也让「只想看看配置填对没」
        变成一个有副作用的动作。
        """
        import freeagent.feishu.sender as sender_mod

        def boom(*a, **kw):
            raise AssertionError("默认自检不许联网")

        monkeypatch.setattr(sender_mod, "urllib_transport", boom)
        checks = run_checks(db_path=tmp_path / "d.db", env=GOOD_ENV)
        assert any(c.name.startswith("凭据") for c in checks) is False

    def test_live_adds_token_check(self, tmp_path, monkeypatch):
        import freeagent.feishu.sender as sender_mod

        def fake(url, payload, headers, timeout):
            return '{"code":0,"msg":"ok","tenant_access_token":"t-x","expire":7200}'

        monkeypatch.setattr(sender_mod, "urllib_transport", fake)
        checks = run_checks(db_path=tmp_path / "d.db", env=GOOD_ENV, live=True)
        assert any(c.name.startswith("凭据") for c in checks), "--live 才该加凭据检查"

    def test_live_skipped_when_no_credentials(self, tmp_path):
        env = {**GOOD_ENV}
        del env[ENV_APP_ID]
        checks = run_checks(db_path=tmp_path / "d.db", env=env, live=True)
        assert not any(c.name.startswith("凭据") for c in checks), (
            "没凭据就别去联网试了"
        )

    def test_bad_config_env_still_lists_each_item(self, tmp_path):
        """不能只丢一句「配置有问题」—— 用户得知道缺哪几个。"""
        checks = run_checks(db_path=tmp_path / "d.db", env={})
        names = {c.name for c in checks}
        assert ENV_APP_ID in names and ENV_ALLOWED in names
        assert any(c.fatal and not c.ok for c in checks)


class TestReport:
    def test_marks_each_state(self):
        text = format_report([
            Check("好的", True, "没问题"),
            Check("坏的", False, "有问题", fatal=True),
            Check("将就", False, "注意"),
        ])
        assert "[OK  ]" in text
        assert "[致命]" in text
        assert "[警告]" in text
        assert "1 项致命问题" in text

    def test_all_good_gives_start_hint(self):
        text = format_report([Check("好的", True, "没问题")])
        assert "python -m freeagent.feishu.bridge" in text

    def test_no_fatal_no_warning_wording(self):
        text = format_report([Check("将就", False, "注意")])
        assert "配置可用" in text


class TestDoctorCli:
    def test_exit_code_reflects_fatal(self, tmp_path, capsys):
        from freeagent.feishu.doctor import main

        code = main(["--db", str(tmp_path / "d.db")])
        assert code == 1, "有致命问题应退 1"

    def test_exit_zero_when_good(self, tmp_path, monkeypatch, capsys):
        from freeagent.feishu import doctor

        # 必须设环境变量：doctor 默认读 os.environ，不设的话配置必然致命。
        # 踩过的坑：这条一开始只 monkeypatch 了 check_sdk 就期望退 0 ——
        # 结果退 1，因为真正的阻塞是「没配 FEISHU_*」，跟 SDK 无关。
        for k, v in GOOD_ENV.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setattr(
            doctor, "check_sdk", lambda: Check("飞书 SDK", True, "已安装"),
        )
        code = doctor.main(["--db", str(tmp_path / "d.db")])
        assert code == 0
        out = capsys.readouterr().out
        assert "配置可用" in out

    def test_module_run_actually_invokes_main(self, tmp_path):
        """``-m`` 入口不能是哑的（真出过这个 bug：守卫漏了，退出 0 且无输出）。"""
        import os
        import subprocess
        import sys

        env = {k: v for k, v in os.environ.items() if not k.startswith("FEISHU_")}
        env.update({"PYTHONPATH": "src", "PYTHONIOENCODING": "utf-8"})
        p = subprocess.run(
            [sys.executable, "-m", "freeagent.feishu.doctor", "--db",
             str(tmp_path / "d.db")],
            env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )
        assert p.returncode == 1, f"缺配置应退 1，实际 {p.returncode}"
        assert "飞书通道自检" in p.stdout
