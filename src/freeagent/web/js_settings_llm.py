"""设置页的「智能层」那一块：服务商、Key、模型、连通性测试。

从 :mod:`js_settings` 拆出来，因为那个文件已经 190 行，而 web 层有
**250 行的硬上限**。拆开之后两块各自都短，而 ``page.py`` 负责按顺序拼装。

## 四个交互决定

1. **模型用 ``<datalist>``，不用 ``<select>``。**
   ``<select>`` 只能选列出的项，一旦这家出了新模型就选不到；``<datalist>``
   是「有建议、也能自己敲」—— 正好对应「内置精选 + 手输逃生舱」，而且**零 JS**。

2. **Key 输入框永远是空的。**
   明文不可能回显（响应里只有掩码），所以加载后框内必然为空。已经在的
   Key 用**占位符**提示，而不是把掩码填进去 —— 填进去的话，用户一改
   就变成「用新值覆盖」，而他其实只是想改模型。

3. **换服务商就预填那家的默认地址与模型。**
   显式可见的联动，好过在 ``load_config`` 里悄悄改写存好的配置 ——
   后者会让「我明明存的是 A，怎么变成 B 了」变成一个查不到的问题。

4. **不需要 Key 的服务商，把「清空 Key」那一行收起来。**
   留着「留空=不改」这种提示在「压根没有 Key」的场景下是纯噪音。
"""

from __future__ import annotations

__all__ = ["JS_SETTINGS_LLM"]

