"""启动与视图切换。


"""

from __future__ import annotations

__all__ = ["JS_BOOT"]

JS_BOOT = r"""// 全局错误陷阱（12.7 的对称面：接口有令牌，脚本异常也必须有形）。
// 背景：.then(成功, 失败) 里**成功回调抛的异常不会走失败分支**，会变成
// unhandledrejection 被浏览器吞掉 —— 症状就是「按钮点了没反应」。
// 所以在入口处挂两个全局钩子，把任何漏网异常变成可见的 toast + console。
window.addEventListener("error", (ev) => {
  try { toast("脚本错误：" + (ev.message || "未知")); } catch (_) {}
  if (window.console && console.error) console.error("[freeagent]", ev.error || ev.message);
});
window.addEventListener("unhandledrejection", (ev) => {
  const r = ev.reason;
  const m = r && r.message ? r.message : String(r);
  try { toast("未处理错误：" + m); } catch (_) {}
  if (window.console && console.error) console.error("[freeagent]", r);
});

async function refreshHeader() {
  try {
    const h = await api("/api/health");
    $("#hdr").textContent =
      "智能层 " + h.llm + (h.key_configured ? "（已配 Key）" : "（规则层）") +
      (h.energy_windows ? " · 精力档位已启用" : "");
  } catch (e) { /* 状态栏失败不影响主功能 */ }
}

async function load(next) {
  view = next;
  detail = null;
  $("#nav").hidden = false;
  document.querySelectorAll("#nav button").forEach((b) =>
    b.setAttribute("aria-selected", String(b.dataset.view === view)));
  const root = $("#root");
  root.innerHTML = '<div class="empty">载入中…</div>';
  try {
    if (view === "settings") {
      // 设置页不放「新建事务」表单：那是事务入口，设置页不是
      renderSettings(await api("/api/settings"));
      await refreshHeader();
      return;
    }
    if (view === "chat") {
      await loadChat();
      return;
    }
    if (view === "oc") {
      // 编程页签：四段选择 + 日常可选模型。选项来自 /api/oc/options，
      // 与飞书那四张卡同一个来源（services/oc_discovery）。
      loadOcSelection(root);
      return;
    }
    if (view === "feishu") {
      // 两个端点顺序取：状态拿不到也要把日志显示出来，否则「页面空了」
      // 会被理解成「什么都没开」，而真相可能是「日志能看但状态接口挂了」。
      let st, lg;
      try {
        st = await api("/api/feishu/status");
      } catch (e) {
        root.innerHTML = '<div class="note">状态读不出来：' + esc(e.message) + "</div>";
        return;
      }
      try {
        lg = await api("/api/feishu/log?lines=200");
      } catch (e) {
        lg = { exists: false, path: "", lines: [], notice: "日志读不出来：" + e.message };
      }
      // 配置与启停各自单独取：读不到只该让**那一块**变不可用，
      // 不该让状态和日志一起消失 —— 三块各有各的失败理由，混在一起报
      // 就分不清是哪一块坏了。
      let cfg = null;
      try {
        cfg = await api("/api/feishu/config");
      } catch (e) {
        cfg = null;
      }
      let ctl = null;
      try {
        ctl = await api("/api/feishu/bridge");
      } catch (e) {
        ctl = null;
      }
      renderFeishu(st, lg, cfg, ctl);
      return;
    }
    // 提醒先查：合并成一条摘要，不逐条弹（与终端同一条规则）
    try {
      const rem = await api("/api/reminders");
      if (rem.count) {
        root.appendChild(
          el("div", "note", "🔔 " + esc(rem.text))
        );
      }
    } catch (e) { /* 提醒查不到不该挡住主视图 */ }
    if (view === "today") {
      const data = await api("/api/today");
      renderList(data, "今天没有排进来的事务。去终端用 /today-pin 排期。");
    } else if (view === "roles") {
      const data = await api("/api/roles");
      renderRoles(data);
    } else {
      const data = await api("/api/all?scope=" + (view === "closed" ? "closed" : "open"));
      renderList(data, view === "closed" ? "还没有已结束的事务。" : "没有未结束的事务。");
    }
    const roles = await api("/api/roles");
    root.insertBefore(newForm(roles.options), root.firstChild);
  } catch (e) {
    root.innerHTML = '<div class="note">出错了：' + esc(e.message) + "</div>";
  }
  await refreshHeader();
}

document.querySelectorAll("#nav button").forEach((b) => {
  if (b.dataset.view) b.onclick = () => load(b.dataset.view);
});
$("#refresh").onclick = () => {
  if (detail) openTask(detail.task.id);
  else load(view);
};
load("chat");"""
