"""脚本公共部分：DOM helper、请求、toast、列表渲染与「批量算了」勾选态。

## 为什么「批量算了」的勾选态在这里

列表渲染（renderList / taskCard）在本文件，而勾选态要被**三处**读到：
工具条重绘、卡片渲染、视图切换时清理。放在这里而不是 renderList 内部，
是为了让「切视图即清空」有一个稳定的存放处 —— 只活在 renderList 里的
状态会被下一次重渲染无声吞掉，而用户看到的后果是「勾了半天的选择
跳个页回来还在」，紧接着一把算掉他以为已经取消的那些。
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

// --- 批量「算了」的勾选态 --------------------------------------------------- //
//
// 独立变量而不是 Set 的属性：读它的地方有三处（工具条、卡片渲染、视图
// 切换清理）。挂属性在 Set 上是「聪明写法」，但 clear() 会不会清掉它、
// 展开时会不会带出来，都要读代码的人多想一步 —— 少一个这种念头。
// 集合存 id：勾选/取消 O(1) 且天然去重。
//
// 只在「今天 / 全部未结束」两个列表视图生效：终态视图（已结束）没有
// 批量意义，角色视图根本不是事务列表。**离开列表视图即清零**
// （renderList 里判）—— 两个列表视图之间保留：人在「全部」里勾到一半
// 去「今天」看一眼很常见，为此清空是惩罚正确行为。但勾选绝不比一次
// 操作活得更久：刷新、离开列表、批量完成，三条路都清。
let pickMode = false;
const picked = new Set();

function isPickableView() {
  return view === "today" || view === "all";
}

function clearPick() {
  // 只清状态不重渲染 —— 给视图切换用（调用方紧接着会 load）。
  pickMode = false;
  picked.clear();
}

function enterPickMode() {
  pickMode = true;
  picked.clear();
  load(view);
}

function exitPickMode() {
  clearPick();
  load(view);
}

// 批量工具条：已选 N · 批量算了 · 全选 · 退出选择。
// 只读「同一份数据」的纪律不受影响：这里没有任何筛选/隐藏 ——
// 勾选只是为了指认「要改哪几条」，视图读的仍是全量。
function repaintPickBar() {
  $("#pickbar")?.remove();
  if (!pickMode) return;
  const bar = el("div", "pickbar");
  bar.id = "pickbar";
  bar.appendChild(el("span", "pickcount", "已选 " + picked.size + " 条"));
  const go = el("button", "btn primary", "批量算了");
  go.onclick = batchDrop;
  const all = el("button", "btn", "全选");
  all.onclick = () => {
    document.querySelectorAll(".card.clickable").forEach((c) => {
      const b = c.querySelector(".pickbox");
      if (b && !b.checked) { b.checked = true; picked.add(c.dataset.tid); }
    });
    repaintPickBar();
  };
  const off = el("button", "btn", "退出选择");
  off.onclick = exitPickMode;
  bar.appendChild(go); bar.appendChild(all); bar.appendChild(off);
  $("#root").insertBefore(bar, $("#root").firstChild);
}

async function batchDrop() {
  if (!picked.size) { toast("先勾几条"); return; }
  // 确认是必须的：勾选是临时的，drop 虽然可审计、终态也可逆
  // （DROPPED → INBOX 合法），一把算掉几十条仍该让人停一秒。
  if (!confirm("把这 " + picked.size + " 条标为「算了」？记录仍保留、可审计。")) return;
  try {
    const r = await api("/api/tasks/drop", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids: [...picked], reason: "网页端批量算了" }),
    });
    toast(r.summary);
    clearPick();
    load(view);
  } catch (e) {
    // 保留勾选不清：失败时人想改一改再试，清掉等于让重新勾一遍。
    toast("批量算了失败：" + e.message);
  }
}

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
  card.dataset.tid = item.id;
  card.innerHTML =
    (pickMode
      ? '<span class="pick"><input type="checkbox" class="pickbox"' +
        (picked.has(item.id) ? " checked" : "") + "></span>"
      : "") +
    '<span class="row1"><span class="idx">' + (i + 1) + ".</span>" +
    '<span class="title">' + esc(item.title) + "</span>" +
    (item.rolled_over ? '<span class="tag warn">顺延</span>' : "") + "</div>" +
    '<div class="row1" style="margin-top:5px">' +
      '<span class="roles">' + item.roles.map(esc).join("、") + "</span>" +
      '<span class="tag">' + esc(item.kind_label) + "</span>" + stateTag(item) +
      (item.scheduled_for ? '<span class="meta">排 ' + esc(item.scheduled_for) + "</span>" : "") +
      '<span class="score">' + item.total_weight + " 分</span></div>" +
    reasonChips(item);
  if (pickMode) {
    // 勾选**不许**打开详情：不 stopPropagation 的话点复选框等于点卡片，
    // 人没法「只勾不打开」。所以选择模式下整张卡都不挂打开详情的回调。
    const box = card.querySelector(".pickbox");
    box.onclick = (e) => {
      e.stopPropagation();
      if (box.checked) picked.add(item.id); else picked.delete(item.id);
      repaintPickBar();
    };
  } else {
    card.onclick = () => openTask(item.id);
  }
  return card;
}

function renderList(data, emptyText) {
  const root = $("#root");
  root.innerHTML = "";
  // 进了不可批量操作的视图（已结束/角色）就退掉勾选态：留着它，
  // 下次回到列表时勾选还在，而人早忘了自己勾过什么。
  if (!isPickableView()) clearPick();
  if (data.rollover && data.rollover.summary) {
    root.appendChild(el("div", "note", "顺延：" + esc(data.rollover.summary)));
  }
  if (pickMode && isPickableView()) {
    repaintPickBar();
  } else if (isPickableView() && data.count) {
    // 入口只在「有东西可勾」时出现：空列表上放个「多选」是噪音。
    const bar = el("div", "pickbar");
    const enter = el("button", "btn", "多选·批量算了");
    enter.onclick = enterPickMode;
    bar.appendChild(enter);
    root.appendChild(bar);
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
