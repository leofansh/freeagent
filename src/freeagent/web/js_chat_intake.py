"""对话页「派活」按钮：一句话认项目 + 填目录，确认闸门不动（V1.171；V1.173 松开进度词拦截）。

点输入栏「派活」按钮：取当前输入 → 认项目 / brief（POST /api/agent/intake，
花模型一次）→ 写 prefs 的 directory + project_id → brief 填回输入框 →
调现有 chatDelegate()（它弹 确认弹窗 让人看目录 + 档位，再派发）。

为什么不在每句话上自动识别：那会让每条普通对话都白花一次模型请求，且刚修好的
「答非所问」可能被新误判回潮。改成显式按钮触发，只在人真要派活时才花这一次。

V1.173 松开进度词拦截：点派活 = 明确派遣意图，含「了解项目开发进度」这类话
事实在代码仓库、只有引擎能查，照派（判非委派时用原话 + 当前目录直接走
chatDelegate）。含进度词不再被拦；普通对话（回车）仍走 V1.170 的事务层让路。

自动的是「认项目 + 填目录 + 不用翻菜单」，不自动的是确认——确认弹窗
这道闸门完全由 chatDelegate 承担，本模块不绕过、不复制派发逻辑（一处能力一份实现）。
"""
from __future__ import annotations

__all__ = ["JS_CHAT_INTAKE"]

JS_CHAT_INTAKE = r"""
// 对话页「派活」按钮（V1.171）：见模块 docstring。
// 模块级 carry：追问上下文跨视图重绘保留（同 V1.142 教训，绑 DOM 会丢）。
let chatIntakeCarry = null;

async function chatIntakeDispatch() {
  const ta = $("#chatinput");
  const text = (ta ? ta.value : "").trim();
  if (!text) { toast("先写一句要做什么，再点派活"); return; }
  // V1.173：不再按进度词拦截。点派活 = 明确派遣意图——含「了解项目开发进度」
  // 这类话事实在代码仓库、只有引擎能查，照派；是不是委派由人的点击 + 确认闸门定。
  try {
    // carry 非空时不带 text 语义，board 侧按 need 规则消费上一句的回答。
    const res = await post("/api/agent/intake", { text: text, carry: chatIntakeCarry });
    if (res.need) {
      // 追问：上下文留模块级，问题摆给人，答案在下一段输入里（下一句即答案）。
      chatIntakeCarry = res.carry;
      toast("还差点信息：" + (res.question || "请补充") + " — 在输入框回答后再点派活");
      if (ta) { ta.value = ""; ta.focus(); }
      return;
    }
    chatIntakeCarry = null;
    if (res.recognized && res.directory && res.brief) {
      await chatIntakeApply(res);
    } else {
      // V1.173：点派活就是明确意图，intake 判非委派也照派——用原话 + 当前目录
      // 直接走 chatDelegate（确认闸门仍由它弹）。没连模型 / 没目录的情况，
      // chatDelegate 自己会 fail-closed 提示，不在这一层瞎拦。
      await chatDelegate();
    }
  } catch (e) {
    toast("识别失败：" + e.message);
  }
}

// 写 prefs + 把 brief 填回输入框 + 调现有 chatDelegate()。
// 确认闸门在 chatDelegate 的 确认弹窗 里——这里只铺路，不绕闸门。
async function chatIntakeApply(res) {
  let pid = "";
  try {
    const ps = await api("/api/agent/projects");
    const norm = (s) => (s || "").replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
    const hit = (ps.projects || []).find((p) => norm(p.directory) === norm(res.directory));
    if (hit) pid = hit.id || "";
  } catch (e) { /* 认不出 project_id 不挡派活，directory 才是关键 */ }
  const body = { directory: res.directory };
  if (pid) body.project_id = pid;
  try {
    await post("/api/agent/prefs_save", body);
  } catch (e) {
    toast("保存工作目录失败：" + e.message);
    return;
  }
  const ta = $("#chatinput");
  if (ta) { ta.value = res.brief; ta.style.height = "auto"; }
  toast("认出来了：在「" + (res.project || res.directory) + "」里 " + res.brief + "。确认一下就派活。");
  // 全局函数（JS_CHAT_SEARCH 提供），确认 + 派发都由它做，本模块不复制。
  await chatDelegate();
}

function chatIntakeMount(btn) {
  if (!btn) return;
  btn.onclick = () => { chatIntakeDispatch(); };
}
"""
