"""多 provider 支持的测试。

覆盖四条边界，缺一条就会以「很难查」的方式坏掉：

1. **注册表** —— 加了 provider 忘了加 Key 白名单 → 界面保存成功、重启后 Key 没了。
2. **优先级** —— 环境变量 vs ``llm.env``。写反了，用户改了界面却不生效。
3. **空串 ≠ 清空** —— 空串当清空，保存一次别的设置就把 Key 抹了。
4. **不泄密** —— 明文绝不出现在响应里，也绝不写进 ``config.json``。
"""

from __future__ import annotations

import json

import pytest

from freeagent import llm_env
from freeagent.config import (
    API_KEY_ENV,
    DEFAULT_PROVIDER,
    Config,
    ValidationError,
    config_from_settings,
    load_config,
    resolve_provider,
    save_config,
)
from freeagent.secrets_file import UnknownKeyError
from freeagent.services.llm import build_provider
from freeagent.services.llm.providers import (
    PROVIDERS,
    all_key_env_vars,
    get_profile,
)

# 每个 provider 的 Key 变量名都要真的存在，否则界面写进去的东西读不出来
ENV_FOR = {p.id: p.key_env_var for p in PROVIDERS}


@pytest.fixture(autouse=True)
def _no_real_keys(monkeypatch):
    """把**所有** provider 的变量清掉。

    只清 DeepSeek 那一个是不够的：切到 Ollama 的测试会意外读到用户真机上
    那个 OLLAMA_API_KEY，于是「没配 Key 时应该怎么表现」这类断言全部失效，
    而且只在开发者自己机器上绿 —— 那是最坏的测试。
    """
    for name in all_key_env_vars():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("FREEAGENT_LLM_PROVIDER", raising=False)


# --- 1. 注册表 -------------------------------------------------------------- #


class TestRegistry:
    def test_every_provider_has_a_key_slot(self):
        """白名单必须是**推导**出来的，且覆盖全部 provider。"""
        assert all_key_env_vars() == {p.key_env_var for p in PROVIDERS}
        assert all_key_env_vars() <= llm_env.ALLOWED_KEYS

    def test_ids_are_unique(self):
        ids = [p.id for p in PROVIDERS]
        assert len(ids) == len(set(ids)), "有 provider 重名了"

    def test_unknown_returns_none_not_raise(self):
        """调用方要能如实显示「不认识这个」，而不是 500。"""
        assert get_profile("nope") is None
        assert get_profile("") is None

    def test_unknown_in_config_raises_loudly(self):
        """但配置文件里写了不认识的就**必须报错**。

        静默回落的表现是「下拉里选着 Kimi、请求却打到了 DeepSeek」，
        而界面上看不出任何异常 —— 那类问题排查成本极高。
        """
        with pytest.raises(ValidationError) as ei:
            resolve_provider("claude")
        assert "claude" in str(ei.value)

    def test_blank_falls_back_to_default(self):
        """老配置没有这个键 —— 它必须回到 DeepSeek，不能报错。"""
        assert resolve_provider(None) == DEFAULT_PROVIDER
        assert resolve_provider("  ") == DEFAULT_PROVIDER

    def test_keyless_provider_declares_it(self):
        # 先断言取到了档案再取属性：get_profile 返回 Optional，直接
        # `.needs_key` 在档案不存在时会抛 AttributeError —— 那种失败
        # 读起来像「代码坏了」，而实际只是「这家没注册」。
        ollama = get_profile("ollama")
        assert ollama is not None
        assert ollama.needs_key is False
        deepseek = get_profile("deepseek")
        assert deepseek is not None
        assert deepseek.needs_key is True


# --- 2. 优先级：环境变量 > llm.env > 未配 ----------------------------------- #


class TestKeyPrecedence:
    def test_not_configured(self, tmp_path):
        key, source = llm_env.resolve_key(API_KEY_ENV, home=tmp_path)
        assert key is None
        assert source.label == "未配置"

    def test_file_is_used(self, tmp_path):
        llm_env.write_env({API_KEY_ENV: "sk-file"}, home=tmp_path)
        key, source = llm_env.resolve_key(API_KEY_ENV, home=tmp_path)
        assert key == "sk-file"
        assert "llm.env" in source.label

    def test_env_beats_file(self, tmp_path, monkeypatch):
        llm_env.write_env({API_KEY_ENV: "sk-file"}, home=tmp_path)
        monkeypatch.setenv(API_KEY_ENV, "sk-env")
        key, source = llm_env.resolve_key(API_KEY_ENV, home=tmp_path)
        assert key == "sk-env", "环境变量没有压过文件"
        assert source.is_env, "必须说得出是哪份在生效，否则用户改错地方"

    def test_config_takes_it_from_env_not_its_own_json(self, tmp_path):
        cfg = Config(home=str(tmp_path), model="m")
        assert cfg.api_key is None
        assert cfg.key_source == "未配置"

    def test_keyless_provider_needs_no_key(self, tmp_path):
        """Ollama 没配 Key 也算「有凭据」——否则永远退回规则层且毫无提示。"""
        cfg = Config(home=str(tmp_path), provider="ollama")
        assert cfg.api_key is None
        assert cfg.has_credentials is True

    def test_cloud_provider_without_key_is_credsless(self, tmp_path):
        assert Config(home=str(tmp_path), provider="kimi").has_credentials is False


