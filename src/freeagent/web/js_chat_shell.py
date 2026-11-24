"""对话页的三栏外壳（V1.154；V1.159 起两侧默认收起；V1.160 窄屏互斥；V1.163 输入栏加推理档）：左工作区 / 中对话 / 右产物。

OpenCode 与 WorkBuddy 都是这个形态：中间永远是一条对话，工作痕迹在右栏。
本模块只负责把现有数据端点摆成两侧面板，不改任何写入口 / 授权 / 端点。
"""
from __future__ import annotations

__all__ = ["JS_CHAT_SHELL"]

JS_CHAT_SHELL = r"""// 对话页三栏外壳：左工作区 / 中对话 / 右产物，两侧默认收起（V1.159），窄屏互斥（V1.160）。
// 开合有两处入口——栏内"收起"与"…"菜单——都走 chatPanelToggle 这一个函数，
// 免得两处各自记一份状态后漂移（同 V1.115 不另存 touched_files 的道理）。
// 窄屏（视口 <1000px）放不下两条侧栏，开一侧就收另一侧，中间对话不被挤扁（V1.160）。
const CHAT_NARROW = 1000;
function chatPanelToggle(which, force) {
  const panel = which === "rail" ? $("#chat-rail") : $("#chat-side");
  const btn = which === "rail" ? $("#chat-rail-toggle") : $("#chat-side-toggle");
  if (!panel || !btn) return;
  const name = which === "rail" ? "工作区" : "产物";
  const open = force === undefined ? panel.hidden : force;
  panel.hidden = !open;
  // 规则固定：窄屏开这一侧就收另一侧，不记"后开哪个"（记了就是第二个真源）。
  // 另一侧按钮在收起的面板里看不见，文案不必改；下次打开菜单时按状态刷新。
  if (open && window.innerWidth < CHAT_NARROW) {
    const other = which === "rail" ? $("#chat-side") : $("#chat-rail");
    if (other) other.hidden = true;
  }
  // 按钮就在面板里，只有打开时才看得见；文案固定说"收起"，不跟着翻来翻去。
  btn.textContent = "收起" + name + " ×";
  btn.title = "收起" + name;
}

function chatShellMount() {
  const root = $("#root");
  const center = root && root.querySelector(".chat");
  if (!root || !center || $("#chat-shell")) return;
  const shell = el("div", "chat-shell");
  const left = el("aside", "chat-rail"); left.id = "chat-rail";
  const right = el("aside", "chat-side"); right.id = "chat-side";
  // 第一眼只给一条对话：两侧"备着"而不是一上来就占满屏（V1.159）。
  left.hidden = true; right.hidden = true;
  const lt = el("button", "chat-panel-close", "收起工作区 ×"); lt.id = "chat-rail-toggle";
  const rt = el("button", "chat-panel-close", "收起产物 ×"); rt.id = "chat-side-toggle";
  lt.onclick = () => chatPanelToggle("rail", false);
  rt.onclick = () => chatPanelToggle("side", false);
  left.appendChild(lt); right.appendChild(rt);
  // 数据区单独成容器：paint 只清空它，不动"收起"按钮（V1.169 修重复渲染）。
  // 不清空会怎样：切视图 / 状态刷新 / 项目切换都反复调 paint，每次往 aside
  // 里堆一份同样的清单，截图里产物栏就重复了两遍。
  const railBody = el("div", "chat-rail-body"); railBody.id = "chat-rail-body";
  const sideBody = el("div", "chat-side-body"); sideBody.id = "chat-side-body";
  left.appendChild(railBody); right.appendChild(sideBody);
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
  // 顺序 = 插入顺序（都插在箭头前）：项目 / 模型 / 推理档 / 授权档 / 派活。
  // 推理档紧跟模型（档位依附于模型，选完模型顺手选强度），授权档收尾（V1.164）。
  // 项目是按钮不是原生 select：豆包式面板要塞「新建 / 选目录」操作项，原生下拉放不下（V1.165）。
  // V1.172 人性布局：四个下拉都是「出发前配置」，派活和 ↑ 都是「把这句话发出去」的动作，
  // 故派活放最后、紧挨箭头前——先配好、最后决定怎么发；配置区不被动作按钮打断。
  const proj = el("button", "chat-ctrl chat-proj-btn");
  proj.id = "chat-ctrl-project"; proj.title = "项目";
  tools.insertBefore(proj, arrow);
  const model = mk("chat-ctrl-model", "模型");
  const variant = mk("chat-ctrl-variant", "推理档");
  const mode = mk("chat-ctrl-mode", "授权档");
  // 派活按钮（V1.171；V1.172 挪到最右）：一句话起手的显式入口。不在每句话自动识别
  // （白花模型 + 答非所问误判回潮），要点才识别——自动认项目/填目录，确认闸门仍由
  // chatDelegate 弹，误点也只是弹确认框、取消即可。
  const dispatch = el("button", "chat-ctrl chat-dispatch-btn", "派活");
  dispatch.id = "chat-ctrl-dispatch"; dispatch.title = "认出项目和要做的事，确认后交给引擎";
  tools.insertBefore(dispatch, arrow);
  const fill = (sel, items, current) => {
    // 重填前先清空：换模型后推理档要按新模型重列，不请空会越堆越多（V1.163）。
    sel.textContent = "";
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
  // 项目选择器交给豆包式面板模块挂载（保留 #chat-ctrl-project 这个 id 供守卫识别）。
  chatProjMount(proj, ps.projects || [], prefs.project_id || "");
  chatIntakeMount(dispatch);  // 派活按钮绑 onclick（V1.171）
  fill(model, (oc.model && oc.model.options || []).map(
    (o) => [o.value, o.label]), prefs.model || "");
  fill(mode, (st.modes || []).map((m) => [m.key, m.label]), prefs.mode || "ask");
  // 推理档选项依附模型：首项恒是空值「不指定」。模型没有可用档位时只剩这一项，
  // 此时置灰禁用——常驻可见但不造假选择（V1.163，对齐豆包 / OpenCode 的形态）。
  const variantOpts = (oc.variant && oc.variant.options) || [];
  const usableVariants = variantOpts.filter((o) => o.value);
  fill(variant, variantOpts.map((o) => [o.value, o.label]), prefs.variant || "");
  variant.disabled = usableVariants.length === 0;
  variant.title = usableVariants.length === 0 ? "当前模型没有可选推理档" : "推理档";
  model.onchange = async () => {
    // 换模型清档（同委派总览页 V1.163）：旧档位可能对新车模型无效，
    // 前端立即回到「不指定」并重新拉该模型的档位列表，避免选了 high 却静默失效。
    await save({ model: model.value, variant: "" });
    try {
      const fresh = await api("/api/oc/options");
      const opts = (fresh.variant && fresh.variant.options) || [];
      fill(variant, opts.map((o) => [o.value, o.label]), "");
      variant.disabled = opts.filter((o) => o.value).length === 0;
      variant.title = variant.disabled ? "当前模型没有可选推理档" : "推理档";
    } catch (e) { /* 拉不到就保留空项并禁用，不猜档位 */ variant.disabled = true; }
  };
  variant.onchange = () => save({ variant: variant.value });
  mode.onchange = () => save({ mode: mode.value });
}

async function chatRailPaint() {
  // 两级会话栏（V1.176）：项目 → 该项目下的会话，整块交给 chat_sessions 模块。
  // 这里只做一层委托，避免 js_chat_shell.py 撞 250 行上限（同 js_chat_store 的拆法）。
  if (typeof chatSessionsPaint === "function") { await chatSessionsPaint(); return; }
  // 兜底：模块没拼进来时退回单层项目列表（旧形态），不让左栏空白。
  const box = $("#chat-rail-body");
  if (!box) return;
  box.textContent = "";
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

let chatSideTimer = null;
// 派活后不跳工作台（V1.175）：右栏自己轮询，直到这一轮进终态才停。
// 只挂一条链——重复挂会让多条响应各自重画，产物栏闪成一片。
function chatSideArm() {
  if (chatSideTimer) return;
  chatSideTimer = setInterval(async () => {
    const st = await api("/api/agent/state").catch(() => null);
    if (!st || !st.turn || ["running", "waiting"].indexOf(st.turn.status) < 0) {
      clearInterval(chatSideTimer); chatSideTimer = null;
    }
    chatSidePaint();
  }, 1500);
}

async function chatSidePaint() {
  const box = $("#chat-side-body");
  if (!box) return;
  // 同 chatRailPaint：多次调用（切视图 / 刷新 / 项目切换）会重复堆产物，先清空（V1.169）。
  box.textContent = "";
  let st;
  try { st = await api("/api/agent/state"); }
  catch (e) { box.appendChild(el("div", "hint", "产物读不到：" + e.message)); return; }
  const turn = st.turn;
  const head = el("div", "row1", "产物"); box.appendChild(head);
  if (!turn) {
    box.appendChild(el("div", "hint", "还没有委派产物。在对话里写一句，点「派活」或直接回车。"));
    return;
  }
  box.appendChild(el("div", "hint", "最近一轮：" + (turn.status || "未知") +
    (turn.brief ? " · " + turn.brief : "")));
  // 逐次授权卡：真正的允许 / 拒绝仍在工作台（闸门没搬），这里只给一条路标。
  // 不摆按钮——摆了却没接线，等于骗人点（fail-closed 要说清去哪决定）。
  if (turn.ask) {
    box.appendChild(el("div", "chat-side-ask", "这一轮等你决定：" + (turn.ask.label || "") +
      " —— 去「更多 → 委派总览」点允许 / 拒绝"));
  }
  const files = turn.touched_files || [];
  if (files.length) {
    box.appendChild(el("div", "chat-side-lbl", "改动文件"));
    const ul = el("div", "chat-side-files");
    files.forEach((f) => ul.appendChild(el("div", "chat-side-file", typeof f === "string" ? f : (f.path || ""))));
    box.appendChild(ul);
  }
  // 跑过的命令（V1.116 的安全项）：自动档下不弹卡，这里不说就只有引擎自己知道。
  const cmds = turn.commands || [];
  if (cmds.length) {
    box.appendChild(el("div", "chat-side-lbl", "跑过的命令"));
    const cu = el("div", "chat-side-files");
    cmds.forEach((c) => cu.appendChild(el("div", "chat-side-file", typeof c === "string" ? c : (c.text || ""))));
    box.appendChild(cu);
  }
  // 答复正文：模型输出不是 HTML，一律 textContent（同工作台）。
  if (turn.text) {
    const pre = el("pre", "chat-side-answer");
    pre.style.whiteSpace = "pre-wrap";
    pre.textContent = turn.text;
    box.appendChild(pre);
  }
  if (!files.length && !cmds.length && !turn.text) {
    box.appendChild(el("div", "hint", "这一轮没有可列出的改动 / 命令 / 答复。"));
  }
  // running / waiting 时装轮询，进终态由 chatSideArm 自己清。
  if (turn.status === "running" || turn.status === "waiting") chatSideArm();
}
"""
