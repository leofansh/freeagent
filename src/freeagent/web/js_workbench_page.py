"""工作台前端（三）：页面装配、目录与模型选择（设计文档 21.4 / V1.114）。

## 目录浏览为什么只到「点」为止

列目录的端点在服务端也守着同一条纪律（只列目录、最多 300 项）。这里再守
一次是因为**人**会连点：`node_modules` 一个目录展开就上千项，而它真正需要
的只是「往上两层、进 src」。

## 反斜杠不写成字面量

路径分隔符用 ``String.fromCharCode(92)`` 拼 —— 这段 JS 活在一个
``r\"\"\"...\"\"\"`` 里，字面反斜杠紧贴引号会变成 ``\\"``，浏览器侧直接语法错误
（``test_web_js_syntax`` 的 ``test_no_escaped_quotes_leaked_into_js`` 咬的
就是这个）。
"""

from __future__ import annotations

__all__ = ["JS_WORKBENCH_PAGE"]

JS_WORKBENCH_PAGE = r"""async function wbBrowse(path) {
  const box = $("#wb-dirs");
  if (!box) return;
  box.textContent = "读目录…";
  let data;
  try {
    data = await api("/api/agent/dirs?path=" + encodeURIComponent(path || ""));
  } catch (e) {
    box.textContent = "读不出来：" + e.message;
    return;
  }
  box.innerHTML = "";
  box.appendChild(el("div", "note", esc(data.path) +
    (data.truncated ? "（只列前 " + data.items.length + " 个，共 " + data.total + "）" : "")));
  const sel = el("select");
  sel.id = "wb-dir-pick";
  if (data.parent) {
    const up = el("option", null, ".. 上一级");
    up.value = data.parent;
    sel.appendChild(up);
  }
  const seg = data.path.indexOf(String.fromCharCode(92)) >= 0
    ? String.fromCharCode(92) : "/";
  data.items.forEach((name) => {
    const o = el("option", null, name);
    o.value = (data.path.endsWith(seg) ? data.path : data.path + seg) + name;
    sel.appendChild(o);
  });
  sel.onchange = () => {
    $("#wb-dir").value = sel.value;
    wbBrowse(sel.value);
  };
  box.appendChild(sel);
}

async function wbModels() {
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
      "清单是空的" + (data.model && data.model.note ? "：" + data.model.note : "") +
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
  bar.appendChild(el("div", "row1", "工作目录"));
  const dir = el("input");
  dir.id = "wb-dir";
  dir.value = prefs.directory || "";
  dir.placeholder = "绝对路径，例如 D:/projects/my-app";
  dir.style.width = "60%";
  bar.appendChild(dir);
  const browse = el("button", "btn", "浏览");
  browse.onclick = () => wbBrowse(dir.value.trim());
  bar.appendChild(browse);

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
    "换档只对**下一轮**生效，这一轮仍按开始时的那一档。"));
  root.appendChild(bar);

  const dirs = el("div", "note");
  dirs.id = "wb-dirs";
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
  send.onclick = () => wbSend(false);
  ask.appendChild(send);
  const stop = el("button", "btn danger", "停止");
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
