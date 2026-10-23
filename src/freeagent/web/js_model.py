"""Web「画像」视图（设计文档 V1.59）：个人模型 —— 节奏 / 关系 / 承诺 三面。

为什么独立成文件：``js_detail.py`` 已顶到 250 行上限（``TestWebPackageStaysSmall``
守着那条线）。渲染只调 ``/api/model``，**不自算任何业务判断** —— 逾期、几条承诺
全是服务层说了算（12.1.1「一份能力一份实现」）。关系账里的卡片点了调
``openContact``（定义在 js_contacts，同一个 <script> 里全局可用），直接复用
「见人前看一页账」，不另写一遍。
"""

from __future__ import annotations

__all__ = ["JS_MODEL"]

JS_MODEL = r"""async function renderModel(root) {
  root.innerHTML = "";
  let m;
  try {
    m = await api("/api/model");
  } catch (e) {
    root.innerHTML = '<div class="note">画像读不出来：' + esc(e.message) + "</div>";
    return;
  }
  const days = Math.round((new Date(m.until) - new Date(m.since)) / 86400000) + 1;
  root.appendChild(el("h2", null, "连续性个人模型 · 近 " + days + " 天"));

  // 节奏：只摆事实，不排序打分、不给建议（与 /model 同一份纪律）。
  const rhythm = [];
  if (m.done_in_window) rhythm.push("完成 " + m.done_in_window + " 件");
  if (m.rollover_total) rhythm.push("顺延 " + m.rollover_total + " 次");
  if (m.stuck_waiting.length) rhythm.push("卡住的等候 " + m.stuck_waiting.length + " 件");
  if (rhythm.length) {
    root.appendChild(el("h3", null, "节奏"));
    root.appendChild(el("div", "meta", rhythm.join(" · ")));
    if (m.role_activity.length) {
      root.appendChild(el("div", "meta",
        "角色活跃度：" + m.role_activity.map(r => r.name + " " + r.count).join(" · ")));
    }
  } else {
    root.appendChild(el("div", "note", "节奏：这段时间没有完成的记录。"));
  }

  // 关系账：只列「有未结清账」的人，点开看一页账（复用 openContact）。
  root.appendChild(el("h3", null, "关系账（还跟谁有未结清的账）"));
  if (!m.relationships.length) {
    root.appendChild(el("div", "note", "目前没有挂到具体人的未结清承诺或等候。"));
  } else {
    m.relationships.forEach((c) => {
      const bits = [];
      if (c.open_promises) bits.push("我欠他 " + c.open_promises + " 条承诺");
      if (c.open_waitings) bits.push("在等他 " + c.open_waitings + " 笔");
      const card = el("div", "card clickable");
      card.innerHTML =
        '<div class="row1"><span class="title">' + esc(c.name) + "</span></div>" +
        '<div class="meta">' + esc(bits.join(" · ")) + "</div>";
      card.onclick = () => openContact(c.id);
      root.appendChild(card);
    });
  }

  // 承诺与目标：对外承诺（可能已逾期）+ 记忆里的「约定 / 目标」，原样列出。
  root.appendChild(el("h3", null, "承诺与目标"));
  if (!m.commitments.length) {
    root.appendChild(el("div", "note",
      "没有未兑现的对外承诺，也没记过「约定 / 目标」。"));
  } else {
    m.commitments.forEach((c) => {
      let line = "[" + c.kind + "] " + esc(c.text);
      if (c.overdue) line += " · 已逾期";
      root.appendChild(el("div", "meta", line));
    });
  }

  root.appendChild(el("div", "note",
    "以上是从你的记录里现推的**事实**，不是评价 —— 要不要顺着它调整，由你决定。"));
}"""
