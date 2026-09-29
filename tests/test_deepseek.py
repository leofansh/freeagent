"""DeepSeek 智能层测试。**全程离线** —— transport 被打桩，一次网络请求都不发。

重点测三类东西：
1. 五个方法各自能正确发起调用并解析响应；
2. 异常路径（HTTP/超时/坏 JSON/空内容）都归一为 ``LLMError``，**且不泄露 key**；
3. **输出校验拦住模型编造** —— 角色名白名单、kind 白名单、草稿骨架修复。
"""

from __future__ import annotations

import json
from typing import Callable

import pytest

from freeagent.config import API_KEY_ENV, Config, load_config
from freeagent.domain import LLMError
from freeagent.services.llm import (
    RoleHint,
    RuleBasedProvider,
    SignalRef,
    TaskRef,
    build_provider,
)
from freeagent.services.llm.deepseek import (
    ALLOWED_KINDS,
    DeepSeekConfig,
    DeepSeekProvider,
    _repair_skeleton,
    _validate_classification,
)
from freeagent.services.llm.rules import ROLE_MATCH_THRESHOLD

SECRET = "sk-test-THIS-MUST-NEVER-APPEAR-IN-ERRORS"


def _reply(content: str) -> str:
    return json.dumps({"choices": [{"message": {"content": content}}]})


class StubTransport:
    """记录调用，按脚本返回响应。"""

    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, url, payload, headers, timeout) -> str:
        self.calls.append(
            {"url": url, "payload": dict(payload), "headers": dict(headers), "timeout": timeout}
        )
        if not self.responses:
            raise AssertionError("StubTransport 被调用次数超出预期")
        return self.responses.pop(0)

    @property
    def last_system(self) -> str:
        return self.calls[-1]["payload"]["messages"][0]["content"]

    @property
    def last_user(self) -> str:
        return self.calls[-1]["payload"]["messages"][1]["content"]


@pytest.fixture()
def provider() -> DeepSeekProvider:
    return DeepSeekProvider(DeepSeekConfig(), SECRET, transport=StubTransport())


def _p(*responses: str, fallback=None) -> tuple[DeepSeekProvider, StubTransport]:
    t = StubTransport(*responses)
    return DeepSeekProvider(DeepSeekConfig(), SECRET, transport=t, fallback=fallback), t


HINTS = [RoleHint("工作项目A", "销售 周报"), RoleHint("家庭", "孩子 学校")]


