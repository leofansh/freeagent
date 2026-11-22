"""工作台产物侧栏（V1.132 / V1.135）。

为什么产物要同屏：真机上一轮里「碰过哪些文件 / 跑了哪些命令 / 回了什么」
挤在流里，长轮次要滚很久才看得到；侧栏把这三样固定在一处，边看步骤边对产物。

V1.135 加两样（借鉴 WorkBuddy 右栏）：
- **变更**：读 engine_ledger 的结构化账（路径 + 次数），不解析答复正文；
  回看历史轮次时状态没带账，那时不画，免得把「本会话累计」说成「那一轮」。
- **定位**：产物 / 变更里的文件路径各挂一个按钮，走服务端受控端点在系统
  文件管理器中选中；路径来自引擎，放 dataset、不拼 innerHTML。

为什么只填、不自己起 DOM 容器：容器由 js_workbench_page 装配
（`.wb-col-side#wb-side`），这一块只负责往里填内容 —— 一份能力一处实现，
填和摆不混在一起。

所有模型给的文本（文件 / 命令 / 正文）一律先 esc 或 textContent ——
和流里那三块同一条纪律（V1.117：正文不是 HTML）。
"""

from __future__ import annotations

__all__ = ["JS_WORKBENCH_SIDE"]

JS_WORKBENCH_SIDE = r"""// 工作台产物侧栏：把文件 / 命令 / 正文固定在同一屏右侧。
// 复制 / 导出只操作这一轮的答复原文（V1.133）—— 浏览器本地完成，不上传、
// 不经过服务端，所以「产物直接可交」这件事没有第二处真源。
function wbCopyText(text) {
  if (!navigator.clipboard) { toast("复制不了：这个浏览器没有剪贴板接口，请手动选中。"); return; }
  navigator.clipboard.writeText(text).then(
    () => toast("答复已复制到剪贴板。"),
    () => toast("复制失败，请手动选中文本。")
  );
}

function wbExportMd(text) {
  const blob = new Blob([text], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "freeagent-答复.md";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

// 在系统文件管理器中定位（V1.135）：只把路径交给服务端受控端点，
// 由它验根内、用参数数组起进程 —— 前端不拼命令、不碰本地执行。
function wbReveal(path) {
  api("/api/agent/reveal", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path: path })
  }).then(
    () => toast("已在文件管理器中定位。"),
    (e) => toast("定位失败：" + e.message)
  );
}

// 一条带「定位」的文件行。路径放 dataset 属性而不是拼进 innerHTML ——
// 路径来自引擎，带引号 / 尖括号时拼字符串会变成 HTML（V1.117 的坑）。
function wbLocateRow(path, suffix) {
  const row = el("div", "note wb-file-row");
  row.appendChild(el("span", "wb-file-path", esc(path)));
  if (suffix) row.appendChild(el("span", "wb-file-count", esc(suffix)));
  const loc = el("button", "chip wb-locate", "定位");
  loc.dataset.path = path;
  loc.onclick = () => wbReveal(loc.dataset.path);
  row.appendChild(loc);
  return row;
}

// 变更账（V1.135）：三态分开。unreadable 不许说成「没改」，
// 账里只有路径与次数、没有逐行差异 —— 如实说清，不编一个 diff。
function wbPaintLedger(side, ledger) {
  if (ledger.state === "unreadable") {
    side.appendChild(el("div", "note",
      "读不到改动账：" + esc(ledger.reason || "原因未知") + "—— 别把「没记录」当成「没改」。"));
    return;
  }
  if (ledger.state === "none") {
    side.appendChild(el("div", "note",
      "引擎这次会话里没有记到写文件操作 —— 这不等于没做事。"));
    return;
  }
  const rows = ledger.files || [];
  if (!rows.length) {
    side.appendChild(el("div", "note", "账里没有逐文件记录（引擎只给了路径与次数，没有逐行差异）。"));
    return;
  }
  rows.forEach((f) => side.appendChild(wbLocateRow(f.path, " ×" + f.count)));
  if (ledger.bash_failed) {
    side.appendChild(el("div", "note", esc(String(ledger.bash_failed)) + " 条命令非 0 退出"));
  }
  if (ledger.truncated) {
    side.appendChild(el("div", "note", "只翻了前几页，上面是下界（至少这么多）。"));
  }
}

function wbSidePaint(turn, ledger) {
  // 只填**上块**（V1.142）：右栏下块「这个工作区有什么」是常驻的，
  // 每轮整块清空会把文件清单一起抹掉、再重读一遍。
  const side = $("#wb-side-turn");
  if (!side) return;
  side.innerHTML = "";
  if (!turn) {
    side.appendChild(el("div", "note", "这一轮还没有产物。"));
    return;
  }
  side.appendChild(el("div", "wb-label", "产物"));

  const files = turn.files || [];
  if (files.length) {
    files.forEach((f) => side.appendChild(wbLocateRow(f, "")));
  } else {
    side.appendChild(el("div", "note", "这一轮没碰文件。"));
  }

  // 变更账是「本会话累计」。回看历史轮次时 st 没带 ledger —— 那时不画，
  // 免得把本会话的账说成那一轮的改动（标注说证据，也要说清范围）。
  if (ledger) {
    side.appendChild(el("div", "wb-label", "变更（本会话）"));
    wbPaintLedger(side, ledger);
  }

  const cmds = turn.commands || [];
  side.appendChild(el("div", "wb-label", "跑过的命令"));
  if (cmds.length) {
    cmds.forEach((c) => side.appendChild(el("div", "note", "$ " + esc(c))));
  } else {
    side.appendChild(el("div", "note", "这一轮没跑命令。"));
  }

  if (turn.text) {
    side.appendChild(el("div", "wb-label", "答复"));
    const acts = el("div", "chips");
    const cp = el("button", "chip", "复制答复");
    cp.onclick = () => wbCopyText(turn.text);
    const ex = el("button", "chip", "导出 .md");
    ex.onclick = () => wbExportMd(turn.text);
    acts.appendChild(cp);
    acts.appendChild(ex);
    side.appendChild(acts);
    const pre = el("pre", "card");
    pre.style.whiteSpace = "pre-wrap";
    pre.style.margin = "6px 0";
    pre.textContent = turn.text;
    side.appendChild(pre);
  }
}
"""
