"""角色视图与事务详情页。


"""

from __future__ import annotations

__all__ = ["JS_DETAIL"]

JS_DETAIL = r"""function renderRoles(data) {
  const root = $("#root");
  root.innerHTML = "";
  if (!data.count) {
    root.appendChild(el("div", "empty", "还没有角色。直接说一句话就会自动建一个。"));
  }
  const block = (title, list) => {
    if (!list.length) return;
    root.appendChild(el("div", "section-label", title));
    list.forEach((r) => {
      const c = el("div", "card");
      c.innerHTML =
        '<div class="row1"><span class="title">' + esc(r.name) + "</span>" +
        (r.active ? "" : '<span class="tag">静置</span>') +
        (r.merged_into_name ? '<span class="tag">已并入 ' + esc(r.merged_into_name) + "</span>" : "") +
        "</div>" +
        (r.note ? '<div class="meta" style="margin-top:4px">' + esc(r.note) + "</div>" : "") +
        (r.default_definition_of_done
          ? '<div class="meta" style="margin-top:4px">默认完成标准：' + esc(r.default_definition_of_done) + "</div>"
          : "");
      root.appendChild(c);
    });
  };
  block("活跃", data.active);
  block("静置（数据保留，不出现在默认列表）", data.silenced);
  $("#foot").textContent = "角色可在终端用 /role-rename /role-silence /merge 管理。";
}

async function post(path, body) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

function actionBar(data) {
  const t = data.task;
  const bar = el("div", "acts");
  bar.appendChild(el("span", "lbl", "改状态"));
  // 只渲染服务层说合法的迁入 —— 前端不自己判断
  (data.allowed_next_states || []).forEach((s) => {
    if (s.value === t.state) return; // 当前状态不做按钮
    // 用接口给的 action，不自己猜（状态名 ≠ 动作名）
    if (!s.action) return;
    const map = { active: "开始做", inbox: "等会再做",
                  blocked: "放一放", done: "做完啦", dropped: "算了" };
    const b = el("button", "btn" + (s.value === "done" ? " primary" : ""),
                 map[s.value] || s.label);
    b.onclick = () => {
      // 「放一放」天然要知道在等什么，否则 BLOCKED 是个黑洞状态
      if (s.value === "blocked") return askWaiting(t.id, bar);
      b.disabled = true;
      mutate(t.id, s.action, "已改为「" + s.label + "」", b);
    };
    bar.appendChild(b);
  });
  if (t.is_open) {
    if (t.scheduled_for) {
      const u = el("button", "btn", "移出排期");
      u.onclick = () => mutate(t.id, "unpin", "已移出排期", u);
      bar.appendChild(u);
    } else {
      const p = el("button", "btn", "排到今天");
      p.onclick = () => mutate(t.id, "pin", "已排到今天", p);
      bar.appendChild(p);
    }
  }
  return bar;
}

function askWaiting(id, bar) {
  const row = el("div", "grid");
  row.style.width = "100%";
  row.style.marginTop = "8px";
  const input = el("input");
  input.type = "text";
  input.placeholder = "在等什么？例如：客户法务回复";
  const ok = el("button", "btn primary", "记下");
  const cancel = el("button", "btn", "取消");
  ok.onclick = async () => {
    const v = input.value.trim();
    if (!v) { toast("要说清在等什么"); return; }
    ok.disabled = true;
    await mutate(id, "blocked", "已放一放，等「" + v + "」", ok);
  };
  cancel.onclick = () => row.remove();
  input.onkeydown = (e) => { if (e.key === "Enter") ok.click(); };
  row.appendChild(input); row.appendChild(ok); row.appendChild(cancel);
  bar.appendChild(row);
  input.focus();
}

async function mutate(id, action, okMsg, btn) {
  try {
    await post("/api/task/" + encodeURIComponent(id) + "/" + action);
    toast(okMsg);
    openTask(id);
  } catch (e) {
    if (btn) btn.disabled = false;
    toast(e.message);
  }
}

function renderTask(data) {
  const t = data.task;
  const root = $("#root");
  root.innerHTML = "";
  const back = el("a", "back", "← 返回");
  back.href = "#";
  back.onclick = (e) => { e.preventDefault(); load(view); };
  root.appendChild(back);

  const head = el("div", "card");
  head.innerHTML =
    '<div class="row1"><span class="title">' + esc(t.title) + "</span>" +
    '<span class="tag">' + esc(t.kind_label) + "</span>" + stateTag(t) + "</div>" +
    '<div class="row1" style="margin-top:5px"><span class="roles">' +
      t.roles.map(esc).join("、") + "</span>" +
      (t.scheduled_for ? '<span class="meta">排 ' + esc(t.scheduled_for) + "</span>" : "") +
      (t.due_time ? '<span class="meta">截止 ' + when(t.due_time) + "</span>" : "") +
      (t.reminder_time ? '<span class="meta">提醒 ' + when(t.reminder_time) + "</span>" : "") +
    "</div>";
  head.appendChild(actionBar(data));
  root.appendChild(head);

  const dl = el("div", "card");
  const pairs = [
    ["意图", t.intent || "（未澄清）"],
    ["生效完成标准", data.effective_definition_of_done || "（未声明）"],
  ];
  if (data.waiting_on) {
    pairs.push(["等候", data.waiting_on.who_or_what +
      (data.waiting_on.follow_up_at ? "（跟进 " + when(data.waiting_on.follow_up_at) + "）" : "")]);
  }
  dl.innerHTML = "<dl>" + pairs.map(([k, v]) =>
    "<dt>" + esc(k) + "</dt><dd>" + esc(v) + "</dd>").join("") + "</dl>";
  root.appendChild(dl);

  if (data.artifact) {
    const a = el("div", "card");
    const label = { draft: "草稿", accepted: "已采纳", superseded: "已被取代" }[data.artifact.status];
    a.innerHTML = '<div class="row1"><span class="title">当前稿</span>' +
      '<span class="tag">version ' + data.artifact.version + "</span>" +
      '<span class="tag">' + label + "</span></div>" +
      '<pre class="artifact">' + esc(data.artifact.content) + "</pre>";
    root.appendChild(a);
  }
  if (data.progress_note) {
    const p = el("div", "card");
    p.innerHTML = '<div class="section-label" style="margin-top:0">进度</div><div>' +
      esc(data.progress_note) + "</div>";
    root.appendChild(p);
  }
  if (data.records && data.records.length) {
    const r = el("div", "card");
    r.innerHTML = '<div class="section-label" style="margin-top:0">最近记录</div>' +
      '<ul class="records">' + data.records.slice(-8).map((x) =>
        "<li><time>" + when(x.ts) + "</time>" + esc(x.content) + "</li>").join("") + "</ul>";
    root.appendChild(r);
  }
  const n = el("div", "card");
  n.innerHTML = '<div class="section-label" style="margin-top:0">下一步建议</div>' +
    '<ol class="next">' + data.next_actions.map((x) => "<li>" + esc(x) + "</li>").join("") + "</ol>";
  root.appendChild(n);
  $("#foot").textContent = "恢复契约：意图 / 完成标准 / 当前稿 / 进度 / 记录 / 等候 / 下一步。";
}

async function openTask(id) {
  try {
    detail = await api("/api/task/" + encodeURIComponent(id));
    $("#nav").hidden = true;
    renderTask(detail);
  } catch (e) {
    toast(e.message);
  }
}

function newForm(options) {
  const f = el("form", "new card");
  const todayISO = new Date().toISOString().slice(0, 10);
  f.innerHTML =
    '<div class="section-label" style="margin-top:0">新建事务</div>' +
    '<div class="grid">' +
      '<input type="text" id="t" placeholder="一句话说清要做的事，例如：下周二要交的销售周报初稿" required>' +
      '<select id="k"><option value="action">动作</option><option value="wait">等候</option>' +
      '<option value="reminder">提醒</option></select>' +
      '<input type="date" id="d" value="' + todayISO + '" title="排到哪一天（留空=先不排）">' +
      '<button class="btn primary" type="submit">记下</button></div>' +
    '<div class="grid" id="rolebar" style="margin-top:9px"></div>' +
    '<div class="meta" style="margin-top:8px">角色必须选，不做推断 —— ' +
    '想让它自己判断，去终端直接说一句话。</div>' +
    '<div class="err" id="e" hidden></div>';

  // 角色按钮：必须能看到当前选中的是哪个
  const bar = f.querySelector("#rolebar");
  const pick = (id) => {
    f.querySelector("#rid").value = id;
    bar.querySelectorAll("button").forEach((b) =>
      b.setAttribute("aria-pressed", String(b.dataset.rid === id)));
  };
  options.forEach((o) => {
    const b = el("button", "btn", esc(o.name));
    b.type = "button";
    b.dataset.rid = o.id;
    b.setAttribute("aria-pressed", "false");
    b.onclick = () => pick(o.id);
    bar.appendChild(b);
  });
  const hidden = el("input");
  hidden.type = "hidden"; hidden.id = "rid";
  f.appendChild(hidden);
  if (options.length) pick(options[0].id);

  f.onsubmit = async (ev) => {
    ev.preventDefault();
    const errBox = f.querySelector("#e");
    errBox.hidden = true;
    const day = f.querySelector("#d").value;
    try {
      await api("/api/task", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: f.querySelector("#t").value,
          kind: f.querySelector("#k").value,
          role_ids: [f.querySelector("#rid").value],
          scheduled_for: day || null,
        }),
      });
      f.querySelector("#t").value = "";
      toast(day ? "已记下，已排到 " + day : "已记下（未排期）");
      load(view);
    } catch (e) {
      errBox.hidden = false;
      errBox.textContent = e.message;
    }
  };
  return f;
}"""
