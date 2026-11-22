"""工作台前端（一）：状态与刷新（设计文档 21.4 / V1.114）。

## 它在替换什么

改造前，「让 OpenCode 干活」的唯一路径是**旧「编程」页（今委派总览）里四段选择 + 建一条委派
事务 + 去终端敲 /start**。那三步里只有第一步属于人（选目录、选模型），
后两步是系统该自己判断的事。这一块把第一步做成一页。

## 三条界面纪律

1. **状态不用颜色代替文字**：``在等授权``、``在思考``、``已完成`` 各写各的
   ——「转圈」和「卡住」在界面上必须长得不一样（12.7.2）。
2. **判断要可反驳**：没交给引擎时，把理由和「仍交给引擎」的按钮一起摆出来。
   规则错了，人能当场推翻，而不是被它挡在外面。
3. **认领不完的事要说出来**：引擎来时带着「本目录自动档仍来问我」这类备注，
   原样显示 —— 那是覆盖层没生效的信号，不能当无事发生。

## 为什么拆成三个文件

web 包单文件 250 行上限（``TestWebPackageStaysSmall``）。口径按职责分：
**状态刷新**（本文件） / **卡片与发送**（``js_workbench_card``） /
**页面装配与目录模型选择**（``js_workbench_page``）。
"""

from __future__ import annotations

__all__ = ["JS_WORKBENCH"]

