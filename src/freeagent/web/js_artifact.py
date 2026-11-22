"""产出验收的按钮（设计文档 12.9，2026-10-17）。

## 为什么单独一个文件

``js_detail.py`` 已经在 249 行，正撞 ``TestWebPackageStaysSmall`` 的
250 行上限。而这两行按钮**必须加** —— 服务层做完、Web 端点做完，
界面上没有按钮，就是那条验证纪律说的「HTTP 成功 ≠ 用户能看到」：
接口全绿，而用户仍然只能去终端。

所以新开一个模块，而不是把 ``js_detail.py`` 挤爆或删掉别人的注释。

## 只在「当前稿是草稿」时出现

已采纳的稿不该再给「验收」按钮 —— 点它必然得到一句「已经验收过了」。
那种按钮属于「点了才知道不行」，是本仓明确不做的事
（见 ``endpoints_artifact._require_draft``：三种不能各给一句不同的话）。

判定用**服务层给的 status**，前端不自己推：「这个状态能不能验收」
是领域知识，猜错就是那个 404/报错按钮。
"""

from __future__ import annotations

__all__ = ["JS_ARTIFACT"]

JS_ARTIFACT = r"""// 产出验收：把「待你验收」从任务上的一句话变成两个能点的按钮。
//
// 复用 js_detail 里的 post() / toast() / openTask() —— 它们是全局的，
// 刻意不重复一份 fetch 封装：令牌注入、错误转消息都在那儿，
// 复制一份迟早与它漂移。
function artifactCopyBar(data, card) {
  const a = data.artifact;
  if (!a || !a.content) return;
  const title = data.task && data.task.title ? data.task.title : "产物";
  // 反斜杠与引号走 char code：raw 串里写字面量会漏出转义引号让 JS 炸掉
  // （V1.114 的坑，test_web_js_syntax 咬的就是这个）。
  const illegal = [String.fromCharCode(92), "/", ":", "*", "?",
                   String.fromCharCode(34), "<", ">", "|"];
  const safe = title.split("").map((c) => illegal.includes(c) ? "_" : c).join("");
  const row = el("div", "acts");
  row.appendChild(el("span", "lbl", "产物"));
  const cp = el("button", "btn", "复制这一版");
  cp.onclick = () => {
    if (!navigator.clipboard) { toast("复制不了：这个浏览器没有剪贴板接口。"); return; }
    navigator.clipboard.writeText(a.content).then(
      () => toast("这一版已复制。"), () => toast("复制失败，请手动选中。"));
  };
  const ex = el("button", "btn", "导出 .md");
  ex.onclick = () => {
    const blob = new Blob([a.content], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = safe + ".md";
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  };
  row.appendChild(cp);
  row.appendChild(ex);
  card.appendChild(row);
}

function artifactBar(data, card) {
  const a = data.artifact;
  // 复制 / 导出对任何有正文的稿都给（含已采纳版）—— 那是「直接可交」，
  // 与验收是两件事，不能被草稿态判定挡掉。
  artifactCopyBar(data, card);
  // 只有草稿可验收。已采纳 / 已被取代都不给按钮。
  if (!a || a.status !== "draft") return;
  const t = data.task;

  const bar = el("div", "acts");
  bar.appendChild(el("span", "lbl", "验收"));

  const accept = el("button", "btn primary", "验收这一版");
  accept.onclick = async () => {
    accept.disabled = true;
    try {
      const r = await post(
        "/api/task/" + encodeURIComponent(t.id) + "/accept");
      toast(r.message || "已验收");
      openTask(t.id);
    } catch (e) {
      accept.disabled = false;
      toast(e.message);
    }
  };
  bar.appendChild(accept);

  // 「再来一版」只在有已采纳版时才有意义 —— 而此刻是草稿态，
  // 所以它归到下一轮：用户先验收，再在详情里开新版本。
  if (data.accepted_artifact) {
    const reopen = el("button", "btn", "以已采纳版为基础再出一版");
    reopen.onclick = async () => {
      reopen.disabled = true;
      try {
        const r = await post(
          "/api/task/" + encodeURIComponent(t.id) + "/reopen");
        toast(r.message || "开了新版本");
        openTask(t.id);
      } catch (e) {
        reopen.disabled = false;
        toast(e.message);
      }
    };
    bar.appendChild(reopen);
  }

  card.appendChild(bar);
}"""