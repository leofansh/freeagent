"""对话内搜索（V1.132）：Ctrl/Cmd+F 在对话里搜历史、逐条跳转。

为什么不依赖浏览器原生 Ctrl+F：浏览器只搜当前 DOM，而对话历史是滚动容器
里的动态内容，原生搜不出「第 3 / 12 条」；自己实现才能给计数和循环跳转。

为什么高亮前必须还原原文：反复搜索若不先撤掉上一次的 <mark>，会从 mark
里再套 mark，越搜越乱。原文存进 dataset.faOrig，还原时一份真源。

所有拼接走 textContent / createTextNode，不走 innerHTML（V1.117 的教训：
对话正文来自人和模型，进 innerHTML 就是注入面）。
"""

from __future__ import annotations

__all__ = ["JS_CHAT_SEARCH"]

JS_CHAT_SEARCH = r"""// 对话内搜索（V1.132）：在 #chatlog 前插一条搜索框，逐条高亮命中。
// 对话工具（V1.149）也挂在这里：搜索 / 清空 / 多选删除同属「对这段对话做事」，
// 一起收进右上角"…"，不再各占一颗输入栏按钮（豆包同款位置）。
let faSearchHits = [];
let chatToolsBound = false;

async function chatDelegate() {
  const ta = $("#chatinput");
  const text = (ta ? ta.value : "").trim();
  if (!text) { toast("先写一句要做什么"); return; }
  let st;
  try { st = await api("/api/agent/state"); }
  catch (e) { toast("读不到工作台设置：" + e.message); return; }
  const p = st.prefs || {};
  const dir = p.directory || "";
  if (!dir) { toast("还没选工作目录 —— 先去工作台选一个"); return; }
  const modeText = p.mode === "auto_dir" ? "本目录内自动" : "逐次授权";
  // 显式确认：派发出去会花钱、可能改文件，目录与档位必须让人看一眼。
  if (!window.confirm("把这句话交给引擎执行？\n\n目录：" + dir +
      "\n档位：" + modeText + "\n\n" + text)) return;
  try {
    const r = await post("/api/agent/start", {
      text: text, directory: dir, mode: p.mode || "ask", force: true,
    });
    addMsg("bot", "已交给引擎：" + (r.reason || "正在后台跑"), "kind-answer", null, false);
    ta.value = ""; ta.style.height = "auto";
    // 不跳工作台（V1.175）：进度 / 答复 / 改动就在对话页右栏看，人才不离开这一轮。
    // 宽屏直接开右栏；逐次授权要弹卡时右栏会给「去工作台决定」入口（授权闸门没动）。
    if (typeof chatPanelToggle === "function") chatPanelToggle("side", true);
    if (typeof chatSidePaint === "function") chatSidePaint();
  } catch (e) { toast("派发失败：" + e.message); }
}

function chatToolsMount() {
  const chat = document.querySelector(".chat");
  if (!chat || $("#chat-tools")) return;
  const box = el("div", "chat-tools");
  box.id = "chat-tools";
  const btn = el("button", "btn ghost", "···");
  btn.id = "chat-more-btn";
  btn.title = "对话工具：搜索 / 清空 / 多选";
  btn.setAttribute("aria-haspopup", "true");
  btn.setAttribute("aria-expanded", "false");
  const menu = el("div", "menu-pop chat-tool-menu");
  menu.id = "chat-tool-menu";
  menu.hidden = true;
  const add = (label, fn) => {
    const b = el("button", null, label);
    b.onclick = () => {
      menu.hidden = true; btn.setAttribute("aria-expanded", "false"); fn();
    };
    menu.appendChild(b);
    return b;
  };
  // 对话是唯一入口：一句话在这里说完，"交给它"也在这里（V1.152）。
  add("交给它执行", chatDelegate);
  // 工作区 / 产物默认收起（V1.159）：开合统一走 chatPanelToggle，
  // 菜单文案每次打开都按当前状态刷新，免得写着"打开"点了却关掉。
  const railItem = add("打开工作区", () => chatPanelToggle("rail"));
  const sideItem = add("打开产物", () => chatPanelToggle("side"));
  add("查找对话内容（Ctrl+F）", chatSearchOpen);
  if (history_.length) {
    add("清空对话", clearHistory);
    add("多选删除", chatEnterPick);
  }
  btn.onclick = (e) => {
    e.stopPropagation();
    const rail = $("#chat-rail"), side = $("#chat-side");
    if (rail && railItem) railItem.textContent = rail.hidden ? "打开工作区" : "收起工作区";
    if (side && sideItem) sideItem.textContent = side.hidden ? "打开产物" : "收起产物";
    menu.hidden = !menu.hidden;
    btn.setAttribute("aria-expanded", String(!menu.hidden));
  };
  box.appendChild(btn); box.appendChild(menu);
  chat.appendChild(box);
  if (!chatToolsBound) {
    chatToolsBound = true;
    // 监听只绑一次，但不能捕获当次的 menu/btn —— renderChatShell 每次
    // root.innerHTML="" 重建 .chat，被捕获的节点重绘后就不在文档里了，
    // Esc / 点外关闭会永久失效（同 V1.142 文件清单"状态绑旧节点"的教训）。
    // 现查现用，监听活的 DOM。
    document.addEventListener("click", (e) => {
      const b = $("#chat-tools");
      if (b && !b.contains(e.target)) {
        const m = $("#chat-tool-menu"), mb = $("#chat-more-btn");
        if (m) m.hidden = true;
        if (mb) mb.setAttribute("aria-expanded", "false");
      }
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        const m = $("#chat-tool-menu"), mb = $("#chat-more-btn");
        if (m) m.hidden = true;
        if (mb) mb.setAttribute("aria-expanded", "false");
      }
    });
  }
}

let faSearchIdx = -1;

function chatSearchOpen() {
  if ($("#fa-search")) { $("#fa-search-inp").focus(); return; }
  const log = $("#chatlog");
  if (!log || !log.parentNode) return;
  const bar = el("div", "fa-search");
  bar.id = "fa-search";
  const inp = el("input");
  inp.id = "fa-search-inp";
  inp.type = "search";
  inp.placeholder = "搜这段对话（Enter 下一条，Esc 关闭）";
  const cnt = el("span", "fa-search-cnt");
  cnt.id = "fa-search-cnt";
  const prev = el("button", "btn ghost", "上一条");
  const next = el("button", "btn ghost", "下一条");
  const close = el("button", "btn ghost", "关闭");
  prev.onclick = () => chatSearchStep(-1);
  next.onclick = () => chatSearchStep(1);
  close.onclick = chatSearchClose;
  inp.oninput = () => chatSearchRun(inp.value);
  inp.onkeydown = (e) => {
    if (e.key === "Enter") { e.preventDefault(); chatSearchStep(e.shiftKey ? -1 : 1); }
    else if (e.key === "Escape") { e.preventDefault(); chatSearchClose(); }
  };
  bar.appendChild(inp); bar.appendChild(cnt); bar.appendChild(prev);
  bar.appendChild(next); bar.appendChild(close);
  log.parentNode.insertBefore(bar, log);
  inp.focus();
}

function chatSearchClear() {
  document.querySelectorAll(".msg .bubble").forEach((b) => {
    if (b.dataset.faOrig !== undefined) {
      b.textContent = b.dataset.faOrig;  // 还原原文，撤掉所有 <mark>
      delete b.dataset.faOrig;
    }
  });
  faSearchHits = [];
  faSearchIdx = -1;
}

function chatSearchRun(q) {
  chatSearchClear();  // 先还原上一次高亮，否则 <mark> 会嵌套
  const cnt = $("#fa-search-cnt");
  if (!q) { if (cnt) cnt.textContent = ""; return; }
  const needle = q.toLowerCase();
  document.querySelectorAll(".msg .bubble").forEach((b) => {
    const raw = b.textContent || "";
    const lower = raw.toLowerCase();
    if (lower.indexOf(needle) === -1) return;
    b.dataset.faOrig = raw;  // 原文存一份，关闭 / 下次搜索时还原
    let from = 0;
    let pos = lower.indexOf(needle, from);
    while (pos !== -1) {
      if (pos > from) b.appendChild(document.createTextNode(raw.slice(from, pos)));
      const m = el("mark", "fa-hit");
      m.textContent = raw.slice(pos, pos + q.length);  // 动态文本走 textContent
      b.appendChild(m);
      from = pos + q.length;
      pos = lower.indexOf(needle, from);
      faSearchHits.push({ bubble: b, mark: m });
    }
    if (from < raw.length) b.appendChild(document.createTextNode(raw.slice(from)));
  });
  if (cnt) cnt.textContent = faSearchHits.length
    ? "1 / " + faSearchHits.length : "没有命中";
  faSearchIdx = faSearchHits.length ? 0 : -1;
  chatSearchMark();
}

function chatSearchMark() {
  faSearchHits.forEach((h, i) => h.mark.classList.toggle("fa-hit-cur", i === faSearchIdx));
}

function chatSearchStep(dir) {
  if (!faSearchHits.length) return;
  faSearchIdx = (faSearchIdx + dir + faSearchHits.length) % faSearchHits.length;
  chatSearchMark();
  const h = faSearchHits[faSearchIdx];
  if (h && h.mark.scrollIntoView) h.mark.scrollIntoView({ block: "center", behavior: "smooth" });
  const cnt = $("#fa-search-cnt");
  if (cnt) cnt.textContent = (faSearchIdx + 1) + " / " + faSearchHits.length;
}

function chatSearchClose() {
  chatSearchClear();
  const bar = $("#fa-search");
  if (bar && bar.parentNode) bar.parentNode.removeChild(bar);
}

// Ctrl/Cmd+F 只在对话视图接管，别的视图让浏览器自己搜。
document.addEventListener("keydown", (e) => {
  if (!(e.ctrlKey || e.metaKey) || e.key.toLowerCase() !== "f") return;
  if (typeof view !== "undefined" && view !== "chat") return;
  e.preventDefault();
  chatSearchOpen();
});"""
