"""设置页智能层那段 JS 的**运行期**测试：在 node 里用 DOM 替身真跑一遍。

## 为什么要有这个测试

``node --check`` 只验语法。它抓不到「变量是 ``null`` 却去取它的属性」这类
运行期错误 —— 而那会让设置页**整页白屏**（``renderSettings`` 中途中断，
root 停在半成品状态）。这个仓库被白屏咬过两次（``BANDS`` 缺失、
``clearBox.parentElement``），所以这次在 Python 侧也钉一道。

真在浏览器里点是最强的验证，但开发机/CI 未必有 Playwright，所以这里退一步：
用最小 DOM 替身在 node 里跑。抓不到样式与布局，但**能抓崩溃与取值错误** ——
而那正是白屏的两大来源。

没有 node 就跳过，不让核心包的「零依赖」测试在缺 node 的机器上假红。
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from freeagent.web.js_settings_llm import JS_SETTINGS_LLM

node = pytest.mark.skipif(
    shutil.which("node") is None, reason="没装 node，跳过 JS 运行期测试"
)

#: 最小 DOM 替身 + JS 需要的全局。``el``/``esc``/``api`` 由 js_core 提供，
#: 这里重实现一份 —— 那个文件太大，不该为了这个测试整份加载进来。
_SHIM = """
function makeNode(tag) {
  return {
    tagName: tag, className: "", innerHTML: "", textContent: "",
    value: "", type: "", disabled: false, placeholder: "", checked: false,
    style: {}, children: [], attrs: {},
    appendChild(c) { this.children.push(c); c.parentElement = this; return c; },
    setAttribute(k, v) { this.attrs[k] = v; },
    getAttribute(k) { return this.attrs[k]; },
  };
}
const ROOT = makeNode("root");
globalThis.document = {
  createElement: makeNode,
  querySelector: () => makeNode("q"),
};
const el = (t, c, h) => { const n = makeNode(t); n.className = c || ""; n.innerHTML = h == null ? "" : h; return n; };
const esc = (s) => String(s == null ? "" : s);
const api = async () => ({ ok: true, detail: "stub", models: ["m1"] });
const toast = () => {};
const refreshHeader = async () => {};
function findNode(node, tag) {
  if (node.tagName === tag) return node;
  for (const c of node.children || []) {
    const hit = findNode(c, tag);
    if (hit) return hit;
  }
  return null;
}
"""

#: 驱动：跑几条形态，把结果以 JSON 打到 stdout 供 Python 断言。
_DRIVER = """
const render = renderLlmSection;
const out = {};

function scenario(key, payload) {
  const r = render(payload);
  const sel = findNode(r.node, "select");
  out[key] = { read: r.read(), hasSelect: !!sel, _sel: sel, _r: r };
}

// 1) 已配 Key 的云厂商
scenario("cloud", PAYLOAD.cloud);
// 2) 不需要 Key 的本机服务
scenario("keyless", PAYLOAD.keyless);
// 3) 需要手填地址的 custom
scenario("custom", PAYLOAD.custom);
// 4) 只注册了一家的极端情况
scenario("single", PAYLOAD.single);
// 5) providers 为空（后端异常时的降级，必须不崩）
scenario("empty", PAYLOAD.empty);

// 6) 真的切一次：云厂商 -> custom
const sel = out.cloud._sel;
sel.value = "custom";
sel.onchange();
out.switched = out.cloud._r.read();

// 7) 切换后再切到本机服务
sel.value = "ollama";
sel.onchange();
out.switched2 = out.cloud._r.read();

// 8) 勾了「清空 Key」。
//    必须**另起一个干净实例**：上面 6/7 把 cloud 那个实例的下拉拨成了
//    ollama，而 ollama 是 needs_key=false 的 provider —— read() 会（正确地）
//    根本不下发任何 Key 字段。在那个实例上断言 clear_api_key，等于在
//    「本来就不该发 Key」的场合要求它发，读起来会像是代码坏了。
scenario("clear", PAYLOAD.cloud);
for (const n of walkInputs(out.clear._r.node)) {
  if (n.type === "checkbox") { n.checked = true; break; }
}
out.cleared = out.clear._r.read();

function walkInputs(node) {
  const acc = [];
  for (const c of node.children || []) { acc.push(c); acc.push(...walkInputs(c)); }
  return acc;
}

