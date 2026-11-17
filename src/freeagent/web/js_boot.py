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
  await refreshBadges();
}

// 导航角标（V1.58）：不用点开就知道有没有事。只报数不报内容，0 隐藏。
async function refreshBadges() {
  let s;
  try {
    s = await api("/api/summary");
  } catch (e) { return; /* 角标失败不影响主功能 */ }
  setBadge("b-today", s.today);
  setBadge("b-promises", s.promises_overdue);
  setBadge("b-pending", s.pending);
  setBadge("b-nudge", s.nudge);
  // 承诺/待处理/催办都下沉在「更多」里：三个计数聚合到更多按钮上，
  // 否则菜单项藏着角标，用户不展开就永远看不到有待办。
  setBadge("b-more", (s.promises_overdue || 0) + (s.pending || 0) + (s.nudge || 0));
}

function setBadge(id, n) {
  const elm = document.getElementById(id);
  if (!elm) return;
  if (!n) { elm.hidden = true; return; }
  elm.textContent = n > 99 ? "99+" : String(n);
  elm.hidden = false;
}

async function load(next) {
  view = next;
  detail = null;
  // 连续快速切视图时，上一次 load() 的异步回包可能后于本次落盘，把页面画成
  // 旧视图（走查实测：遍历完飞书页再回对话，飞书的回包晚到，盖掉了对话壳）。
  // 每次进入记自己的目标视图；await 回来后若已被更新的切换取代，放弃重绘。
  const mine = next;
  const superseded = () => view !== mine;
  // 页脚是跨视图持久的元素：不清空的话，上一视图的说明（含终端命令）
  // 会原样漏到下一视图 —— 承诺页底部挂着「角色可在终端 …」就是这么来的。
  $("#foot").textContent = "";
  // 切走时停掉工作台的轮询：否则它会在一个已经不存在的 DOM 上一路重画
  // （typeof 守卫：模块没进页面时不能炸）。
  if (typeof wbStopPoll === "function") wbStopPoll();
  $("#nav").hidden = false;
  document.querySelectorAll("#nav button[data-view]").forEach((b) =>
    b.setAttribute("aria-selected", String(b.dataset.view === view)));
  // 当前视图在下拉里时，选中态挂在「更多」上；切走后收起浮层。
  $("#more-btn").setAttribute("aria-selected", String(MENU_VIEWS.has(view)));
  closeMore();
  const root = $("#root");
  root.innerHTML = '<div class="empty">载入中…</div>';
  try {
    if (view === "settings") {
      // 设置页不放「新建事务」表单：那是事务入口，设置页不是
      const data = await api("/api/settings");
      if (superseded()) return;
      renderSettings(data);
      await refreshHeader();
      return;
    }
    if (view === "chat") {
      await loadChat();
      if (superseded()) return;
      // 首屏也要刷头部状态：规则层 / 已配 Key 与其它视图一致可见，
      // 不能只有进了设置页才知道自己在降级。
      await refreshHeader();
      return;
    }
    if (view === "workbench") {
      // 工作台（21.4）：选目录 / 模型 / 授权档，一句话进去干活。
      // 提前 return，不加「新建事务」表单 —— 那一页不是事务入口。
      await loadWorkbench();
      if (superseded()) return;
      return;
    }
    if (view === "oc") {
      // 编程页签：四段选择 + 日常可选模型。选项来自 /api/oc/options，
      // 与飞书那四张卡同一个来源（services/oc_discovery）。
      loadOcSelection(root);
      return;
    }
    if (view === "services") {
      // 后台服务页：进程状态与启停。控制与配置分离的「控制」那一半。
      loadServices();
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
      if (superseded()) return;
      try {
        lg = await api("/api/feishu/log?lines=200");
      } catch (e) {
        lg = { exists: false, path: "", lines: [], notice: "日志读不出来：" + e.message };
      }
      // 配置单独取：读不到只该让配置那一块变不可用，不该让状态和日志
      // 一起消失。启停不在本页 —— 已统一到「后台服务」页签（/api/services）。
      let cfg = null;
      try {
        cfg = await api("/api/feishu/config");
      } catch (e) {
        cfg = null;
      }
      if (superseded()) return;
      renderFeishu(st, lg, cfg);
      return;
    }
    // 提醒先查：合并成一条摘要，不逐条弹（与终端同一条规则）
    try {
      const rem = await api("/api/reminders");
      // 安静窗口（9.8）：窗口内 count=0 但 quiet 有文案 —— 也要说出来，
      // 否则用户以为提醒功能坏了（跟终端 [提醒] 那一行同一份道理）。
      if (rem.count) {
        root.appendChild(
          el("div", "note", "🔔 " + esc(rem.text))
        );
      } else if (rem.quiet) {
        root.appendChild(el("div", "note", "🌙 " + esc(rem.quiet)));
      }
    } catch (e) { /* 提醒查不到不该挡住主视图 */ }
    if (view === "contacts") {
      // 关系视图：点开即看联系人一页账，不用记 /contact 命令（11.20.6）。
      // 提醒照常先查（contacts 分支提前 return，不走下面的新建表单）。
      await renderContacts(root);
      if (superseded()) return;
      return;
    }
    if (view === "promises") {
      // 承诺视图：我欠谁的 + 直接结账（11.19 / V1.55）。提前 return，不加新建表单。
      await renderPromises(root);
      if (superseded()) return;
      return;
    }
    if (view === "pending") {
      // 待处理视图：收集器里的捕获 + 一键澄清成事务（11.18 / V1.56）。
      // 提前 return，不加新建表单 —— 澄清走服务层唯一建事务入口。
      await renderPending(root);
      if (superseded()) return;
      return;
    }
    if (view === "nudge") {
      // 催办视图：该催的候选 + 起草铺事实留白草稿（11.20 / V1.57）。
      // 提前 return，不加新建表单 —— 草稿不建事务、系统不发。
      await renderNudge(root);
      if (superseded()) return;
      return;
    }
    if (view === "model") {
      // 画像视图：节奏 / 关系 / 承诺 三面（11.17 / V1.59）。
      // 提前 return，不加新建表单 —— 只读聚合，只摆事实。
      await renderModel(root);
      if (superseded()) return;
      return;
    }
    if (view === "today") {
      const data = await api("/api/today");
      if (superseded()) return;
      renderList(data, "今天没有排进来的事务。在上面记下一件事并填日期，就会排进来；已有事务打开后可排到今天。");
    } else if (view === "roles") {
      const data = await api("/api/roles");
      if (superseded()) return;
      renderRoles(data);
    } else {
      const data = await api("/api/all?scope=" + (view === "closed" ? "closed" : "open"));
      if (superseded()) return;
      renderList(data, view === "closed" ? "还没有已结束的事务。" : "没有未结束的事务。");
    }
    const roles = await api("/api/roles");
    if (superseded()) return;
    root.insertBefore(newForm(roles.options), root.firstChild);
  } catch (e) {
    root.innerHTML = '<div class="note">出错了：' + esc(e.message) + "</div>";
  }
  await refreshHeader();
}

document.querySelectorAll("#nav [data-view]").forEach((b) => {
  b.onclick = () => {
    // 离开对话就退掉多选删除模式（它只属于对话）。不 reload ——
    // 紧接着的 load 会重绘一切。typeof 守卫：模块没进页面时不能炸。
    if (b.dataset.view !== "chat" && typeof chatPickExit === "function") {
      chatPickExit(false);
    }
    closeMore();
    load(b.dataset.view);
  };
});
$("#refresh").onclick = () => {
  if (detail) openTask(detail.task.id);
  else load(view);
};
load("chat");"""
