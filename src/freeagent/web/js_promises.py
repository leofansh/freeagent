"""Web「承诺」视图：我欠谁的（未兑现承诺列表 + 直接结账）。

为什么独立成文件：``js_detail.py`` 已顶到 250 行上限（``TestWebPackageStaysSmall``
守着那条线）。列表数据来自只读 ``/api/promises``，结账走
``POST /api/promise/<id>/<action>`` —— **不自算任何业务判断**（哪些逾期、
能不能结，全是服务层说了算，12.1.1「一份能力一份实现」）。
"""

from __future__ import annotations

__all__ = ["JS_PROMISES"]

JS_PROMISES = r"""async function renderPromises(root) {
  root.innerHTML = "";
  let data;
  try {
    data = await api("/api/promises");
  } catch (e) {
    root.innerHTML = '<div class="note">承诺读不出来：' + esc(e.message) + "</div>";
    return;
  }
  if (!data.count) {
    root.appendChild(el("div", "empty", "没有未兑现的承诺。记一条：/promise 记 <事务id> <承诺> 关系=名字。"));
    return;
  }
  root.appendChild(el("h2", null, "我欠谁的（未兑现承诺 · " + data.count + " 条）"));
  // 只摆事实：逾期标「已逾期」，不写「违约」—— 判违约是用户的事（11.19.5）。
  data.items.forEach((p) => {
    const card = el("div", "card");
    let head = esc(p.statement);
    if (p.contact_name) head += " · " + esc(p.contact_name);
    if (p.promised_for) {
      head += " · 期限 " + p.promised_for.slice(0, 16).replace("T", " ");
      if (p.overdue) head += " · 已逾期";
    }
    card.appendChild(el("div", "row1", el("span", "title", head)));
    const acts = el("div", "acts");
    acts.appendChild(el("span", "lbl", "结账"));
    const btn = (label, action, primary) => {
      const b = el("button", "btn" + (primary ? " primary" : ""), label);
      b.onclick = () => settle(p.id, action, b);
      return b;
    };
    acts.appendChild(btn("兑现", "keep", true));
    acts.appendChild(btn("破了", "break", false));
    acts.appendChild(btn("收回", "withdraw", false));
    card.appendChild(acts);
    root.appendChild(card);
  });
  $("#foot").textContent = "结账只在你欠对方这一侧生效；逾期只标事实，不判违约。";
}

async function settle(id, action, btn) {
  btn.disabled = true;
  try {
    await post("/api/promise/" + id + "/" + action, {});
    toast("已结账");
    renderPromises($("#root"));
  } catch (e) {
    btn.disabled = false;
    toast("结账失败：" + e.message);
  }
}"""