JS_WORKBENCH = r"""// ---------------------------------------------------------------- 工作台
// 一次只装一条计时器（setTimeout 链）：一条链只有一个在飞的请求。
// 用 setInterval 的话，请求在飞时两次响应会各自再装一次计时器。
let wbTimer = null;
let wbTurn = null;
let wbBusy = false;
//: 正在看的是哪一条历史（空 = 在看最新）。它同时是「别让轮询把它盖掉」的闸：
//: 一条 2 秒的轮询会把人刚点开的那件事替换成最新的那件，看起来像「点了没反应」。
let wbHistId = "";

function wbStopPoll() {
  if (wbTimer) { clearTimeout(wbTimer); wbTimer = null; }
}

function wbArm(ms) {
  wbStopPoll();
  wbTimer = setTimeout(wbPoll, ms);
}

async function wbPoll() {
  // 视图被切走之后不许再画：那块 DOM 已经不在了，画上去的东西
  // 人也看不见，只会把计时器一条条堆下去。
  if (!document.getElementById("wb-stream")) { wbStopPoll(); return; }
  let st;
  try {
    st = await api("/api/agent/state");
  } catch (e) {
    // 读不到就退避重试，而不是停 —— 授权卡还在等着人点，
    // 「界面停在旧快照」会被理解成「那张卡没有了」（V1.107 的教训）。
    wbArm(5000);
    return;
  }
  wbPaint(st);
  const t = st.turn;
  if (t && (t.status === "running" || t.status === "waiting")) wbArm(1000);
  else {
    wbStopPoll();
    // 一轮刚落定 → 刷新「最近的活」：否则列表里还缺刚刚这一件，
    // 而人正盯着它想确认「这一轮记下来没有」。
    if (!wbHistId) wbHistory();
  }
}

function wbSpin(status) {
  return {
    running: "引擎在跑…",
    waiting: "在等你回答（它停下来了，不是在思考）",
    done: "已完成",
    failed: "没跑成",
    stopped: "已停止",
  }[status] || status;
}

function wbStep(status) {
  // 三种状态各写各的：把「还没完」和「出错」都叫「执行」，
  // 等于让人回去数工具次数（11.9.12：标注说证据，不说结论）。
  return { running: "进行中", done: "已执行", failed: "出错" }[status] || status;
}

function wbPaint(st) {
  const eng = st.engine || null;
  const turn = st.turn;
  wbTurn = turn;
  // 产物侧栏（V1.132）：和流同屏，无轮次时也要清掉上一轮的残留。
  if (typeof wbSidePaint === "function") wbSidePaint(turn, st.ledger);
  // 按钮可用态随状态走：没在跑就停不掉，上一轮没完再发只会吃到「还在跑」。
  const live = !!(turn && (turn.status === "running" || turn.status === "waiting"));
  const stopBtn = $("#wb-stop"); if (stopBtn) stopBtn.disabled = !live;
  const sendBtn = $("#wb-send"); if (sendBtn) sendBtn.disabled = live;

  const engBox = $("#wb-engine");
  // 只画「现在这台引擎」：点开一条历史时 st.engine 是空的，
  // 那时把引擎一行刷成 undefined 会让人以为引擎没了。
  if (engBox && eng) {
    // 只留「就绪/在跑」+ 用量一行：pid / 会话 ID / 目录是排障才要看的内部细节，
    // 摆在工作台顶栏等于把底座当主角（第21章「引擎藏到后台、用户无感」）。
    const bits = ["引擎：" + (eng.status === "ready" ? "就绪" : eng.status)];
    if (eng.error) bits.push("失败原因：" + eng.error);
    // 用量（V1.133）：读不到就说读不到，绝不显示 0 —— 把故障显示成「用了 0」是说谎。
    const u = st.usage;
    if (u && u.state === "ok") bits.push("Token 输入 " + u.input + " / 输出 " + u.output);
    else if (u && u.reason) bits.push("Token 读不到：" + u.reason.slice(0, 60));
    engBox.textContent = bits.join(" · ");
  }

  const stream = $("#wb-stream");
  if (!stream) return;
  stream.innerHTML = "";
  if (wbHistId) {
    // 看历史时先说明「这不是现在」—— 否则一条两小时前的失败会被
    // 读成「它现在又坏了」。
    const back = el("button", "btn", "回到最新");
    back.onclick = () => { wbHistId = ""; loadWorkbench(); };
    const head = el("div", "note", "正在看一条历史（不是现在）· ");
    head.appendChild(back);
    stream.appendChild(head);
  }
  if (!turn) {
    stream.appendChild(el("div", "note",
      "还没有跑过。选好项目和模型，说一句话 —— 要不要交给引擎由它自己判断。"));
    return;
  }
  stream.appendChild(el("div", "note",
    (turn.started_at ? esc(turn.started_at) + " · " : "") +
    esc(wbSpin(turn.status)) +
    " · 用时 " + turn.elapsed + "s" +
    (turn.tool_calls ? " · 工具 " + turn.tool_calls + " 次" : "") +
    (turn.tools && turn.tools.length ? "（" + esc(turn.tools.join("、")) + "）" : "")));
  if (turn.brief) {
    stream.appendChild(el("div", "row1", "我说：" + esc(turn.brief)));
  }
  if (turn.context_note) {
    // 「它带了什么」必须可见：读不出来与「没有事」在界面上是两句话。
    stream.appendChild(el("div", "note", "背景：" + esc(turn.context_note)));
  }
  const steps = turn.steps || [];
  if (steps.length) {
    // 一句「交给它」之后，人能看到的中间过程就是这份清单 ——
    // 「在跑」加一个工具计数说不出「跑到第几步、哪一步卡住」。
    stream.appendChild(wbStepsCard(steps));
  }
  if (turn.reason) {
    stream.appendChild(el("div", "note",
      (turn.status === "failed" || turn.status === "stopped" ? "⚠ " : "") +
      esc(turn.reason)));
  }
  // 「它碰过哪些文件 / 跑了哪些命令」（V1.116）。**命令这一列是安全项**：
  // 自动档下命令不弹卡，界面上再不说，人只能翻引擎自己的日志才能知道
  // 刚刚发生了什么。命令与路径都是模型给的原文，先过 esc 再渲染（V1.117）。
  const files = turn.files || [];
  const cmds = turn.commands || [];
  if (files.length) {
    const box = el("div", "card wb-process");
    box.appendChild(el("div", "row1", "碰过的文件（" + files.length + "）"));
    files.forEach((f) => box.appendChild(el("div", "note", esc(f))));
    stream.appendChild(box);
  }
  if (cmds.length) {
    const box = el("div", "card wb-process");
    box.appendChild(el("div", "row1", "它跑过的命令（" + cmds.length + "）"));
    cmds.forEach((c) => box.appendChild(el("div", "note", "$ " + esc(c))));
    stream.appendChild(box);
  }
  // 失败 / 被叫停之后给一条「下一步」：**填回输入框而不是替人重发** ——
  // 重发是花钱的动作，且可能正因为它刚出事才停下的（人要先看一眼再决定）。
  if (turn.status === "failed" || turn.status === "stopped") {
    const again = el("button", "btn", "照这句话再来一次");
    again.onclick = () => {
      const ta = $("#wb-text");
      if (!ta) return;
      ta.value = turn.brief || "";
      ta.focus();
      toast("已经填回输入框 —— 看一眼再点「交给它」");
    };
    stream.appendChild(again);
  }
  (turn.notes || []).forEach((n) => stream.appendChild(el("div", "note", "· " + esc(n))));
  if (turn.text) {
    const pre = el("pre", "card");
    pre.style.whiteSpace = "pre-wrap";
    pre.style.margin = "6px 0";
    pre.textContent = turn.text;  // 正文一律 textContent：模型输出不是 HTML。
    stream.appendChild(pre);
  }
  if (turn.ask) stream.appendChild(wbAsk(turn));
  // 看历史时不装轮询：那条链会把画好的内容换成最新的那一轮。
  if (!wbHistId && (turn.status === "running" || turn.status === "waiting")) {
    if (!wbTimer) wbArm(500);
  }
}

async function wbHistory() {
  const box = $("#wb-history");
  if (!box) return;
  let data;
  try {
    data = await api("/api/agent/turns?limit=10");
  } catch (e) {
    box.textContent = "读不到历史：" + e.message;
    return;
  }
  box.innerHTML = "";
  if (!data.available) {
    // 「没有历史能力」与「历史是空的」是两件事：前者要说清，否则人以为
    // 自己没干过活，或以为记录被清了。
    box.appendChild(el("div", "note",
      "这一版没有落库 —— 重启之后只能看到最近一轮。"));
    return;
  }
  if (!data.turns.length) {
    box.appendChild(el("div", "note", "还没有记录。"));
    return;
  }
  data.turns.forEach((t) => {
    const brief = (t.brief || "").length > 40
      ? t.brief.slice(0, 40) + "…" : (t.brief || "（没有记下原话）");
    const b = el("button", "btn", esc(wbSpin(t.status)) + " · " + esc(brief));
    b.style.display = "block";
    b.style.margin = "2px 0";
    b.onclick = () => wbOpen(t.id);
    box.appendChild(b);
  });
}

async function wbOpen(id) {
  // 先停轮询再画：顺序反了，轮询可能在 fetch 回来之前就把画面换成最新那条。
  wbStopPoll();
  let data;
  try {
    data = await api("/api/agent/turn?id=" + encodeURIComponent(id));
  } catch (e) {
    toast("这一条读不出来：" + e.message);
    return;
  }
  wbHistId = id;
  wbPaint({ turn: data.turn });
}
"""
