"""Web「关系」视图：联系人列表 + 点开看一页账（设计文档 11.20.6）。

为什么独立成文件：``js_detail.py`` 已顶到 250 行上限（``TestWebPackageStaysSmall``
守着那条线）。渲染只调 ``/api/contacts`` 与 ``/api/contact`` 两个端点，
**不自算任何业务判断** —— 哪些逾期、几条承诺，全是服务层说了算
（12.1.1「一份能力一份实现」）。判违约是用户的事，界面只摆事实。
"""

from __future__ import annotations

__all__ = ["JS_CONTACTS"]

JS_CONTACTS = r"""async function renderContacts(root) {
  root.innerHTML = "";
  let data;
  try {
    data = await api("/api/contacts");
  } catch (e) {
    root.innerHTML = '<div class="note">关系读不出来：' + esc(e.message) + "</div>";
    return;
  }
  if (!data.count) {
    root.appendChild(el("div", "empty", "还没有关系对象。记一条带 关系=名字 的承诺或等候，就会在这里按人聚合。"));
    return;
  }
  root.appendChild(el("h2", null, "关系对象 · " + data.count + " 个"));
  // 列表只给「几条承诺 + 几笔在等」两行计数，点进去才是逐条账 ——
  // 列表别堆细节，明细交给一页账（同 /api/contacts 的设计）。
  data.items.forEach((c) => {
    const tags = [];
    if (c.open_promises) tags.push(c.open_promises + " 条承诺未兑现");
    if (c.open_waitings) tags.push(c.open_waitings + " 笔在等他");
    const card = el("div", "card clickable");
    card.innerHTML =
      '<div class="row1"><span class="title">' + esc(c.name) + "</span>" +
      (c.note ? '<span class="meta">' + esc(c.note) + "</span>" : "") +
      "</div>" +
      (tags.length ? '<div class="meta">' + tags.join(" · ") + "</div>" : "");
    card.onclick = () => openContact(c.id);
    root.appendChild(card);
  });
  $("#foot").textContent = "关系可在飞书/终端用 /contact 管理，在对话里敲 /contact <名字> 直接看一页账。";
}

async function openContact(id) {
  const root = $("#root");
  root.innerHTML = '<div class="empty">载入中…</div>';
  let ledger;
  try {
    ledger = await api("/api/contact?id=" + id);
  } catch (e) {
    toast("读一页账失败：" + e.message);
    load("contacts");
    return;
  }
  root.innerHTML = "";
  const back = el("button", "btn", "← 返回关系列表");
  back.onclick = () => load("contacts");
  root.appendChild(back);
  root.appendChild(el("h2", null, esc(ledger.contact.name) + " · 一页账"));
  root.appendChild(el("h3", null, "我欠他的（未兑现承诺 · " + ledger.promises.length + " 条）"));
  if (!ledger.promises.length) root.appendChild(el("div", "note", "没有未兑现的承诺。"));
  // 只摆事实：逾期标「已逾期」，不写「违约」—— 判违约是用户的事（11.19.5）。
  ledger.promises.forEach((p) => {
    let line = esc(p.statement);
    if (p.promised_for) {
      line += " · 期限 " + p.promised_for.slice(0, 16).replace("T", " ");
      if (p.overdue) line += " · 已逾期";
    }
    line += " → " + esc(p.task_title);
    root.appendChild(el("div", "meta", line));
  });
  root.appendChild(el("h3", null, "我在等他的（未结束等候 · " + ledger.waitings.length + " 笔）"));
  if (!ledger.waitings.length) root.appendChild(el("div", "note", "没有未结束的等候。"));
  ledger.waitings.forEach((w) => {
    let line = "等 " + esc(w.who_or_what) + "（从 " + w.since.slice(0, 10) + " 起）";
    if (w.follow_up_at) {
      line += " · 跟进 " + w.follow_up_at.slice(0, 16).replace("T", " ");
      if (w.followup_overdue) line += " · 已过跟进点";
    }
    line += " → " + esc(w.task_title);
    root.appendChild(el("div", "meta", line));
  });
  root.appendChild(el("div", "note", "结账：/promise keep|break|withdraw（见 /promise 列表）。记新的：/promise 记 <事务id> <承诺> 关系=" + esc(ledger.contact.name)));
}"""
