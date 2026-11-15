"""凭据来源探针的守卫（设计文档 11.10.2 的「凭据」一列）。

## 为什么这几条断言是硬的

这一行的唯一价值是**让人少查半天**。它一旦说谎，人就会照着错的方向去改配置 ——
比「不显示这一行」坏得多。所以锁四件事：

1. **密钥值绝不出现**。提供方名与环境变量名可以打印，值是另一回事：
   名字能让人去核对配置，值落进日志 / 截图 / 工单就收不回来。
2. **「文件在、读不懂」不许返成「没登录」**。前者要人去核对文件或重登（可能新版
   换了结构），后者只说明没配 —— 合成一个，人就会去重登，而问题在别处。
3. **`none` 说的是「一次观察」**，不是「一定没有凭据」。存放位置随版本改，
   把观察写成断言，下一版本仓就开始说谎。
4. **测试不许依赖开发者本机**。文件名 / 提供方个数 / 环境变量全部注入 ——
   「本机碰巧有」的测试在别人机器上是另一种颜色，而那种红绿没人能解释。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from freeagent.services import executor_credentials as C


def _write_auth(home: Path, payload: object, *, sub: str = ".local/share/opencode") -> Path:
    """在 ``home`` 下造一个 ``auth.json``。"""
    p = home / sub / "auth.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# 五种情形
# --------------------------------------------------------------------------- #

class TestStates:
    def test_login_lists_providers_by_name(self, tmp_path):
        _write_auth(tmp_path, {
            "deepseek": {"type": "api", "key": "sk-1"},
            "openrouter": {"type": "api", "key": "sk-2"},
        })
        got = C.probe_opencode_credentials(env={}, home=tmp_path)
        assert got.state == C.LOGIN
        assert "2 个提供方" in got.detail
        assert "deepseek" in got.detail and "openrouter" in got.detail

    def test_oauth_style_entry_counts_as_configured(self, tmp_path):
        """``access``/``refresh`` 型（oauth 登录）也算配好了。

        只认 ``key`` 会把 oauth 用户报成「没登录」—— 而他们恰恰**刚刚**登过。
        """
        _write_auth(tmp_path, {"copilot": {"type": "oauth", "access": "a", "refresh": "r"}})
        got = C.probe_opencode_credentials(env={}, home=tmp_path)
        assert got.state == C.LOGIN
        assert "copilot" in got.detail

    def test_env_only_when_no_auth_file(self, tmp_path):
        got = C.probe_opencode_credentials(env={"OPENAI_API_KEY": "sk-x"}, home=tmp_path)
        assert got.state == C.ENV_ONLY
        assert "OPENAI_API_KEY" in got.detail

    def test_both_says_it_does_not_judge_precedence(self, tmp_path):
        """两处都有时**不判断**谁生效 —— 那是执行器的内部规则。

        写死一个优先级，下一版它改了，本仓就开始给错答案；而这条答案会被人
        拿去决定「该删哪一个」。
        """
        _write_auth(tmp_path, {"deepseek": {"type": "api", "key": "sk-1"}})
        got = C.probe_opencode_credentials(
            env={"ANTHROPIC_API_KEY": "sk-y"}, home=tmp_path)
        assert got.state == C.BOTH
        assert "不判断" in got.detail

    def test_neither_is_an_observation_not_a_verdict(self, tmp_path):
        got = C.probe_opencode_credentials(env={}, home=tmp_path)
        assert got.state == C.NONE
        assert "不是断言" in got.detail, "观察不许写成结论"
        # 试过的位置要列出来，人才能自己核对「它到底找的是哪」
        # 列的是**目录**：文件路径本身就以 auth.json 结尾，再补一句「没有 auth.json」
        # 会读成「没有 X/…/X」
        assert ".local/share/opencode" in got.detail
        # 出路要给
        assert "opencode auth login" in got.detail

    def test_empty_auth_file_differs_from_missing_one(self, tmp_path):
        """文件在、里面没东西 —— 与「文件都没有」是两种话。

        前者说明人装过 opencode 但没登录；后者说明连目录都没有（可能换了路径）。
        都归到 `none` 没错，但 detail 必须说清是哪种，否则人去删文件或去改路径。
        """
        _write_auth(tmp_path, {})
        got = C.probe_opencode_credentials(env={}, home=tmp_path)
        assert got.state == C.NONE
        assert "在，但里面没有可用提供方" in got.detail

    def test_broken_json_is_unknown_not_none(self, tmp_path):
        """**这一条是整组里最要紧的。** 读不懂 ≠ 没登录。"""
        p = tmp_path / ".local/share/opencode/auth.json"
        p.parent.mkdir(parents=True)
        p.write_text("{ this is not json", encoding="utf-8")
        got = C.probe_opencode_credentials(env={}, home=tmp_path)
        assert got.state == C.UNKNOWN, "读不懂不许返成「没登录」"
        assert "别按「没登录」处理" in got.detail

    def test_non_object_json_is_unknown(self, tmp_path):
        _write_auth(tmp_path, ["not", "a", "dict"])
        assert C.probe_opencode_credentials(env={}, home=tmp_path).state == C.UNKNOWN

    def test_untested_renders_nothing(self):
        """没探针的执行器（hermes）**不编一行「不知道」**。"""
        assert C.render_credential(C.UNTESTED) == ""


# --------------------------------------------------------------------------- #
# 不泄密
# --------------------------------------------------------------------------- #

class TestNoSecretLeak:
    def test_key_value_never_appears(self, tmp_path):
        secret = "sk-DO-NOT-LEAK-0001"
        _write_auth(tmp_path, {"deepseek": {"type": "api", "key": secret}})
        got = C.probe_opencode_credentials(env={}, home=tmp_path)
        assert got.state == C.LOGIN
        assert secret not in got.detail
        assert secret not in C.render_credential(got.state, got.detail)

    def test_env_value_never_appears(self, tmp_path):
        secret = "sk-DO-NOT-LEAK-0002"
        got = C.probe_opencode_credentials(env={"DEEPSEEK_API_KEY": secret}, home=tmp_path)
        assert got.state == C.ENV_ONLY
        assert secret not in got.detail
        assert "DEEPSEEK_API_KEY" in got.detail, "名字要留 —— 人要拿它去核对"

    def test_both_leak_nothing(self, tmp_path):
        _write_auth(tmp_path, {"deepseek": {"type": "api", "key": "sk-A"}})
        got = C.probe_opencode_credentials(
            env={"OPENROUTER_API_KEY": "sk-B"}, home=tmp_path)
        assert "sk-A" not in got.detail and "sk-B" not in got.detail


# --------------------------------------------------------------------------- #
# 环境变量按名字形状扫
# --------------------------------------------------------------------------- #

class TestEnvScan:
    def test_only_names_ending_in_api_key_count(self, tmp_path):
        """开关名 ``..._API_KEY_HELPER_DISABLED`` 不算凭据。

        按「包含 api_key」扫会把它算进来 —— 于是「有凭据」而其实没有。
        形状匹配必须按**结尾**判（11.9.11：错判的代价远大于漏判）。
        """
        got = C.probe_opencode_credentials(
            env={"CODEBUDDY_API_KEY_HELPER_DISABLED": "1"}, home=tmp_path)
        assert got.state == C.NONE

    def test_empty_value_does_not_count(self, tmp_path):
        """设成空串 = 没设。它在进程里存在，但拿它去调模型必然失败。"""
        got = C.probe_opencode_credentials(env={"OPENAI_API_KEY": "   "}, home=tmp_path)
        assert got.state == C.NONE


# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #

class TestPaths:
    def test_xdg_data_home_is_tried(self, tmp_path):
        """设了 ``XDG_DATA_HOME`` 就去那儿找。

        只看默认路径的风险是「设了但本仓没认」→ 报「没看到凭据」而其实有，
        那是错判方向。
        """
        xdg = tmp_path / "xdg"
        _write_auth(xdg, {"deepseek": {"type": "api", "key": "sk-1"}}, sub="opencode")
        got = C.probe_opencode_credentials(
            env={"XDG_DATA_HOME": str(xdg)}, home=tmp_path / "home")
        assert got.state == C.LOGIN

    def test_home_shown_as_tilde(self, tmp_path):
        """输出去掉家目录绝对路径 —— 截图 / 工单里不必带用户名。"""
        _write_auth(tmp_path, {"deepseek": {"type": "api", "key": "sk-1"}})
        got = C.probe_opencode_credentials(env={}, home=tmp_path)
        assert "~/.local/share/opencode/auth.json" in got.detail


# --------------------------------------------------------------------------- #
# 呈现
# --------------------------------------------------------------------------- #

class TestRender:
    @pytest.mark.parametrize("state,label", [
        (C.LOGIN, "登录态"),
        (C.ENV_ONLY, "只有环境变量"),
        (C.BOTH, "两处都有"),
        (C.NONE, "两处都没看到"),
        (C.UNKNOWN, "认不出"),
    ])
    def test_every_state_has_its_own_label(self, state, label):
        """五种情形五种话 —— 合成一句「凭据状态未知」等于让人自己猜。"""
        assert label in C.render_credential(state, "细节")

    def test_detail_is_appended(self):
        assert C.render_credential(C.LOGIN, "细节") == "凭据来源：登录态（`auth.json`） —— 细节"

    def test_no_detail_is_not_a_dangling_dash(self):
        assert C.render_credential(C.LOGIN, "") == "凭据来源：登录态（`auth.json`）"
