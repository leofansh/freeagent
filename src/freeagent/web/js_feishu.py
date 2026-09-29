"""飞书通道页（设计方案 12.7）。

三条不能违反的约束，都写在代码注释里，因为它们都是「看起来多余」的那种：

1. **陈旧不显示成「已连接」**。进程被强杀时状态文件会留在盘上，
   照着它显示绿色 = 对着尸体报健康。虚假的安心比「未运行」更糟。
2. **每项状态都要能回答「所以呢」**。「connected: true」对用户没有信息量，
   要说清「群里 @ 它能回」还是「群里 @ 它不回（身份未知，门控失败关闭）」。
3. **「没配」和「配了但连不上」必须分开看**。这与 11.9.1「两种群里不响应
   必须能分开看」同源 —— 分不清原因，用户就只能猜。
"""

from __future__ import annotations

__all__ = ["JS_FEISHU"]

JS_FEISHU = r"""const FEISHU_STATE_LABEL = {
  starting: "启动中",
  ready: "就绪",
  degraded: "降级（部分功能不可用）",
  down: "已停止",
};

// 把状态翻译成「所以呢」。**不给用户看裸字段** —— connected: true
// 对没有飞书背景的人毫无信息量。
function feishuVerdict(s) {
  if (!s.status_file_exists) {
    return ["桥接没在跑",
      "没有状态文件。要用它得先起桥接：" +
      "tools\\run_feishu.bat（会交互式问你要 App Secret，" +
      "不落文件也不进 shell 历史）。"];
  }
  if (s.stale) {
    const age = s.age_seconds == null ? "" :
      "（最后一次心跳在 " + Math.round(s.age_seconds) + " 秒前）";
    return ["状态已陈旧" + age,
      "**这不等于它活着。** 进程被强杀时状态文件会留在盘上，" +
      "所以这里显示的是一份遗留记录，不是实时状态。重新起桥接即可。"];
  }
  if (s.state === "degraded") {
    return ["部分不可用",
      "桥接在跑，但 bot 身份没探到 → **群里 @ 它不会回**（失败关闭）。" +
      "私聊不受影响。跑 doctor 看这一项：" +
      "python -m freeagent.feishu.doctor --live"];
  }
  if (s.connected) {
    return ["已连接",
      "群里 @ 它才会回（@ 别人或不 @ 都不回）。"];
  }
  return ["正在连接", "长连接还没建立，稍等片刻。刷新看看。"];
}

function feishuField(label, value, hint) {
  return '<div class="field"><label>' + esc(label) + "</label>" +
    '<div class="lbl">' + esc(value) + "</div>" +
    (hint ? '<div class="hint">' + hint + "</div>" : "") + "</div>";
}

// 「最近找过 bot 的人」—— 填白名单时**照着填**，不用去翻日志。
//
// 为什么值得单独一块：白名单最大的坑是「不知道该填什么」。飞书同一个人有
// 三层 id（open_id 应用级 / user_id 租户级 / union_id），而换飞书应用
// open_id 就会变。把事件里**实际读到**的 id 摆出来，用户就不用猜。
//
// 刻意不显示消息正文 —— 有隐私，而这里只回答「你是谁」。
function feishuSeenSenders(s) {
  const rows = s.seen_senders || [];
  if (!rows.length) return null;

  const box = el("div", "card");
  box.innerHTML =
    '<div class="row1"><span class="title">最近找过它的人</span>' +
    '<span class="meta">' + rows.length + " 人</span></div>" +
    '<div class="hint" style="margin-top:6px">' +
    "从飞书发一句话（私聊直接发，群里 <b>@ 它</b>）就会出现。" +
    "<b>open_id</b> 换飞书应用就会变；<b>user_id</b> 跨应用不变 —— " +
    "想以后换应用不用重填白名单，就填 user_id。</div>";

  const table = el("div", "hint");
  table.style.marginTop = "8px";
  table.innerHTML = rows.map((r) => {
    const tag = r.matched
      ? '<span class="tag open">已在白名单</span>'
      : '<span class="tag warn">未授权</span>';
    const ids = [];
    if (r.open_id) ids.push("open_id " + esc(r.open_id));
    if (r.user_id) ids.push("user_id " + esc(r.user_id));
    if (r.union_id) ids.push("union_id " + esc(r.union_id));
    return "<div style='margin:4px 0'>" + tag + " " +
      (ids.join("<br>") || "（没有任何 id）") +
      (r.is_group ? ' <span class="meta">（群里 @ 的）</span>' : "") +
      "</div>";
  }).join("");
  box.appendChild(table);
  return box;
}

function renderFeishu(s, log, cfg, ctl) {
  const root = $("#root");
  root.innerHTML = "";

  const [headline, explain] = feishuVerdict(s);

  // --- 状态卡：先说「能不能用」，再说细节 ---
  const card = el("div", "card");
  let tagCls = "tag";
  let tagText = "未运行";
  if (s.stale && s.status_file_exists) { tagCls = "tag warn"; tagText = "已陈旧"; }
  else if (s.running) { tagCls = "tag open"; tagText = "已连接"; }
  else if (s.state === "degraded") { tagCls = "tag warn"; tagText = "降级"; }
  else if (s.status_file_exists) { tagCls = "tag warn"; tagText = "未连接"; }

  card.innerHTML =
    '<div class="row1"><span class="title">飞书通道</span>' +
    '<span class="' + tagCls + '">' + esc(tagText) + "</span></div>" +
    '<div class="why" style="margin-top:6px"><b>' + esc(headline) + "</b> — " +
    explain + "</div>" +
    (s.last_error
      ? '<div class="note" style="margin-top:6px">最近错误：' + esc(s.last_error) + "</div>"
      : "");
  root.appendChild(card);

  // --- 细节：每项都带「所以呢」 ---
  const g = el("div", "grid");
  g.innerHTML =
    feishuField("bot 身份",
      s.bot_open_id ? (s.bot_name || s.bot_open_id) : "未探到",
      s.bot_open_id
        ? "群聊 @ 门控靠它比对。@ 别人不抢答。"
        : "<b>没有身份 = 群里 @ 它永远不回。</b>doctor 能查这一项。") +
    feishuField("白名单", s.allowed_users_count == null ? "未知" : s.allowed_users_count + " 人",
      s.allowed_users_count === 0
        ? "<b>0 人 = 谁都不许指挥你。</b>这是刻意的安全底线。"
        : "只有名单内的 open_id 能让它动手。") +
    feishuField("去重表", s.dedup_entries == null ? "未知" : s.dedup_entries + " 条",
      "跨重启去重，所以重连重投不会重复建事务。") +
    feishuField("单实例锁", s.lock_port == null ? "未知" : "端口 " + s.lock_port,
      "同机只允许一个桥接。端口被占 = 已经有桥接在跑。") +
    feishuField("进程号", s.pid == null ? "—" : String(s.pid),
      s.pid == null ? "" : "想确认它还活着可以查这个号。");
  root.appendChild(g);

  // --- 配置：先说「改了没生效」，再给表单 ---
  // 顺序有意放在状态之后：先知道「能不能用」，再有「改哪儿」。
  // --- 白名单：先给「实际见到的 id」，再给表单 ---
  // 顺序有意：人看到自己真实的 id 之后再去填表单，而不是对着空框猜。
  const seenCard = feishuSeenSenders(s);
  if (seenCard) root.appendChild(seenCard);

  if (cfg) {
    const banner = feishuRestartBanner(cfg);
    if (banner) root.appendChild(banner);
    // 用闭包把 s / log / ctl 绑上：保存回调只回传新配置，直接传 renderFeishu
    // 会让它把配置当成「状态」传进第一个参数，重绘出一团乱码。
    root.appendChild(feishuConfigForm(
      cfg, (next) => renderFeishu(s, log, next, ctl)));
  } else {
    root.appendChild(el("div", "note",
      "配置读不出来，所以改不了。下面是状态和日志。"));
  }

  // --- 启停 ---
  if (ctl) {
    root.appendChild(feishuControlCard(
      ctl, (next) => renderFeishu(s, log, cfg, next)));
  } else {
    root.appendChild(el("div", "note",
      "启停状态读不出来，所以不能从这里启动或停止桥接。"));
  }

  // --- 排查路径：出问题先看哪儿 ---
  root.appendChild(el("div", "hint",
    "<b>出问题先按这个顺序看：</b><br>" +
    "1. 上面「bot 身份」是不是「未探到」—— 这是「群里 @ 它不回」的头号原因；<br>" +
    "2. 「白名单」是不是 0 人，或者你的 open_id 不在里面（换飞书应用后 open_id 会变）；<br>" +
    "3. 下面日志里有没有 <code>不在白名单</code> / <code>失败关闭</code> / <code>端口</code>；<br>" +
    "4. 都看不出就自己跑一次自检：<code>python -m freeagent.feishu.doctor --live</code>。"));

  // --- 日志 ---
  const logCard = el("div", "card");
  const head = '<div class="row1"><span class="title">桥接日志</span>' +
    '<span class="meta">' + esc(log.path) + "</span></div>";
  let bodyHtml;
  if (!log.exists) {
    bodyHtml = '<div class="hint" style="margin-top:6px">' + esc(log.notice) + "</div>";
  } else if (!log.lines.length) {
    bodyHtml = '<div class="empty" style="margin-top:6px">日志是空的。</div>';
  } else {
    const tinted = log.lines.map((l) => {
      let c = "";
      if (l.indexOf("ERROR") >= 0) c = " log-err";
      else if (l.indexOf("WARNING") >= 0) c = " log-warn";
      return '<div class="logline' + c + '">' + esc(l) + "</div>";
    }).join("");
    bodyHtml = '<div class="logbox">' + tinted + "</div>" +
      '<div class="hint">共 ' + log.total_lines + " 行，只显示尾部。点右上「刷新」重新读。</div>";
  }
  logCard.innerHTML = head + bodyHtml;
  root.appendChild(logCard);

  $("#foot").textContent =
    "配置存在本机磁盘上，Secret 只显示掩码、永不回传。改完记得重启桥接 —— " +
    "它是独立进程，读的是启动时那份配置。";
}"""
