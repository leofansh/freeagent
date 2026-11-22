"""拍照识物的界面：选图 → 读图 → 出结果卡片。

只读进内存、只换回文字。**不落盘** —— 铭牌照片里可能有地址、门牌、工牌。

「搜商品」刻意只给**深链**：识别出型号后跳到京东/淘宝自己的搜索页。
不调任何商家 API —— 那个搜索页有你的登录态、优惠券、评价筛选，
比接口只能给的字段好用得多。
"""

__all__ = ["JS_VISION"]

JS_VISION = r'''
// --- 拍照识物 ------------------------------------------------------------- //

let visionBusy = false;

let visionOutsideBound = false;

function loadVisionBar() {
  const bar = $("#visionbar");
  if (!bar) return;
  bar.innerHTML = "";
  const file = el("input");
  file.type = "file";
  file.accept = "image/jpeg,image/png,image/gif,image/webp";
  file.id = "visionfile";
  file.style.display = "none";
  // V1.149：不再单独摆一颗"📷 拍照识物"胶囊，收进输入框左侧的"+"。
  const btn = el("button", "btn ghost attach", "+");
  btn.id = "visionbtn";
  btn.title = "添加图片（拍照识物）";
  btn.setAttribute("aria-haspopup", "true");
  btn.setAttribute("aria-expanded", "false");
  const menu = el("div", "attach-menu");
  menu.id = "attach-menu";
  menu.hidden = true;
  menu.appendChild(el("div", "menu-group", "添加"));
  // 借鉴 OpenCode 的 + 菜单：只列本仓真有的入口，不摆 / @ ! 这些没有的。
  const item = el("button", "attach-item", "🖼 图片和文件");
  item.id = "attach-image";
  item.appendChild(el("span", "attach-hint", "Ctrl+U"));
  item.onclick = () => { file.click(); };
  menu.appendChild(item);
  const paste = el("button", "attach-item", "📋 粘贴图片");
  paste.id = "attach-paste";
  paste.appendChild(el("span", "attach-hint", "Ctrl+V"));
  paste.onclick = () => {
    close();
    const ta = $("#chatinput"); if (ta) ta.focus();
    toast("直接按 Ctrl+V 把图片粘进输入框");
  };
  menu.appendChild(paste);
  // 借鉴豆包 / WorkBuddy 的分组，但只列本仓真有的入口 —— 不摆没有的技能 / 云盘。
  menu.appendChild(el("div", "menu-group", "已有能力"));
  const nav = (label, view) => {
    const b = el("button", "attach-item", label);
    b.onclick = () => { close(); load(view); };
    menu.appendChild(b);
  };
  nav("角色与专家", "roles");
  nav("后台服务", "services");
  nav("飞书通道", "feishu");
  const note = el("div", "hint attach-note");
  note.id = "visionnote";
  menu.appendChild(note);
  const close = () => {
    menu.hidden = true;
    btn.setAttribute("aria-expanded", "false");
  };
  btn.onclick = (e) => {
    e.stopPropagation();
    menu.hidden = !menu.hidden;
    btn.setAttribute("aria-expanded", String(!menu.hidden));
  };
  // 监听只绑一次：loadVisionBar 每次切回对话页都会跑，重复绑会攒一堆闭包。
  if (!visionOutsideBound) {
    visionOutsideBound = true;
    document.addEventListener("click", (e) => {
      const b = $("#visionbar");
      if (b && !b.contains(e.target)) close();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") close();
      // Ctrl+U = 菜单上写的快捷键，必须真能按（写了不认比不写更坏）。
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "u" && view === "chat") {
        e.preventDefault(); file.click();
      }
    });
  }
  file.onchange = () => {
    close();
    if (file.files && file.files[0]) doVision(file.files[0]);
  };
  bar.appendChild(btn);
  bar.appendChild(menu);
  bar.appendChild(file);

  api("/api/vision")
    .then((s) => {
      // 没配 Key 就把入口禁用并说清理由 —— 点了才报错等于骗人。
      if (!s.available) { btn.disabled = true; item.disabled = true; }
      note.textContent = s.notice;
    })
    .catch(() => { note.textContent = "（识别状态未知）"; });
}

function doVision(blob) {
  if (visionBusy) return;
  if (blob.size > 8 * 1024 * 1024) {
    toast("图片太大了（上限 8 MB）");
    return;
  }
  visionBusy = true;
  const btn = $("#visionbtn");
  const note = $("#visionnote");
  btn.disabled = true;
  note.textContent = "正在读图（约 3–10 秒）…";
  toast("正在读图（约 3–10 秒）…");  // 菜单已收起，进度要在看得见的地方说
  const reader = new FileReader();
  reader.onload = async () => {
    try {
      const r = await api("/api/vision", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ image: reader.result }),
      });
      note.textContent = "";
      showVision(r);
    } catch (e) {
      note.textContent = "";
      addMsg("bot", "没能识别：" + e.message, "kind-cannot");
    } finally {
      visionBusy = false;
      btn.disabled = false;
    }
  };
  reader.onerror = () => {
    note.textContent = "";
    toast("读不出这个文件");
    visionBusy = false;
    btn.disabled = false;
  };
  reader.readAsDataURL(blob);
}

function showVision(r) {
  const log = $("#chatlog");
  const m = el("div", "msg bot kind-answer");
  m.appendChild(el("div", "bubble",
    (r.ok ? "读出来了：" : "没读出型号：") + esc(r.summary)));
  m.appendChild(visionFacts(r));
  if (r.links && r.links.length) m.appendChild(visionLinks(r));
  if (r.task_seed) m.appendChild(visionTaskSeed(r));
  log.appendChild(m);
  log.scrollTop = log.scrollHeight;
}

function visionFacts(r) {
  const box = el("div", "items");
  const card = el("div", "tcard");
  const rows = [];
  if (r.brand) rows.push("品牌 " + esc(r.brand));
  if (r.model) rows.push("型号 " + esc(r.model));
  if (r.spec) rows.push("规格 " + esc(r.spec));
  if (r.raw_text) rows.push("铭牌文字 " + esc(r.raw_text));
  card.innerHTML =
    (rows.length ? rows.join("<br>") : "（没读出可用的信息）") +
    (r.note ? '<div class="why">' + esc(r.note) + "</div>" : "");
  box.appendChild(card);
  return box;
}

function visionLinks(r) {
  const box = el("div", "items");
  const card = el("div", "tcard");
  card.innerHTML =
    "<div>按 <b>" + esc(r.query) + "</b> 搜索：</div>" +
    '<div class="why">' + r.links.map((l) =>
      '<a href="' + esc(l.url) + '" target="_blank" rel="noopener noreferrer">'
      + esc(l.name) + "</a>").join(" · ") + "</div>";
  box.appendChild(card);
  return box;
}

function visionTaskSeed(r) {
  const box = el("div", "items");
  const mk = el("button", "chip", "记一条「" + r.task_seed.title + "」");
  const picker = el("div", "why");
  picker.style.display = "none";
  mk.onclick = async () => {
    if (picker.style.display !== "none") {
      picker.style.display = "none";
      return;
    }
    if (!picker.childElementCount) {
      try {
        const roles = await api("/api/roles");
        if (!roles.options.length) { toast("先建一个角色"); return; }
        const sel = el("select");
        roles.options.forEach((o) => {
          const op = el("option", null, esc(o.name));
          op.value = o.id;
          sel.appendChild(op);
        });
        picker.appendChild(sel);
        picker.appendChild(visionGoButton(r, sel));
      } catch (err) {
        toast("读角色失败：" + err.message);
        return;
      }
    }
    picker.style.display = "block";
  };
  box.appendChild(mk);
  box.appendChild(picker);
  return box;
}

function visionGoButton(r, sel) {
  const go = el("button", null, "建任务");
  go.onclick = async () => {
    try {
      const created = await api("/api/task", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: r.task_seed.title,
          intent: r.task_seed.intent,
          role_ids: [sel.value],
        }),
      });
      addMsg("me", "记一条：" + created.task.title);
      addMsg("bot", "已建好，id " + created.task.id.slice(0, 8)
             + "。要排期就点「今天」视图里的它 → 排进今天。", "kind-answer");
    } catch (err) {
      toast("建任务失败：" + err.message);
    }
  };
  return go;
}
'''