# =============================================================================
# 1. 五个方法的正常路径
# =============================================================================
class TestHappyPath:
    def test_classify_uses_json_mode(self):
        p, t = _p(
            _reply(
                '{"kind":"action","role_guesses":[{"role_name":"工作项目A","confidence":0.9}],'
                '"need_clarification":false,"clarifying_question":null}'
            )
        )
        r = p.classify("下周二交周报", HINTS)
        assert r.kind == "action"
        assert r.need_clarification is False
        assert r.role_guesses[0].role_name == "工作项目A"
        assert t.calls[0]["payload"]["response_format"] == {"type": "json_object"}
        assert t.calls[0]["url"].endswith("/chat/completions")

    def test_classify_passes_role_catalogue_in_prompt(self):
        p, t = _p(
            _reply(
                '{"kind":"action","role_guesses":[],"need_clarification":true,'
                '"clarifying_question":"哪个脉络？"}'
            )
        )
        p.classify("随便弄一下", HINTS)
        system = t.last_system
        assert "工作项目A" in system and "家庭" in system
        assert "不得自创" in system

    def test_key_travels_in_headers_not_payload(self):
        p, t = _p(_reply("标题"))
        p.refine_title("帮我记一下 周报")
        call = t.calls[0]
        assert call["headers"]["Authorization"] == f"Bearer {SECRET}"
        assert SECRET not in json.dumps(call["payload"], ensure_ascii=False), (
            "key 绝不能出现在 payload 里（payload 容易被日志打印）"
        )

    def test_refine_title(self):
        p, _ = _p(_reply("下周二要交的销售周报初稿"))
        assert p.refine_title("帮我记一下 下周二要交的销售周报初稿。") == (
            "下周二要交的销售周报初稿"
        )

    def test_refine_title_strips_quotes(self):
        p, _ = _p(_reply('"周报初稿"'))
        assert p.refine_title("周报") == "周报初稿"

    def test_refine_title_caps_length_in_code(self):
        """提示词里的字数只是建议，代码层必须兜住上限。"""
        from freeagent.services.llm.rules import TITLE_MAX_LEN

        long_title = "下周二要交的销售周报初稿，重点讲增长，数据在CRM，另外还要覆盖渠道表现与下周计划"
        assert len(long_title) > TITLE_MAX_LEN
        p, _ = _p(_reply(long_title))
        got = p.refine_title("随便")
        assert len(got) <= TITLE_MAX_LEN, got
        assert got != long_title

    def test_refine_title_cap_cuts_at_separator(self):
        from freeagent.services.llm.rules import TITLE_MAX_LEN

        long_title = "标题" + "很长" * 30 + "，尾巴在这里"
        p, _ = _p(_reply(long_title))
        got = p.refine_title("随便")
        assert len(got) <= TITLE_MAX_LEN
        assert "，" not in got and "," not in got, "应退到分隔符处切"

    def test_split_steps(self):
        p, _ = _p(_reply("- 收集数据\n- 写初稿\n- 找老板 review"))
        task = TaskRef(id="x", title="季度总结", intent="写一份总结")
        steps = p.split_steps(task)
        assert steps == ("收集数据", "写初稿", "找老板 review")

    def test_split_steps_strips_numbering(self):
        p, _ = _p(_reply("1. 第一步\n2) 第二步\n- 第三步"))
        assert p.split_steps(TaskRef(id="x", title="t")) == ("第一步", "第二步", "第三步")

    def test_split_steps_dedupes(self):
        p, _ = _p(_reply("- 同样的\n- 同样的\n- 另一个"))
        assert p.split_steps(TaskRef(id="x", title="t")) == ("同样的", "另一个")

    def test_split_steps_drops_headings(self):
        p, _ = _p(_reply("## 计划\n- 真的步骤\n\n## 备注"))
        assert p.split_steps(TaskRef(id="x", title="t")) == ("真的步骤",)

    def test_draft_keeps_skeleton(self):
        body = (
            "# 周报\n\n## 目标\n给出增长结论\n\n"
            "## 完成标准\n老板看得懂\n\n## 材料\n- [TODO] 数据来源\n"
        )
        p, _ = _p(_reply(body))
        task = TaskRef(id="x", title="周报", intent="给出增长结论")
        text = p.draft(task, "")
        for section in ("## 目标", "## 完成标准", "## 材料", "## 待确认"):
            assert section in text, f"缺 {section}"
        assert "[TODO]" in text

    def test_draft_strips_code_fence(self):
        p, _ = _p(_reply("```markdown\n# 周报\n\n## 目标\nx\n```"))
        text = p.draft(TaskRef(id="x", title="周报"), "")
        assert "```" not in text
        assert "## 目标" in text

    def test_suggest_schedule(self):
        p, _ = _p(_reply("这两条提示说明时间紧，但先做哪个你定。"))
        text = p.suggest_schedule(
            TaskRef(id="x", title="t"), [SignalRef("deadline_risk", 40, "临近截止")]
        )
        assert "你定" in text

    def test_config_endpoint_and_timeout_forwarded(self):
        cfg = DeepSeekConfig(model="m", base_url="https://h/v1", timeout=3.5)
        t = StubTransport(_reply("ok"))
        p = DeepSeekProvider(cfg, SECRET, transport=t)
        p.suggest_schedule(TaskRef(id="x", title="t"), [])
        assert t.calls[0]["timeout"] == 3.5
        assert t.calls[0]["url"] == "https://h/v1/chat/completions"
        assert t.calls[0]["payload"]["model"] == "m"


