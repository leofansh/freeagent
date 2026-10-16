"""委派实时面板（设计文档 12.8 / V1.35）：进度 + 授权卡 + 提问。

## 为什么是**轮询**而不是 SSE

``server.py`` 每个响应都写死 ``Content-Length`` 且一次性写 body，
而 ``do_GET`` **全程持 ``app.lock``** —— 长连接持锁等于整个界面卡死
（今天视图、对话、设置页一起）。要 SSE 就得先动这两处，
而 2 秒延迟对「看一眼它在干什么、点一个允许」完全够。
详见设计文档 12.8「传输决策」。

## 为什么不重画整页

每 2 秒 ``replaceChildren`` 一次会把用户正在输入的答复**抹掉**，
光标跳回开头，用户会发现「打了一半的字没了」——
那是本面板唯一真正会伤人的交互事故。

所以：**载荷没变就不动 DOM**（用一份 JSON 签名比对）。
于是「你在打字」期间界面一个字都不动；真有新东西时（卡片来了/答完了）
才重画，并按凭据把草稿与光标位置**还原**回去。

## 与飞书的关系

同一套服务层、同一份落库状态（一份能力一份实现，12.1.1）。
这个面板**不**调 opencode、不发起第二次 SSE 订阅 ——
opencode 的事件流只有执行器一个消费者。
"""

from __future__ import annotations

__all__ = ["JS_DELEGATE_LIVE"]

