"""工作台前端（二·补）：工作目录浏览（设计文档 21.4 / V1.123，V1.127 重做）。

## 为什么从 ``js_workbench_page`` 拆出来

页面装配 + 模型清单 + 目录浏览三段挤在一个文件里，250 行体积守卫
（``TestWebPackageStaysSmall``）守不住；按职责把「目录浏览」这一刀独立出来。

## V1.127 重做了什么

旧版把一整层目录塞进原生 ``<select>``：选中即进入下一层，导航与选择挤在同一
个控件里，首屏还混着点目录与 Windows 遗留 junction。这一版改成：

- 行列表点行进入，底部「选用此目录」选定当前层；
- 可见路径输入框，可粘贴绝对路径、回车走同一道校验跳转；
- 最近目录一行（项目档案），快捷位置 / 盘符 / 面包屑 / 当前层过滤保留。

## 目录浏览为什么只到「点」为止

列目录的端点在服务端也守着同一条纪律（只列目录、最多 300 项）。这里再守
一次是因为**人**会连点：`node_modules` 一个目录展开就上千项，而它真正需要
的只是「往上两层、进 src」。快捷位置与盘符只是浏览起点，点下去仍走同一个
校验端点，不是白名单。

## 反斜杠不写成字面量

路径分隔符用 ``String.fromCharCode(92)`` 拼 —— 这段 JS 活在一个
``r\"\"\"...\"\"\"`` 里，字面反斜杠紧贴引号会变成 ``\\"``，浏览器侧直接语法错误
（``test_web_js_syntax`` 的 ``test_no_escaped_quotes_leaked_into_js`` 咬的
就是这个）。
"""

from __future__ import annotations

__all__ = ["JS_WORKBENCH_DIRS"]

