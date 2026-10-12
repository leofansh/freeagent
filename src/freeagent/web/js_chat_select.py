"""对话的「多选删除」（对标飞书的「选择以下消息」）。

## 为什么拆出来

js_chat.py 已逼近 250 行守卫（用户工作区版本已超），而这是独立功能域：
选择态、删除语义、工具条。与 :mod:`js_oc_cards` 同一拆法。

## 删除语义（与飞书对齐）

- **删所选**：只删勾上的
- **删到此为止（含这条）**：从勾的那条起，往后全删。刻意**要求只勾一条**：
  飞书原版就是长按某一条出的菜单，「删以下」的锚点必须唯一。勾多条时
  不猜、不默认取最早那条 —— 明说请只勾一条，猜的成本是人丢消息。

## 为什么删除后整段重渲染

``history_`` 与 ``#chatlog`` 的子节点是 1:1 的（addMsg/addChips 各追加
一个）。删除后重渲染是维持这个不变式最便宜的方式；局部摘 DOM 会让
「第 3 条」在数组和界面里指的不是同一样东西，后面的勾选全错位。
工具条挂在 **chat wrapper**（log 的父节点）而不是 log 里 —— 否则它会
成为 log 的子节点，把 1:1 Mapping 打破。
"""

from __future__ import annotations

__all__ = ["JS_CHAT_SELECT"]

JS_CHAT_SELECT = r"""
// 选择态。下标即身份：删除后整段重渲染，不存在「勾了下标 3、删完第 3 条
// 变成别人」的漂移。跨出对话视图即清（js_boot 的导航钩子负责）。
let chatPickMode = false;
const chatPicked = new Set();

function chatPickLog() {
  const log = $("#chatlog");
  return log ? [...log.children] : [];
}

// 给渲染好的消息行挂复选框 + 工具条。非选择模式时是 no-op ——
// loadChat 每次渲染完都会调它，不需要调用方判状态。
function chatPickPaint() {
  $("#chatpickbar")?.remove();
  if (!chatPickMode) return;
  const rows = chatPickLog();
  if (!rows.length) { chatPickExit(true); return; }
  rows.forEach((row, i) => {
    if (row.dataset.pick) return;      // 重绘不重复挂
    row.dataset.pick = "1";
    const box = el("input");
    box.type = "checkbox";
    box.className = "chatpickbox";
    box.checked = chatPicked.has(i);
    box.onclick = (e) => {
      // 必须拦：消息行里的事务卡片有自己的 onclick（打开任务），
      // 不拦的话勾选就等于打开了某个任务。
      e.stopPropagation();
      if (box.checked) chatPicked.add(i); else chatPicked.delete(i);
      chatPickPaint();
    };
    row.insertBefore(box, row.firstChild);
  });
  const wrap = $("#chatlog").parentElement;
  const bar = el("div", "chatpickbar");
  bar.id = "chatpickbar";
  bar.appendChild(el("span", "chatpickcount",
                     "已选 " + chatPicked.size + " 条"));
  const del = el("button", "btn primary", "删所选");
  del.onclick = () => chatPickDelete(null);
  const tail = el("button", "btn", "删到此为止（含这条）");
  tail.title = "从勾选的这条起，把它和之后的都删掉。只勾一条时可用。";
  tail.onclick = () => chatPickDeleteToHere();
  const all = el("button", "btn", "全选");
  all.onclick = () => {
    chatPickLog().forEach((row, i) => {
      chatPicked.add(i);
      const b = row.querySelector(".chatpickbox");
      if (b) b.checked = true;
    });
    chatPickPaint();
  };
  const off = el("button", "btn", "取消");
  off.onclick = () => chatPickExit(true);
  bar.appendChild(del); bar.appendChild(tail);
  bar.appendChild(all); bar.appendChild(off);
  wrap.insertBefore(bar, $("#chatlog"));
}

function chatPickExit(reload) {
  chatPickMode = false;
  chatPicked.clear();
  if (reload) load("chat");
}

function chatEnterPick() {
  if (!history_.length) { toast("还没有对话可删"); return; }
  chatPickMode = true;
  chatPicked.clear();
  load("chat");
}

// 重建指代基线（「第二个」这类指代靠它）。逻辑与 renderChatShell 里
// 那段相同，但不复制一份 —— 删完消息后最后一条带条目的 bot 消息可能
// 已经变了，不重建的话人接一句「第二个」会指向被删掉的那批。
function chatPickRebuildRefs() {
  lastItemIds = [];
  for (let i = history_.length - 1; i >= 0; i--) {
    const m = history_[i];
    if (m.who === "me" || m.who === "chips") continue;
    if (m.items && m.items.length) {
      lastItemIds = m.items.map((x) => x.task_id);
      break;
    }
  }
}

function chatPickDelete(fromIndex) {
  const before = history_.length;
  const kept = fromIndex === null || fromIndex === undefined
    ? history_.filter((m, i) => !chatPicked.has(i))
    : history_.slice(0, fromIndex);
  if (kept.length === before) { toast("没选要删的消息"); return; }
  history_ = kept;
  chatPicked.clear();
  chatPickRebuildRefs();
  chatSave(history_);
  load("chat");
}

function chatPickDeleteToHere() {
  if (chatPicked.size !== 1) {
    toast("「删到此为止」只勾那一条锚点（勾了 " + chatPicked.size + " 条）");
    return;
  }
  const anchor = [...chatPicked][0];
  // 二次确认：这一刀下去删的是**一段**，不是一条。
  const n = history_.length - anchor;
  if (!confirm("删除这条及之后的 " + n + " 条消息？删了找不回来。")) return;
  chatPickDelete(anchor);
}
"""