const clean = (o) => JSON.parse(JSON.stringify(o, (k, v) => (k.startsWith("_") ? undefined : v)));
console.log(JSON.stringify(clean(out)));
"""


def _payloads() -> str:
    """各形态的 ``/api/settings`` payload。"""
    deepseek = {
        "id": "deepseek", "label": "DeepSeek", "needs_key": True,
        "key_configured": True, "key_masked": "sk-a…wxyz",
        "key_source": "文件 llm.env 的 DEEPSEEK_API_KEY",
        "key_shadowed_by_env": True, "key_env_var": "DEEPSEEK_API_KEY",
        "default_base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-chat", "models": ["deepseek-chat"],
        "requires_base_url": False, "note": "",
    }
    ollama = {
        "id": "ollama", "label": "Ollama", "needs_key": False,
        "key_configured": False, "key_masked": "", "key_source": "未配置",
        "key_shadowed_by_env": False, "key_env_var": "OLLAMA_API_KEY",
        "default_base_url": "http://127.0.0.1:11434/v1",
        "default_model": "qwen3:8b", "models": [],
        "requires_base_url": False, "note": "",
    }
    custom = {
        "id": "custom", "label": "其它", "needs_key": True,
        "key_configured": False, "key_masked": "", "key_source": "未配置",
        "key_shadowed_by_env": False, "key_env_var": "OPENAI_COMPATIBLE_API_KEY",
        "default_base_url": "", "default_model": "", "models": [],
        "requires_base_url": True, "note": "",
    }
    base = {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "config_path": "C:/Users/x/.freeagent/config.json",
    }
    return json.dumps({
        "cloud": {**base, "provider": "deepseek",
                  "providers": [deepseek, ollama, custom]},
        "keyless": {**base, "provider": "ollama",
                    "providers": [deepseek, ollama, custom]},
        "custom": {**base, "provider": "custom",
                   "providers": [deepseek, ollama, custom]},
        "single": {**base, "provider": "deepseek", "providers": [deepseek]},
        "empty": {**base, "provider": "deepseek", "providers": []},
    })


@node
def test_all_shapes_render_without_throwing(tmp_path):
    """任何一家的形态都不能把 renderLlmSection 弄崩。"""
    script = tmp_path / "h.mjs"
    script.write_text(
        f"{_SHIM}\nconst PAYLOAD = {_payloads()};\n{JS_SETTINGS_LLM}\n{_DRIVER}",
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, f"JS 崩了：\n{proc.stderr}"
    out = json.loads(proc.stdout)

    # 每种形态都得把服务商下拉渲染出来
    for key in ("cloud", "keyless", "custom", "single", "empty"):
        assert out[key]["hasSelect"], f"{key} 形态没渲染出服务商下拉"


@node
def test_switching_provider_does_not_carry_previous_values(tmp_path):
    """回归：切到「其它兼容服务」**不能**带上一家的地址和模型。

    带过去的症状很隐蔽：用户选「其它兼容服务」，看到框里已经填着
    DeepSeek 的地址和模型名，以为是自己配的，一保存就成了
    「自定义端点 = DeepSeek」—— 正是「界面说一套、实际做另一套」。
    """
    script = tmp_path / "h.mjs"
    script.write_text(
        f"{_SHIM}\nconst PAYLOAD = {_payloads()};\n{JS_SETTINGS_LLM}\n{_DRIVER}",
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, f"JS 崩了：\n{proc.stderr}"
    out = json.loads(proc.stdout)

    switched = out["switched"]
    assert switched["provider"] == "custom"
    assert "deepseek" not in (switched["base_url"] or "").lower(), (
        f"切到 custom 仍带着上一家的地址：{switched}"
    )
    assert "deepseek" not in (switched["model"] or "").lower(), (
        f"切到 custom 仍带着上一家的模型名：{switched}"
    )
    # 留空是**故意的**：服务端会明确拦下并要求填地址，比带个错值强。
    assert switched["base_url"] == ""
    assert switched["model"] == ""

    # 切到本机服务应该用它自己的默认值
    assert out["switched2"]["base_url"] == "http://127.0.0.1:11434/v1"
    assert out["switched2"]["model"] == "qwen3:8b"


@node
def test_keyless_provider_never_sends_a_key(tmp_path):
    """本机服务不需要 Key，界面上也**不许**下发 api_key。

    带了也没用（服务端不读），但会让「这个 provider 到底要不要 Key」
    这件事在代码里变得含糊 —— 哪天有人给 ollama 套上鉴权，这行就会
    静默变成「永远发不出 Key」。
    """
    script = tmp_path / "h.mjs"
    script.write_text(
        f"{_SHIM}\nconst PAYLOAD = {_payloads()};\n{JS_SETTINGS_LLM}\n{_DRIVER}",
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, f"JS 崩了：\n{proc.stderr}"
    out = json.loads(proc.stdout)

    assert "api_key" not in out["keyless"]["read"]
    assert "clear_api_key" not in out["keyless"]["read"]


@node
def test_blank_key_is_not_sent(tmp_path):
    """输入框空着就**别把空串发上去**。

    服务端把空串理解成「不改」，但让前端干脆不发更不容易误解 ——
    而且万一后端哪天改成「空串 = 清空」（曾经就是这么设计的），
    这个前端就立刻开始**抹掉用户的 Key**。
    """
    script = tmp_path / "h.mjs"
    script.write_text(
        f"{_SHIM}\nconst PAYLOAD = {_payloads()};\n{JS_SETTINGS_LLM}\n{_DRIVER}",
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, f"JS 崩了：\n{proc.stderr}"
    out = json.loads(proc.stdout)

    assert "api_key" not in out["cloud"]["read"], "没填却把 api_key 发了出去"
    # 而勾了「清空」要真的发出去 —— 不然那个勾选框就是个摆设
    assert out["cleared"].get("clear_api_key") is True