# --- 3. 密钥文件本身 -------------------------------------------------------- #


class TestSecretFile:
    def test_rejects_unknown_key(self, tmp_path):
        """白名单是安全边界，不能因为「多写一个键方便」就开口子。"""
        with pytest.raises(UnknownKeyError):
            llm_env.write_env({"PYTHONPATH": "C:/evil"}, home=tmp_path)

    def test_rejects_non_ascii_credential(self, tmp_path):
        """肉眼分不出的全角/零宽字符会让服务端只回 401，必须当场拦。"""
        with pytest.raises(ValueError) as ei:
            llm_env.write_env({API_KEY_ENV: "sk-abc\u3000def"}, home=tmp_path)
        assert API_KEY_ENV in str(ei.value)

    def test_empty_value_deletes_the_line(self, tmp_path):
        llm_env.write_env({API_KEY_ENV: "sk-1"}, home=tmp_path)
        llm_env.write_env({API_KEY_ENV: ""}, home=tmp_path)
        assert API_KEY_ENV not in llm_env.read_env(tmp_path)

    def test_unmentioned_keys_survive(self, tmp_path):
        """存 A 的 Key 不能把 B 的抹了 —— 所有 provider 共存于一个文件。"""
        llm_env.write_env({"DEEPSEEK_API_KEY": "sk-d"}, home=tmp_path)
        llm_env.write_env({"MOONSHOT_API_KEY": "sk-m"}, home=tmp_path)
        got = llm_env.read_env(tmp_path)
        assert got["DEEPSEEK_API_KEY"] == "sk-d"
        assert got["MOONSHOT_API_KEY"] == "sk-m"

    def test_survives_concatenated_and_placeholder(self, tmp_path):
        """两种已知损坏形态必须自愈：粘连行、被打断的 ``***``。"""
        path = tmp_path / "llm.env"
        path.write_text(
            "DEEPSEEK_API_KEY=***MOONSHOT_API_KEY=sk-ok\n", encoding="utf-8"
        )
        got = llm_env.read_env(tmp_path)
        assert got == {"MOONSHOT_API_KEY": "sk-ok"}

    def test_reads_bom_file(self, tmp_path):
        """PowerShell 写出的 .env 带 BOM；读不出来会**静默**变成「没配」。"""
        (tmp_path / "llm.env").write_text(
            "DEEPSEEK_API_KEY=sk-bom\n", encoding="utf-8-sig"
        )
        assert llm_env.read_env(tmp_path)["DEEPSEEK_API_KEY"] == "sk-bom"

    def test_mask_never_shows_the_middle(self):
        from freeagent.secrets_file import mask_secret

        # 刻意**不**用 sk- 加长串：仓库里有个守卫测试专门扫
        # ``sk-[A-Za-z0-9]{16,}``（防真 key 硬编码进源码），写成那样会让它红。
        # 掩码要验的只是「头尾各露 4 位、中间不露」，与前缀长什么样无关。
        secret = "sk-abcd1234-efgh5678"
        masked = mask_secret(secret)
        assert "bcd1234" not in masked, "掩码漏了中段"
        assert "efgh" not in masked, "掩码漏了中段"
        assert masked == "sk-a…5678"
        # 短的不能因为头尾重叠而全露
        assert mask_secret("abcdefgh") == "****"
        assert mask_secret("") == ""


# --- 4. 配置读写 ------------------------------------------------------------ #


class TestConfigProvider:
    def test_old_config_without_provider_still_loads(self, tmp_path):
        """升级不能弄坏已有用户的 config.json。"""
        (tmp_path / "config.json").write_text(
            json.dumps({"llm": {"model": "deepseek-chat"}}), encoding="utf-8"
        )
        cfg = load_config(tmp_path)
        assert cfg.provider == "deepseek"
        assert cfg.model == "deepseek-chat"

    def test_provider_is_persisted(self, tmp_path):
        save_config(Config(home=str(tmp_path), provider="kimi"), home=tmp_path)
        raw = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert raw["llm"]["provider"] == "kimi"

    def test_settings_accepts_provider(self, tmp_path):
        out = config_from_settings(
            {"provider": "qwen"}, base=Config(home=str(tmp_path))
        )
        assert out.provider == "qwen"

    def test_settings_rejects_unknown_provider(self, tmp_path):
        with pytest.raises(ValidationError):
            config_from_settings(
                {"provider": "gemini"}, base=Config(home=str(tmp_path))
            )

    def test_custom_requires_base_url(self, tmp_path):
        """漏填地址的报错必须发生在**保存时**，不是几百毫秒后的网络错误。"""
        with pytest.raises(ValidationError) as ei:
            config_from_settings(
                {"provider": "custom", "base_url": "", "model": "m"},
                base=Config(home=str(tmp_path)),
            )
        assert "接口地址" in str(ei.value)

    def test_saving_settings_keeps_delegate_allowlist(self, tmp_path):
        """回归：原来存一次设置就会把委派白名单清空。

        空 projects = 委派关闭，而那份白名单被文档称作整条委派链路
        最硬的安全约束 —— 界面上存个模型名就把它抹掉，不能接受。
        """
        base = Config(
            home=str(tmp_path),
            delegate_projects=("D:/code/keepme",),
            delegate_model="opencode/big-pickle",
        )
        out = config_from_settings({"model": "new-model"}, base=base)
        assert out.delegate_projects == ("D:/code/keepme",)
        assert out.delegate_model == "opencode/big-pickle"


