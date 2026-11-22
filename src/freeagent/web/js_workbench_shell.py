"""工作台外壳两件（V1.142 / 借鉴 CLOAK 的 Zen 模式）。

两件都是**外壳层**的事：不碰数据、不碰引擎，只决定「这一刻看得见什么」。

## 安静模式抄什么、不抄什么

CLOAK 官网能力清单写着「Zen 模式与语音：安静界面里语音提问」。这里只抄
「把外壳收掉」那半句：顶部配置表单、左栏工作区、右栏产物一起收起，宽度
还给正文与输入框。**语音不抄** —— 本仓没有录音通道，接进来是新能力面，
不是这一版的范围。

**收掉不等于失明**：顶上留一条窄条，写着「安静模式 · 当前工作区」，带一枚
退出按钮，Esc 也退。不写这一条，人按下去之后不知道自己正在哪个仓库里动手
—— 那是把「安静」做成「失明」。

状态存 localStorage：这是**这台浏览器**的显示偏好，不是服务端配置 —— 换台
机器看同一批工作区，该由人自己再决定一次。存不下就按「没开」，不影响这一
轮能不能发。

## 为什么还要那句「它在后台自己跑」

V1.136 把 pid / 会话 ID / 目录这些内部细节收干净了，副作用是**收过头**：
人不知道关掉这一页之后那一轮还在不在跑。收细节是为了「底座不当主角」，
但「底座在不在跑、我关了它会不会断」恰恰是主角该知道的事 —— 这一句恒显，
并且指向停止口。
"""

from __future__ import annotations

__all__ = ["JS_WORKBENCH_SHELL"]

JS_WORKBENCH_SHELL = r"""let wbZenOn = false;
let wbZenBound = false;

// 后台运行那一句（V1.142）。刻意**不随状态变**：它是事实，不是状态 ——
// 引擎就绪时也这么写，失败时也这么写，人只需要读一次。
function wbRunNote() {
  const box = el("div", "wb-run-note");
  box.id = "wb-run-note";
  box.textContent =
    "它在后台自己跑：关掉这一页不会停，回来还看得到这一轮的进度；" +
    "要它马上停按「停止」；连服务一起停，就关掉那个命令行窗口。";
  return box;
}

// 挂载（V1.142）：一条窄条插在 .wb-view 最前面，一枚按钮挂进顶部表单。
// 由 loadWorkbench 末尾调一次（页面重建后 DOM 是新的，必须重新挂）。
function wbZenBoot() {
  const view = $(".wb-view");
  if (!view) return;
  const strip = el("div", "wb-zen-strip");
  strip.id = "wb-zen-strip";
  view.insertBefore(strip, view.firstChild);
  const bar = $("#wb-bar");
  if (bar) bar.appendChild(wbZenButton());
  try {
    wbZenOn = window.localStorage.getItem("wbZen") === "1";
  } catch (e) {
    // 存不下（隐私模式 / 关了本地存储）就按「没开」：这是显示偏好，
    // 不该因此挡住这一轮能不能发。
    wbZenOn = false;
  }
  if (!wbZenBound) {
    wbZenBound = true;
    // 绑定一次就够：document 上的监听不会随 #root 重建而消失。
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && wbZenOn) wbZenToggle();
    });
  }
  wbZenApply();
}

function wbZenButton() {
  const b = el("button", "btn", "安静");
  b.id = "wb-zen";
  b.onclick = () => wbZenToggle();
  return b;
}

function wbZenToggle() {
  wbZenOn = !wbZenOn;
  try {
    window.localStorage.setItem("wbZen", wbZenOn ? "1" : "0");
  } catch (e) {
    // 同上：存不下不影响这一次的显示。
  }
  wbZenApply();
}

function wbZenApply() {
  const view = $(".wb-view");
  if (!view) return;
  if (wbZenOn) view.classList.add("is-zen"); else view.classList.remove("is-zen");
  const btn = $("#wb-zen");
  if (btn) btn.textContent = wbZenOn ? "退出安静" : "安静";
  const strip = $("#wb-zen-strip");
  if (!strip) return;
  strip.innerHTML = "";
  if (!wbZenOn) return;
  // 收起来之后仍然要知道在哪个工作区 —— 这一句是「安静」能成立的前提。
  const cur = typeof wbCurrentProject === "function" ? wbCurrentProject() : null;
  strip.appendChild(el("span", "wb-zen-where",
    "安静模式 · " + (cur ? esc(cur.name) : "还没选工作区")));
  const out = el("button", "btn", "退出（Esc）");
  out.onclick = () => wbZenToggle();
  strip.appendChild(out);
}
"""
