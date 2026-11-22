"""工作台前端（四）：项目选择器（设计文档 21.6 / V1.124）。

## 为什么首屏是项目列表，不是文件浏览

人打开工作台心里问的是「我在哪个项目里干活」，不是「我要去哪个文件夹」。
所以这块对齐豆包 / WorkBuddy 的形状：搜索框 + 已有项目 + 分隔线 +
「不使用项目 / 新建项目 / 选择本地文件夹」。文件系统浏览退到二级 ——
浏览器拿不到系统原生文件夹对话框，那一层仍由 ``js_workbench_dirs`` 兜底。

## 状态归属

``wbProjects`` 是档案清单；``wbProjectId`` / ``wbProjectDir`` 是当前选中。
``#wb-dir`` 隐藏输入框仍保留 —— ``wbSend`` 读它取目录，发送链路不改。
"""

from __future__ import annotations

__all__ = ["JS_WORKBENCH_PROJECTS"]

JS_WORKBENCH_PROJECTS = r"""let wbProjects = [];
let wbProjectId = "";
let wbProjectDir = "";
//: 新建项目表单里已选的目录（与当前选中项目互不干扰）。
let wbFormDir = "";

function wbInitProjects(st) {
  wbProjects = st.projects || [];
  wbProjectId = (st.prefs && st.prefs.project_id) || "";
  wbProjectDir = (st.prefs && st.prefs.directory) || "";
  const dirInput = $("#wb-dir");
  if (dirInput) dirInput.value = wbProjectDir;
  wbRenderProjectButton();
  wbRenderProjectMenu();
  if (typeof wbRailPaint === "function") wbRailPaint();
}

function wbCurrentProject() {
  return wbProjects.find((p) => p.id === wbProjectId) || null;
}

function wbRenderProjectButton() {
  const btn = $("#wb-project-btn");
  if (!btn) return;
  const cur = wbCurrentProject();
  btn.textContent = cur ? ("项目：" + cur.name) : "项目：未选择";
}

function wbToggleMenu(force) {
  const menu = $("#wb-project-menu");
  if (!menu) return;
  const show = force != null ? force : menu.hidden;
  menu.hidden = !show;
  if (show) wbRenderProjectMenu();
}

function wbRenderProjectMenu() {
  const list = $("#wb-project-list");
  if (!list) return;
  list.innerHTML = "";
  const q = (($("#wb-project-filter") || {}).value || "").trim().toLowerCase();
  const hits = wbProjects.filter((p) => !q ||
    p.name.toLowerCase().includes(q) ||
    p.directory.toLowerCase().includes(q));
  if (!hits.length) {
    list.appendChild(el("div", "note", wbProjects.length
      ? "没有匹配的项目" : "还没有项目 —— 选个本地文件夹开始"));
  }
  hits.forEach((p) => {
    const row = el("button", "btn");
    row.style.display = "block";
    row.appendChild(el("div", null,
      (p.id === wbProjectId ? "✓ " : "") + esc(p.name)));
    row.appendChild(el("div", "note", esc(p.directory)));
    row.onclick = () => wbSelectProject(p.id);
    list.appendChild(row);
  });
}

async function wbLoadProjects() {
  try {
    const data = await api("/api/agent/projects");
    wbProjects = data.projects || [];
    if (data.project_id) wbProjectId = data.project_id;
    wbRenderProjectMenu();
    wbRenderProjectButton();
    // 左栏（V1.139）与下拉共用这一份清单：选中态、轮次数只在这里画一次。
    if (typeof wbRailPaint === "function") wbRailPaint();
    // 右栏文件清单（V1.142）跟着同一个刷新点走：切/建/删工作区都在这里
    // 汇合，文件块不需要再各挂一次调用。
    if (typeof wbFilesPaint === "function") wbFilesPaint();
  } catch (e) {
    // 列表读不到不挡发送：目录仍在隐藏输入框里，这一轮照走。
  }
}

async function wbSelectProject(id) {
  try {
    const res = await post("/api/agent/project_select", { id: id });
    const p = res.project;
    wbProjectId = p ? p.id : "";
    wbProjectDir = p ? p.directory : "";
    const dirInput = $("#wb-dir");
    if (dirInput) dirInput.value = wbProjectDir;
    wbToggleMenu(false);
    wbRenderProjectButton();
    wbLoadProjects();
  } catch (e) {
    toast(e.message);
  }
}

// -- 新建项目表单 ------------------------------------------------------------ //

function wbShowForm() {
  wbFormDir = "";
  const form = $("#wb-project-form");
  if (form) form.hidden = false;
  const name = $("#wb-form-name"); if (name) name.value = "";
  const note = $("#wb-form-note"); if (note) note.value = "";
  wbFormDirLine();
  wbToggleMenu(false);
}

function wbFormDirLine() {
  const line = $("#wb-form-dir");
  if (line) line.textContent = wbFormDir ? ("目录：" + wbFormDir) : "还没选目录";
}

function wbBrowseFormDir() {
  const box = $("#wb-dirs");
  if (box) box.hidden = false;
  // 浏览面板选定后把目录回填到表单，而不是直接建档。
  wbBrowse(wbFormDir || "", (path) => {
    wbFormDir = path;
    wbFormDirLine();
  });
}

async function wbSubmitProject() {
  if (!wbFormDir) { toast("先选一个本地文件夹"); return; }
  const name = ($("#wb-form-name") || {}).value || "";
  const note = ($("#wb-form-note") || {}).value || "";
  try {
    const res = await post("/api/agent/project_save", {
      directory: wbFormDir, name: name.trim(), note: note.trim(),
    });
    const p = res.project;
    if (p) {
      wbProjectId = p.id;
      wbProjectDir = p.directory;
      const dirInput = $("#wb-dir");
      if (dirInput) dirInput.value = wbProjectDir;
    }
    const form = $("#wb-project-form");
    if (form) form.hidden = true;
    wbLoadProjects();
  } catch (e) {
    toast(e.message);
  }
}

// -- 直接导入本地文件夹 -------------------------------------------------------- //

function wbBrowseImport() {
  wbToggleMenu(false);
  const box = $("#wb-dirs");
  if (box) box.hidden = false;
  // 选定即建档：名取目录末段，备注留空，事后可在项目上补。
  wbBrowse(wbProjectDir || "", async (path) => {
    try {
      const res = await post("/api/agent/project_save", { directory: path });
      const p = res.project;
      if (p) {
        wbProjectId = p.id;
        wbProjectDir = p.directory;
        const dirInput = $("#wb-dir");
        if (dirInput) dirInput.value = wbProjectDir;
      }
      if (box) box.hidden = true;
      wbLoadProjects();
    } catch (e) {
      toast(e.message);
    }
  });
}
"""
