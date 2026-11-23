"""对话界面。

存取相关的 ``chatDay`` / ``chatLoad`` / ``chatSave`` / ``clearHistory``
与两个状态变量在 **:mod:`js_chat_store`** —— 那是另一个职责（刷新后还在不在），
拆开是为了守住 250 行的体积守卫。
"""

from __future__ import annotations

__all__ = ["JS_CHAT"]

JS_CHAT = r"""function chatScroll() {
  const log = $("#chatlog");
  if (log) log.scrollTop = log.scrollHeight;
}

// 入参是**条目数组**，不是整份 reply：曾经两种类型混用，发问时 .map 抛错、
// 回放时读 reply.items 拿到 undefined 又把任务卡片全丢了（测试全绿也没抓到）。
// 现在两边统一成数组。
function chatItems(items) {
  if (!items || !items.length) return null;
  const box = el("div", "items");
  items.forEach((it) => {
    const c = el("div", "tcard");
    const why = it.reasons && it.reasons.length
      ? '<div class="why">' + esc(it.reasons.join("；")) + "</div>"
      : (it.detail ? '<div class="why">' + esc(it.detail) + "</div>" : "");
    c.innerHTML =
      "<b>" + esc(it.title) + "</b> " +
      '<span class="meta">' + esc(it.state_label) +
      (it.total_weight ? " · " + it.total_weight + " 分" : "") +
      (it.roles && it.roles.length ? " · " + esc(it.roles.join("、")) : "") +
      (it.scheduled_for ? " · 排 " + esc(it.scheduled_for) : "") +
      "</span>" + why;
    c.onclick = () => openTask(it.task_id);
    box.appendChild(c);
  });
  return box;
}

// history 存「文字 + 种类 + 条目摘要」，够恢复上下文与渲染，不必存 HTML
function addMsg(who, text, cls, items, store, meta) {
  const log = $("#chatlog");
  if (log) { log.classList.remove("is-empty"); const w = log.querySelector(".chat-welcome"); if (w) w.remove(); }  // 欢迎区与居中态都只在空对话时存在
  // 回放历史时把生成日期刷新成当天：日期是生成时冻进文本的，
  // 服务端不会重渲染已存文本。generated_at 只有「今天」类答复才有；
  // 老存档没有 meta，判空守卫直接跳过（不改写、不报错）。
  if (who === "bot" && meta && meta.generated_at &&
      meta.generated_at !== chatDay() && text.indexOf(meta.generated_at) === 0) {
    text = chatDay() + text.slice(meta.generated_at.length);
  }
  const m = el("div", "msg " + (who === "me" ? "me" : "bot " + (cls || "")));
  m.appendChild(el("div", "bubble", esc(text)));
  const box = (who === "bot" && items && items.length) ? chatItems(items) : null;
  if (box) m.appendChild(box);
  log.appendChild(m);
  if (store !== false) {
    history_.push({
      who: who, text: text, cls: cls || "", meta: meta || null,
      items: (items || []).map((i) => ({
        task_id: i.task_id, title: i.title, state_label: i.state_label,
        total_weight: i.total_weight, reasons: i.reasons || [],
        detail: i.detail || "", roles: i.roles || [],
        scheduled_for: i.scheduled_for,
      })),
    });
    chatSave(history_);
  }
  chatScroll();
  return m;
}

function addChips(list, store) {
  const log = $("#chatlog");
  if (!list || !list.length) return;
  const row = el("div", "chips");
  list.forEach((s) => {
    const b = el("button", "chip", esc(s));
    b.onclick = () => { const ta = $("#chatinput"); if (ta) { ta.value = s; ta.focus(); ta.dispatchEvent(new Event("input")); } };
    row.appendChild(b);
  });
  log.appendChild(row);
  if (store !== false) {
    history_.push({ who: "chips", text: list.slice() });
    chatSave(history_);
  }
  chatScroll();
}

function renderChatShell(menu) {
  const root = $("#root");
  root.innerHTML = "";
  const wrap = el("div", "chat");
  const log = el("div", "chatlog");
  log.id = "chatlog";
  wrap.appendChild(log);

  // wrap 必须**在往里 addMsg 之前**就进文档。
  //
  // 踩过的坑（真在浏览器里跑出来的）：addMsg / addChips 用
  // ``document.querySelector("#chatlog")`` 找容器，而游离元素 querySelector
  // 不到 —— 于是首次载入时这里是 null，``log.appendChild(...)`` 必崩。
  // 崩了之后 loadChat 的 catch 又调 addMsg 报同样的错，**原始错误被覆盖**，
  // 用户只看到「Cannot read properties of null」这种没法排查的字样。
  root.appendChild(wrap);

  if (!history_.length) {
    const saved = chatLoad();
    if (saved) history_ = saved;
    // 恢复上一轮的事务 id，这样刷新后还能接「第二个」
    for (let i = history_.length - 1; i >= 0; i--) {
      const m = history_[i];
      if (m.who === "me" || m.who === "chips") continue;
      if (m.items && m.items.length) {
        lastItemIds = m.items.map((x) => x.task_id);
        break;
      }
    }
  } else {
    history_.forEach((m) => {
      if (m.who === "chips") addChips(m.text, false);
      else addMsg(m.who, m.text, m.cls, m.items, false, m.meta);
    });
  }
  // 空对话：摆居中欢迎区（renderChatWelcome 在 js_onboard；声明提升）。
  if (!history_.length) {
    log.classList.add("is-empty"); if (menu) renderChatWelcome(menu);
  }

  const bar = buildBar();
  wrap.appendChild(bar);
  // 对话工具（搜索 / 清空 / 多选）收进右上角"…"，不占输入栏。
  chatToolsMount();
  chatShellMount();  // V1.154：左工作区 / 右产物三栏外壳
  // wrap 早就挂到 root 上了（见上面那行），这里只补输入框。
  // 页脚不摆这两句话（V1.158）：能力边界在欢迎区（menu.notice），免责标记
  // 只在真正排序的「今天 / 事务」页脚出现（renderList 用 data.disclaimer）。
  // 都不是关键动作信息，放在输入栏下面只是噪音。
  $("#chatinput").focus();
}

function buildBar() {
  const bar = el("div", "chatbar");
  // V1.153：输入区做成一体容器；+ 与发送箭头并排在输入框下方那一行。
  const tools = el("div", "chatbar-tools");
  const vbar = el("div"); vbar.id = "visionbar"; tools.appendChild(vbar);
  const ta = el("textarea");
  ta.id = "chatinput";
  ta.rows = 1;
  ta.placeholder = "问我「今天该做什么」，或直接说一件事记下来（回车发送）";
  bar.appendChild(ta);
  // 箭头在 + 同一行右端（回车仍是主路径，箭头照顾触屏）。
  const send = el("button", "btn primary send-arrow", "↑");
  send.id = "chatsend"; send.title = "发送（回车）";
  send.onclick = () => sendChat();
  tools.appendChild(send); bar.appendChild(tools);
  // 直接把剪贴板里的图片粘进输入框 → 走同一条视觉识别链路（V1.150）。
  ta.onpaste = (e) => {
    const items = e.clipboardData && e.clipboardData.items;
    if (!items) return;
    for (const it of items) {
      if (it.type && it.type.indexOf("image/") === 0) {
        const blob = it.getAsFile();
        if (blob) { e.preventDefault(); doVision(blob); return; }
      }
    }
  };
  ta.title = "回车发送，Shift+回车换行；可直接粘贴图片";
  ta.onkeydown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendChat();
    }
  };
  ta.oninput = () => {
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 140) + "px";
  };
  return bar;
}

async function sendChat(preset) {
  const ta = $("#chatinput");
  const text = (preset !== undefined ? preset : ta.value).trim();
  if (!text) return;
  addMsg("me", text);
  if (ta.value) ta.value = "";
  ta.style.height = "auto";
  // 清掉旧的快捷按钮，避免它们堆在下面看不懂
  $("#chatlog").querySelectorAll(".chips").forEach((n) => n.remove());
  // 旧建议行撤出 DOM 时 history_ 也要一起撤，否则刷新重放时退役 chips 插回消息中间。
  history_ = history_.filter((m) => m.who !== "chips");
  chatSave(history_);
  const thinking = addMsg("bot", "…", "", null, false);
  try {
    const reply = await api("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text, last_items: lastItemIds }),
    });
    thinking.remove();
    lastItemIds = (reply.items || []).map((i) => i.task_id);
    addMsg("bot", reply.text, "kind-" + reply.kind, reply.items || [], true,
           { generated_at: reply.generated_at || "", route: reply.route || "" });
    addChips(reply.suggestions);
    if (reply.task_id) {
      try {
        const r = await api("/api/today");
        renderList(r, "今天没有排进来的事务。");
      } catch (e) {}
    }
  } catch (e) {
    thinking.remove();
    addMsg("bot", "出错了：" + e.message, "kind-cannot");
  }
  ta.focus();
}

async function loadChat() {
  renderChatShell(null);
  try {
    const menu = await api("/api/chat");
    if (view !== "chat") return;  // 切走了：旧 load 的回包不许盖新视图
    if (!history_.length) renderChatShell(menu);
  } catch (e) {
    addMsg("bot", "载入开场白失败：" + e.message, "kind-cannot", null, false);
  }
  // 规则层降级要在首屏可见，不藏到设置页才说。判据用实现名而不是
  // key_configured：Ollama 这类本机服务不需要 Key，凭据判据为真，
  // 不该被误报成「未配置」。
  try {
    const h = await api("/api/health");
    if (h.llm === "RuleBasedProvider") {
      const log = $("#chatlog");
      const bar = el("div", "chat-status");
      bar.appendChild(el("span", null,
        "当前是规则层（离线）：能记事、排期、提醒；角色判断和草稿是模板。配置模型后启用智能回复。"));
      const go = el("button", "btn", "去配置模型"); go.onclick = () => load("settings");
      bar.appendChild(go);
      // 空对话时并进欢迎区；有历史才置顶。
      const welcome = log.querySelector(".chat-welcome");
      if (welcome) welcome.appendChild(bar); else log.prepend(bar);
    }
  } catch (e) { /* 状态读不到不挡住对话 */ }
  loadVisionBar();
  chatPickPaint();  // 多选删除模式挂复选框；非选择模式时 no-op
}
"""
