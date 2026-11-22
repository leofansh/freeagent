"""工作台前端（五）：工作区常驻左栏（V1.139 / 设计文档 21.7）。

## 从下拉改成常驻左栏

项目选择器（V1.124）是「一枚按钮 + 下拉」，选完即收起 —— 人干活的全过程
里看不见「我还有哪些工作区、各自跑过多少」。CLOAK 把工作区钉在左栏，这次
抄的只是这层**可见性**，不抄它的团队后台，也不抄「完全访问权限」默认放开
（两样都不合本仓的本地单人 / fail-closed 口径，见 21.7）。

## 累积块（``#wb-harness``）是什么

选中工作区之后，把「它已经记住了什么」摆出来：备注、跑过几轮、这一轮会不
会带上今天的记录、规则从哪来。四行**全是已有事实的回显**，不是新能力；
没有的一项如实写「还没有」，读不出来的写「读不到」—— 两者不能合成一句话。

## 位置

左栏挂在 ``.wb-cols`` 最左（``wbRailMount`` 由 ``loadWorkbench`` 一行接进来）。
本文件不重建正文 DOM：只画 ``#wb-rail-list`` 与 ``#wb-harness``，正文里的
``#wb-inject`` 勾选状态读取后只做回显。
"""

from __future__ import annotations

__all__ = ["JS_WORKBENCH_RAIL"]

JS_WORKBENCH_RAIL = r"""let wbRailQ = "";
let wbRailEditing = false;
let wbRailEditId = "";

function wbRailMount() {
  const col = el("div", "wb-col-rail");
  col.id = "wb-rail";
  const card = el("div", "card wb-rail-card");
  card.appendChild(el("div", "wb-label", "工作区"));
  const filter = el("input");
  filter.id = "wb-rail-filter";
  filter.type = "search";
  filter.placeholder = "搜索工作区…";
  filter.oninput = () => { wbRailQ = filter.value; wbRailPaint(); };
  card.appendChild(filter);
  const list = el("div");
  list.id = "wb-rail-list";
  card.appendChild(list);
  col.appendChild(card);
  const harness = el("div", "card wb-harness");
  harness.id = "wb-harness";
  col.appendChild(harness);
  return col;
}

function wbRailPaint() {
  const list = $("#wb-rail-list");
  if (!list) return;
  // 切了工作区就不再停留在上一块的备注编辑里（否则会对着新项目写旧备注）。
  if (wbRailEditId && wbRailEditId !== wbProjectId) wbRailEditing = false;
  list.innerHTML = "";
  const q = (wbRailQ || "").trim().toLowerCase();
  const all = wbProjects || [];
  const hits = all.filter((p) => !q ||
    (p.name || "").toLowerCase().includes(q) ||
    (p.directory || "").toLowerCase().includes(q));
  if (!hits.length) {
    list.appendChild(el("div", "note", all.length
      ? "没有匹配的工作区" : "还没有工作区 —— 选一个本地文件夹，它就从这里开始积累"));
    if (!all.length) {
      const add = el("button", "btn", "选本地文件夹");
      add.onclick = () => wbBrowseImport();
      list.appendChild(add);
    }
  }
  hits.forEach((p) => {
    const cls = "btn wb-rail-row" + (p.id === wbProjectId ? " is-current" : "");
    const row = el("button", cls);
    row.appendChild(el("div", "wb-rail-name", esc(p.name || "")));
    row.appendChild(el("div", "wb-rail-meta", wbRailMeta(p)));
    row.onclick = () => wbSelectProject(p.id);
    list.appendChild(row);
  });
  if (!wbRailEditing) wbHarnessPaint();
}

function wbRailMeta(p) {
  const bits = [wbTurnsText(p.turns)];
  if (p.directory) bits.push(p.directory);
  return esc(bits.join(" · "));
}

function wbTurnsText(n) {
  // 读不到（null）与还没跑过（0）分成两句话说 —— 把读不出来写成 0 是替人下结论。
  if (n == null) return "轮次读不到";
  return n ? (n + " 轮") : "还没跑过";
}

function wbHarnessPaint() {
  const box = $("#wb-harness");
  if (!box) return;
  box.innerHTML = "";
  box.appendChild(el("div", "wb-label", "这个工作区累积了什么"));
  const cur = typeof wbCurrentProject === "function" ? wbCurrentProject() : null;
  if (!cur) {
    box.appendChild(el("div", "note",
      "还没选中工作区 —— 选中之后这里会列出它已经记住了什么。"));
    return;
  }
  if (wbRailEditing) {
    const ta = el("textarea");
    ta.id = "wb-rail-note";
    ta.value = cur.note || "";
    box.appendChild(ta);
    const row = el("div", "row1");
    const save = el("button", "btn primary", "存下");
    save.onclick = () => wbRailSaveNote(ta.value);
    const cancel = el("button", "btn", "取消");
    cancel.onclick = () => { wbRailEditing = false; wbHarnessPaint(); };
    row.appendChild(save);
    row.appendChild(cancel);
    box.appendChild(row);
  } else {
    // 一行一条：键在上一行小字，值在下一行 —— 备注可能很长，并排挤成
    // 「备注 文字 改」会把按钮顶到第二行（V1.139 走查看到的样子）。
    const line = el("div", "wb-harness-line");
    const head = el("div", "wb-harness-head");
    head.appendChild(el("span", "wb-harness-k", "备注"));
    const edit = el("button", "btn wb-harness-edit", "改");
    edit.onclick = () => {
      wbRailEditing = true;
      wbRailEditId = cur.id;
      wbHarnessPaint();
    };
    head.appendChild(edit);
    line.appendChild(head);
    line.appendChild(el("span", "wb-harness-v",
      cur.note ? esc(cur.note) : "还没有备注"));
    box.appendChild(line);
  }
  box.appendChild(wbHarnessLine("跑过", wbTurnsText(cur.turns)));
  // 勾选框是正文里的那一个（#wb-inject）：这里只回显，不在两处各放一个开关。
  const inject = $("#wb-inject");
  box.appendChild(wbHarnessLine("背景",
    (inject && !inject.checked) ? "只带这句话（已关）" : "今天的事 / 承诺 / 记忆"));
  box.appendChild(wbHarnessLine("规则", "项目里的 AGENTS.md 由后台引擎自己读"));
}

function wbHarnessLine(k, v) {
  const line = el("div", "wb-harness-line");
  line.appendChild(el("span", "wb-harness-k", k));
  line.appendChild(el("span", "wb-harness-v", esc(v)));
  return line;
}

async function wbRailSaveNote(text) {
  const cur = wbCurrentProject();
  if (!cur) return;
  try {
    await post("/api/agent/project_update", {
      id: cur.id, name: cur.name, note: (text || "").trim(),
    });
    wbRailEditing = false;
    wbRailEditId = "";
    wbLoadProjects();
    toast("备注存下了 —— 下一轮会作为背景带上");
  } catch (e) {
    toast(e.message);
  }
}
"""
