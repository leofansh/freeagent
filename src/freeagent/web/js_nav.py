"""「更多」下拉的开合、选中态与聚合角标（V1.119）。

导航一级入口收敛后，低频视图收进下拉；下拉接线单独成块，守住 web 包
单文件 250 行上限（沿用 js_workbench 三段拆块的先例）。

作用域与 js_boot 同属一个拼装出来的 ``<script>``：``MENU_VIEWS`` 与
``closeMore`` 由 boot 的 ``load()`` 引用；函数声明跨块可见，常量在整块
脚本求值后才被运行期调用，顺序上 js_nav 排在 js_boot 之前进页面。
"""

from __future__ import annotations

__all__ = ["JS_NAV"]

JS_NAV = r"""// 收进「更多」下拉的视图清单。选中其中任一视图时，更多按钮要呈现选中态 ——
// 否则高亮停在空的一级导航上，人不知道自己在哪页。
const MENU_VIEWS = new Set([
  "closed", "roles", "model", "week", "contacts", "promises", "pending",
  "nudge", "oc", "services", "feishu", "settings", "harness",
]);

const moreBtn = $("#more-btn");
const morePop = $("#more-pop");

function closeMore() {
  if (!morePop) return;
  morePop.hidden = true;
  if (moreBtn) moreBtn.setAttribute("aria-expanded", "false");
}

moreBtn.onclick = (ev) => {
  ev.stopPropagation();
  morePop.hidden = !morePop.hidden;
  moreBtn.setAttribute("aria-expanded", String(!morePop.hidden));
};

// 点浮层外面或按 Esc 收起；浮层内点选项走 boot 的视图接线，选中后一并收口。
document.addEventListener("click", (ev) => {
  if (!$("#more-menu").contains(ev.target)) closeMore();
});
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") closeMore();
});
"""
