"""凭据落盘的安全测试（设计方案 12.7）。

**白名单那几条是安全测试，不是功能测试** —— 它们失败意味着开了个
RCE 入口，所以断言写得比平常更死。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from freeagent.feishu.secret_store import (
    ALLOWED_KEYS,
    UnknownKeyError,
    env_path,
    mask_secret,
    read_env,
    write_env,
)


# --- 白名单：这是安全边界 ------------------------------------------------- #
class TestWhitelistIsASecurityBoundary:
    @pytest.mark.parametrize("dangerous", [
        "PYTHONPATH",     # 下次起子进程在 main 之前加载攻击者代码
        "LD_PRELOAD",     # 同上，Linux 动态链接器
        "DYLD_INSERT_LIBRARIES",
        "NODE_OPTIONS",   # Node 解释器
        "EDITOR",         # 值错 = 下次 $EDITOR 时 RCE
        "BROWSER", "VISUAL", "PAGER", "SHELL",
        "PATH",           # 太宽，不该由界面改
        "GIT_SSH_COMMAND",
    ])
    def test_dangerous_names_are_rejected(self, tmp_path, dangerous):
        """这些名字**形状完全合法**，所以只有白名单能挡住它们。"""
        assert dangerous.isidentifier() or dangerous.replace("_", "").isalnum()
        with pytest.raises(UnknownKeyError):
            write_env({dangerous: "x"}, tmp_path)

    def test_rejection_message_lists_supported_keys(self, tmp_path):
        """报错要说清支持什么 —— 否则用户不知道怎么改。"""
        with pytest.raises(UnknownKeyError) as ei:
            write_env({"PYTHONPATH": "x"}, tmp_path)
        assert "FEISHU_APP_ID" in str(ei.value)

    def test_unknown_key_is_not_silently_dropped(self, tmp_path):
        """静默忽略比报错更糟：界面会说「已保存」，实际没写。"""
        try:
            write_env({"NOPE": "1"}, tmp_path)
        except UnknownKeyError:
            pass
        else:
            pytest.fail("未知 key 必须抛错，不能静默忽略")

    def test_allowed_set_is_exactly_five(self):
        assert ALLOWED_KEYS == {
            "FEISHU_APP_ID", "FEISHU_APP_SECRET",
            "FEISHU_ALLOWED_USERS", "FEISHU_DOMAIN", "FEISHU_LOCK_PORT",
        }

    def test_other_projects_keys_are_still_rejected(self, tmp_path):
        """连「相邻项目的合法变量」也不放行 —— 不认识就是不认识。"""
        with pytest.raises(UnknownKeyError):
            write_env({"HERMES_APP_SECRET": "x"}, tmp_path)


# --- 读盘容错 ------------------------------------------------------------- #
class TestReadIsForgiving:
    def test_missing_file_is_empty_not_error(self, tmp_path):
        assert read_env(tmp_path) == {}

    def test_bom_does_not_hide_first_key(self, tmp_path):
        """**Windows 特有的坑**：PowerShell 写的 .env 带 BOM。

        用 utf-8 读，第一个变量名会变成 ``\\ufeffFEISHU_APP_ID``，
        于是「配置写了但桥接当没有」，且**没有任何报错**。
        """
        p = env_path(tmp_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\xef\xbb\xbfFEISHU_APP_ID=cli_x\n")
        got = read_env(tmp_path)
        assert got.get("FEISHU_APP_ID") == "cli_x", (
            f"BOM 吃掉了变量名，实际读到：{got!r}"
        )

    def test_concatenated_pairs_are_split(self, tmp_path):
        """两对 KEY=VALUE 粘在一行（中间少了换行）必须能自愈。"""
        p = env_path(tmp_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            "FEISHU_APP_ID=cli_xFEISHU_DOMAIN=lark\n", encoding="utf-8")
        got = read_env(tmp_path)
        assert got.get("FEISHU_APP_ID") == "cli_x"
        assert got.get("FEISHU_DOMAIN") == "lark"

    def test_placeholder_counts_as_unset(self, tmp_path):
        """被打断的写入会留 ``KEY=***`` —— 界面必须显示「未配置」。"""
        p = env_path(tmp_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("FEISHU_APP_SECRET=***\n", encoding="utf-8")
        assert "FEISHU_APP_SECRET" not in read_env(tmp_path)

    def test_unknown_keys_in_file_are_ignored_not_removed(self, tmp_path):
        """别人写的变量我们不认，但**不该替他删掉**。"""
        p = env_path(tmp_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("SOMEONE_ELSE=1\nFEISHU_APP_ID=cli_x\n", encoding="utf-8")
        assert read_env(tmp_path).get("FEISHU_APP_ID") == "cli_x"
        write_env({"FEISHU_DOMAIN": "feishu"}, tmp_path)
        assert "SOMEONE_ELSE=1" in p.read_text(encoding="utf-8")

    def test_value_with_equals_sign_survives(self, tmp_path):
        """值里含 ``=`` 不该被当成新键切开。"""
        p = env_path(tmp_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("FEISHU_APP_SECRET=ab=cd=ef\n", encoding="utf-8")
        assert read_env(tmp_path).get("FEISHU_APP_SECRET") == "ab=cd=ef"

    def test_comments_and_blanks_survive(self, tmp_path):
        write_env({"FEISHU_APP_ID": "cli_x"}, tmp_path)
        p = env_path(tmp_path)
        p.write_text("# 我加的注释\n\n" + p.read_text(encoding="utf-8"),
                     encoding="utf-8")
        write_env({"FEISHU_DOMAIN": "lark"}, tmp_path)
        assert "我加的注释" in p.read_text(encoding="utf-8")


# --- 写盘 ----------------------------------------------------------------- #
class TestWrite:
    def test_round_trip(self, tmp_path):
        write_env({"FEISHU_APP_ID": "cli_x", "FEISHU_APP_SECRET": "s3cr3t"}, tmp_path)
        got = read_env(tmp_path)
        assert got["FEISHU_APP_ID"] == "cli_x"
        assert got["FEISHU_APP_SECRET"] == "s3cr3t"

    def test_partial_update_keeps_others(self, tmp_path):
        write_env({"FEISHU_APP_ID": "cli_x", "FEISHU_DOMAIN": "lark"}, tmp_path)
        write_env({"FEISHU_DOMAIN": "feishu"}, tmp_path)
        got = read_env(tmp_path)
        assert got["FEISHU_APP_ID"] == "cli_x", "没提到的键不该被抹掉"
        assert got["FEISHU_DOMAIN"] == "feishu"

    def test_empty_value_is_a_delete(self, tmp_path):
        """清空 = 删掉这个键，而不是写一个空值。"""
        write_env({"FEISHU_DOMAIN": "lark"}, tmp_path)
        write_env({"FEISHU_DOMAIN": ""}, tmp_path)
        assert "FEISHU_DOMAIN" not in read_env(tmp_path)

    def test_no_half_written_file(self, tmp_path):
        write_env({"FEISHU_APP_ID": "cli_x"}, tmp_path)
        leftovers = list(env_path(tmp_path).parent.glob("feishu_env_*"))
        assert not leftovers, f"留下了半截临时文件：{leftovers}"

    def test_creates_parent_dir(self, tmp_path):
        deep = tmp_path / "a" / "b"
        write_env({"FEISHU_APP_ID": "cli_x"}, deep)
        assert read_env(deep).get("FEISHU_APP_ID") == "cli_x"

    def test_repeated_writes_do_not_duplicate(self, tmp_path):
        """重复写不该让同一个键出现多行（Hermes 为此专门写了清洗）。"""
        for _ in range(4):
            write_env({"FEISHU_APP_ID": "cli_x"}, tmp_path)
        text = env_path(tmp_path).read_text(encoding="utf-8")
        assert text.count("FEISHU_APP_ID=") == 1, f"键重复了：\n{text}"


# --- 掩码：界面唯一能看到的东西 --------------------------------------------- #
class TestMasking:
    def test_shows_head_and_tail(self):
        assert mask_secret("kRZ6KfmGmpfN6SvkHISh5fHKC4XVKyNa") == "kRZ6…KyNa"

    def test_short_value_is_never_revealed(self):
        """短值取头尾会重叠 = 全露。宁可只说「已设置」。"""
        for v in ("abc", "12345678", "123456789"):
            out = mask_secret(v)
            assert v not in out or out == "****", f"短值泄露了：{v!r} -> {out!r}"

    def test_empty_stays_empty(self):
        assert mask_secret("") == ""

    def test_no_endpoint_returns_plaintext(self):
        """整个存储层不该有任何函数把明文交给界面。"""
        import freeagent.feishu.secret_store as m
        assert not any(
            "reveal" in n.lower() or "plaintext" in n.lower()
            for n in dir(m)
        ), "出现了回传明文的入口"
