"""委派实时面板的**轮询那一半**（设计文档 12.8 / V1.35 / V1.106）。

## 为什么从 :mod:`js_delegate_live` 拆出来

那个文件画卡（授权卡 / 提问卡 / 进度条），这个管**什么时候画**。合在一起会顶破
250 行那道守卫。**跨文件调用没问题**：:mod:`page` 把所有 JS 块拼进**同一个**
``<script>``，函数声明会提升、作用域是同一个。

## 这里最要紧的一条：**不重建 DOM**

载荷没变就不动 DOM（签名比对）。这不只是省事：早先把「已经 N 秒没动静」算进
签名，于是 age 每 2 秒变一次 → 每 2 秒 ``replaceChildren`` 一次 → **按钮每 2 秒
被换掉**，真人手慢时点击落在已分离的节点上，症状是「点允许偶尔没反应」，
而自动化测试抓不到（测试的点击比人快 20 倍）；提问框里打了一半的话也会被抹掉。

所以年龄数字由 :func:`tickLiveAges` **原地改一个文本节点**，它不进签名
（``age_seconds`` 每轮都变，是快照时刻的产物）。

## 失败**不能就此不装下一次**（V1.106 修）

旧实现是「一轮读不到就停」：理由是多半服务在重启，而每 2 秒重试只会刷日志，
于是留了一句兜底 ——「用户切一次页签自然就重来了」。那个兜底**恰好要求人先
离开再去**，而正盯着面板等授权的人不会切页签：

    一轮读失败（服务重启 / 笔记本睡眠醒来 / localhost 抖一下）
    → 计时器不再装 → 面板停在旧快照
    → **一张仍然有效、仍然等你点的授权卡，在界面上看起来像没有了**

这正是 lenses L2「闸门是状态不是约定」的形状：授权一直在库里，界面说没有。
而 Web 的授权卡只有这一处在画（``js_delegate_live`` 的卡由它驱动）。

新处置三条，缺一条都不够：

1. **有限退避**（5s / 10s / 30s）而不是一直重试 —— 服务没起来时无限重试等于
   刷日志，而本地服务「重启」通常几秒就回。
2. **退避用完就停，但必须说出来**：面板上留一句「不再自动重试」+ 一个「重试」
   按钮，并明说上面那张是最后一次读到的样子、可能已经过期。悄悄停掉比报错更坏
   —— 人会把过期快照当成现状。
3. **回到前台立刻探一次**（``visibilitychange`` / ``pageshow``）：后台页签的计时器
   会被节流到分钟级，而「离开时还在跑、回来才有卡」正是最该看一眼的时刻。
   这条判断抄自 OpenHands Agent Canvas 的 ``usePageVisibilityReconnect`` ——
   它还点破另一件事：冻结的计时器回到前台会**补发一串陈旧重试**。

⚠️ 别改回「失败即停」，也别改成无限重试：前者静默丢闸门，后者刷日志。
"""

from __future__ import annotations

__all__ = ["JS_DELEGATE_LIVE_POLL"]

