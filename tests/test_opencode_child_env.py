"""子进程环境白名单的守卫。

## 为什么这些测试重要

改之前是 ``env = dict(os.environ)`` —— **全量继承**。改之后是白名单。
那么「白名单到底挡住了什么」必须**可断言**，否则下一次有人为了方便
再加一行 ``env.update(os.environ)``，没人会发现已经退回原点。

所以这里不只测「该有的有」，**更测「该没的没有」**：故意在父进程里放一批
看起来很危险但与「调模型」无关的变量（``GITHUB_TOKEN`` /
``AWS_SECRET_ACCESS_KEY`` / ``SSH_AUTH_SOCK`` …），断言它们**一个都不进**。

## 刻意不参数化常量

第一版写成 ``@pytest.mark.parametrize("name", sorted(_JAILED_ENV))``。
那是**同义反复** —— 断言「代码符合那个常量自己」，常量写错了照样通过。
所以改成下面两个**显式名单**：它们是「我认为该被关进 jail / 该被放行的名字」，
常量与实现哪个跑偏都会被抓到。

顺带一个实测到的坑：``from freeagent.services.opencode_server import _私有名``
在 **pytest 收集期**会 ImportError（同一个名字在函数体内 import 正常、
普通 python 也正常；公有名两种情况都正常）。所以本文件只 import **公有名**。

## 判据

``build_child_env`` 是纯函数，直接对比它构造出的 dict；
「真起得起来吗」由 ``tools/`` 下的真机探针负责，单元测试不重复验那件事。
"""
from __future__ import annotations

import os
import pathlib
from typing import Any

import pytest

from freeagent.services.opencode_server import (
    build_child_env,
    child_env_visibility,
)

#: 期望**被关进 jail** 的名字。显式写出来，不从实现里取 —— 见模块 docstring。
EXPECTED_JAILED = (
    "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME",
    "TMPDIR", "TEMP", "TMP",
)

#: 期望**放行**的 OS 必需变量。``SYSTEMROOT`` 尤其关键：缺了 Winsock 都初始化不了。
EXPECTED_OS = ("SYSTEMROOT", "COMSPEC", "PATHEXT")

#: 与「让 opencode 调模型」**无关**的凭据。全部不该进子进程。
UNRELATED_SECRETS = (
    "GITHUB_TOKEN", "GH_TOKEN", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "AZURE_CLIENT_SECRET", "SSH_AUTH_SOCK", "NPM_TOKEN", "DOCKER_PASSWORD",
    "KUBECONFIG", "GOOGLE_APPLICATION_CREDENTIALS",
)


@pytest.fixture
def fake_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """在父进程里铺一批变量：OS 必需 + 模型凭据 + 无关凭据 + 随便一个。"""
    for name in EXPECTED_OS:
        monkeypatch.setenv(name, f"val-{name}")
    monkeypatch.setenv("NUMBER_OF_PROCESSORS", "8")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-secret")
    for name in UNRELATED_SECRETS:
        monkeypatch.setenv(name, f"leak-{name}")
    monkeypatch.setenv("TOTALLY_UNRELATED", "leak-plain")


def _child(monkeypatch: pytest.MonkeyPatch, **kw: Any) -> dict[str, str]:
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    return build_child_env(jail=pathlib.Path(r"C:\jail"), **kw)


class TestInheritsNothing:
    def test_unrelated_secrets_do_not_pass(self, fake_env, monkeypatch):
        """**核心断言**：与调模型无关的凭据一个都不进子进程。"""
        env = _child(monkeypatch)
        leaked = [n for n in UNRELATED_SECRETS if n in env]
        assert leaked == [], f"泄漏：{leaked}"

    def test_unrelated_plain_var_does_not_pass(self, fake_env, monkeypatch):
        assert "TOTALLY_UNRELATED" not in _child(monkeypatch)

    def test_child_only_adds_jail_names_and_drops_a_lot(self, fake_env, monkeypatch):
        """子进程**只该新增 jail 那几个变量名**，且必须**确实少了一批**。

        第一版写成 ``set(child) < set(parent)``（真子集），**是错的**：
        Windows 上父环境本来没有 ``HOME`` / ``XDG_*``，而子进程**正该**把它们
        建出来 —— 那就是「关进 jail」这件事本身。所以「严格子集」这个断言
        会把正确的实现判成错的。

        精确的说法是两条：
        - **新增的**只能就是 jail 名单（多一个都是泄漏面）
        - **少掉的**必须非空（一个都没少就等于还在全量继承）
        """
        child = set(_child(monkeypatch))
        parent = set(os.environ)
        assert child - parent <= set(EXPECTED_JAILED), \
            f"新增了不该有的变量：{sorted(child - parent)}"
        assert parent - child, "一个变量都没少 —— 白名单没生效？"

    def test_before_and_after_contrast_is_real(self, fake_env, monkeypatch):
        """把「改前」显式算出来对照 —— 证明这次改动不是空转。

        改前 = 父环境全继承；改后 = 白名单。两者可见集合必须**真的不同**。
        """
        before = set(os.environ)
        after = set(_child(monkeypatch))
        assert before - after, "白名单与全量继承的差集为空 —— 改动没生效？"


