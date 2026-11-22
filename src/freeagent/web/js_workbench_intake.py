"""工作台前端：一句话起手（V1.141）。

一句话认出「改哪个项目 + 做什么」，预填 #wb-dir / #wb-text，人确认后点
既有的「交给它」。拿不准就追问，下一句当答案（确定规则，见 delegate_intake）。
动态文本一律 textContent / esc()，不进 innerHTML。
"""

from __future__ import annotations

__all__ = ["JS_WORKBENCH_INTAKE"]

JS_WORKBENCH_INTAKE = r"""let wbIntakeCarry = null;

function wbIntakeMount() {
  const box = el("div", "card");
  box.id = "wb-intake";
  box.appendChild(el("div", "row1", "一句话起手（自动认出项目，再确认）"));
  const input = el("input");
  input.id = "wb-intake-text";
  input.placeholder = "例如：让它改 X 项目 把登录页按钮改成蓝色";
  input.onkeydown = (e) => {
    if (e.key === "Enter") { e.preventDefault(); wbIntake(); }
  };
  box.appendChild(input);
  const go = el("button", "btn", "识别");
  go.id = "wb-intake-go";
  go.onclick = () => wbIntake();
  box.appendChild(go);
  const result = el("div");
  result.id = "wb-intake-result";
  box.appendChild(result);
  return box;
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
