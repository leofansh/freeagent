"""工作台前端（三）：页面装配与模型清单（设计文档 21.4 / V1.114）。

目录浏览独立在 ``js_workbench_dirs``（250 行体积守卫逼出来的拆块）；本文件
只装配卡片、模型清单与发送区。

## 反斜杠不写成字面量

路径分隔符相关的拼法在 ``js_workbench_dirs``；这段文件里不拼路径，注释留
在这里是提醒后来者：JS 活在 ``r\"\"\"...\"\"\"`` 里，字面反斜杠紧贴引号会
变成 ``\\"``，浏览器侧直接语法错误。
"""

from __future__ import annotations

__all__ = ["JS_WORKBENCH_PAGE"]

JS_WORKBENCH_PAGE = r"""async function wbModels() {
  const list = $("#wb-models");
  if (!list) return;
  list.textContent = "读模型清单…";
  let data;
  try {
    data = await api("/api/oc/options");
  } catch (e) {
    list.textContent = "读不出来：" + e.message + "（可以手填模型名）";
    return;
  }
  const opts = (data.model && data.model.options) || [];
  list.innerHTML = "";
  opts.forEach((o) => {
    const b = el("button", "btn", esc(o.label) + (o.hint ? " · " + esc(o.hint) : ""));
    b.onclick = () => { $("#wb-model").value = o.value; };
    list.appendChild(b);
  });
  if (!opts.length) {
    list.appendChild(el("div", "note",
      "清单是空的" + (data.model && data.model.note ? "：" + esc(data.model.note) : "") +
      "。可以手填模型名，或留空用引擎默认。"));
  }
}

async function loadWorkbench() {
  wbStopPoll();
  wbHistId = "";
  const root = $("#root");
  root.innerHTML = "";
  let st;
  try { st = await api("/api/agent/state"); }
  catch (e) {
    root.innerHTML = '<div class="note">状态读不出来：' + esc(e.message) + "</div>";
    return;
  }
  const prefs = st.prefs || {};

  const bar = el("div", "card");

  // 项目选择器（21.6 / V1.124）：触发芯片 + 下拉（搜索 / 已有项目 / 三个动作）。
  // 文件系统浏览退到二级，由「选择本地文件夹」与新建表单里的浏览按钮打开。
  const pmenu = el("div", "menu");
  const pbtn = el("button", "btn", "项目：未选择");
  pbtn.id = "wb-project-btn";
  pbtn.onclick = () => wbToggleMenu();
  const pop = el("div", "menu-pop");
  pop.id = "wb-project-menu";
  pop.hidden = true;
  const search = el("input");
  search.id = "wb-project-filter";
  search.placeholder = "搜索项目…";
  search.oninput = wbRenderProjectMenu;
  pop.appendChild(search);
  const list = el("div");
  list.id = "wb-project-list";
  pop.appendChild(list);
  const clearBtn = el("button", "btn", "不使用项目");
  clearBtn.onclick = () => wbSelectProject("");
  const newBtn = el("button", "btn", "新建项目");
  newBtn.onclick = wbShowForm;
  const folderBtn = el("button", "btn", "选择本地文件夹");
  folderBtn.onclick = wbBrowseImport;
  pop.appendChild(clearBtn);
  pop.appendChild(newBtn);
  pop.appendChild(folderBtn);
  pmenu.appendChild(pbtn);
  pmenu.appendChild(pop);
  bar.appendChild(pmenu);

  // 新建项目表单：名称 / 备注 / 目录。目录仍走二级浏览面板，选定回填。
  const form = el("div", "card");
  form.id = "wb-project-form";
  form.hidden = true;
  const name = el("input");
  name.id = "wb-form-name";
  name.placeholder = "项目名（留空取目录末段）";
  form.appendChild(name);
  const note = el("input");
  note.id = "wb-form-note";
  note.placeholder = "备注（可选）";
  form.appendChild(note);
  const dline = el("div", "note");
  dline.id = "wb-form-dir";
  form.appendChild(dline);
  const formBrowse = el("button", "btn", "浏览选目录");
  formBrowse.onclick = wbBrowseFormDir;
  const submit = el("button", "btn primary", "创建");
  submit.onclick = wbSubmitProject;
  const cancel = el("button", "btn", "取消");
  cancel.onclick = () => { form.hidden = true; };
  const formRow = el("div", "row1");
  formRow.appendChild(formBrowse);
  formRow.appendChild(submit);
  formRow.appendChild(cancel);
  form.appendChild(formRow);
  bar.appendChild(form);

  // 隐藏目录框：wbSend 仍读它取当前项目目录，发送链路不改。
  const dir = el("input");
  dir.type = "hidden";
  dir.id = "wb-dir";
  dir.value = prefs.directory || "";
  bar.appendChild(dir);
  wbInitProjects(st);

  bar.appendChild(el("div", "row1", "模型"));
  const model = el("input");
  model.id = "wb-model";
  model.value = prefs.model || "";
  model.placeholder = "留空 = 引擎默认";
  model.style.width = "60%";
  bar.appendChild(model);
  const pick = el("button", "btn", "载入清单");
  pick.onclick = wbModels;
  bar.appendChild(pick);

  bar.appendChild(el("div", "row1", "授权档"));
  const mode = el("select");
  mode.id = "wb-mode";
  (st.modes || []).forEach((m) => {
    const o = el("option", null, esc(m.label) + " —— " + esc(m.note));
    o.value = m.key;
    mode.appendChild(o);
  });
  mode.value = prefs.mode || "ask";
  bar.appendChild(mode);
  bar.appendChild(el("div", "note",
    "默认逐次授权。选「本目录内自动」只是少问几次写文件与 shell，越界仍然不可能。"));
  // 自动档的代价要说清（V1.116）：**改档不改变已经在跑的那一轮**，
  // 而且那一轮里跑过的命令不会弹卡 —— 只会在下面「它跑过的命令」里
  // 留下原文。不写这句，人会以为「自动档 = 它做了什么都有人点过头」。
  bar.appendChild(el("div", "note",
    "自动档下跑命令不会弹卡，但每一条都会列在「它跑过的命令」里；" +
    "换档只对下一轮生效，这一轮仍按开始时的那一档。"));
  root.appendChild(bar);

  const dirs = el("div", "note");
  dirs.id = "wb-dirs";
  dirs.hidden = true;
  root.appendChild(dirs);
  const models = el("div", "row1");
  models.id = "wb-models";
  root.appendChild(models);

  const ask = el("div", "card");
  const ta = el("textarea");
  ta.id = "wb-text";
  ta.rows = 3;
  ta.placeholder = "一句话：改哪个文件、修什么、跑什么。别换行 —— 含换行会让引擎升级它自己的模型。";
  ta.style.width = "100%";
  // 回车直接发出（Shift+回车才换行）：占位文案已经说「别换行」，再让人
  // 去摸鼠标点按钮是多一步。与对话页同一个键位惯例（js_chat）。
  ta.onkeydown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); wbSend(false); }
  };
  ask.appendChild(ta);
  // 带上「今天的事 / 承诺 / 相关记忆」（21.5 / V1.115）。默认开 —— 但
  // 开关必须看得见：它把这台机器上的私人记录送进模型，人有权当场关掉，
  // 而不是只能改配置文件。
  const injectRow = el("div", "note");
  const inject = el("input");
  inject.type = "checkbox";
  inject.id = "wb-inject";
  inject.checked = true;
  injectRow.appendChild(inject);
  injectRow.appendChild(el("span", null,
    " 带上今天的事、未结清的承诺与相关记忆（会显示它带了什么）"));
  ask.appendChild(injectRow);
  const send = el("button", "btn primary", "交给它");
  send.id = "wb-send";
  send.onclick = () => wbSend(false);
  ask.appendChild(send);
  const stop = el("button", "btn danger", "停止");
  stop.id = "wb-stop";
  stop.onclick = async () => {
    const t = wbTurn;
    if (!t) return;
    try { await post("/api/agent/stop", { id: t.id }); } catch (e) { toast(e.message); }
    wbArm(200);
  };
  ask.appendChild(stop);
  root.appendChild(ask);

  const route = el("div", "note");
  route.id = "wb-route";
  root.appendChild(route);

  const eng = el("div", "note");
  eng.id = "wb-engine";
  root.appendChild(eng);

  const stream = el("div");
  stream.id = "wb-stream";
  root.appendChild(stream);

  // 「最近的活」（21.4 / V1.115）：点开某一条就走 wbOpen，看的是当时的
  // 全文与结局，而不是现在。放在正文下面而不是侧面 —— 这一页的主动作
  // 永远是「说一句话」，历史是回头看的。
  root.appendChild(el("div", "row1", "最近的活"));
  const hist = el("div");
  hist.id = "wb-history";
  root.appendChild(hist);

  wbPaint(st);
  wbHistory();
}
"""