class TestJail:
    @pytest.mark.parametrize("name", EXPECTED_JAILED)
    def test_every_home_like_var_points_at_the_jail(self, name, fake_env, monkeypatch):
        """家目录 / 配置 / 缓存 / 临时目录**全部**指向一次性目录。

        改之前只改了 ``XDG_CONFIG_HOME``，于是配置隔开了但数据与缓存仍写回
        真实家目录，而 ``HOME``/``USERPROFILE`` 让 opencode 能读 ``~/.ssh``。
        """
        env = _child(monkeypatch)
        assert env.get(name) == str(pathlib.Path(r"C:\jail")), name

    def test_home_is_not_the_real_home(self, fake_env, monkeypatch):
        real = os.environ.get("USERPROFILE")
        if real:
            assert _child(monkeypatch)["USERPROFILE"] != real

    def test_temp_is_overridden_even_though_parent_has_it(self, fake_env, monkeypatch):
        """父环境里本来就有 ``TEMP``/``TMP``（Windows 一定有）——
        必须被 jail 覆盖掉，而不是「父环境有就沿用」。"""
        env = _child(monkeypatch)
        assert env["TEMP"] != os.environ.get("TEMP")
        assert env["TMP"] != os.environ.get("TMP")


class TestPath:
    def test_path_is_an_allowlist(self, fake_env, monkeypatch):
        env = _child(monkeypatch, exe=r"C:\tools\opencode.exe")
        assert env["PATH"] != os.environ.get("PATH")
        assert r"C:\tools" in env["PATH"]

    def test_path_contains_system_dirs(self, fake_env, monkeypatch):
        assert "System32" in _child(monkeypatch, exe=r"C:\tools\opencode.exe")["PATH"]

    def test_no_exe_means_no_leading_empty_segment(self, fake_env, monkeypatch):
        """没给 exe 时不该以空段开头 —— 空段在 Windows 上等于「当前目录」。"""
        assert not _child(monkeypatch)["PATH"].startswith(os.pathsep)


class TestModelCredentials:
    def test_model_key_passes_through(self, fake_env, monkeypatch):
        """模型凭据**必须**放行 —— 配置里不写 provider/key。

        挡掉它，委派会直接 401/402（实测：探针里 deepseek 靠环境里的 key 跑通）。
        """
        env = _child(monkeypatch)
        assert env["DEEPSEEK_API_KEY"] == "sk-deepseek-secret"
        assert env["OPENAI_API_KEY"] == "sk-openai-secret"

    def test_absent_key_is_simply_absent(self, fake_env, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        assert "DEEPSEEK_API_KEY" not in _child(monkeypatch)

    def test_credential_names_are_overridable(self, fake_env, monkeypatch):
        env = _child(monkeypatch, credential_names=["MY_MODEL_KEY"])
        assert "MY_MODEL_KEY" not in env        # 父环境里没有
        assert "DEEPSEEK_API_KEY" not in env    # 覆盖后不放行默认那批


class TestOsRequired:
    @pytest.mark.parametrize("name", EXPECTED_OS)
    def test_os_required_actually_passes(self, name, fake_env, monkeypatch):
        """这些**缺了服务就起不来**，不是「不安全」而是「不工作」。"""
        assert name in _child(monkeypatch), name

    def test_absent_in_parent_stays_absent(self, monkeypatch):
        monkeypatch.delenv("SYSTEMROOT", raising=False)
        assert "SYSTEMROOT" not in _child(monkeypatch)


class TestExtra:
    def test_extra_is_applied(self, fake_env, monkeypatch):
        env = _child(monkeypatch, extra={"OPENCODE_SERVER_PASSWORD": "pw"})
        assert env["OPENCODE_SERVER_PASSWORD"] == "pw"

    def test_extra_can_override(self, fake_env, monkeypatch):
        """``extra`` 优先级最高 —— 密码与用户名必须压得住任何同名项。"""
        assert _child(monkeypatch, extra={"PATH": r"C:\only"})["PATH"] == r"C:\only"

    def test_user_extra_reaches_the_child(self, fake_env, monkeypatch):
        assert _child(monkeypatch, extra={"CUSTOM": "v"})["CUSTOM"] == "v"


class TestVisibilityReport:
    def test_report_names_but_not_values(self, fake_env, monkeypatch):
        """审计输出**只给名字**。凭据的值不进日志。"""
        rep = child_env_visibility(
            _child(monkeypatch), secret_names=["DEEPSEEK_API_KEY"])
        assert "sk-deepseek-secret" not in repr(rep)
        assert "DEEPSEEK_API_KEY" in repr(rep)

    def test_report_lists_visible_names(self, fake_env, monkeypatch):
        env = _child(monkeypatch)
        rep = child_env_visibility(env)
        assert set(rep["可见变量"]) == set(env)
        assert set(rep["其中非空"]) == {k for k, v in env.items() if v != ""}

    def test_report_marks_empty_credentials(self, fake_env, monkeypatch):
        env = _child(monkeypatch)
        env["DEEPSEEK_API_KEY"] = ""
        rep = child_env_visibility(env, secret_names=["DEEPSEEK_API_KEY"])
        assert "DEEPSEEK_API_KEY（空）" in rep["凭据类（已隐去值）"]
