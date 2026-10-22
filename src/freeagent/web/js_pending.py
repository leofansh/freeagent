"""Web「待处理」视图：收集器里的捕获 + 一键澄清成事务（设计文档 11.18）。

为什么独立成文件：``js_detail.py`` 已顶到 250 行上限（``TestWebPackageStaysSmall``
守着那条线）。列表数据来自只读 ``/api/pending``，澄清走
``POST /api/pending/clarify`` —— **不自猜内容**：澄清是唯一建事务入口，
标题缺省时由服务层给诚实标题（文件名 / 图片+时间），不编内容（12.1.1）。
"""

from __future__ import annotations

__all__ = ["JS_PENDING"]

JS_PENDING = r"""async function renderPending(root) {
  root.innerHTML = "";
  let data;
  try {
    data = await api("/api/pending");
  } catch (e) {
    root.innerHTML = '<div class="note">待处理读不出来：' + esc(e.message) + "</div>";
    return;
  }
  if (!data.count) {
    root.appendChild(el("div", "empty", "收集器是空的。飞书里收下的图片/文件会在这里排队等你澄清。"));
    return;
  }
  root.appendChild(el("h2", null, "待处理收集器 · " + data.count + " 条"));
  // 只摆事实（11.18）：类型、序号、名字、大小、时间、路径。「它是什么」
  // 由用户澄清时定 —— 界面不猜内容。
  data.items.forEach((it) => {
    const kindLabel = it.kind === "image" ? "图片" : "文件";
    const card = el("div", "card");
    const row = el("div", "row1");
    const title = el("span", "title", "第 " + it.index + " 条 · " + kindLabel + " · " + esc(it.name));
    row.appendChild(title);
    card.appendChild(row);
    let meta = "";
    if (it.size) meta += "大小 " + it.size + " B";
    if (it.created_at) meta += (meta ? " · " : "") + "收下 " + esc(it.created_at.slice(0, 16).replace("T", " "));
    if (it.path) meta += (meta ? " · " : "") + "本地 " + esc(it.path);
    if (meta) card.appendChild(el("div", "meta", meta));
    const acts = el("div", "acts");
    const cbtn = el("button", "btn primary", "澄清成事务");
    cbtn.onclick = () => openClarify(it.id, card, acts);
    acts.appendChild(cbtn);
    card.appendChild(acts);
    root.appendChild(card);
  });
  $("#foot").textContent = "澄清是唯一把捕获变成事务的入口（11.18）；不猜内容，标题由你定，留空用诚实标题。";
}

function openClarify(id, card, acts) {
  acts.innerHTML = "";
  const input = el("input");
  input.placeholder = "标题（可留空：用诚实标题 —— 文件名 / 图片+时间）";
  const ok = el("button", "btn primary", "确认澄清");
  const cancel = el("button", "btn", "取消");
  ok.onclick = () => clarify(id, input.value, ok);
  cancel.onclick = () => renderPending($("#root"));
  acts.appendChild(input);
  acts.appendChild(ok);
  acts.appendChild(cancel);
  input.focus();
}

async function clarify(id, title, btn) {
  btn.disabled = true;
  try {
    const body = { id: id };
    const t = (title || "").trim();
    if (t) body.title = t; // 缺省走服务层诚实标题（不猜内容）
    await post("/api/pending/clarify", body);
    toast("已澄清成事务");
    renderPending($("#root"));
  } catch (e) {
    btn.disabled = false;
    toast("澄清失败：" + e.message);
  }
}"""
