"""对话左栏的两级会话列表（项目 → 会话）。

为什么独立成模块（V1.176）：``js_chat_shell.py`` 已 221 行，会话栏要再塞
两级列表 / 切会话 / 历史迁移，必然撞 250 行守卫。这里只做视图层，
写入全走既有端点（session_switch / session_new / session_import 等），
不复制后端逻辑（12.1.1 一份能力一份实现）。

一处真源：聊天历史按会话分 key（见 js_chat_store.py 的 chatKey()），
切会话只换 chatSessionId；引擎侧的会话由 prefs.session_id 与后端
EngineHost 持有，前端不另存一份。
"""

__all__ = ["JS_CHAT_SESSIONS"]

JS_CHAT_SESSIONS = r"""
// 历史会话迁移：老 key freeagent.chat.v1 有数据、但库里还没会话时，
// 建一条 hist: 会话承接。只搬一次（搬完删老 key）。
async function chatSessionsBoot() {
  try {
    const saved = localStorage.getItem(CHAT_KEY + ".current");
    chatSessionId = saved || "";
  } catch (e) { chatSessionId = ""; }

  let st;
  try { st = await api("/api/agent/state"); }
  catch (e) { return; }

  const pref = st && st.prefs ? String(st.prefs.session_id || "") : "";
  if (pref && pref !== chatSessionId) chatUseSession(pref);

  // 没有目录就读不到会话（sessions.available=false）——那不是「空」，
  // 也不是迁移时机：没有归属目录，建不出历史会话。
  const ss = st && st.sessions;
  if (!ss || !ss.available) return;
  const rows = ss.sessions || [];

  let old = null;
  try {
    const raw = localStorage.getItem(CHAT_KEY);
    const data = raw ? JSON.parse(raw) : null;
    if (Array.isArray(data) && data.length) old = data;
  } catch (e) { return; }
  if (!old || rows.length) return;  // 有会话，或没有老消息：不迁移

  let imported;
  try {
    imported = await post("/api/agent/session_import", { title: "历史会话" });
  } catch (e) { return; }
  const sid = imported && imported.session ? imported.session.id : "";
  if (!sid) return;
  chatUseSession(sid);
  chatSave(old);
  try { localStorage.removeItem(CHAT_KEY); } catch (e) {}
}

// 左栏两级：项目（可点选） → 该项目下的会话（带日期 / 选中态）。
// 每个项目各拉一次 /api/agent/sessions?directory= —— 项目数上限 20，
// N+1 次只读请求可接受；后端只列目录、不读内容。
async function chatSessionsPaint() {
  const box = $("#chat-rail-body");
  if (!box) return;
  box.textContent = "";
  let data;
  try { data = await api("/api/agent/projects"); }
  catch (e) { box.appendChild(el("div", "hint", "工作区读不到：" + e.message)); return; }
  const list = (data.projects || []).slice(0, 20);
  if (!list.length) {
    box.appendChild(el("div", "hint", "还没有项目档案，去工作台新建一个。"));
    return;
  }

  // 新建会话：归属当前目录（后端 session_new 自己从 prefs 解析）。
  const add = el("button", "btn ghost chat-rail-add", "＋ 新建会话");
  add.onclick = async () => {
    try {
      const res = await post("/api/agent/session_new", {});
      if (res && res.session && res.session.id) {
        await chatSessionSwitch(res.session.id);
        toast("已新建会话");
      }
    } catch (e) { toast("新建失败：" + e.message); }
  };
  box.appendChild(add);

  const wrap = el("div", "chat-rail-list");
  for (const p of list) {
    const head = el("button", "chat-rail-item is-project", p.name || p.path || "未命名");
    head.title = p.path || "";
    head.onclick = async () => {
      try {
        await post("/api/agent/project_select", { id: p.id });
        toast("已切到「" + (p.name || p.path) + "」");
        chatSidePaint();
      } catch (e) { toast("切换失败：" + e.message); }
    };
    wrap.appendChild(head);

    let sd;
    try {
      sd = await api("/api/agent/sessions?directory=" + encodeURIComponent(p.path || ""));
    } catch (e) { sd = { available: false, reason: e.message, sessions: [] }; }
    if (!sd.available) {
      wrap.appendChild(el("div", "hint", sd.reason ? ("会话读不到：" + sd.reason) : "会话读不到"));
      continue;
    }
    const items = sd.sessions || [];
    if (!items.length) {
      wrap.appendChild(el("div", "hint", "这个项目还没有会话"));
      continue;
    }
    for (const s of items) {
      const current = s.id === chatSessionId;
      const row = el("div", "chat-rail-item is-session");
      row.style.paddingLeft = "14px";
      row.style.borderLeft = current ? "3px solid var(--accent, #2f6f4f)" : "3px solid transparent";
      row.title = s.title || "";
      const label = el("span", "chat-rail-sess-title");
      label.textContent = (current ? "● " : "  ") + (s.title || "未命名");
      row.appendChild(label);
      const date = el("span", "chat-rail-sess-date");
      date.textContent = s.updated_at ? when(s.updated_at) : "";
      row.appendChild(date);
      row.onclick = () => chatSessionSwitch(s.id);

      const ren = el("button", "chat-rail-sess-act", "改");
      ren.onclick = async (ev) => {
        ev.stopPropagation();
        const title = window.prompt("会话名称", s.title || "");
        if (title === null) return;
        try {
          await post("/api/agent/session_rename", { id: s.id, title: title });
          chatSessionsPaint();
        } catch (e) { toast("改名失败：" + e.message); }
      };
      row.appendChild(ren);

      const del = el("button", "chat-rail-sess-act", "删");
      del.onclick = async (ev) => {
        ev.stopPropagation();
        if (!window.confirm("删除会话「" + (s.title || "") + "」？历史对话仍按会话保留，不会一起删。")) return;
        try {
          await post("/api/agent/session_delete", { id: s.id });
          if (s.id === chatSessionId) chatUseSession("");
          chatSessionsPaint();
        } catch (e) { toast("删除失败：" + e.message); }
      };
      row.appendChild(del);
      wrap.appendChild(row);
    }
  }
  box.appendChild(wrap);
}

// 切会话：换前端 key + 清内存消息 + 停右栏轮询 + 走 load("chat") 整页重绘。
// 会话归属写进 prefs 由后端 session_switch 负责；前端不碰 prefs_save，
// 避免与项目/模型等字段的全量覆盖打架（V1.174 教训）。
async function chatSessionSwitch(sid) {
  if (!sid) return;
  try {
    await post("/api/agent/session_switch", { id: sid });
  } catch (e) { toast("切换失败：" + e.message); return; }
  chatUseSession(sid);
  history_ = [];
  if (typeof chatSideTimer !== "undefined" && chatSideTimer) {
    clearInterval(chatSideTimer);
    chatSideTimer = null;
  }
  load("chat");
}
"""
