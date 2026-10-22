"""Web「催办」视图：该催的候选 + 起草铺事实留白草稿（设计文档 11.20 / 20.1.4）。

为什么独立成文件：``js_detail.py`` 已顶到 250 行上限（``TestWebPackageStaysSmall``
守着那条线）。候选来自只读 ``/api/nudge/candidates``，起草走
``/api/nudge/draft?task_id=xxx`` —— **系统没有发送能力**：草稿只铺事实、
正文留白（[TODO]），复制出去发不发由用户（20.1.4）。不代替用户结账
（11.20.2 纪律 4）。
"""

from __future__ import annotations

__all__ = ["JS_NUDGE"]

JS_NUDGE = r"""async function renderNudge(root) {
  root.innerHTML = "";
  let data;
  try {
    data = await api("/api/nudge/candidates");
  } catch (e) {
    root.innerHTML = '<div class="note">催办读不出来：' + esc(e.message) + "</div>";
    return;
  }
  if (!data.count) {
    root.appendChild(el("div", "empty", "现在没有该催的。等对方的（follow_up 已过）或我欠对方的（承诺逾期）会在这里出现。"));
    return;
  }
  root.appendChild(el("h2", null, "该催的 · " + data.count + " 条"));
  // 只摆事实（11.20）：方向 / 对象 / 等了多久。不判违约、不自动催。
  data.items.forEach((it) => {
    const card = el("div", "card");
    const row = el("div", "row1");
    const dir = it.direction === "wait" ? "等对方（问进度/提醒交付）" : "欠对方（告知延期/给新时间）";
    const who = it.contact_name || "[未挂关系对象]";
    row.appendChild(el("span", "title", esc(it.task_title) + " · " + esc(who) + " · " + esc(it.elapsed)));
    card.appendChild(row);
    card.appendChild(el("div", "meta", "方向：" + dir));
    const acts = el("div", "acts");
    const dbtn = el("button", "btn primary", "起草");
    dbtn.onclick = () => openDraft(it.task_id, acts, dbtn);
    acts.appendChild(dbtn);
    card.appendChild(acts);
    root.appendChild(card);
  });
  $("#foot").textContent = "草稿只铺库里的事实、正文留白（11.20）。系统不会发 —— 复制出去，发不发你定。";
}

async function openDraft(taskId, acts, btn) {
  btn.disabled = true;
  try {
    const data = await api("/api/nudge/draft?task_id=" + encodeURIComponent(taskId));
    acts.innerHTML = "";
    const ta = el("textarea");
    ta.rows = 12;
    ta.value = data.draft;
    ta.readOnly = false;
    const cbtn = el("button", "btn", "复制草稿");
    cbtn.onclick = () => copyDraft(ta, cbtn);
    acts.appendChild(ta);
    acts.appendChild(cbtn);
  } catch (e) {
    btn.disabled = false;
    toast("起草失败：" + e.message);
  }
}

async function copyDraft(ta, btn) {
  try {
    await navigator.clipboard.writeText(ta.value);
    toast("已复制。系统不会替你发 —— 粘贴到你想发的地方再发。");
  } catch (e) {
    ta.select();
    toast("自动复制被拦，请手动全选复制（Ctrl+C）");
  }
}"""
