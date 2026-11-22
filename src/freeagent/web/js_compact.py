"""简洁模式开关（V1.132）。

默认开：第一次进来就把工作台的过程信息（步骤 / 文件 / 命令足迹）折起来，
让人第一眼看到「做了什么、说了什么」，而不是几十行工具流水。

为什么状态存 localStorage：切换是人的偏好，不该每刷新一次就重来。为什么
读不到按「开」处理：隐私模式下 localStorage 可能抛异常，此时 fail-closed
到「简洁」—— 宁可多折一次，也不把过程信息糊人一脸。

为什么按钮由 JS 挂进 #nav 而不是写死在 markup：开关是纯前端偏好，不值得
为它动静态骨架；nav 的 ghost 样式已经现成（style_nav.py）。
"""

from __future__ import annotations

__all__ = ["JS_COMPACT"]

JS_COMPACT = r"""// 简洁模式（V1.132）：默认折叠过程信息，按钮挂在导航条上随时切回详细。
const FA_COMPACT_KEY = "fa_compact";
let _faCompactMem = null;  // 内存覆盖：localStorage 写不进时本次仍生效

function compactOn() {
  if (_faCompactMem !== null) return _faCompactMem;
  try {
    // 只有显式存成 "0" 才是关；没存过 / 读不到都按默认开。
    return localStorage.getItem(FA_COMPACT_KEY) !== "0";
  } catch (e) {
    return true;
  }
}

function compactApply() {
  const on = compactOn();
  document.body.classList.toggle("compact", on);
  const b = $("#compact-btn");
  if (b) {
    b.textContent = on ? "简洁" : "详细";
    b.setAttribute("aria-pressed", on ? "true" : "false");
    b.title = on
      ? "当前简洁模式：折叠工具过程。点这里看详细。"
      : "当前详细模式：展开全部过程。点这里回到简洁。";
  }
}

function compactToggle() {
  const next = !compactOn();
  _faCompactMem = next;  // 先让本次点击生效，不依赖 localStorage 写成功
  try {
    localStorage.setItem(FA_COMPACT_KEY, next ? "1" : "0");
  } catch (e) {
    // 写不进去（隐私模式等）：本次仍切换，只是刷新后记不住。
  }
  compactApply();
}

function compactInit() {
  if ($("#compact-btn")) { compactApply(); return; }  // 幂等：重复调用不重复挂
  const nav = $("#nav");
  if (!nav) return;
  const b = el("button", "ghost", "简洁");
  b.id = "compact-btn";
  b.type = "button";
  b.title = "折叠工作台的工具过程；点这里切换简洁 / 详细";
  b.setAttribute("aria-pressed", "true");
  b.onclick = compactToggle;
  nav.appendChild(b);
  compactApply();
}
compactInit();"""
