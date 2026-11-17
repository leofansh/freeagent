"""工作台前端（二）：授权卡与发送（设计文档 21.4 / V1.114）。

## 为什么卡与发送要单独一个文件

这两段是**同一个决定的两半**：一张卡怎么问（`wbAsk`）、答复怎么回去
（`wbDecide`）、以及没有卡的时候一句话怎么发出去（`wbSend`）。三者共用
``wbBusy`` 这一个「上一次点击还没回来」的锁，拆开写才不会各自装一把锁。

## 一张卡只有两种

- 授权（``kind !== "question"``）：两个按钮，允许 / 拒绝。**没有「永远允许」**
  —— 覆盖层是按目录写的，凭空一个 always 会让「换个目录不该继承」这条失效。
- 提问：一个输入框 + 答复。不答等于停 —— 让引擎空等是在浪费它的额度。
"""

from __future__ import annotations

__all__ = ["JS_WORKBENCH_CARD"]

JS_WORKBENCH_CARD = r"""function wbAsk(turn) {
  const box = el("div", "card");
  box.appendChild(el("div", "row1", "等你决定：" + esc(turn.ask.label || "")));
  if (turn.ask.detail) box.appendChild(el("div", "note", esc(turn.ask.detail)));

  const bar = el("div", "row1");
  if (turn.ask.kind === "question") {
    const input = el("input");
    input.id = "wb-answer";
    input.placeholder = "你的回答";
    input.onkeydown = (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        wbDecide(turn.id, true, (input.value || "").trim());
      }
    };
    bar.appendChild(input);
    const send = el("button", "btn primary", "答复");
    send.onclick = () => wbDecide(turn.id, true, (input.value || "").trim());
    bar.appendChild(send);
    const skip = el("button", "btn danger", "不答，停掉");
    skip.onclick = () => wbDecide(turn.id, false, "");
    bar.appendChild(skip);
  } else {
    const ok = el("button", "btn primary", "允许这一次");
    ok.onclick = () => wbDecide(turn.id, true, "");
    bar.appendChild(ok);
    const no = el("button", "btn danger", "拒绝");
    no.onclick = () => wbDecide(turn.id, false, "");
    bar.appendChild(no);
    box.appendChild(el("div", "note",
      "只允许这一次。没有「永远允许」—— 换目录不会继承任何授权。"));
  }
  box.appendChild(bar);
  return box;
}

async function wbDecide(id, allow, answer) {
  if (wbBusy) return;
  wbBusy = true;
  try {
    await post("/api/agent/decide", { id: id, allow: allow, answer: answer });
  } catch (e) {
    toast(e.message);
  } finally {
    wbBusy = false;
  }
  wbArm(100);
}

async function wbSend(force) {
  if (wbBusy) return;
  const dir = ($("#wb-dir") || {}).value || "";
  const text = ($("#wb-text") || {}).value || "";
  const model = ($("#wb-model") || {}).value || "";
  const mode = ($("#wb-mode") || {}).value || "ask";
  const injectBox = $("#wb-inject");
  const inject = injectBox ? injectBox.checked : true;
  if (!text.trim()) { toast("先写一句话"); return; }
  if (!dir.trim()) { toast("先选项目（本地文件夹）"); return; }
  wbBusy = true;
  let res;
  try {
    res = await post("/api/agent/start", {
      text: text, directory: dir.trim(), model: model.trim(),
      mode: mode, force: !!force, inject: !!inject,
    });
  } catch (e) {
    toast(e.message);
    wbBusy = false;
    return;
  } finally { wbBusy = false; }

  const route = $("#wb-route");
  if (route) {
    route.innerHTML = "";
    const d = res.decision || {};
    route.appendChild(el("div", "note",
      esc((res.routed ? "已交给引擎：" : "没交给引擎：") + (d.reason || ""))));
    if (!res.routed) {
      // 规则错了人应当能当场推翻 —— 这才是它敢「宁可漏判」的底气。
      const b = el("button", "btn", "仍交给引擎执行");
      b.onclick = () => wbSend(true);
      route.appendChild(b);
    }
  }
  if (res.routed) { $("#wb-text").value = ""; }
  // 发新的一句话 = 回到「现在」：还留着历史标记的话，画面上会挂一条
  // 「正在看历史」，而人刚才是要它动手。
  wbHistId = "";
  wbPaint({ turn: res.turn });
  if (res.turn && (res.turn.status === "running" || res.turn.status === "waiting")) {
    wbArm(500);
  }
}
"""
