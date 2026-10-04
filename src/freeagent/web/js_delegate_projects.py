"""委派白名单的授权界面（设计文档 12.7.1）。

四条不能违反的约束：

1. **未授权的必须一眼看出来**，不能和已授权的混在一起 —— 混了就会
   「看着能用、点下去被拒」。所以未授权的整行压暗，并且按钮写明「允许」。
2. **路径不由用户输入**。后端给什么就显示什么，用户只做「允许 / 撤销」
   的判断。这样既免了抄绝对路径，又不把准入权交给外部状态。
3. **授权是显式动作**，所以按钮不是「刷新」而是「允许」。点一下就改
   ``config.json``，必须让用户知道这一点。
4. **改了要说清要不要重启**。桥接是独立进程，读的是启动时那份配置；
   不说这句，用户会以为「保存了就是生效了」（与 ``js_feishu_config``
   第 3 条同一条纪律）。
"""

from __future__ import annotations

__all__ = ["JS_DELEGATE_PROJECTS"]

JS_DELEGATE_PROJECTS = r"""// 委派白名单的授权列表。
//
// 刻意**不**提供「手动填路径」的输入框：路径来自 OpenCode 的项目注册表，
// 照着抄一遍只会引入拼错的机会，而拼错的表现是一句「不在白名单」。
function delegateProjectsView(data, onChanged) {
  const box = el("div", "card");

  const head = el("div", "row1");
  head.appendChild(el("span", "title", "可委派项目"));
  const n = (data.items || []).length;
  const okCount = (data.items || []).filter((i) => i.allowed).length;
  head.appendChild(el("span", okCount ? "tag open" : "tag warn",
    okCount + " / " + n + " 已授权"));
  box.appendChild(head);

  if (!data.opencode_found) {
    box.appendChild(el("div", "note",
      "读不到 OpenCode 的项目列表。请确认它已安装并至少打开过一次 —— " +
      "这个列表是从它的项目注册表里查出来的，不是我这边猜的。"));
    return box;
  }

  if (!n) {
    box.appendChild(el("div", "hint", "OpenCode 里还没有任何项目。"));
    return box;
  }

  box.appendChild(el("div", "hint",
    "这些项目来自 OpenCode。<b>列在这里不等于已经授权</b> —— " +
    "要让助手能改某个项目，得点「允许」。"));

  const list = el("div", "fields");
  (data.items || []).forEach((item) => {
    const row = el("div", "field");
    if (!item.allowed) row.style.opacity = "0.72";

    const line = el("div", "row1");
    line.appendChild(el("span", null, item.name));
    line.appendChild(el("span", item.allowed ? "tag open" : "tag",
      item.allowed ? "已授权" : "未授权"));
    row.appendChild(line);

    // 路径只读展示，不给输入框。
    row.appendChild(el("div", "hint", "<code>" + esc(item.path) + "</code>"));

    const btn = el("button", null, item.allowed ? "撤销授权" : "允许");
    btn.type = "button";
    // 错误**行内显示**，不用 toast —— ``toast(msg)`` 只接受一个参数且没有
    // 错误样式，多传的第二个参数会被静默丢弃，于是「授权失败」会长得和
    // 「授权成功」一模一样。那正是本模块最不该丢的信号。
    const errBox = el("div", "err");
    btn.onclick = async () => {
      btn.disabled = true;
      btn.textContent = "处理中…";
      errBox.textContent = "";
      try {
        const body = item.allowed
          ? { revoke: item.name }
          : { allow: item.name };
        const after = await api("/api/delegate/projects", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        toast(item.allowed ? "已撤销" : "已授权");
        onChanged(after);
      } catch (e) {
        btn.disabled = false;
        btn.textContent = item.allowed ? "撤销授权" : "允许";
        errBox.textContent = e.message;
      }
    };
    row.appendChild(btn);
    row.appendChild(errBox);
    list.appendChild(row);
  });
  box.appendChild(list);

  if ((data.whitelist || []).length) {
    box.appendChild(el("div", "hint",
      "已写入 <code>config.json</code> 的路径：" +
      data.whitelist.map((p) => "<div>· <code>" + esc(p) + "</code></div>")
        .join("")));
  }

  box.appendChild(delegateRestartBanner());
  return box;
}

// 「改了但没生效」的提示。桥接与执行器都是独立进程，读的是**启动时**那份
// config.json —— 不说这句，用户会以为授权立刻就能用。
function delegateRestartBanner() {
  return el("div", "note",
    "⚠ <b>改了要重启才生效。</b>桥接和执行器都在启动时读一次配置，" +
    "现在授权的只是文件；重启它们（tools\\run_feishu.bat / " +
    "python -m freeagent.delegate）之后新白名单才被读进去。");
}"""