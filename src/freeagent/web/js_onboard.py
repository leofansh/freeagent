"""对话首屏欢迎区（V1.145）。

为什么独立成文件：``js_chat.py`` 已贴 250 行上限，首屏只做「空对话时
摆一块居中欢迎区 + 可点例子」，不该挤进去。

纪律：点例子只**填进输入框**（与 chips 同一交互，可改再发），不替人发；
欢迎区只在历史为空时出现，有消息后由 ``addMsg`` 摘掉 ``.is-empty``。
它取代 V1.133 的可关闭引导卡 —— 空对话本来就该友好，永久关闭是反模式
（关掉之后首屏只剩一个空框，人反而更不知道从哪开始）。
"""

from __future__ import annotations

__all__ = ["JS_ONBOARD"]

JS_ONBOARD = r"""// 首屏欢迎区：空对话时居中摆一句介绍 + 可点的例子。点一下填框，不发送。
function renderChatWelcome(menu) {
  const log = $("#chatlog");
  if (!log || log.querySelector(".chat-welcome")) return;

  const box = el("div", "chat-welcome");
  box.appendChild(el("div", "cw-hero", "你好，我是值守"));
  box.appendChild(el("div", "cw-sub", esc(menu.notice || "")));
  box.appendChild(el("div", "cw-label", "试试这样问"));

  const grid = el("div", "cw-grid");
  (menu.can_do || []).forEach((s) => {
    const b = el("button", "cw-card", esc(s));
    b.onclick = () => {
      const ta = $("#chatinput");
      if (!ta) return;
      ta.value = s;
      ta.focus();
      ta.dispatchEvent(new Event("input"));
    };
    grid.appendChild(b);
  });
  box.appendChild(grid);
  log.appendChild(box);
}
"""