# =============================================================================
# 2. 输出校验：拦住模型编造
# =============================================================================
class TestOutputValidation:
    def test_invented_role_name_is_discarded(self):
        """模型自创角色名必须丢弃 —— 否则 CLI 会真的建出这个角色。"""
        payload = {
            "kind": "action",
            "role_guesses": [{"role_name": "不存在的脉络", "confidence": 0.99}],
            "need_clarification": False,
        }
        r = _validate_classification(payload, ["工作项目A", "家庭"])
        assert r.role_guesses == ()
        assert r.need_clarification is True
        assert r.clarifying_question

    def test_known_role_is_kept(self):
        payload = {
            "kind": "action",
            "role_guesses": [{"role_name": "家庭", "confidence": 0.8}],
            "need_clarification": False,
        }
        r = _validate_classification(payload, ["工作项目A", "家庭"])
        assert r.role_guesses[0].role_name == "家庭"
        assert r.need_clarification is False

    def test_bad_kind_is_rejected(self):
        with pytest.raises(LLMError, match="未知的 kind"):
            _validate_classification({"kind": "urgent"}, ["a"])

    def test_non_object_is_rejected(self):
        with pytest.raises(LLMError):
            _validate_classification(["not", "a", "dict"], ["a"])

    def test_confidence_is_clamped(self):
        payload = {
            "kind": "action",
            "role_guesses": [{"role_name": "a", "confidence": 5.0}],
        }
        r = _validate_classification(payload, ["a"])
        assert r.role_guesses[0].confidence == 1.0

    def test_low_confidence_forces_clarification(self):
        payload = {
            "kind": "action",
            "role_guesses": [{"role_name": "a", "confidence": 0.05}],
            "need_clarification": False,
        }
        r = _validate_classification(payload, ["a"])
        assert r.need_clarification is True

    def test_high_confidence_overrides_model_need_flag(self):
        payload = {
            "kind": "action",
            "role_guesses": [{"role_name": "a", "confidence": 0.9}],
            "need_clarification": True,
        }
        r = _validate_classification(payload, ["a"])
        assert r.need_clarification is False, "够自信就不该再追问"

    def test_missing_question_gets_generated(self):
        payload = {"kind": "action", "role_guesses": [], "need_clarification": True}
        r = _validate_classification(payload, ["a", "b"])
        assert r.clarifying_question and "？" in r.clarifying_question

    def test_two_candidates_question_names_both(self):
        payload = {
            "kind": "action",
            "role_guesses": [
                {"role_name": "a", "confidence": 0.2},
                {"role_name": "b", "confidence": 0.18},
            ],
            "need_clarification": True,
        }
        r = _validate_classification(payload, ["a", "b"])
        assert "a" in r.clarifying_question and "b" in r.clarifying_question

    def test_all_kinds_allowed(self):
        for kind in ALLOWED_KINDS:
            r = _validate_classification({"kind": kind, "role_guesses": []}, [])
            assert r.kind == kind

    def test_skeleton_repair_adds_missing_sections(self):
        task = TaskRef(id="x", title="周报", intent="做出来")
        text = _repair_skeleton("## 目标\n做出来", task, "")
        for section in ("## 目标", "## 完成标准", "## 材料", "## 待确认"):
            assert section in text

    def test_skeleton_repair_forces_title(self):
        task = TaskRef(id="x", title="周报", intent="做出来")
        assert _repair_skeleton("随便写点", task, "").startswith("# 周报")

    def test_skeleton_repair_adds_todo_when_none(self):
        """模型把待确认删了也要补回来 —— 那正是「我没信息」的标记。"""
        task = TaskRef(id="x", title="周报", intent="做出来", definition_of_done="能交")
        text = _repair_skeleton(
            "## 目标\n做出来\n\n## 完成标准\n能交\n\n## 材料\n- 数据\n\n## 待确认\n- 无",
            task,
            "",
        )
        assert "[TODO]" in text

    def test_skeleton_repair_includes_instruction(self):
        task = TaskRef(id="x", title="周报", intent="做出来")
        text = _repair_skeleton("## 目标\nx\n\n## 完成标准\ny\n\n## 材料\nz\n\n## 待确认\n- [TODO] w", task, "重点讲增长")
        assert "重点讲增长" in text

    def test_threshold_is_shared_with_rule_layer(self):
        assert ROLE_MATCH_THRESHOLD == 0.34


