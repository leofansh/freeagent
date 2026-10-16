"""委派实时面板的**轮询那一半**（设计文档 12.8 / V1.35）。

## 为什么从 :mod:`js_delegate_live` 拆出来

那个文件画卡（授权卡 / 提问卡 / 进度条），这个管**什么时候画**。
合在一起会顶破 250 行那道守卫 —— 而那个守卫存在的原因很实在：
单页界面天生是一份文档，拆得太细没人能一眼看完，拆得太粗没人敢改。

**跨文件调用没问题**：:mod:`page` 把所有 JS 块拼进**同一个** ``<script>``，
函数声明会提升、作用域是同一个（``js_oc_cards`` 与 ``js_oc_selection``
早就是这种关系）。

## 这里最要紧的一条：**不重建 DOM**

载荷没变就不动 DOM（用一份签名比对）。这不只是省事：

* **点击不会被吞。** 早先把「已经 N 秒没动静」算进签名，于是 age 每 2 秒变一次
  → 每 2 秒 ``replaceChildren`` 一次 → **按钮每 2 秒被换掉**。真人手慢时
  点击落在已分离的节点上，症状是「点允许偶尔没反应」—— 而它在
  自动化测试里抓不到（测试的点击比人快 20 倍）。
* **打字不会被抹掉。** 提问框里打了半句话，界面一重画就没了。

所以年龄数字由 :func:`tickLiveAges` **原地改一个文本节点**，
它不进签名（``age_seconds`` 每轮都变，是快照时刻的产物）。

## 为什么失败就**不装下一次**

一轮读不到就停。多半是服务正在重启 —— 而每 2 秒重试一次只会把日志刷满，
而用户切一次页签自然就重来了。
"""

from __future__ import annotations

__all__ = ["JS_DELEGATE_LIVE_POLL"]

JS_DELEGATE_LIVE_POLL = r"""
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
      + "（可能只是这一步慢；要停就点上面那条委派的「停止这条委派」）";
  });
}

// 面板本体。返回 true 表示「面板已不在页面上」，调用方据此收摊。
function paintLivePanel(box, data, repaint) {
  if (!liveMounted(box)) return true;
  const items = data.items || [];
  // **签名刻意不含 age_seconds**（见模块说明）。
  const sig = JSON.stringify(items.map((it) => [
    it.task_id, it.status, it.recent, it.tool_calls, it.allowed,
    it.updated_at, it.stale, it.title, it.state,
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

// 拉一次 + 装下一次。间隔由**服务端**给（12.8：poll_seconds=0 就是不轮询）。
function pollLive(box, repaint) {
  api("/api/delegate/live").then((d) => {
    if (paintLivePanel(box, d, repaint)) { stopLivePoll(); return; }
    stopLivePoll();
    const secs = d.poll_seconds || 0;
    if (secs <= 0) return;              // 服务端说别轮询（脚本化/低配）
    liveTimer = setInterval(() => {
      if (!liveMounted(box)) { stopLivePoll(); return; }
      pollLive(box, repaint);
    }, Math.round(secs * 1000));
  }, () => {
    // 读不到就**不装下一次**：多半是服务正在重启（见模块说明）。
  });
}

// 挂载。root 是编程页签的容器（切页签时会被 replaceChildren 换掉）。
function mountLivePanel(root) {
  const box = el("div", "");
  box.id = "livepanel";
  box.appendChild(el("div", "loading", "正在看有没有在跑的委派…"));
  root.appendChild(box);
  liveSig = "";                          // 换页签重来一遍
  const repaint = (stopped) => {
    // stop=true 时直接收摊：执行器已经被叫停，再拉一次只会显示一张作废的卡。
    if (stopped) {
      stopLivePoll();
      box.replaceChildren(el("div", "note",
        "已停掉这条委派 —— 页面不再轮询。已完成的改动仍然有效，剩下的没做。"));
      return;
    }
    pollLive(box, repaint);
  };
  pollLive(box, repaint);
  return box;
}
"""