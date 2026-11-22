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
let faSearchHits = [];
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