JS_WORKBENCH_DIRS = r"""let wbDirItems = [];
let wbDirData = null;
//: 二级浏览的选定回调：建档 / 表单回填各传一个；为空时沿用旧行为（写回 #wb-dir）。
let wbPickCallback = null;

function dirSep(p) {
  return p.indexOf(String.fromCharCode(92)) >= 0 ? String.fromCharCode(92) : "/";
}

// 把绝对路径拆成可点击的面包屑段；Windows 盘符段与 Unix 根分别拼累积路径。
function dirSegments(p) {
  const seg = dirSep(p);
  const parts = p.split(seg).filter(Boolean);
  const segs = [];
  let acc = "";
  parts.forEach((name, i) => {
    acc = seg === String.fromCharCode(92)
      ? (i === 0 ? name + seg : acc + name + seg)
      : (acc === "/" ? "" : acc) + seg + name;
    segs.push({ label: name, path: acc });
  });
  return segs;
}

function paintDirList() {
  const list = $("#wb-dir-list");
  if (!list || !wbDirData) return;
  list.innerHTML = "";
  const q = (($("#wb-dir-filter") || {}).value || "").trim().toLowerCase();
  if (wbDirData.parent) {
    const up = el("button", "dir-row", ".. 上一级");
    up.onclick = () => wbBrowse(wbDirData.parent, wbPickCallback);
    list.appendChild(up);
  }
  const seg = dirSep(wbDirData.path);
  const base = wbDirData.path.endsWith(seg) ? wbDirData.path : wbDirData.path + seg;
  const hits = wbDirItems.filter((n) => !q || n.toLowerCase().includes(q));
  if (!hits.length) {
    list.appendChild(el("div", "note", "这一层没有匹配的文件夹"));
    return;
  }
  hits.forEach((name) => {
    const row = el("button", "dir-row", esc(name));
    row.title = base + name;
    row.onclick = () => wbBrowse(base + name, wbPickCallback);
    list.appendChild(row);
  });
}

async function wbBrowse(path, onPick) {
  const box = $("#wb-dirs");
  if (!box) return;
  wbPickCallback = onPick || null;
  box.hidden = false;
  box.textContent = "读目录…";
  let data;
  try {
    data = await api("/api/agent/dirs?path=" + encodeURIComponent(path || ""));
  } catch (e) {
    box.textContent = "读不出来：" + e.message;
    return;
  }
  wbDirData = data;
  wbDirItems = data.items;
  box.innerHTML = "";
  // 无论从 chips / 面包屑 / 路径框哪条路过来，隐藏目录框都跟着当前层走，
  // 否则人看到的路径与发送时用的目录对不上。
  const dirInput = $("#wb-dir");
  if (dirInput) dirInput.value = data.path;

  // 最近目录：项目档案里用过的文件夹，直达不用逐级翻。这是入口不是白名单。
  if ((data.recent || []).length) {
    const recent = el("div", "chips");
    data.recent.forEach((r) => {
      const b = el("button", "chip", esc(r.label));
      b.title = r.path;
      b.onclick = () => wbBrowse(r.path, wbPickCallback);
      recent.appendChild(b);
    });
    box.appendChild(recent);
  }

  // 快捷位置（家目录/桌面/下载/文档）与可用盘符：点下去仍走同一个校验端点。
  const quick = el("div", "chips");
  (data.places || []).forEach((pl) => {
    const b = el("button", "chip", esc(pl.label));
    b.onclick = () => wbBrowse(pl.path, wbPickCallback);
    quick.appendChild(b);
  });
  (data.drives || []).forEach((d) => {
    const b = el("button", "chip", esc(d));
    b.onclick = () => wbBrowse(d, wbPickCallback);
    quick.appendChild(b);
  });
  box.appendChild(quick);

  const crumb = el("div", "row1");
  dirSegments(data.path).forEach((s, i, arr) => {
    if (i > 0) crumb.appendChild(el("span", null, " / "));
    const b = el("button", "chip", esc(s.label));
    if (i === arr.length - 1) b.disabled = true;
    else b.onclick = () => wbBrowse(s.path, wbPickCallback);
    crumb.appendChild(b);
  });
  box.appendChild(crumb);

  // 可见路径框：可粘贴绝对路径；回车按同一道 resolve_workdir 校验后跳转。
  const pathInput = el("input");
  pathInput.type = "text";
  pathInput.id = "wb-dir-path";
  pathInput.value = data.path;
  pathInput.placeholder = "粘贴或输入绝对路径，回车跳转";
  pathInput.onkeydown = (ev) => {
    if (ev.key === "Enter") {
      ev.preventDefault();
      wbBrowse(pathInput.value.trim(), wbPickCallback);
    }
  };
  box.appendChild(pathInput);

  const note = el("div", "note");
  note.id = "wb-dir-note";
  note.textContent = data.truncated
    ? "只列前 " + data.items.length + " 个，共 " + data.total + " 个文件夹"
    : "";
  box.appendChild(note);

  const filter = el("input");
  filter.type = "text";
  filter.id = "wb-dir-filter";
  filter.placeholder = "过滤当前层目录…";
  filter.oninput = paintDirList;
  box.appendChild(filter);

  const list = el("div", "dir-list");
  list.id = "wb-dir-list";
  box.appendChild(list);
  paintDirList();

  // 二级兜底：选定当前层才建档 / 回填。没有回调时写回隐藏目录框，沿用旧行为。
  const useRow = el("div", "row1");
  const use = el("button", "btn primary",
    wbPickCallback ? "选用此目录" : "用这个目录");
  use.onclick = () => {
    const p = wbDirData.path;
    if (wbPickCallback) { wbPickCallback(p); }
    else { const d = $("#wb-dir"); if (d) d.value = p; }
  };
  const close = el("button", "btn", "收起浏览");
  close.onclick = () => { box.hidden = true; };
  useRow.appendChild(use);
  useRow.appendChild(close);
  box.appendChild(useRow);
}
"""
