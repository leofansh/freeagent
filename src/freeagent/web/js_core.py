"""脚本公共部分：DOM helper、请求、列表渲染。


"""

from __future__ import annotations

__all__ = ["JS_CORE"]

JS_CORE = r""""use strict";
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s == null ? "" : s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;");
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html != null) n.innerHTML = html;
  return n;
};
let view = "chat";
let detail = null;
let history_ = [];   // 对话历史，落在 localStorage（刷新/换视图都不丢）

async function api(path, opts) {
  // 会话令牌：服务端给每个 /api/（除白名单）都加了校验（设计方案 12.7）。
  // 令牌由服务端注入 window.__FREEAGENT_SESSION__，用户不用、也不该看到它。
  // 拿不到就说明页面没被正确注入 —— 那时报错比静默发出无凭证请求好，
  // 后者会得到一个 401 然后让人以为「服务坏了」。
  const tok = window.__FREEAGENT_SESSION__;
  if (!tok) throw new Error("页面缺少会话令牌，请重新打开这个地址");
  opts = opts || {};
  opts.headers = Object.assign(
    { "X-FreeAgent-Session": tok }, opts.headers || {}
  );
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({ error: "响应不是 JSON" }));
  if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
  return data;
}
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 1900);
}
const when = (iso) => iso ? iso.replace("T", " ").slice(5, 16) : "";
const stateTag = (t) =>
  '<span class="tag' + (t.is_open ? " open" : "") + '">' + esc(t.state_label) + "</span>";

function reasonChips(item) {
  if (!item.signals || !item.signals.length)
    return '<div class="reasons"><span class="reason">无命中信号</span></div>';
  return '<div class="reasons">' + item.signals.map((s) =>
    '<span class="reason">' + esc(s.reason) + " <b>+" + s.weight + "</b></span>"
  ).join("") + "</div>";
}

function taskCard(item, i) {
  const card = el("div", "card clickable");
  card.innerHTML =
    '<div class="row1"><span class="idx">' + (i + 1) + ".</span>" +
    '<span class="title">' + esc(item.title) + "</span>" +
    (item.rolled_over ? '<span class="tag warn">顺延</span>' : "") + "</div>" +
    '<div class="row1" style="margin-top:5px">' +
      '<span class="roles">' + item.roles.map(esc).join("、") + "</span>" +
      '<span class="tag">' + esc(item.kind_label) + "</span>" + stateTag(item) +
      (item.scheduled_for ? '<span class="meta">排 ' + esc(item.scheduled_for) + "</span>" : "") +
      '<span class="score">' + item.total_weight + " 分</span></div>" +
    reasonChips(item);
  card.onclick = () => openTask(item.id);
  return card;
}

function renderList(data, emptyText) {
  const root = $("#root");
  root.innerHTML = "";
  if (data.rollover && data.rollover.summary) {
    root.appendChild(el("div", "note", "顺延：" + esc(data.rollover.summary)));
  }
  if (!data.count) {
    root.appendChild(el("div", "empty", emptyText));
  } else {
    data.items.forEach((it, i) => root.appendChild(taskCard(it, i)));
  }
  // 只有今天视图带这个字段（serialize.today_payload 给的）。
  // 「不进今天视图」是设计（设计方案 6.2），但**不告诉你它在别处**是缺陷 ——
  // 界面显示「今天 0 条」而用户刚记的事明明在库里，他会以为没记上。
  if (data.unscheduled_count) {
    root.appendChild(el(
      "div", "note",
      "另有 " + data.unscheduled_count + " 条没排期 —— 没排期的不进今天视图，"
      + "排个期或到「全部」里找它们。"
    ));
  }
  $("#foot").textContent =
    data.disclaimer + " —— 助手不替你决定先做哪个。点任意一条可打开恢复契约。";
}

// --- 对话 ------------------------------------------------------------------ //
// 首屏就是一个输入框。能问什么在开场白里给成可点的按钮 ——
// 猜谜语式的助手没人会用第二次。
//
// 对话历史存 localStorage：切页签、刷新都不该丢 ——「像豆包」这点很关键，
// 之前一换视图整段对话就没了。"""