JS_DELEGATE_LIVE_POLL = r"""
// 退避档位（秒）。**有界**：用完就不再自动试，改成面板上说清楚（见模块说明 1、2）。
const LIVE_RETRY_SECS = [5, 10, 30];
// 服务端给的轮询间隔。``null`` = 还不知道（首轮就失败也要退避重试）；
// ``0`` = 服务端明确说别轮询（12.8 的脚本化/低配）。两者不能混 ——
// 混了就会在「别轮询」的部署上被一次回前台唤醒打出一个请求。
let liveSecs = null;
let liveRetryIdx = 0;
// 最近一次挂载的面板与重画回调。回前台要探的是**当前**这一份，
// 所以每次 mountLivePanel 都覆盖它，而不是把它闭进监听里。
let liveBox = null;
let liveRepaint = null;
let liveVisibilityBound = false;

// 把「已经 N 秒没动静」原地刷新。**不重建 DOM** —— 见上面那条注释。
//
// 为什么用浏览器的 Date.now() 而不用服务端给的 age_seconds：
// age_seconds 是**快照时刻**算出来的，下一轮它会跳 2 秒；而 Date.now()
// 每轮都是真的，于是文字与用户心里的「多久了」一致。阈值仍取服务端那份
// stale 判定（那是唯一权威），这里只负责显示。
function tickLiveAges(box) {
  box.querySelectorAll("[data-age-for]").forEach((note) => {
    const at = Date.parse(note.dataset.ageFor || "");
    if (isNaN(at)) return;
    const secs = Math.max(0, Math.round((Date.now() - at) / 1000));
    note.textContent = "已经 " + secs + " 秒没动静了 —— 疑似卡住。"
      + "（可能只是这一步慢；要停就点上面那条委派的「停止这条委派」）"
      // 12.11.1：陈旧行附带出路文案（指向下面的「重新派发 / 放弃」按钮）。
      // 服务端整句给好，这里**只拼不解读** —— 再拆一次就成两份实现，
      // 漂移了就得改两处。
      + (note.dataset.outlet ? " " + note.dataset.outlet : "");
  });
}

// 面板上那句「读不到了」。**单开一个节点、不进签名**：进了签名，它每轮都会
// 被 replaceChildren 冲掉，而它恰恰是要一直挂着的。位置固定在面板最上面 ——
// 授权卡就在下面，得先看见「这卡可能过期」再看见卡本身。
function clearLiveError(box) {
  const n = box.querySelector("[data-live-error]");
  if (n) n.remove();
}

function showLiveError(box, text, onRetry) {
  let n = box.querySelector("[data-live-error]");
  if (!n) {
    n = el("div", "note");
    n.dataset.liveError = "1";
    box.insertBefore(n, box.firstChild);
  }
  n.textContent = text;
  if (onRetry) {
    const b = el("button", "chip", "重试");
    b.type = "button";
    b.addEventListener("click", onRetry);
    n.appendChild(document.createTextNode(" "));
    n.appendChild(b);
  }
}

// 面板本体。返回 true 表示「面板已不在页面上」，调用方据此收摊。
function paintLivePanel(box, data, repaint) {
  if (!liveMounted(box)) return true;
  const items = data.items || [];
  // **签名刻意不含 age_seconds**（见模块说明）。
  const sig = JSON.stringify(items.map((it) => [
    it.task_id, it.status, it.recent, it.tool_calls, it.allowed,
    it.updated_at, it.stale, it.title, it.state, it.outlet || "",
    (it.actions || []).join(","),
    (it.pending || []).map((p) => [
      p.credential, p.kind, p.subject, p.detail, p.question,
      p.can_decide, p.blocked_reason, p.answered_count, p.task_id,
    ]),
  ]));
  if (sig !== liveSig) {
    liveSig = sig;
    const frag = document.createDocumentFragment();
    if (!items.length) {
      frag.appendChild(el("div", "note",
        "现在没有在跑的委派，也没有等你批的东西。" +
        "上面建好并「开始做」之后，这里会实时显示进度与授权卡。"));
    } else {
      frag.appendChild(el("div", "title", "正在跑 / 等你决定"));
      items.forEach((it) => frag.appendChild(liveItemView(it, repaint)));
    }
    box.replaceChildren(frag);

    // 重画会打断正在输入的那一条：把草稿与焦点**放回去**。
    const act = document.activeElement;
    const cred = act && act.dataset ? act.dataset.cred : null;
    if (cred) {
      const ta = box.querySelector('[data-cred="' + cred + '"]');
      if (ta) {
        ta.value = liveDrafts[cred] || "";
        ta.focus();
        const end = ta.value.length;
        ta.setSelectionRange(end, end);
      }
    }
  }
  tickLiveAges(box);
  return false;
}

// 自己装下一次。``setTimeout`` 而不是 ``setInterval``：**同一条链**，
// 于是不可能「上一轮还没回来、下一轮又发出去」。重叠在慢机器上不只是
// 白发请求 —— 两次响应会各自再装一次计时器，旧的被清掉，你以为还在跑。
function armLive(box, repaint, secs) {
  stopLivePoll();
  liveTimer = setTimeout(() => { pollLive(box, repaint); },
    Math.round(secs * 1000));
}

// 拉一次 + 装下一次。间隔由**服务端**给（12.8：poll_seconds=0 就是不轮询）。
function pollLive(box, repaint) {
  if (!liveMounted(box)) { stopLivePoll(); return; }
  api("/api/delegate/live").then((d) => {
    liveRetryIdx = 0;                    // 读到了：退避档位清零
    clearLiveError(box);                 // 签名没变时不会重画，得显式摘掉
    if (paintLivePanel(box, d, repaint)) { stopLivePoll(); return; }
    liveSecs = d.poll_seconds || 0;
    if (liveSecs <= 0) { stopLivePoll(); return; }
    armLive(box, repaint, liveSecs);
  }, () => {
    // 读不到：退避重试（模块说明 1），不再「就此不装下一次」。
    stopLivePoll();
    if (!liveMounted(box)) return;
    // 年龄照墙上时间刷新：读不到不等于时间停了，冻住数字同样是谎报。
    tickLiveAges(box);
    if (liveRetryIdx < LIVE_RETRY_SECS.length) {
      const wait = LIVE_RETRY_SECS[liveRetryIdx];
      liveRetryIdx += 1;
      showLiveError(box, "这一轮没读到面板，" + wait + " 秒后自动重试（第 " +
        liveRetryIdx + "/" + LIVE_RETRY_SECS.length + " 次）。", null);
      armLive(box, repaint, wait);
      return;
    }
    showLiveError(box,
      "已经连续 " + LIVE_RETRY_SECS.length + " 次读不到面板，不再自动重试" +
      "（多半是服务没起来）。下面那张是「最后一次读到的样子」，可能已经过期 ——" +
      // 这一段是 textContent，**不解析 markdown**：写成 ``**…**`` 会原样印出
      // 两个星号（V1.92 在飞书卡上刚踩过同一个坑）。
      "点「重试」再读一次；要批的卡也可以在飞书那边批（同一份数据）。",
      () => { liveRetryIdx = 0; liveSig = ""; pollLive(box, repaint); });
  });
}

// 回到前台：立刻探一次，不等当前那一轮到点。
function liveWake() {
  if (liveSecs === 0) return;            // 不许轮询（服务端说的 / 人已叫停）
  if (!liveBox || !liveMounted(liveBox)) return;
  liveRetryIdx = 0;
  stopLivePoll();
  pollLive(liveBox, liveRepaint);
}

// 监听只买一次：切页签会反复调 mountLivePanel，而 DOM 监听**不会**随盒子被
// 摘掉而失效 —— 每挂载一次加一个的话，回来一趟就是 N 个并发请求。
function bindLiveVisibility() {
  if (liveVisibilityBound) return;
  liveVisibilityBound = true;
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") liveWake();
  });
  // 从 bfcache 回来不触发 visibilitychange（快照恢复时状态一直是 visible）。
  window.addEventListener("pageshow", (ev) => { if (ev.persisted) liveWake(); });
}

// 挂载。root 是编程页签的容器（切页签时会被 replaceChildren 换掉）。
function mountLivePanel(root) {
  const box = el("div", "");
  box.id = "livepanel";
  box.appendChild(el("div", "loading", "正在看有没有在跑的委派…"));
  root.appendChild(box);
  liveSig = "";                          // 换页签重来一遍
  liveRetryIdx = 0;                      // 人工重进页签 = 重新给满退避
  liveBox = box;
  const repaint = (stopped) => {
    // stop=true 时直接收摊：执行器已经被叫停，再拉一次只会显示一张作废的卡。
    // ``liveSecs = 0`` 与「服务端说 poll_seconds=0」是同一条判据的两个来源：
    // 都是「这块面板不该再轮询」。少了它，回一次前台就会被 liveWake 唤醒，
    // 而界面上刚写完「页面不再轮询」—— 说了不做就是谎报。
    if (stopped) {
      liveSecs = 0;
      stopLivePoll();
      box.replaceChildren(el("div", "note",
        "已停掉这条委派 —— 页面不再轮询。已完成的改动仍然有效，剩下的没做。"));
      return;
    }
    pollLive(box, repaint);
  };
  liveRepaint = repaint;
  bindLiveVisibility();
  pollLive(box, repaint);
  return box;
}
"""
