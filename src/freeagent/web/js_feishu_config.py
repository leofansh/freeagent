"""飞书配置表单（设计方案 12.7）。

单独一个模块，跟 :mod:`js_feishu` 的状态/日志分开：``js_feishu.py`` 已经
136 行，再塞表单就顶破 250 行上限，而这两块的**性质也不同** —— 那块只读、
这块会改凭据。

四条不能违反的约束：

1. **Secret 输入框永远是空的**，已有值只以掩码出现在 placeholder 里。
   把明文填回输入框，等于让它出现在浏览器自动填充、截图和肩窥里。
2. **留空 = 不改**，不是「清空」。这是最容易出事的地方：一个从没碰过的
   输入框提交上去，如果当成清空，就会把用户正在用的凭据抹掉，而界面
   还会显示「已保存」。真要删得勾那个单独的框。
3. **保存后必须说清要不要重启**。桥接是独立进程，它读的是启动时的配置；
   不说这句，用户会以为「保存了就是生效了」。
4. **「缺什么」要写出来**。必填项没配齐时不拦着保存（用户可能分几次填），
   但必须告诉他还差哪几项，否则他会以为配好了、桥接却起不来。
"""

from __future__ import annotations

__all__ = ["JS_FEISHU_CONFIG"]

JS_FEISHU_CONFIG = r"""// 界面上用友好名字，接口用环境变量名 —— 后者只在提交那一刻出现。
const FEISHU_FIELD_LABEL = {
  FEISHU_APP_ID: "App ID",
  FEISHU_APP_SECRET: "App Secret",
  FEISHU_ALLOWED_USERS: "白名单 open_id",
  FEISHU_DOMAIN: "域名",
  FEISHU_LOCK_PORT: "单实例锁端口",
};

// 必填项的补充说明。写在这里而不是让后端只回一个 key 列表：光有
// 「FEISHU_ALLOWED_USERS」这个变量名，没人知道该填成什么。
const FEISHU_FIELD_HINT = {
  FEISHU_APP_ID: "飞书开放平台「凭证与基础信息」里的 App ID，形如 cli_xxx。",
  FEISHU_APP_SECRET: "<b>留空表示不改</b>，不会清掉已存的。" +
    "换新 Secret 时整个粘进来。",
  FEISHU_ALLOWED_USERS: "逗号分隔（私聊发一句话或群里 @ 它，上面会列出实际 id，" +
    "照着填）。<b>0 人 = 谁都不许指挥你</b>，这是刻意的安全底线。" +
    "填 ou_ 开头的是 open_id（换应用会变），填裸串是 user_id（跨应用不变，"+
    "需应用申请对应权限）。<b>别填 oc_</b> —— 那是会话 id，不是人的。",
  FEISHU_DOMAIN: "国内填 feishu，国际版填 lark。填错会一直连不上。",
  FEISHU_LOCK_PORT: "默认 8771。同机只允许一个桥接，占用 = 已经有一个在跑。",
};

// 哪些字段要原样带回去、哪些是秘密。空值一律不提交（后端把空 Secret
// 当「不改」，空字符串则会让非秘密项被删掉）。
function feishuConfigPayload(inputs, clearSecret) {
  const out = {};
  Object.keys(inputs).forEach((k) => {
    const v = inputs[k].value.trim();
    if (k === "FEISHU_APP_SECRET") {
      if (v) out[k] = v;            // 只有真填了新的才提交
    } else if (v) {
      out[k] = v;
    }
  });
  if (clearSecret && clearSecret.checked) out.clear_secret = true;
  return out;
}

function feishuConfigForm(cfg, onSaved) {
  const form = el("form", "set");
  const inputs = {};

  const card = el("div", "card");
  card.innerHTML =
    '<div class="row1"><span class="title">通道配置</span>' +
    (cfg.secret_configured
      ? '<span class="tag open">Secret 已配</span>'
      : '<span class="tag warn">还没配全</span>') + "</div>" +
    (cfg.missing.length
      ? '<div class="note" style="margin-top:6px">还差：' +
        cfg.missing.map((k) => esc(FEISHU_FIELD_LABEL[k] || k)).join("、") +
        "。缺了桥接会拒绝启动（宁可不许，也不能默认放行）。</div>"
      : '<div class="hint" style="margin-top:6px">必填项都齐了。</div>') +
    '<div class="hint" style="margin-top:6px">存在 <code>' + esc(cfg.env_path) +
    "</code>。改这里等于改那个文件。";
  form.appendChild(card);

  Object.keys(FEISHU_FIELD_LABEL).forEach((key) => {
    const isSecret = cfg.secret_keys.indexOf(key) >= 0;
    const input = el("input");
    input.type = isSecret ? "password" : "text";
    input.name = key;
    input.autocomplete = "off";
    if (isSecret) {
      // 绝不用 value= 回填已有 Secret。只在 placeholder 里给掩码。
      input.placeholder = cfg.secret_configured
        ? "已配置（" + esc(cfg.values[key] || "") + "）。留空 = 不改"
        : "还没配。粘上 App Secret";
    } else {
      input.value = cfg.values[key] || "";
    }
    inputs[key] = input;

    const w = el("div", "field");
    w.appendChild(el("label", null, esc(FEISHU_FIELD_LABEL[key])));
    w.appendChild(input);
    w.appendChild(el("div", "hint", FEISHU_FIELD_HINT[key] || ""));
    // 逐条说明已填条目**大概**是什么形状。白名单填错的坑就在这：
    // 用户不知道 ou_ 开头的是什么、裸串又是什么，填错了也没人告诉他。
    if (key === "FEISHU_ALLOWED_USERS" && (cfg.allowed_entries || []).length) {
      const notes = el("div", "hint");
      notes.innerHTML = cfg.allowed_entries.map((e) =>
        "<div>· <code>" + esc(e.id) + "</code> —— " + esc(e.kind) + "</div>"
      ).join("");
      w.appendChild(notes);
    }
    form.appendChild(w);
  });

  // 清除 Secret 要**显式勾选**，不能由空输入框触发 —— 见模块说明第 2 条。
  const clearBox = el("input");
  clearBox.type = "checkbox";
  const clearWrap = el("label", "check");
  clearWrap.appendChild(clearBox);
  clearWrap.appendChild(el("span", null, "清除已保存的 App Secret"));
  const clearField = el("div", "field");
  clearField.appendChild(clearWrap);
  clearField.appendChild(el("div", "hint",
    "桥接下次启动就会因为没有 Secret 而拒绝启动。想换凭据直接改上面的输入框，勾这个是「彻底不要了」。"));
  form.appendChild(clearField);

  const actions = el("div", "actions");
  const save = el("button", null, "保存配置");
  save.type = "submit";
  const msg = el("span", "hint");
  actions.appendChild(save);
  actions.appendChild(msg);
  form.appendChild(actions);

  form.onsubmit = async (e) => {
    e.preventDefault();
    msg.textContent = "保存中…";
    try {
      const after = await api("/api/feishu/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(feishuConfigPayload(inputs, clearBox)),
      });
      msg.textContent = "";
      toast("已保存");
      onSaved(after);
    } catch (err) {
      msg.innerHTML = '<span class="err">' + esc(err.message) + "</span>";
    }
  };
  return form;
}

// 「改了但没生效」的提示。单独成函数是因为它跟表单无关，
// 状态页的别的位置也可能需要它（比如以后加了启停按钮之后）。
function feishuRestartBanner(cfg) {
  if (!cfg.restart_required) return null;
  return el("div", "note",
    "⚠ <b>配置改了，但桥接还在用启动时那份。</b>" +
    "桥接是独立进程，要重启它才生效 —— 现在保存的值只对下次启动起作用。" +
    "起桥接用 tools\\run_feishu.bat。");
}"""