# =============================================================================
# 3. 异常路径
# =============================================================================
class TestErrorPaths:
    @pytest.mark.parametrize(
        "raw",
        [
            "not json at all",
            json.dumps({"choices": []}),
            json.dumps({"choices": [{"message": {}}]}),
            json.dumps({"choices": [{"message": {"content": "  "}}]}),
            json.dumps({"choices": [{"nomessage": 1}]}),
            json.dumps({"error": {"message": "quota exceeded"}}),
            json.dumps([1, 2, 3]),
        ],
    )
    def test_malformed_responses_become_llm_error(self, raw):
        p, _ = _p(raw)
        with pytest.raises(LLMError):
            p.refine_title("周报")

    def test_error_message_never_contains_key(self):
        p, _ = _p(json.dumps({"error": {"message": "bad"}}))
        with pytest.raises(LLMError) as exc:
            p.refine_title("周报")
        assert SECRET not in str(exc.value)

    def test_transport_exception_is_wrapped(self):
        def boom(url, payload, headers, timeout):
            raise RuntimeError("socket exploded")

        p = DeepSeekProvider(DeepSeekConfig(), SECRET, transport=boom)
        with pytest.raises(LLMError) as exc:
            p.refine_title("周报")
        assert "调用失败" in str(exc.value)

    def test_json_mode_refused_by_api_is_reported(self):
        p, _ = _p(json.dumps({"error": {"message": "json mode unsupported"}}))
        with pytest.raises(LLMError, match="json mode unsupported"):
            p.classify("周报", HINTS)

    def test_fenced_json_is_recovered(self):
        p, _ = _p(_reply('```json\n{"kind":"wait","role_guesses":[]}\n```'))
        assert p.classify("等回复", HINTS).kind == "wait"

    def test_json_with_preamble_is_recovered(self):
        p, _ = _p(_reply('好的，判断如下：\n{"kind":"action","role_guesses":[]}'))
        assert p.classify("周报", HINTS).kind == "action"

    def test_unparseable_json_is_reported(self):
        p, _ = _p(_reply("完全不是 JSON"))
        with pytest.raises(LLMError):
            p.classify("周报", HINTS)

    def test_empty_api_key_is_rejected_at_construction(self):
        with pytest.raises(LLMError, match="Key"):
            DeepSeekProvider(DeepSeekConfig(), "   ")


# =============================================================================
# 4. 降级
# =============================================================================
class TestDegradation:
    def test_falls_back_to_rule_layer(self):
        rules = RuleBasedProvider()
        p, _ = _p("garbage", fallback=rules)
        r = p.classify("下周二要交的销售周报初稿", HINTS)
        assert r.kind == "action", "应退回规则层的判断"
        assert p.degraded_reason is not None

    def test_degradation_reason_is_recorded_once_per_call(self):
        p, _ = _p("garbage", "garbage", fallback=RuleBasedProvider())
        p.classify("周报", HINTS)
        first = p.degraded_reason
        p.classify("周报", HINTS)
        assert p.degraded_reason == first

    def test_without_fallback_raises(self):
        p, _ = _p("garbage")
        with pytest.raises(LLMError):
            p.classify("周报", HINTS)

    @pytest.mark.parametrize("method,args", [
        ("refine_title", ("周报",)),
        ("split_steps", (TaskRef(id="x", title="t"),)),
        ("draft", (TaskRef(id="x", title="t"), "")),
        ("suggest_schedule", (TaskRef(id="x", title="t"), [])),
    ])
    def test_every_method_degrades(self, method, args):
        p, _ = _p("garbage", fallback=RuleBasedProvider())
        result = getattr(p, method)(*args)
        assert result  # 降级后仍有可用输出
        assert p.degraded_reason is not None

    def test_empty_title_degrades(self):
        p, _ = _p(_reply("   "), fallback=RuleBasedProvider())
        assert p.refine_title("帮我记一下 周报初稿") == "周报初稿"

    def test_unparseable_steps_degrade(self):
        p, _ = _p(_reply("没有任何可解析的行"), fallback=RuleBasedProvider())
        assert len(p.split_steps(TaskRef(id="x", title="t", intent="整一下"))) == 3

    def test_empty_suggestion_degrades(self):
        p, _ = _p(_reply("  "), fallback=RuleBasedProvider())
        assert "由你决定" in p.suggest_schedule(TaskRef(id="x", title="t"), [])


