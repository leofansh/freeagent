"""对话界面。


"""

from __future__ import annotations

__all__ = ["JS_CHAT"]

JS_CHAT = r"""const CHAT_KEY = "freeagent.chat.v1";
let lastItemIds = [];      // 上一轮答复里的事务 id，用来接「第二个」这类指代

function chatLoad() {
  try {
    const raw = localStorage.getItem(CHAT_KEY);
    if (!raw) return null;
    const data = JSON.parse(raw);
    return Array.isArray(data) ? data : null;
  } catch (e) { return null; }
}

function chatSave(messages) {
  try {
    localStorage.setItem(CHAT_KEY, JSON.stringify(messages.slice(-60)));
  } catch (e) { /* 隐私模式下写不了就算了，不影响主功能 */ }
}

function chatScroll() {
  const log = $("#chatlog");
  if (log) log.scrollTop = log.scrollHeight;
}

// 入参是**条目数组**，不是整份 reply。
//
// 踩过的坑（浏览器里实测到的，测试套件全绿也没抓到）：这里原本收整份
// reply，调用处也传 `reply`；而下面 addMsg 里存历史那行又按**数组**用
// `(items || []).map(...)`。同一个参数被当成两种类型，于是
// 送一条问句就抛 `(items || []).map is not a function` ——
// 答复文字已经渲染出来了，紧跟着又补一句「出错了」。
// 反过来从历史恢复时传的是数组，chatItems 读 `reply.items` 拿到 undefined
// 就静默返回 null，**可点的任务卡片全丢了**，一声不吭。
// 现在统一成数组，两边都对。
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
function addMsg(who, text, cls, items, store) {
  const log = $("#chatlog");
  const m = el("div", "msg " + (who === "me" ? "me" : "bot " + (cls || "")));
  m.appendChild(el("div", "bubble", esc(text)));
  const box = (who === "bot" && items && items.length) ? chatItems(items) : null;
  if (box) m.appendChild(box);
  log.appendChild(m);
  if (store !== false) {
    history_.push({
      who: who, text: text, cls: cls || "",
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
    b.onclick = () => sendChat(s);
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

  // 拍照入口的容器要**先于**输入框存在，loadVisionBar 才能挂上去
  const vbar = el("div", "chips");
  vbar.id = "visionbar";
  wrap.appendChild(vbar);

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
      else addMsg(m.who, m.text, m.cls, m.items, false);
    });
  }
  if (!history_.length && menu) {
    addMsg("bot", menu.notice + "\n可以问我：", "kind-help", null, false);
    addChips(menu.can_do, false);
  }

  const bar = buildBar();
  wrap.appendChild(bar);
  // wrap 早就挂到 root 上了（见上面那行），这里只补输入框。
  $("#foot").textContent =
    "我能读你已记的事，也能记新的；不改已有事务。启用了排序的地方都是启发式提示，不是评分。";
  $("#chatinput").focus();
}

function buildBar() {
  const bar = el("div", "chatbar");
  const ta = el("textarea");
  ta.id = "chatinput";
  ta.rows = 1;
  ta.placeholder = "问我「今天该做什么」，或直接说一件事记下来";
  const send = el("button", null, "发送");
  send.id = "chatsend";
  bar.appendChild(ta);
  bar.appendChild(send);
  if (history_.length) {
    const clear = el("button", null, "清空");
    clear.title = "清掉这段对话（不影响已记的事务）";
    clear.onclick = clearHistory;
    bar.appendChild(clear);
  }
  ta.onkeydown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendChat();
    }
  };
  send.onclick = () => sendChat();
  ta.oninput = () => {
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 140) + "px";
  };
  return bar;
}

function clearHistory() {
  history_ = [];
  lastItemIds = [];
  try { localStorage.removeItem(CHAT_KEY); } catch (e) {}
  load("chat");
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
  const thinking = addMsg("bot", "…", "", null, false);
  try {
    const reply = await api("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text, last_items: lastItemIds }),
    });
    thinking.remove();
    lastItemIds = (reply.items || []).map((i) => i.task_id);
    addMsg("bot", reply.text, "kind-" + reply.kind, reply.items || []);
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
    if (!history_.length) renderChatShell(menu);
  } catch (e) {
    addMsg("bot", "载入开场白失败：" + e.message, "kind-cannot", null, false);
  }
  loadVisionBar();
}
"""