JS_DELEGATE_LIVE = r"""
// 轮询句柄与签名。**模块级**是有意的：页面只有一个「编程」页签，
// 而计时器必须活过一次 render（render 返回后 root 里才有那个盒子）。
let liveTimer = null;
let liveSig = "";
// credential -> 用户已输入但还没提交的答复。重画时按它还原。
let liveDrafts = {};

function stopLivePoll() {
  if (liveTimer !== null) { clearInterval(liveTimer); liveTimer = null; }
}

// 面板元素还在文档里吗？切走页签时它被 replaceChildren 摘掉了，
// 于是这里自己停 —— 与其依赖「每次切页都记得调 stop」，
// 不如让计时器自己看一眼。漏停的后果是离开页签仍在轮询。
function liveMounted(box) {
  return box && document.body.contains(box);
}

// 渲染一条待决项：授权卡或提问卡。
// can_decide=false 时**不藏**（照 12.7.1「列出来 ≠ 已授权」那条），
// 只把按钮换成一句「去哪儿批」。
function livePendingCard(p, repaint) {
  const box = el("div", "card");
  const isQ = p.kind === "question";
  box.innerHTML = '<div class="row1"><span class="title">' +
    esc(p.subject) + '</span><span class="tag">' +
    (isQ ? "提问" : "要授权") + "</span></div>";

  if (isQ) {
    box.appendChild(el("div", "hint", esc(p.question || p.subject)));
  } else {
    // detail 是服务端精心拼的「动作 / 影响路径 / **diff** / 授权多久」。
    // 刻意**原样**渲染（只转义）：diff 一旦被截断或重新拼装，
    // 用户点的就是「信任」而不是「确认」。
    const d = el("div", "hint");
    d.innerHTML = mdish(p.detail || "");
    box.appendChild(d);
  }

  const row = el("div", "currow");
  if (p.can_decide) {
    if (isQ) {
      const ta = el("textarea", "");
      ta.rows = 2;
      ta.style.width = "100%";
      ta.dataset.cred = p.credential;
      ta.placeholder = "回答它（下一问答完后这里会换成下一个问题）";
      ta.value = liveDrafts[p.credential] || "";
      ta.addEventListener("input", () => {
        liveDrafts[p.credential] = ta.value;
      });
      const send = el("button", "btn primary", "回答");
      send.addEventListener("click", () => {
        const text = (liveDrafts[p.credential] || "").trim();
        if (!text) { toast("答复不能是空的"); return; }
        send.disabled = true;
        api("/api/delegate/questions/" + encodeURIComponent(p.credential), {
          method: "POST",
          body: JSON.stringify({ text: text }),
        }).then((r) => {
          delete liveDrafts[p.credential];
          toast((r && r.message) || "已记下");
          // ⚠️ **必须是无参刷新，不是 repaint(true)**。
          // 真机踩过（V1.35 浏览器实测）：这里照抄了「停止」那支的
          // ``repaint(true)`` —— 而那个参数的含义是「执行器已被叫停，
          // 收摊不再轮询」。症状是**答完第一个问题，整个面板显示
          // 「已停掉这条委派」然后停更**，剩下 N-1 个问题永远没人答。
          //
          // 一次问一个：答完要立刻拿到**下一个问题**（``/api/delegate/live``
          // 会按 spec 的下一个空槽给 question），所以只要刷新。
          repaint();
        }, (e) => { toast("没记上：" + e.message); send.disabled = false; });
      });
      box.appendChild(ta);
      row.appendChild(send);
    } else {
      [["允许", "allow", "primary"], ["拒绝", "deny", ""],
       ["停止这条委派", "stop", ""]].forEach((pair) => {
        const b = el("button", "btn " + pair[2], pair[0]);
        b.addEventListener("click", () => {
          b.disabled = true;
          const url = pair[1] === "stop"
            ? "/api/delegate/approvals/" + encodeURIComponent(p.credential) + "/stop"
            : "/api/delegate/approvals/" + encodeURIComponent(p.credential);
          const body = pair[1] === "stop" ? {} : { decision: pair[1] };
          api(url, { method: "POST", body: JSON.stringify(body) })
            .then((r) => {
              toast((r && r.message) || "已记下");
              // 停掉整条委派后，这一轮**不再等**下一次授权 ——
              // 立刻再拉一次只是让用户看着一张已经作废的卡。
              repaint(pair[1] === "stop");
            }, (e) => { toast("没生效：" + e.message); b.disabled = false; });
        });
        row.appendChild(b);
      });
      box.appendChild(el("div", "hint",
        "只批准<strong>这一个动作</strong>，下一个还会再问。代码不该在你没点过的情况下动。"));
    }
  } else {
    box.appendChild(el("div", "note warn", esc(p.blocked_reason || "这条不该由网页端决定")));
  }
  box.appendChild(row);
  return box;
}

// 服务端 detail 是 Markdown 子集（**加粗**、`代码`、```代码块```）。
// 只渲染这三种，**不做通用 Markdown**：全量实现要么引依赖、
// 要么写一个能出 XSS 的自制解析器 —— 而这块文本的来源是我们自己的 store，
// 它写进去的形状是固定的。
function mdish(text) {
  const fence = text.split("```");
  if (fence.length > 1) {
    // 有代码块：奇数段是代码，偶数段是散文。
    return fence.map((part, i) => i % 2
      ? "<pre class=" + '"log"' + ">" + esc(part.replace(/^[^\n]*\n/, "")) + "</pre>"
      : mdish(part)).join("");
  }
  return esc(text)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
}

// 一条正在跑的委派。
function liveItemView(it, repaint) {
  const box = el("div", "card");
  const stale = it.stale;
  box.innerHTML = '<div class="row1"><span class="title">' +
    esc(it.title || it.task_id) + '</span><span class="tag">' +
    esc(it.status || "…") + "</span></div>";
  box.appendChild(el("div", "hint",
    "项目：" + esc(it.project || "（未知）") +
    "　工具调用 <strong>" + it.tool_calls + "</strong> 次（允许 " +
    it.allowed + " 次）"));
  if (it.recent) box.appendChild(el("div", "hint", "最近：" + esc(it.recent)));
  if (stale) {
    // 「疑似卡住」**只改显示，不下结论**：执行器每 5 秒写一次，
    // 而 opencode 一次工具调用可能静默更久。写成「已卡住」会让人去重启
    // 一个正在正常跑的东西。
    //
    // ⚠️ 这里**只建一次**，之后每轮轮询由 tickLiveAges() 原地改它的文字。
    // 早先的版本把「已经 N 秒没动静」参与重绘判定，于是 age 每 2 秒变一次 →
    // 每 2 秒重建整棵 DOM → **按钮每 2 秒被换掉**：点击落在已分离的节点上，
    // 症状是「点允许偶尔没反应」，而它只在真人手慢时出现，测试抓不到。
    const note = el("div", "note warn");
    note.dataset.ageFor = it.updated_at;
    box.appendChild(note);
  }
  (it.pending || []).forEach((p) => box.appendChild(livePendingCard(p, repaint)));
  return box;
}
"""

