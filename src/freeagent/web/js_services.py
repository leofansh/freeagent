"""后台服务页：状态、启、停、重启。

为什么独立成模块（而不是塞进某个服务自己的页）：进程生命周期是**跨服务**
的关注点（桥接/执行器/将来的 hermes 都归它管），而配置留在各服务自己的页
—— 混在一处就会出现「恢复出厂要停服务，界面上只有一个服务有按钮」那种
半身不遂。样式全部复用已有类（card/tag/hint/btn），不新增 CSS。
"""

from __future__ import annotations

__all__ = ["JS_SERVICES"]

JS_SERVICES = r"""
// 当前视图是不是本页 —— 动作后只刷本页，不牵连别的。
async function loadServices() {
  const root = $("#root");
  root.innerHTML = '<div class="empty">载入中…</div>';
  let data;
  try {
    data = await api("/api/services");
  } catch (e) {
    root.innerHTML = '<div class="note">读不到服务状态：' + esc(e.message) + "</div>";
    return;
  }
  root.innerHTML = "";
  root.appendChild(el("div", "note",
    "这里管两个后台进程的启停与重启。配置不在这里：飞书凭据与白名单在" +
    "「飞书通道」页，委派白名单在终端的 config.json。"));
  data.items.forEach((s) => root.appendChild(serviceCard(s)));
  $("#foot").textContent = "状态按进程命令行探测；启动的进程脱离本页面运行。";
}

function serviceCard(s) {
  const box = el("div", "card");
  box.innerHTML =
    '<div class="row1"><span class="title">' + esc(s.title) + "</span>" +
    (s.running
      ? '<span class="tag open">在跑</span>'
      : '<span class="tag warn">已停止</span>') +
    (s.running && s.pid
      ? '<span class="meta">pid ' + s.pid +
        (s.started_by_page ? " · 本页面启的" : "") + "</span>"
      : "") +
    "</div>" +
    '<div class="hint" style="margin-top:4px">' + esc(s.note) + "</div>" +
    (s.cmdline
      ? '<div class="hint" style="margin-top:4px">' + esc(s.cmdline) + "</div>"
      : "");
  const bar = el("div", "acts");
  [["start", "启动"], ["stop", "停止"], ["restart", "重启"]].forEach(([a, label]) => {
    const b = el("button", "btn", label);
    b.onclick = () => serviceAct(s.key, a, b);
    bar.appendChild(b);
  });
  box.appendChild(bar);
  return box;
}

async function serviceAct(key, action, btn) {
  // 停止/重启是两个活进程的命：确认一道，别让手滑变成事故。
  if (action !== "start" &&
      !confirm("确定" + {stop: "停止", restart: "重启"}[action] + "这个服务？")) return;
  btn.disabled = true;
  try {
    const r = await api("/api/services/" + encodeURIComponent(key) + "/" + action, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    toast(r.message);
    loadServices();   // 用后端回的真相重绘，不用本地猜的状态
  } catch (e) {
    toast("没有执行：" + e.message);
    btn.disabled = false;
  }
}
"""
