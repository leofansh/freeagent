"""设置页。


"""

from __future__ import annotations

__all__ = ["JS_SETTINGS"]

JS_SETTINGS = r"""// 三个精力档。[key, 标签]。
// 必须与 EnergyWindows（services/sorting.py）一致：morning/afternoon/evening。
// 缺失的后果是整页白屏 —— 见本文件末尾的说明。
const BANDS = [
  ["morning", "早晨"],
  ["afternoon", "下午"],
  ["evening", "晚上"],
];

// 被环境变量压住的字段名 -> 人话。键必须与 config.overridden_by_env() 返回的
// 那些一致（provider / model / base_url / timeout / llm_enabled / allow_fallback）。
const FIELD_LABEL = {
  provider: "服务商",
  model: "模型名",
  base_url: "接口地址",
  timeout: "超时",
  llm_enabled: "智能层开关",
  allow_fallback: "失败退回规则层",
};

function renderSettings(d) {
  const root = $("#root");
  root.innerHTML = "";

  // --- 环境变量覆盖提示：不说，用户会以为设置坏了 ---
  if (d.overridden_by_env && d.overridden_by_env.length) {
    const names = d.overridden_by_env
      .map((f) => FIELD_LABEL[f] || f).join("、");
    root.appendChild(el("div", "note",
      "⚠ 启动时设了环境变量，" + esc(names) +
      " 由环境变量决定，改这里<b>不会生效</b>。要去掉那个环境变量，或改它的值。"));
  }

  // --- 智能层（服务商 / Key / 模型）由 llm_settings_llm 那块渲染 ---
  // 渲染在表单之外，但**必须挂进 form.set 里**（见下面 append 的注释）。
  const llm = renderLlmSection(d);

  const f = el("form", "set");
  // 挂进 form，而不是 root。
  //
  // 踩过的坑（真在浏览器里看出来的）：style.css 里所有字段样式都是
  // **以 form.set 为前缀**的 —— `form.set .field > label { display: block }`
  // 让标签压在输入框上面，`form.set .field input[type=text] { width: 100% }`
  // 让输入框铺满。挂在 form 外面就一条都吃不到：标签变成**和输入框并排**，
  // 输入框退化成通用的 `flex: 1 1 260px`，于是接口地址被截成
  // 「https://api.deepseek.c」，刚好少掉 /v1 —— 而那是个真能跑和真跑不通
  // 的区别。DOM 断言全绿，因为结构没错、只是没吃到样式。
  f.appendChild(llm.node);

  const field = (label, node, hint) => {
    const w = el("div", "field");
    const l = el("label", null, esc(label));
    w.appendChild(l);
    w.appendChild(node);
    if (hint) w.appendChild(el("div", "hint", hint));
    return w;
  };

  const to = el("input"); to.type = "number"; to.min = "1"; to.max = "600";
  to.step = "1"; to.value = String(d.timeout);
  const fb = el("input"); fb.type = "checkbox"; fb.checked = !!d.allow_fallback;
  const ewOn = el("input"); ewOn.type = "checkbox";
  ewOn.checked = !!d.energy_windows;

  f.appendChild(field("超时（秒）", to, "1–600。调大适合慢网络，但失败时会等更久。"));

  const fbWrap = el("label", "check");
  fbWrap.appendChild(fb);
  fbWrap.appendChild(el("span", null, "模型失败时自动退回规则层（关掉则直接报错）"));
  const fbField = el("div", "field");
  fbField.appendChild(fbWrap);
  fbField.appendChild(el("div", "hint",
    "建议保持开启：降级时至少还能用，只是没那么聪明。"));
  f.appendChild(fbField);

  const ewField = el("div", "field");
  const ewHead = el("label", "check");
  ewOn.id = "ewOn";
  ewHead.appendChild(ewOn);
  ewHead.appendChild(el("span", null, "启用精力档位（按时段微调排序）"));
  ewField.appendChild(ewHead);
  const bands = el("div", "bands");
  const bandInputs = {};
  BANDS.forEach(([key, label]) => {
    const pair = d.energy_windows
      ? d.energy_windows[key] : null;
    // 「已启用但这一档没设」要显示**默认三档的值**，不能留空。
    //
    // 踩过的坑（真在浏览器里点出来的）：留空时界面上是空的，而保存时
    // `_settings_energy` 会**跳过**空档、再由 `EnergyWindows(**out)` 用
    // **默认值补上** —— 于是「界面显示的」和「落盘的」不是一回事。
    // 用户看一个空格，配置文件里却躺着一个 5–12，怎么都对不上。
    const DEF = { morning: [5, 12], afternoon: [12, 18], evening: [18, 24] };
    const shown = pair || DEF[key];
    const lo = el("input"); lo.type = "number"; lo.min = "0"; lo.max = "23";
    lo.value = String(shown[0]);
    lo.setAttribute("aria-label", label + "起始小时");
    const hi = el("input"); hi.type = "number"; hi.min = "1"; hi.max = "24";
    hi.value = String(shown[1]);
    hi.setAttribute("aria-label", label + "结束小时");
    bandInputs[key] = [lo, hi];
    const box = el("div");
    box.appendChild(el("span", null, esc(label)));
    box.appendChild(lo);
    box.appendChild(el("span", null, "–"));
    box.appendChild(hi);
    bands.appendChild(box);
  });
  ewField.appendChild(bands);
  ewField.appendChild(el("div", "hint",
    "左闭右开，单位为小时。默认早晨 5–12、下午 12–18、晚上 18–24。"));
  f.appendChild(ewField);

  const syncBands = () => {
    Object.values(bandInputs).forEach(([lo, hi]) => {
      lo.disabled = hi.disabled = !ewOn.checked;
    });
  };
  ewOn.onchange = syncBands;
  syncBands();

  const actions = el("div", "actions");
  const save = el("button", null, "保存并立即生效");
  save.type = "submit";
  const msg = el("span", "hint");
  actions.appendChild(save);
  actions.appendChild(msg);
  f.appendChild(actions);

  f.onsubmit = async (e) => {
    e.preventDefault();
    msg.textContent = "保存中…";
    let payload;
    try {
      // 服务商/Key/模型由 llm 那块收集，这里只补上本表单自己的字段。
      // 顺序很要紧：先拿到 llm 的，再让本表单覆盖 —— 两边没有同名字段，
      // 但写反了会让人以为「以模型名为准」而漏掉服务商。
      payload = llm.read();
      payload.timeout = Number(to.value);
      payload.allow_fallback = fb.checked;
      payload.energy_windows = ewOn.checked
        ? Object.fromEntries(BANDS.map(([k]) => {
            const [lo, hi] = bandInputs[k];
            return [k, [Number(lo.value), Number(hi.value)]];
          }))
        : null;
    } catch (err) {
      msg.textContent = "数值不对，检查一下精力档位";
      return;
    }
    try {
      const after = await api("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      toast("已保存，立即生效");
      msg.textContent = "";
      renderSettings(after);
      await refreshHeader();
    } catch (err) {
      msg.innerHTML = '<span class="err">' + esc(err.message) + "</span>";
    }
  };
  root.appendChild(advancedSection(d));

  root.appendChild(f);

  root.appendChild(el("div", "hint",
    "非秘密项存在 " + esc(d.config_path) + "，改这里等于改那个文件，" +
    "也可以直接编辑它（记得重启）。Key 另存 " +
    esc(String(d.config_path || "").replace(/[\\/][^\\/]+$/, "") || "~/.freeagent") +
    "/llm.env —— 不在这里，因为它不是配置、而是凭据。"));
  $("#foot").textContent =
    "Key 可以在这里填，会写进 llm.env（不写进 config.json）。" +
    "环境变量仍然优先于文件 —— 两边都配了的话，界面会明确提示你。";
}

// 高级设置：默认折叠，里面只放**恢复出厂**。为什么不摆明处也不埋底：
// 摆顶部 = 一屏内伸手可及，误触成本最高；埋底部 = 没人找得到（实测第一
// 位使用者就没看到）。折叠是两不牺牲：一行入口在页顶不用滚，展开是显式
// 动作，破坏性按钮从不出现在「不小心看到」的状态里。
function advancedSection(d) {
  const box = el("details", "advanced");
  const sum = el("summary", "", "高级设置（恢复出厂）");
  box.appendChild(sum);
  box.appendChild(resetSection(d));
  return box;
}

// 恢复出厂。口令**印在卡片上**：它的作用是让人慢一秒，不是保密 ——
// 藏起来只会逼用户输错一次从报错里才知道输什么，白惹一声「功能坏了」。
//（端点仍受会话令牌 + 只监听回环保护，不靠口令保密。）
function resetSection(d) {
  const box = el("div", "card");
  box.innerHTML =
    '<div class="row1"><span class="title">恢复出厂</span>' +
    '<span class="tag warn">危险</span></div>';
  box.appendChild(el("div", "note",
    "清空全部事务数据（事务、记录、产物、审批卡）。" +
    "角色脉络、配置、Key 全部保留。执行前会自动备份，可还原。"));
  const row = el("div", "field");
  const ta = el("input");
  ta.type = "text";
  ta.id = "resetphrase";
  ta.placeholder = "确认口令";
  ta.autocomplete = "off";
  ta.style.width = "100%";
  const go = el("button", "btn", "清空并备份");
  go.onclick = async () => {
    if (!ta.value.trim()) { toast("先输入确认口令"); return; }
    // 二次确认（宁可丑也要多一道「你确定」）
    if (!confirm("将清空全部事务数据（先自动备份）。确定？")) return;
    go.disabled = true;
    try {
      const r = await api("/api/system/reset-data", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ phrase: ta.value.trim() }),
      });
      const total = Object.values(r.deleted || {})
        .reduce((a, b) => a + b, 0);
      toast("已清空 " + total + " 条；备份在 " + r.backup);
      renderSettings(await api("/api/settings"));
    } catch (e) {
      toast("没有执行：" + e.message);
      go.disabled = false;
    }
  };
  row.appendChild(ta);
  row.appendChild(go);
  box.appendChild(row);
  const hint = el("div", "hint",
    "口令（逐字输入）：<b>我确定清空所有事务数据</b>。执行器或桥接在跑时会拒绝（正在写同一个库）—— 先停再试；备份文件名带时间戳。");
  if (d && d.last_backup) hint.innerHTML +=
    " 最近一次自动备份：<b>" + esc(d.last_backup) +
    "</b>（启动时每日一份，保留 7 份）。";
  box.appendChild(hint);
  return box;
}"""