# --- 5. Key 落盘（端点侧） ------------------------------------------------- #


class TestKeyWrite:
    def test_blank_leaves_key_alone(self, tmp_path):
        """空串 = 不改。输入框每次加载都空着，改个模型顺手保存很常见。"""
        from freeagent.web.llm_settings import split_key_fields

        llm_env.write_env({API_KEY_ENV: "sk-keep"}, home=tmp_path)
        base = Config(home=str(tmp_path))
        settings, pending = split_key_fields(
            {"api_key": "", "model": "m"}, base
        )
        assert pending is None
        assert settings == {"model": "m"}
        assert llm_env.read_env(tmp_path)[API_KEY_ENV] == "sk-keep"

    def test_typed_key_goes_to_selected_provider_slot(self, tmp_path):
        """换了 provider 又填 Key，要落到**那家**的变量名下。

        否则切回去会发现「没配」，而用户明明刚填过。
        """
        from freeagent.web.llm_settings import split_key_fields

        base = Config(home=str(tmp_path))
        _settings, pending = split_key_fields(
            {"provider": "kimi", "api_key": "sk-moonshot"}, base
        )
        assert pending is not None
        pending.commit()
        got = llm_env.read_env(tmp_path)
        assert got["MOONSHOT_API_KEY"] == "sk-moonshot"
        assert "DEEPSEEK_API_KEY" not in got

    def test_clear_and_fill_are_mutually_exclusive(self, tmp_path):
        from freeagent.web.llm_settings import split_key_fields

        with pytest.raises(Exception):
            split_key_fields(
                {"api_key": "sk-x", "clear_api_key": True},
                Config(home=str(tmp_path)),
            )

    def test_api_key_never_reaches_config_json(self, tmp_path, monkeypatch):
        """非秘密配置里绝不能出现 Key —— 它会被同步、会被提交。"""
        monkeypatch.setenv(API_KEY_ENV, "sk-secret-value")
        cfg = load_config(tmp_path)
        save_config(cfg, home=tmp_path)
        assert "sk-secret-value" not in (tmp_path / "config.json").read_text(
            encoding="utf-8"
        )
        assert "sk-secret-value" not in json.dumps(cfg.describe(), ensure_ascii=False)


# --- 6. build_provider ----------------------------------------------------- #


class TestBuildProvider:
    def test_keyless_provider_still_builds(self, tmp_path):
        """Ollama 没有 Key 也要能建出 provider，否则永远退回规则层。"""
        cfg = Config(home=str(tmp_path), provider="ollama",
                     base_url="http://127.0.0.1:11434/v1", model="qwen3:8b")
        built = build_provider(cfg)
        assert built.__class__.__name__ == "DeepSeekProvider"

    def test_no_credentials_returns_rules(self, tmp_path):
        built = build_provider(Config(home=str(tmp_path), provider="kimi"))
        assert built.__class__.__name__ == "RuleBasedProvider"

    def test_degradation_names_the_real_provider(self, tmp_path, monkeypatch):
        """用 Kimi 出错却报「deepseek 调用失败」会把人引到错的账单和限流上。"""
        from freeagent.services.llm.deepseek import DeepSeekConfig, DeepSeekProvider
        from freeagent.services.llm.rules import RuleBasedProvider

        p = DeepSeekProvider(
            DeepSeekConfig(model="m", base_url="http://x/v1", name="Kimi / 月之暗面"),
            "sk-1",
            # 必须给 fallback：没有它时 _degrade 会**原样抛出**，不会记下原因。
            fallback=RuleBasedProvider(),
        )
        p._degrade(RuntimeError("boom"))
        # degraded_reason 是 Optional：有 fallback 且刚降过级时必然有值，
        # 但断言它非空本身也是有意义的 —— 少了这句，None 会在下面的
        # `in` 上抛 TypeError，而那个报错看不出「没记下原因」。
        reason = p.degraded_reason
        assert reason is not None, "降级了却没记下原因 —— 用户会看不到任何提示"
        assert "Kimi" in reason
        assert "deepseek" not in reason.lower()