# =============================================================================
# 5. 配置：key 只从环境变量读
# =============================================================================
class TestConfig:
    def test_key_reads_only_from_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv(API_KEY_ENV, "sk-env")
        cfg = load_config(tmp_path)
        assert cfg.api_key == "sk-env"

    def test_no_key_is_none(self, monkeypatch, tmp_path):
        monkeypatch.delenv(API_KEY_ENV, raising=False)
        assert load_config(tmp_path).api_key is None

    def test_config_file_cannot_set_key(self, tmp_path, monkeypatch):
        """把 key 写进配置文件也不认 —— 秘密只走环境变量。"""
        monkeypatch.delenv(API_KEY_ENV, raising=False)
        (tmp_path).mkdir(parents=True, exist_ok=True)
        (tmp_path / "config.json").write_text(
            json.dumps({"api_key": "sk-from-file"}), encoding="utf-8"
        )
        assert load_config(tmp_path).api_key is None

    def test_describe_never_leaks_key(self, tmp_path, monkeypatch):
        monkeypatch.setenv(API_KEY_ENV, SECRET)
        described = load_config(tmp_path).describe()
        assert SECRET not in json.dumps(described, ensure_ascii=False)
        assert described["Key"] == "已配置"

    def test_model_from_file(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
        (tmp_path).mkdir(parents=True, exist_ok=True)
        (tmp_path / "config.json").write_text(
            json.dumps({"llm": {"model": "deepseek-reasoner"}}), encoding="utf-8"
        )
        assert load_config(tmp_path).model == "deepseek-reasoner"

    def test_env_overrides_file(self, tmp_path, monkeypatch):
        (tmp_path).mkdir(parents=True, exist_ok=True)
        (tmp_path / "config.json").write_text(
            json.dumps({"llm": {"model": "from-file"}}), encoding="utf-8"
        )
        monkeypatch.setenv("DEEPSEEK_MODEL", "from-env")
        assert load_config(tmp_path).model == "from-env"

    def test_bad_json_raises_validation_error(self, tmp_path, monkeypatch):
        (tmp_path).mkdir(parents=True, exist_ok=True)
        (tmp_path / "config.json").write_text("{oops", encoding="utf-8")
        with pytest.raises(Exception, match="JSON"):
            load_config(tmp_path)

    def test_non_object_config_rejected(self, tmp_path):
        (tmp_path).mkdir(parents=True, exist_ok=True)
        (tmp_path / "config.json").write_text("[1,2]", encoding="utf-8")
        with pytest.raises(Exception, match="对象"):
            load_config(tmp_path)

    def test_bad_timeout_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_TIMEOUT", "abc")
        with pytest.raises(Exception, match="数字"):
            load_config(tmp_path)

    def test_rules_only_flag_disables_llm(self, tmp_path, monkeypatch):
        monkeypatch.setenv(API_KEY_ENV, SECRET)
        monkeypatch.setenv("FREEAGENT_RULES_ONLY", "1")
        cfg = load_config(tmp_path)
        assert cfg.has_credentials is False

    def test_energy_windows_from_config(self, tmp_path, monkeypatch):
        (tmp_path).mkdir(parents=True, exist_ok=True)
        (tmp_path / "config.json").write_text(
            json.dumps({"energy_windows": {"evening": [19, 23]}}), encoding="utf-8"
        )
        cfg = load_config(tmp_path)
        assert cfg.energy_windows is not None
        assert cfg.energy_windows.evening == (19, 23)

    def test_energy_windows_true_enables_defaults(self, tmp_path, monkeypatch):
        (tmp_path).mkdir(parents=True, exist_ok=True)
        (tmp_path / "config.json").write_text(
            json.dumps({"energy_windows": True}), encoding="utf-8"
        )
        assert load_config(tmp_path).energy_windows is not None

    def test_energy_windows_absent_is_off(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_MODEL", "m")
        assert load_config(tmp_path).energy_windows is None

    def test_energy_windows_bad_shape_rejected(self, tmp_path):
        (tmp_path).mkdir(parents=True, exist_ok=True)
        (tmp_path / "config.json").write_text(
            json.dumps({"energy_windows": {"evening": [19]}}), encoding="utf-8"
        )
        with pytest.raises(Exception, match="evening"):
            load_config(tmp_path)

    def test_allow_fallback_from_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FREEAGENT_ALLOW_FALLBACK", "0")
        assert load_config(tmp_path).allow_fallback is False


# =============================================================================
# 6. 装配
# =============================================================================
class TestProviderSelection:
    def test_no_key_selects_rules(self, tmp_path, monkeypatch):
        monkeypatch.delenv(API_KEY_ENV, raising=False)
        monkeypatch.setenv("FREEAGENT_RULES_ONLY", "")
        assert isinstance(build_provider(load_config(tmp_path)), RuleBasedProvider)

    def test_key_selects_deepseek_with_fallback(self, tmp_path, monkeypatch):
        monkeypatch.setenv(API_KEY_ENV, SECRET)
        p = build_provider(load_config(tmp_path))
        assert isinstance(p, DeepSeekProvider)
        assert p._fallback is not None

    def test_fallback_disabled(self, tmp_path, monkeypatch):
        monkeypatch.setenv(API_KEY_ENV, SECRET)
        monkeypatch.setenv("FREEAGENT_ALLOW_FALLBACK", "0")
        assert build_provider(load_config(tmp_path))._fallback is None

    def test_missing_key_does_not_raise(self, tmp_path, monkeypatch):
        """没配 key 的终端必须照常能用，不能因为缺凭据就崩。"""
        monkeypatch.delenv(API_KEY_ENV, raising=False)
        build_provider(load_config(tmp_path))  # 不抛

    def test_deepseek_satisfies_protocol(self, provider):
        from freeagent.services.llm import LLMProvider

        assert isinstance(provider, LLMProvider)


# =============================================================================
# 7. 装配：配置必须真的生效
# =============================================================================
class TestConfigTakesEffect:
    """回归：`ENERGY_FIT` 曾是死代码（`main()` 不传 energy_windows）。"""

    def test_db_path_decides_where_config_is_read(self, tmp_path, monkeypatch):
        """`--db ./x/agent.db` 读 `./x/config.json`，而不是 ~/.freeagent。"""
        from freeagent.app import build_app

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "config.json").write_text(
            json.dumps({"energy_windows": True}), encoding="utf-8"
        )
        app = build_app(data_dir / "agent.db")
        try:
            assert app.energy_windows is not None, "配置目录应跟随数据库位置"
        finally:
            app.close()

    def test_config_beside_db_beats_home_config(self, tmp_path, monkeypatch):
        """库旁边的配置优先于 HOME 里的那份。"""
        from freeagent.app import build_app

        home_dir = tmp_path / "home"
        home_dir.mkdir()
        (home_dir / "config.json").write_text(
            json.dumps({"energy_windows": True}), encoding="utf-8"
        )
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "config.json").write_text(
            json.dumps({"energy_windows": False}), encoding="utf-8"
        )
        monkeypatch.setenv("FREEAGENT_HOME", str(home_dir))
        app = build_app(data_dir / "agent.db")
        try:
            assert app.energy_windows is None, "库旁的配置应覆盖 HOME 的"
        finally:
            app.close()

    def test_explicit_db_path_creates_missing_parent(self, tmp_path):
        """`--db ./不存在/agent.db` 不该崩。"""
        from freeagent.app import build_app

        target = tmp_path / "brand" / "new" / "agent.db"
        app = build_app(target)
        try:
            assert target.is_file()
        finally:
            app.close()

    def test_energy_fit_actually_fires_in_production_path(self, tmp_path, monkeypatch):
        """端到端：配置文件 → build_app → 排序信号真的命中。"""
        from datetime import date, datetime

        from freeagent.app import build_app
        from freeagent.domain import TaskKind
        from freeagent.services.clock import FrozenClock
        from freeagent.services.sorting import compute_signals

        (tmp_path / "config.json").write_text(
            json.dumps({"energy_windows": {"evening": [19, 23]}}), encoding="utf-8"
        )
        clock = FrozenClock(datetime(2026, 9, 26, 9))
        app = build_app(tmp_path / "agent.db", clock=clock)
        try:
            role = app.roles.create("跑腿杂项")
            t = app.tasks.create("晚上修窗户", [role.id], kind=TaskKind.REMINDER)
            app.tasks.set_reminder_time(t.id, datetime(2026, 9, 26, 20, 30))
            task = app.task_repo.get(t.id)
            codes = [
                s.code.value
                for s in compute_signals(
                    task, clock.now(), clock.today(), energy_windows=app.energy_windows
                )
            ]
            assert "energy_fit" in codes, codes
        finally:
            app.close()

    def test_no_config_means_energy_fit_off(self, tmp_path, monkeypatch):
        from freeagent.app import build_app

        monkeypatch.delenv(API_KEY_ENV, raising=False)
        app = build_app(tmp_path / "agent.db")
        try:
            assert app.energy_windows is None
        finally:
            app.close()

    def test_app_reports_which_provider(self, tmp_path, monkeypatch):
        from freeagent.app import build_app

        monkeypatch.delenv(API_KEY_ENV, raising=False)
        app = build_app(tmp_path / "agent.db")
        try:
            assert app.llm_name == "RuleBasedProvider"
        finally:
            app.close()
