"""对话历史的**存取**（``localStorage``）。

## 为什么单独一个模块

``js_chat.py`` 曾经 259 行、撞了 250 行的体积守卫（``test_web.py``
``TestWebPackageStaysSmall``）。拆分的**边界选在这里**而不是随便一刀：
存取是**另一个职责** —— 渲染关心「消息长什么样」，而这里只关心
「刷新后还在不在」。

## 顺序：必须在 JS_CHAT 之前

``js_chat.py`` 里的 ``addMsg`` / ``renderChatShell`` / ``sendChat`` 全都
直接用 ``chatSave`` / ``chatDay`` / ``lastItemIds``，而 ``js_chat_select``
依赖 ``chatSave``。本模块只声明函数与状态、不调用别的模块的函数，所以
放在 ``JS_CORE`` 之后、``JS_CHAT`` 之前最省心。

``clearHistory`` 里的 ``load("chat")`` 来自 ``js_boot``，而它在**调用时**
才求值（不是定义时），所以本模块排在 ``js_boot`` 之前也没有问题 ——
用户点按钮时整个脚本早就载入完了。
"""

from __future__ import annotations

__all__ = ["JS_CHAT_STORE"]

JS_CHAT_STORE = r"""const CHAT_KEY = "freeagent.chat.v1";
let lastItemIds = [];      // 上一轮答复里的事务 id，用来接「第二个」这类指代

// 当前会话 id（多会话 / V1.176）。空串 = 还没选会话，用基座 key（老数据）。
// chatSessionsBoot 会先从 localStorage 同步恢复，再由 agent_state 异步对账。
let chatSessionId = "";
// 一个会话一个 key —— 一处状态一个真源：切会话只换 id，历史各存各的，
// 不把别的项目/会话的消息混进来（历史会话的 hist: 前缀是合法 key 段）。
function chatKey() { return CHAT_KEY + (chatSessionId ? "." + chatSessionId : ""); }
function chatUseSession(sid) {
  chatSessionId = sid || "";
  lastItemIds = [];          // 上一轮的指代属于上一个会话，不带过来
  try { localStorage.setItem(CHAT_KEY + ".current", chatSessionId); } catch (e) {}
}

// 本地日历日 YYYY-MM-DD —— 与后端 SystemClock.today()（本地日期）对齐。
function chatDay(d) {
  d = d || new Date();
  const p = (n) => (n < 10 ? "0" : "") + n;
  return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate());
}

function chatLoad() {
  try {
    const raw = localStorage.getItem(chatKey());
    if (!raw) return null;
    const data = JSON.parse(raw);
    return Array.isArray(data) ? data : null;
  } catch (e) { return null; }
}

function chatSave(messages) {
  try {
    localStorage.setItem(chatKey(), JSON.stringify(messages.slice(-60)));
  } catch (e) { /* 隐私模式下写不了就算了，不影响主功能 */ }
}

function clearHistory() {
  history_ = [];
  lastItemIds = [];
  try { localStorage.removeItem(chatKey()); } catch (e) {}
  load("chat");
}
"""