"""工作台右栏：这个工作区有什么（V1.142 / 借鉴 CLOAK 的「文件管理」）。

## 补的是哪个缺口

产物与变更（V1.132 / V1.135）只说「这一轮碰了什么」，不说「这个工作区里
本来有什么」—— 想挑一个文件来改，得先去资源管理器里翻一遍再回来。CLOAK 把
文件管理摆在顶栏，这里跟着补一块只读清单。

## 三条纪律

1. **只列名字、大小**，不预览内容：预览要判编码、要挡超大文件与二进制，
   是另一件事，混进这一版就成了半成品。
2. **越界不可能**：前端只送 ``project`` + 工作区内的相对路径，根由服务端从
   档案取；界面里没有「手输路径」的入口（有的话根就白收了）。
3. **四种情况各说各话**：没选工作区 / 目录被删 / 读不出来 / 真的是空的 ——
   把前三种合成「这里是空的」就是把故障当结果。

## 为什么右栏拆成两块

``wbSidePaint`` 每一轮都整块清空重画。文件清单不随轮次变，混在同一块里会
被反复抹掉再重读 —— 所以上层 ``#wb-side-turn`` 归产物，下层 ``#wb-files``
归这个工作区，互不清场。
"""

from __future__ import annotations

__all__ = ["JS_WORKBENCH_FILES"]

JS_WORKBENCH_FILES = r"""let wbFilesPath = "";

// 右栏挂载（V1.142）：由页面装配时调一次，往 .wb-col-side 里放两块。
function wbSideMount(side) {
  const turn = el("div");
  turn.id = "wb-side-turn";
  side.appendChild(turn);
  const files = el("div");
  files.id = "wb-files";
  side.appendChild(files);
}

function wbFilesOpen(rel) {
  wbFilesPaint(rel);
}

function wbFileSize(n) {
  // 读不到大小（stat 失败）与 0 字节不是一回事，分开说。
  if (n == null) return "大小读不到";
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  return (n / 1048576).toFixed(1) + " MB";
}

// ``at`` 是「要画哪一层」：调用点说清 —— 切 / 建 / 删工作区传空（回根），
// 「进去」「上一层」传目标相对路径。**不在这里重置 wbFilesPath**：早期版本把它
// 写在本函数开头，``wbFilesOpen`` 刚设好的路径当场被抹掉，点目录永远原地不动
// （走查第一遍就是这么发现的）。
async function wbFilesPaint(at) {
  const box = $("#wb-files");
  if (!box) return;
  wbFilesPath = at || "";
  box.innerHTML = "";
  box.appendChild(el("div", "wb-label", "这个工作区有什么"));
  const tip = el("div", "note");
  box.appendChild(tip);
  const proj = typeof wbProjectId === "string" ? wbProjectId : "";
  const dir = typeof wbProjectDir === "string" ? wbProjectDir : "";
  if (!proj && !dir) {
    tip.textContent = "还没选工作区 —— 选中之后这里会列出它目录里有什么。";
    return;
  }
  tip.textContent = "读目录…";
  let data;
  try {
    data = await api("/api/agent/files?project=" + encodeURIComponent(proj) +
      "&path=" + encodeURIComponent(wbFilesPath || ""));
  } catch (e) {
    if (box.isConnected) tip.textContent = "读不出来：" + e.message;
    return;
  }
  // 等待期间页面可能已经被重建（切视图 / 重进工作台）：那时的 box 已断开，
  // 往里写就是把内容写进一个没人看的地方。
  if (!box.isConnected) return;
  if (data.state === "no_dir" || data.state === "gone") {
    tip.textContent = data.reason || "读不到这个目录。";
    return;
  }
  tip.textContent = data.path ? ("位置：" + data.path) : ("根：" + data.root);
  if (data.path) {
    const up = el("button", "chip", "上一层");
    up.onclick = () => wbFilesOpen(data.parent || "");
    box.appendChild(up);
  }
  const rows = data.entries || [];
  if (!rows.length) {
    box.appendChild(el("div", "note", "这个目录是空的。"));
  }
  rows.forEach((f) => {
    const row = el("div", "note wb-file-row");
    // 名字来自磁盘，必须 esc —— 文件名里可以带尖括号（V1.117 的坑）。
    row.appendChild(el("span", "wb-file-path", esc(f.name + (f.dir ? "/" : ""))));
    if (f.dir) {
      row.appendChild(el("span", "wb-file-count", "目录"));
      const go = el("button", "chip", "进去");
      go.onclick = () => wbFilesOpen(f.rel);
      row.appendChild(go);
    } else {
      row.appendChild(el("span", "wb-file-count", wbFileSize(f.size)));
      const loc = el("button", "chip wb-locate", "定位");
      loc.dataset.path = f.abs;
      loc.onclick = () => wbReveal(loc.dataset.path);
      row.appendChild(loc);
    }
    box.appendChild(row);
  });
  if (data.truncated) {
    box.appendChild(el("div", "note",
      "只列了前 " + rows.length + " 项（这里一共 " + data.total +
      " 项）—— 再往下得进子目录。"));
  }
  if (data.skipped_links) {
    box.appendChild(el("div", "note",
      "另有 " + data.skipped_links + " 项是链接（junction / 符号链接），没列" +
      " —— 它们会指到工作区外面去。"));
  }
}
"""
