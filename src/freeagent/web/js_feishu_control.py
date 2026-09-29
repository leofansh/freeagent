"""飞书桥接的启停按钮（设计方案 12.7）。

与 :mod:`js_feishu_config` 分开：那边改**配置**，这边动**进程**。

三条不能违反的约束：

1. **不显示没把握的东西**。「已停止」只在真的没有进程时显示；端口被别人占着
   而我们没证据那是自己的桥接时，说成「已停止」就是把「有东西在跑、但我管不着」
   谎报成「干净」。宁可说「端口被占着，管不着」。
2. **重启是独立的按钮，不是保存的副作用**。重启会中断正在收消息的连接，
   不该由一次「保存」顺手触发。
3. **失败要说人话**。后端给的消息是特意写过的（「大概率已经有一个桥接在跑了」），
   直接显示，别在界面上再翻译一遍 —— 翻译只会丢掉信息。
"""

from __future__ import annotations

__all__ = ["JS_FEISHU_CONTROL"]

JS_FEISHU_CONTROL = r"""function feishuControlCard(ctl, onChanged) {
  const card = el("div", "card");
  // running / supervised+exited / port_held-but-not-ours / clean four states,
  // deliberately not merged. Telling "already running" and "someone else holds
  // the port" apart is the whole point: they need different actions.
  let tagCls = "tag warn";
  let tagText = "已停止";
  if (ctl.running) { tagCls = "tag open"; tagText = "运行中"; }
  else if (ctl.supervised && ctl.returncode != null) { tagText = "已退出"; }
  else if (ctl.port_held) { tagCls = "tag warn"; tagText = "端口被占"; }

  const why = el("div", "why");
  why.style.marginTop = "6px";
  why.innerHTML = "<b>" + esc(ctl.message) + "</b>";

  card.innerHTML =
    '<div class="row1"><span class="title">桥接进程</span>' +
    '<span class="' + tagCls + '">' + esc(tagText) + "</span></div>";
  card.appendChild(why);

  const facts = el("div", "grid");
  facts.style.marginTop = "8px";
  facts.innerHTML =
    feishuField("锁端口", ctl.lock_port == null ? "未知" : "端口 " + ctl.lock_port,
      "同机只允许一个桥接。占用 = 已经有一个在跑。") +
    feishuField("进程号", ctl.supervised && ctl.returncode == null
      ? (ctl.pid == null ? "由本界面启动" : String(ctl.pid))
      : (ctl.supervised ? "已退出" : "非本界面启动"),
      ctl.supervised ? "" : "这个桥接不是从本界面启的，所以这里停不掉它。") +
    feishuField("日志", ctl.log_path || "",
      "启停与心跳都写在这里。");
  card.appendChild(facts);

  const actions = el("div", "actions");
  const msg = el("span", "hint");
  const doAction = (action, label) => {
    const b = el("button", null, label);
    b.onclick = async () => {
      b.disabled = true;
      msg.textContent = "处理中…";
      try {
        const after = await api("/api/feishu/bridge", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: action }),
        });
        msg.textContent = "";
        toast(label + "成功");
        onChanged(after);
      } catch (err) {
        // Back-end messages are written for humans; show them as-is.
        msg.innerHTML = '<span class="err">' + esc(err.message) + "</span>";
        b.disabled = false;
      }
    };
    return b;
  };
  // Running -> offer stop/restart; stopped -> offer start. Offering "start"
  // while running would just produce an error message.
  if (ctl.running) {
    actions.appendChild(doAction("restart", "重启"));
    actions.appendChild(doAction("stop", "停止"));
  } else {
    actions.appendChild(doAction("start", "启动"));
  }
  actions.appendChild(msg);
  card.appendChild(actions);
  return card;
}"""
