"""对话页项目面板（V1.165；V1.167 简化入口）：豆包式「选项目 + 选本地文件夹建档」。

另立模块而非塞进 js_chat_shell：面板含搜索 / 列表 / 目录浏览，
会把 js_chat_shell 顶到 250 行上限（工作台选择器也是独立的 js_workbench_projects）。
目录浏览自己画而不复用 wbBrowse：后者绑死 `#wb-dirs` 那套对话页没有的 DOM；
能力在服务端 `/api/agent/dirs`（校验 / 跳重解析点 / 上限 300 都在那里），
两个视图各画各的，共用同一端点与同一写入口 `project_save`，不是第二真源（12.1.1）。
"""

from __future__ import annotations

__all__ = ["JS_CHAT_PROJECTS"]

JS_CHAT_PROJECTS = r"""// 对话页项目面板（V1.165；V1.167 简化入口）：列项目 + 选本地文件夹建档。
// 与工作台共用同一组端点（projects / project_save / project_select / dirs），
// 只在这里另画视图层，写入口仍只有一个。

let chatProjItems = [];  // 项目清单
let chatProjId = "";     // 当前选中
let chatProjBtn = null;  // 触发按钮
let chatProjBound = false;

function chatProjButtonPaint() {
  if (!chatProjBtn) return;
  const cur = chatProjItems.find((p) => p.id === chatProjId) || null;
  chatProjBtn.textContent = cur ? (cur.name || cur.directory || "未命名") : "选项目";
  chatProjBtn.title = cur ? (cur.directory || "") : "选择或新建项目";
}

function chatProjToggle(force) {
  const p = $("#chat-proj-panel");
  if (!p) return;
  const show = force != null ? force : p.hidden;
  p.hidden = !show;
  if (show) chatProjRender();
}

// 点外面 / Esc 关闭只绑一次，但回调里现查当前节点——
// renderChatShell 每次 root.innerHTML='' 重建，捕获旧节点会永久失效（V1.160 教训）。
function chatProjBindClose() {
  if (chatProjBound) return;
  chatProjBound = true;
  document.addEventListener("click", (e) => {
    const p = $("#chat-proj-panel"), wrap = $(".chat-proj-wrap");
    if (p && !p.hidden && wrap && !wrap.contains(e.target)) p.hidden = true;
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { const p = $("#chat-proj-panel"); if (p) p.hidden = true; }
  });
}

function chatProjMount(btn, projects, currentId) {
  chatProjBtn = btn;
  chatProjItems = projects || [];
  chatProjId = currentId || "";
  chatProjButtonPaint();
  if ($("#chat-proj-panel")) return;
  // 按钮外裹一层相对定位容器，面板挂这里——button 里不能放交互式后代。
  const wrap = el("span", "chat-proj-wrap");
  btn.parentNode.insertBefore(wrap, btn);
  wrap.appendChild(btn);
  const panel = el("div", "chat-proj-menu"); panel.id = "chat-proj-panel";
  panel.hidden = true;
  // 面板内点击统一拦在这层，不冒泡到 document 的「点外面关闭」监听。
  // 目录浏览每次重绘 area 都移除刚被点中的 chip / 目录行，等 click 冒泡到
  // document 时 e.target 已脱离 DOM，wrap.contains(e.target) 返回 false →
  // 把面板误关（真实鼠标点击才暴露，evaluate 合成点击不触发这条路径）。
  // 同 V1.160 教训：重绘后旧节点失效；这次栽在 event.target 上。
  // 面板内没有「点了要关闭」的交互，拦在这里不影响任何功能。
  panel.addEventListener("click", (ev) => ev.stopPropagation());
  wrap.appendChild(panel);
  btn.onclick = (ev) => { ev.stopPropagation(); chatProjToggle(); };
  chatProjBindClose();
}

function chatProjRender() {
  const p = $("#chat-proj-panel");
  if (!p) return;
  p.innerHTML = "";
  const filter = el("input"); filter.type = "text";
  filter.id = "chat-proj-filter"; filter.placeholder = "搜索项目名或目录…";
  p.appendChild(filter);
  const list = el("div", "chat-proj-list"); list.id = "chat-proj-list";
  p.appendChild(list);
  const drawList = () => {
    list.innerHTML = "";
    const q = (filter.value || "").trim().toLowerCase();
    const hits = chatProjItems.filter((it) => !q ||
      (it.name || "").toLowerCase().includes(q) ||
      (it.directory || "").toLowerCase().includes(q));
    if (!hits.length) list.appendChild(el("div", "note",
      chatProjItems.length ? "没有匹配的项目" : "还没有项目档案"));
    hits.forEach((it) => {
      const row = el("button", "chat-proj-item");
      row.appendChild(el("div", "chat-proj-name",
        (it.id === chatProjId ? "✓ " : "") + esc(it.name || it.directory || "未命名")));
      row.appendChild(el("div", "chat-proj-dir", esc(it.directory || "")));
      row.onclick = () => chatProjSelect(it.id);
      list.appendChild(row);
    });
  };
  filter.oninput = drawList;
  drawList();

  const acts = el("div", "chat-proj-acts");
  const none = el("button", "chat-proj-act", "不使用项目");
  none.onclick = () => chatProjSelect("");
  acts.appendChild(none);
  // 只留「选本地文件夹」一条建档入口（V1.167）：本仓项目本质是授权目录，
  // 旧「＋创建新项目」表单最终也走 project_save，两者只是名字/备注之别，
  // 删掉表单路径、名取目录末段，少一层交互（与工作台 wbBrowseImport 同语义）。
  const fromDir = el("button", "chat-proj-act", "📁 选择本地文件夹");
  fromDir.onclick = () => chatProjBrowse();
  acts.appendChild(fromDir);
  p.appendChild(acts);

  const area = el("div", "chat-proj-area"); area.id = "chat-proj-area";
  p.appendChild(area);
}

async function chatProjSelect(id) {
  try {
    await post("/api/agent/project_select", { id: id });
    chatProjId = id;
    chatProjButtonPaint();
    chatProjToggle(false);
    if (typeof chatSidePaint === "function") chatSidePaint();
    toast(id ? "已切项目" : "已不使用项目");
  } catch (e) { toast("切换失败：" + e.message); }
}

// 建档写入口只此一个（V1.167 简化后只剩「选本地文件夹」一条路）：
// 名留空取目录末段，与工作台 wbBrowseImport 同一语义，project_save 负责校验目录。
async function chatProjSave(dir) {
  try {
    const res = await post("/api/agent/project_save", { directory: dir, name: "", note: "" });
    if (res.project) chatProjId = res.project.id;
    await chatProjReload(); chatProjToggle(false); toast("项目已建档");
  } catch (e) { toast("建档失败：" + e.message); }
}

// 建档/切换后重拉清单：选中态来自服务端，前端不自己拼 id（一个事实一个写入口）。
async function chatProjReload() {
  try {
    const data = await api("/api/agent/projects");
    chatProjItems = data.projects || [];
    chatProjId = data.project_id || chatProjId;
  } catch (e) { /* 清单读不到不挡已完成的写入，下次打开面板再试 */ }
  chatProjButtonPaint();
  if (typeof chatSidePaint === "function") chatSidePaint();
}

// -- 轻量目录浏览（复用 /api/agent/dirs） ------------------------------------ //

function chatProjBrowse() {
  const area = $("#chat-proj-area");
  if (!area) return;
  area.innerHTML = ""; area.hidden = false;
  chatProjDirsLoad("");
}

async function chatProjDirsLoad(path) {
  const area = $("#chat-proj-area");
  if (!area) return;
  area.textContent = "读目录…";
  let data;
  try { data = await api("/api/agent/dirs?path=" + encodeURIComponent(path || "")); }
  catch (e) { area.textContent = "读不出来：" + e.message; return; }
  area.innerHTML = "";
  if (data.parent) {
    const up = el("button", "chat-proj-dirrow", ".. 上一级");
    up.onclick = () => chatProjDirsLoad(data.parent);
    area.appendChild(up);
  }
  area.appendChild(el("div", "chat-proj-dircrumb", esc(data.path || "")));
  // 快捷位置 + 盘符：跨盘纯点击是目录选择器的基本预期。
  // 后端 /api/agent/dirs 早返回 places/drives，工作台浏览也早画了同款，
  // 轻量面板不画就只能手输绝对路径跨盘（V1.167 走查发现的缺口）。点下去仍走
  // 同一个 chatProjDirsLoad → 同一道 resolve_workdir 校验，不是第二条写入路径。
  const quick = el("div", "chips");
  (data.places || []).forEach((pl) => {
    const b = el("button", "chip", esc(pl.label || pl.path));
    b.onclick = () => chatProjDirsLoad(pl.path);
    quick.appendChild(b);
  });
  (data.drives || []).forEach((d) => {
    const b = el("button", "chip", esc(d));
    b.onclick = () => chatProjDirsLoad(d);
    quick.appendChild(b);
  });
  if (quick.childNodes.length) area.appendChild(quick);
  const pathInput = el("input"); pathInput.type = "text"; pathInput.value = data.path || "";
  pathInput.placeholder = "粘贴绝对路径回车跳转";
  pathInput.onkeydown = (ev) => {
    if (ev.key === "Enter") { ev.preventDefault(); chatProjDirsLoad(pathInput.value.trim()); }
  };
  area.appendChild(pathInput);
  const filter = el("input"); filter.type = "text"; filter.placeholder = "过滤当前层…";
  const list = el("div", "chat-proj-dirlist");
  area.appendChild(filter); area.appendChild(list);
  const draw = () => {
    list.innerHTML = "";
    const q = (filter.value || "").trim().toLowerCase();
    const hits = (data.items || []).filter((n) => !q || n.toLowerCase().includes(q));
    if (!hits.length) list.appendChild(el("div", "note", "这一层没有匹配的文件夹"));
    const sep = (data.path || "").indexOf(String.fromCharCode(92)) >= 0
      ? String.fromCharCode(92) : "/";
    const base = (data.path || "").endsWith(sep) ? data.path : (data.path || "") + sep;
    hits.forEach((name) => {
      const row = el("button", "chat-proj-dirrow", esc(name));
      row.title = base + name;
      row.onclick = () => chatProjDirsLoad(base + name);
      list.appendChild(row);
    });
  };
  filter.oninput = draw; draw();

  const row = el("div", "chat-proj-formrow");
  const use = el("button", "btn primary", "选用此目录");
  use.onclick = () => chatProjSave(data.path);
  const back = el("button", "btn", "返回项目列表");
  back.onclick = () => chatProjRender();
  row.appendChild(use); row.appendChild(back);
  area.appendChild(row);
}
"""