JS_SETTINGS_LLM = r"""// 设置页的智能层部分。被 renderSettings 调用，返回 {node, read}。
// read() 收集非秘密字段 + 可选的 api_key / clear_api_key。

function renderLlmSection(d) {
  const wrap = el("div");
  const profiles = d.providers || [];
  const byId = {};
  profiles.forEach((p) => { byId[p.id] = p; });
  const cur = byId[d.provider] || profiles[0] || null;

  // 密钥文件与 config.json 同目录：从 config_path 反推，别让用户自己找。
  const stateDir = String(d.config_path || "").replace(/[\\/][^\\/]+$/, "")
    || "~/.freeagent";

  // --- 服务商 ---
  const pick = el("select");
  profiles.forEach((p) => {
    const o = el("option");
    o.value = p.id;
    o.textContent = p.label + (p.key_configured ? "（已配 Key）" : "");
    pick.appendChild(o);
  });
  if (cur) pick.value = cur.id;

  // --- 接口地址 / 模型 ---
  const url = el("input"); url.type = "text";
  const model = el("input"); model.type = "text"; model.setAttribute("list", "llmModels");
  const list = el("datalist"); list.id = "llmModels";

  const fillHints = (models) => {
    list.innerHTML = "";
    (models || []).forEach((m) => {
      const o = el("option");
      o.value = m;
      list.appendChild(o);
    });
  };
  const applyProfile = (p, keepCurrent) => {
    url.value = (p && p.default_base_url) || "";
    model.value = (p && p.default_model) || "";
    // 「沿用当前值」**只在初次渲染时做**。切换服务商时不能沿用 ——
    // 那会把上一家的地址和模型带进新那家：用户选「其它兼容服务」，看到
    // 框里已经填着 DeepSeek 的地址和模型名，以为是自己配的，一保存就成了
    // 「自定义端点 = DeepSeek」。这正是「界面说一套、实际做另一套」。
    // 新那家没有默认值时宁可留空 —— 留空会被服务端明确拦下并要求填地址，
    // 而带错值只会在调用时才炸。
    if (keepCurrent) {
      if (!url.value) url.value = d.base_url || "";
      if (!model.value) model.value = d.model || "";
    }
    fillHints(p && p.models);
    url.placeholder = p && p.requires_base_url && !url.value
      ? "必填，比如 http://127.0.0.1:8000/v1" : "";
  };
  applyProfile(cur, true);

  // --- Key ---
  const key = el("input"); key.type = "password"; key.autocomplete = "off";
  const clearBox = el("input"); clearBox.type = "checkbox";
  const keyNote = el("div", "hint");

  // 「清空 Key」那一行**先建好**，paintKey 才敢去显隐它。
  //
  // 踩过的坑（两次，都会整页白屏）：
  //   1) 一开始写 `clearBox.parentElement.style` —— 首次调用 paintKey 时
  //      clearBox 还没进 DOM，parentElement 是 null，`null.style` 抛 TypeError。
  //   2) 修完又把 `const keyRow` 写了第二遍 —— JS 里 const 重复声明是
  //      **整个 script 块的 SyntaxError**，于是**整个应用**都不动，
  //      比白屏还严重。
  // 所以：显隐一个提前建好的容器，且一个名字只声明一次。
  const keyRow = el("div", "field");
  const clearWrap = el("label", "check");
  clearWrap.appendChild(clearBox);
  clearWrap.appendChild(el("span", null, "清空已存的 Key"));
  keyRow.appendChild(clearWrap);

  const paintKey = (p) => {
    if (!p || p.needs_key === false) {
      key.disabled = true;
      key.placeholder = "本机服务不用鉴权，留空即可";
      keyNote.innerHTML = "这家不需要 Key，事务内容不会离开本机。";
      keyRow.style.display = "none";
      return;
    }
    key.disabled = false;
    keyRow.style.display = "";
    // 变量名一律从接口读（``p.key_env_var``），**不写死**。用户要能在界面上
    // 看到「到底是哪个变量」，因为环境变量是更高级的覆盖入口 —— 他得知道
    // 名字才能去 shell 里设，或者去查是哪条把它压住了。
    if (p.key_configured) {
      key.placeholder = "已配置 " + esc(p.key_masked) + "（留空=不改）";
      keyNote.innerHTML = "当前生效：" + esc(p.key_source) +
        "（变量 <code>" + esc(p.key_env_var) + "</code>）。留空表示不改。";
      if (p.key_shadowed_by_env) {
        keyNote.innerHTML +=
          "<b>注意</b>：llm.env 里存着一份，但被环境变量 <code>" +
          esc(p.key_env_var) + "</code> 压住了，在这里重填<b>不会生效</b>" +
          " —— 要换它得改那个环境变量并重启。";
      }
    } else {
      key.placeholder = "填了会写进 " + esc(stateDir) + "/llm.env";
      keyNote.innerHTML = "没配 Key 时跑的是<b>规则层</b>：能建事务、排期、提醒、" +
        "恢复契约，但角色推断只是关键词匹配，草稿/拆步骤基本是模板。" +
        "也可以设环境变量 <code>" + esc(p.key_env_var) + "</code>，它优先于文件。";
    }
  };
  paintKey(cur);

  // --- 动作按钮 ---
  const out = el("div", "hint");
  const probe = el("button", null, "测试连通");
  probe.type = "button";
  const fetchModels = el("button", null, "拉取模型列表");
  fetchModels.type = "button";

  const body = () => JSON.stringify({
    provider: pick.value,
    base_url: url.value,
    model: model.value,
    api_key: key.value,
  });

  probe.onclick = async () => {
    out.textContent = "测试中（这是一次真实请求）…";
    try {
      const r = await api("/api/llm/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: body(),
      });
      out.innerHTML = (r.ok ? '<span class="ok">✓ ' : '<span class="err">✗ ') +
        esc(r.detail) + "</span>";
    } catch (err) {
      out.innerHTML = '<span class="err">' + esc(err.message) + "</span>";
    }
  };

  fetchModels.onclick = async () => {
    out.textContent = "拉取中…";
    try {
      const r = await api("/api/llm/models", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: body(),
      });
      if (!r.ok) {
        out.innerHTML = '<span class="err">' + esc(r.detail) + "</span>";
        return;
      }
      fillHints(r.models);
      out.innerHTML = '<span class="ok">✓ ' + esc(r.detail) + "</span>";
      const typed = model.value.trim();
      if (r.models.length && typed && r.models.indexOf(typed) < 0) {
        out.innerHTML += "　当前填的 <b>" + esc(typed) +
          "</b> 不在列表里，记得挑一个。";
      }
    } catch (err) {
      out.innerHTML = '<span class="err">' + esc(err.message) + "</span>";
    }
  };

  pick.onchange = () => {
    const p = byId[pick.value];
    if (!p) return;
    // 第二个参数**必须**显式给 false：靠「不传 = undefined = falsy」来表达
    // 「不要沿用上一家的值」太脆弱了 —— 哪天有人给 applyProfile 加个默认
    // 参数，这里就静默变成沿用了，而那是个没人看得出来的错。
    applyProfile(p, false);
    paintKey(p);
    out.textContent = "";
  };

  // --- 排版 ---
  const field = (label, node, hint) => {
    const w = el("div", "field");
    w.appendChild(el("label", null, esc(label)));
    w.appendChild(node);
    if (hint) w.appendChild(el("div", "hint", hint));
    return w;
  };

  wrap.appendChild(field("服务商", pick,
    "这一版只支持 OpenAI 兼容的那几家；Claude / Gemini 还不行。"));
  wrap.appendChild(field("接口地址", url,
    "通常不用改 —— 换服务商时会自动预填那家的地址。"));
  wrap.appendChild(list);
  wrap.appendChild(field("模型名", model,
    "下拉里是建议值，<b>也可以自己敲</b>。模型名写错不会立刻报错，" +
    "只有真正用到智能层的功能才会失败。"));
  wrap.appendChild(field("API Key", key, null));
  wrap.appendChild(keyNote);
  wrap.appendChild(keyRow);

  const acts = el("div", "actions");
  acts.appendChild(probe);
  acts.appendChild(fetchModels);
  wrap.appendChild(acts);
  wrap.appendChild(out);

  return {
    node: wrap,
    read: () => {
      const p = byId[pick.value] || {};
      const r = {
        provider: pick.value,
        model: model.value.trim(),
        base_url: url.value.trim(),
      };
      // 空串**不下发**：服务端把空串理解成「不改」，直接不发更不容易误解。
      if (p.needs_key !== false) {
        if (clearBox.checked) r.clear_api_key = true;
        else if (key.value.trim()) r.api_key = key.value.trim();
      }
      return r;
    },
  };
}"""
