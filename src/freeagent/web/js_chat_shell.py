"""对话页的三栏外壳（V1.154）：左工作区 / 中对话 / 右产物，两侧可收。

OpenCode 与 WorkBuddy 都是这个形态：中间永远是一条对话，工作痕迹在右栏。
本模块只负责把现有数据端点摆成两侧面板，不改任何写入口 / 授权 / 端点。
"""
from __future__ import annotations

__all__ = ["JS_CHAT_SHELL"]

JS_CHAT_SHELL = r"""// 对话页三栏外壳：左工作区 / 中对话 / 右产物，两侧可收（V1.154）。
function chatShellMount() {
  const root = $("#root");
  const center = root && root.querySelector(".chat");
  if (!root || !center || $("#chat-shell")) return;
  const shell = el("div", "chat-shell");
  const left = el("aside", "chat-rail"); left.id = "chat-rail";
  const right = el("aside", "chat-side"); right.id = "chat-side";
  const lt = el("button", "btn ghost", "工作区"); lt.id = "chat-rail-toggle";
  const rt = el("button", "btn ghost", "产物"); rt.id = "chat-side-toggle";
  lt.onclick = () => { left.hidden = !left.hidden; };
  rt.onclick = () => { right.hidden = !right.hidden; };
  left.appendChild(lt); right.appendChild(rt);
  shell.appendChild(left); shell.appendChild(center); shell.appendChild(right);
  root.appendChild(shell);
  chatRailPaint(); chatSidePaint(); chatBarControls();
}

async function chatBarControls() {
  const tools = document.querySelector(".chatbar-tools");
  const arrow = tools && tools.querySelector("#chatsend");
  if (!tools || !arrow || $("#chat-ctrl-project")) return;
  const mk = (id, title) => {
    const s = el("select", "chat-ctrl"); s.id = id; s.title = title;
    tools.insertBefore(s, arrow); return s;
  };
  const proj = mk("chat-ctrl-project", "项目");
  const model = mk("chat-ctrl-model", "模型");
  const mode = mk("chat-ctrl-mode", "授权档");
  const fill = (sel, items, current) => {
    items.forEach(([v, label]) => {
      const o = el("option"); o.value = v; o.textContent = label;
      if (v === current) o.selected = true;
      sel.appendChild(o);
    });
  };
  const save = async (body) => {
    try { await post("/api/agent/prefs_save", body); }
    catch (e) { toast("保存失败：" + e.message); }
  };
  const [ps, st, oc] = await Promise.all([
    api("/api/agent/projects").catch(() => ({ projects: [] })),
    api("/api/agent/state").catch(() => ({ prefs: {}, modes: [] })),
    api("/api/oc/options").catch(() => ({ model: { options: [] } })),
  ]);
  const prefs = st.prefs || {};
  fill(proj, [["", "选项目"], ...(ps.projects || []).map(
    (p) => [p.id, p.name || p.path || "未命名"])], prefs.project_id || "");
  fill(model, (oc.model && oc.model.options || []).map(
    (o) => [o.value, o.label]), prefs.model || "");
  fill(mode, (st.modes || []).map((m) => [m.key, m.label]), prefs.mode || "ask");
  proj.onchange = () => post("/api/agent/project_select", { id: proj.value })
    .then(() => { toast("已切项目"); chatSidePaint(); })
    .catch((e) => toast("切换失败：" + e.message));
  model.onchange = () => save({ model: model.value });
  mode.onchange = () => save({ mode: mode.value });
}

async function chatRailPaint() {
  const box = $("#chat-rail");
  if (!box) return;
  let data;
  try { data = await api("/api/agent/projects"); }
  catch (e) { box.appendChild(el("div", "hint", "工作区读不到：" + e.message)); return; }
  const list = (data.projects || []).slice(0, 20);
  if (!list.length) { box.appendChild(el("div", "hint", "还没有项目档案，去工作台新建一个。")); return; }
  const wrap = el("div", "chat-rail-list");
  list.forEach((p) => {
    const b = el("button", "chat-rail-item", p.name || p.path || "未命名");
    b.title = p.path || "";
    b.onclick = async () => {
      try {
        await post("/api/agent/project_select", { id: p.id });
        toast("已切到「" + (p.name || p.path) + "」");
        chatSidePaint();
      } catch (e) { toast("切换失败：" + e.message); }
    };
    wrap.appendChild(b);
  });
  box.appendChild(wrap);
}

async function chatSidePaint() {
  const box = $("#chat-side");
  if (!box) return;
  let st;
  try { st = await api("/api/agent/state"); }
  catch (e) { box.appendChild(el("div", "hint", "产物读不到：" + e.message)); return; }
  const turn = st.turn;
  const head = el("div", "row1", "产物"); box.appendChild(head);
  if (!turn) {
    box.appendChild(el("div", "hint", "还没有委派产物。在对话里说一句，点「… → 交给它执行」。"));
    return;
  }
  box.appendChild(el("div", "hint", "最近一轮：" + (turn.status || "未知") +
    (turn.brief ? " · " + turn.brief : "")));
  const files = turn.touched_files || [];
  if (!files.length) { box.appendChild(el("div", "hint", "这一轮没有可列出的改动文件。")); return; }
  const ul = el("div", "chat-side-files");
  files.forEach((f) => ul.appendChild(el("div", "chat-side-file", typeof f === "string" ? f : (f.path || ""))));
  box.appendChild(ul);
}
"""
