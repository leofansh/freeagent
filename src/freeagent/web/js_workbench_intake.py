"""工作台前端：一句话起手（V1.141）。

一句话认出「改哪个项目 + 做什么」，预填 #wb-dir / #wb-text，人确认后点
既有的「交给它」。拿不准就追问，下一句当答案（确定规则，见 delegate_intake）。
动态文本一律 textContent / esc()，不进 innerHTML。
"""

from __future__ import annotations

__all__ = ["JS_WORKBENCH_INTAKE"]

JS_WORKBENCH_INTAKE = r"""let wbIntakeCarry = null;
// 探测结果留在模块级：工作台重绘会整块换掉卡片，状态跟着 DOM 走会表现成
// 「每次重绘都回到正在检查」（V1.142 文件清单踩过同一个坑）。
let wbIntakeReady = null;

function wbIntakeApply(res) {
  const state = $("#wb-intake-state");
  const input = $("#wb-intake-text");
  const go = $("#wb-intake-go");
  if (!state || !input || !go) return;
  if (!res) { state.textContent = "正在检查前提…"; return; }
  if (res.ready) {
    state.hidden = true;
    input.disabled = false;
    go.disabled = false;
    return;
  }
  state.textContent = res.question || "这句话起手还没准备好。";
  const jump = el("button", "btn", res.reason === "no_model" ? "去设置" : "去授权项目");
  jump.onclick = () => load(res.reason === "no_model" ? "settings" : "feishu");
  state.appendChild(jump);
}

function wbIntakeMount() {
  const box = el("div", "card");
  box.id = "wb-intake";
  box.appendChild(el("div", "row1", "一句话起手（自动认出项目，再确认）"));
  // 前提摆在输入之前（V1.147）：没授权项目 / 没连模型时先说清楚并给入口，
  // 不让人白输一句才被拒。默认禁用，探测到就绪再放开。
  const state = el("div", "hint");
  state.id = "wb-intake-state";
  state.textContent = "正在检查前提…";
  box.appendChild(state);
  const input = el("input");
  input.id = "wb-intake-text";
  input.placeholder = "例如：让它改 X 项目 把登录页按钮改成蓝色";
  input.disabled = true;
  input.onkeydown = (e) => {
    if (e.key === "Enter") { e.preventDefault(); wbIntake(); }
  };
  box.appendChild(input);
  const go = el("button", "btn", "识别");
  go.id = "wb-intake-go";
  go.disabled = true;
  go.onclick = () => wbIntake();
  box.appendChild(go);
  const result = el("div");
  result.id = "wb-intake-result";
  box.appendChild(result);
  wbIntakeApply(wbIntakeReady);
  if (!wbIntakeReady) wbIntakeProbe();
  return box;
}

async function wbIntakeProbe() {
  let res;
  try {
    res = await api("/api/agent/intake/ready");
  } catch (e) {
    // 探测失败不能让卡片假装可用：保守禁用，说清原因（fail-closed）。
    res = { ready: false, reason: "probe_error",
            question: "前提检查失败：" + e.message + " 请刷新页面重试。" };
  }
  wbIntakeReady = res;
  wbIntakeApply(res);
}

async function wbIntake(preset) {
  const field = $("#wb-intake-text");
  const text = (preset !== undefined ? preset : (field ? field.value : "")).trim();
  if (!text && !(wbIntakeCarry && wbIntakeCarry.need)) { toast("先说一句要做什么"); return; }
  const result = $("#wb-intake-result");
  if (!result) return;
  result.textContent = "正在认…";
  let res;
  try {
    res = await post("/api/agent/intake", { text: text, carry: wbIntakeCarry || {} });
  } catch (e) {
    result.textContent = "没认出来：" + e.message;
    return;
  }
  if (res.need) {
    // 追问：摆出问题 + 一个专门回答它的输入框；下一句原样当答案送回。
    wbIntakeCarry = res.carry;
    result.innerHTML = "";
    result.appendChild(el("div", "note", res.question || "还差一点信息。"));
    const ans = el("input");
    ans.id = "wb-intake-answer";
    ans.placeholder = res.need === "project" ? "项目名" : "要做什么";
    ans.onkeydown = (e) => {
      if (e.key === "Enter") { e.preventDefault(); wbIntake((ans.value || "").trim()); }
    };
    result.appendChild(ans);
    const reply = el("button", "btn", "回答");
    reply.onclick = () => wbIntake((ans.value || "").trim());
    result.appendChild(reply);
    return;
  }
  wbIntakeCarry = null;
  if (res.recognized && res.directory && res.brief) {
    const dir = $("#wb-dir"); if (dir) dir.value = res.directory;
    const ta = $("#wb-text"); if (ta) ta.value = res.brief;
    const pbtn = $("#wb-project-btn");
    if (pbtn && res.project) pbtn.textContent = "项目：" + res.project;
    result.textContent = "认出来了：在「" + res.project + "」里 " + res.brief +
      "。已填好，看一眼再点「交给它」。";
  } else {
    // 认不出 / 没连模型 / 没授权项目：如实说原因，绝不替人预填或起一轮。
    result.textContent = res.question || "没认出来，请手动选项目并写要做的事。";
  }
}
"""
