"""委派白名单的授权界面（设计文档 12.7.1 / 12.7.1.1）。

五条不能违反的约束：

1. **未授权的必须一眼看出来**，不能和已授权的混在一起 —— 混了就会
   「看着能用、点下去被拒」。所以未授权的整行压暗，并且按钮写明「允许」。
2. **路径不由用户输入**。后端给什么就显示什么，用户只做「允许 / 撤销」
   的判断。可授权根浏览也一样：只在圈定的根里逐级点，服务端拼绝对路径。
3. **授权是显式动作**，所以按钮不是「刷新」而是「允许」。点一下就改
   ``config.json``，必须让用户知道这一点；「列出来」到「允许」永远是两步。
4. **改了要说清要不要重启**。桥接是独立进程，读的是启动时那份配置；
   不说这句，用户会以为「保存了就是生效了」（与 ``js_feishu_config``
   第 3 条同一条纪律）。
5. **三源的行都要有撤销/允许按钮**（12.7.1.1）：白名单里 OpenCode 不知道
   的行以前只是段纯文本 —— 看得见、撤不掉；进列表就为这个。
"""

from __future__ import annotations

__all__ = ["JS_DELEGATE_PROJECTS"]

JS_DELEGATE_PROJECTS = r"""// 委派白名单的授权列表。候选 = 三源并集（12.7.1.1）：OpenCode 注册表 /
// 已写入 config.json 的白名单 / 可授权根点选。刻意**不**提供「手动填路径」
// 的输入框：路径来自服务端，抄一遍只会引入拼错的机会。
//
// 点选候选与浏览位置放**模块级**：视图每次重绘都重建 DOM，而「点了选
// 还没授权」是跨重绘的会话状态 —— 跟着 DOM 走会表现成「点一下就没了」。
const delegatePicked = [];
let delegateBrowse = { root: null, path: "", dirs: null, err: "" };

const dNorm = (s) => s.replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
const dFull = (roots, p) => roots[p.root] + "/" + p.path;

function delegateProjectsView(data, onChanged) {
  const box = el("div", "card");
  const items = data.items || [];
  const roots = data.browse_roots || [];

  const head = el("div", "row1");
  head.appendChild(el("span", "title", "可委派项目"));
  const okCount = items.filter((i) => i.allowed).length;
  head.appendChild(el("span", okCount ? "tag open" : "tag warn",
    okCount + " / " + items.length + " 已授权"));
  box.appendChild(head);

  // 注册表查不到 ≠ 没东西可显示：白名单行与可授权根浏览不依赖 OpenCode，
  // 这里**不再提前 return**（三源并集之前的旧版会把它们一起藏掉）。
  if (!data.opencode_found) box.appendChild(el("div", "note",
    "读不到 OpenCode 的项目列表（它可能没装或没打开过）。" +
    "已写入 config.json 的白名单行与可授权根浏览不受影响。"));

  if (!items.length && !delegatePicked.length && !roots.length) {
    box.appendChild(el("div", "hint",
      data.opencode_found ? "OpenCode 里还没有任何项目。" : "没有可显示的项目。"));
    return box;
  }
  box.appendChild(el("div", "hint",
    "候选来自三处：OpenCode 注册表、已写入 config.json 的白名单、" +
    "可授权根点选。<b>列在这里不等于已经授权</b> —— 白名单行已授权" +
    "（只能撤销），其余要点「允许」。"));

  const repaint = () => onChanged(data);
  const list = el("div", "fields");
  delegatePicked.forEach((p) => list.appendChild(
    delegatePickedRow(data, p, repaint)));
  items.forEach((item) => list.appendChild(delegateItemRow(item, onChanged)));
  box.appendChild(list);

  if (roots.length) box.appendChild(delegateBrowseSection(data, onChanged));
  box.appendChild(delegateRestartBanner());
  return box;
}

// 已授权行（注册表 / 白名单同构）。撤销统一回传 **path**：白名单行在
// OpenCode 里查不到名字，回传名字会落到 basename 兜底，两条同名目录
// 会删错行 —— 服务端按精确路径优先匹配，路径是唯一无歧义的引用。
//
// 错误**行内显示**，不用 toast —— ``toast(msg)`` 只接受一个参数且没有
// 错误样式，多传的第二个参数会被静默丢弃，于是「授权失败」会长得和
// 「授权成功」一模一样。那正是本模块最不该丢的信号。所以授权按钮的
// 骨架只此一份（dPost），两个行渲染器共用，避免「一处记得、一处忘了」。
async function dPost(body, btn, idle, errBox, okMsg, done) {
  btn.disabled = true;
  btn.textContent = "处理中…";
  errBox.textContent = "";
  try {
    const after = await api("/api/delegate/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    toast(okMsg);
    done(after);
  } catch (e) {
    btn.disabled = false;
    btn.textContent = idle;
    errBox.textContent = e.message;
  }
}

function delegateItemRow(item, onChanged) {
  const row = el("div", "field");
  if (!item.allowed) row.style.opacity = "0.72";
  const line = el("div", "row1");
  line.appendChild(el("span", null, item.name));
  if (item.source === "whitelist") line.appendChild(
    el("span", "tag", "白名单（OpenCode 不知道）"));
  line.appendChild(el("span", item.allowed ? "tag open" : "tag",
    item.allowed ? "已授权" : "未授权"));
  row.appendChild(line);
  row.appendChild(el("div", "hint", "<code>" + esc(item.path) + "</code>"));
  const btn = el("button", null, item.allowed ? "撤销授权" : "允许");
  btn.type = "button";
  const errBox = el("div", "err");
  btn.onclick = () => dPost(
    item.allowed ? { revoke: item.path } : { allow: item.name },
    btn, item.allowed ? "撤销授权" : "允许", errBox,
    item.allowed ? "已撤销" : "已授权", onChanged);
  row.appendChild(btn);
  row.appendChild(errBox);
  return row;
}

// 从可授权根点选、还没授权的候选（客户端会话状态，见文件头注释）。
function delegatePickedRow(data, p, repaint) {
  const roots = data.browse_roots || [];
  const row = el("div", "field");
  const line = el("div", "row1");
  line.appendChild(el("span", null, p.name));
  line.appendChild(el("span", "tag warn", "可授权根点选 · 未授权"));
  row.appendChild(line);
  row.appendChild(el("div", "hint", "<code>" + esc(dFull(roots, p)) + "</code>"));
  const btn = el("button", null, "允许");
  btn.type = "button";
  const drop = el("button", null, "不选了");
  drop.type = "button";
  const errBox = el("div", "err");
  btn.onclick = async () => dPost(
    { allow_pick: { root: p.root, path: p.path } },
    btn, "允许", errBox, "已授权", (after) => {
      const i = delegatePicked.indexOf(p);
      if (i >= 0) delegatePicked.splice(i, 1);
      onChanged(after);
    });
  drop.onclick = () => {
    const i = delegatePicked.indexOf(p);
    if (i >= 0) delegatePicked.splice(i, 1);
    repaint();
  };
  row.appendChild(btn);
  row.appendChild(drop);
  row.appendChild(errBox);
  return row;
}

// 可授权根浏览段（delegate.browse_roots 非空才渲染）。这里**只浏览**：
// 进目录与「选为候选」都不碰服务端写接口，授权只发生在候选行的「允许」。
function delegateBrowseSection(data, onChanged) {
  const roots = data.browse_roots || [];
  const repaint = () => onChanged(data);
  const sec = el("div", "fields");
  sec.appendChild(el("div", "hint",
    "<b>可授权根</b>（config.json 的 <code>delegate.browse_roots</code>）：" +
    "在圈定的范围内逐级点进目录。<b>列出来 ≠ 已授权</b> —— 加成候选后" +
    "还要点「允许」。"));
  if (delegateBrowse.err) sec.appendChild(el("div", "err", esc(delegateBrowse.err)));
  const mk = (label, rel) => {
    const b = el("button", null, esc(label));
    b.type = "button";
    b.onclick = () => delegateBrowseGo(delegateBrowse.root, rel, repaint);
    return b;
  };
  if (delegateBrowse.root === null) {
    roots.forEach((r, i) => {
      const b = el("button", null, esc(r));
      b.type = "button";
      b.onclick = () => delegateBrowseGo(i, "", repaint);
      sec.appendChild(b);
    });
    return sec;
  }
  const segs = delegateBrowse.path ? delegateBrowse.path.split("/") : [];
  const crumb = el("div", "row1");
  crumb.appendChild(mk(roots[delegateBrowse.root], ""));
  segs.forEach((s, i) => {
    crumb.appendChild(el("span", null, " / "));
    crumb.appendChild(mk(s, segs.slice(0, i + 1).join("/")));
  });
  sec.appendChild(crumb);
  if (segs.length) sec.appendChild(mk("↑ 上一级", segs.slice(0, -1).join("/")));
  if (delegateBrowse.dirs === null) {
    sec.appendChild(el("div", "hint", "读取中…"));
    return sec;
  }
  if (!delegateBrowse.dirs.length) {
    sec.appendChild(el("div", "hint", "（这里没有子目录）"));
    return sec;
  }
  delegateBrowse.dirs.forEach((name) => {
    const rel = delegateBrowse.path ? delegateBrowse.path + "/" + name : name;
    const row = el("div", "row1");
    row.appendChild(mk(name, rel));  // 进入
    const add = el("button", null, "选为候选");
    add.type = "button";
    add.onclick = () => delegateAddCandidate(data, repaint, rel);
    row.appendChild(add);
    sec.appendChild(row);
  });
  return sec;
}

function delegateAddCandidate(data, repaint, rel) {
  const roots = data.browse_roots || [];
  const full = roots[delegateBrowse.root] + "/" + rel;
  const seen = (data.items || []).some((i) => dNorm(i.path) === dNorm(full)) ||
    delegatePicked.some((p) => dNorm(dFull(roots, p)) === dNorm(full));
  if (seen) { toast("这个目录已经在列表里了"); return; }
  const segs = rel.split("/");
  delegatePicked.push({
    root: delegateBrowse.root,
    path: rel,
    name: segs[segs.length - 1] || roots[delegateBrowse.root],
  });
  repaint();
}

// 目录导航：先取该层目录，再用**同一份服务端数据**重绘 —— 浏览状态在
// 模块级，重绘只换 DOM 不丢位置。失败写进 err 行内显示并继续展示。
async function delegateBrowseGo(rootIdx, rel, repaint) {
  delegateBrowse = { root: rootIdx, path: rel, dirs: null, err: "" };
  repaint();
  try {
    const d = await api("/api/delegate/browse?root=" + rootIdx +
      "&path=" + encodeURIComponent(rel));
    delegateBrowse.dirs = d.dirs || [];
  } catch (e) {
    delegateBrowse.dirs = [];
    delegateBrowse.err = e.message;
  }
  repaint();
}

// 「改了但没生效」的提示。桥接与执行器都是独立进程，读的是**启动时**那份
// config.json —— 不说这句，用户会以为授权立刻就能用。
function delegateRestartBanner() {
  return el("div", "note",
    "⚠ <b>改了要重启才生效。</b>桥接和执行器都在启动时读一次配置，" +
    "现在授权的只是文件；重启它们（tools\\run_feishu.bat / " +
    "python -m freeagent.delegate）之后新白名单才被读进去。");
}"""
