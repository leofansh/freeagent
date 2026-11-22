"""Web「周回顾」视图（设计文档 6.5 / V1.133）。

为什么独立成文件：``js_boot.py`` 已接近 250 行上限，而这一面有九个分区
（六个事务分区 + V1.71 成长目标 + V1.133 拍板 / 承诺 / 轮数），塞进去会顶破。
渲染只调 ``/api/week``，**不自算任何业务判断** —— 周界、顺延、拍板与承诺的
过滤全在 :class:`~freeagent.services.week.WeekService` 里算完（12.1.1）。

呈现纪律与 CLI ``render_week`` 完全一致：只摆事实、不排序打分、不给建议。
所有动态文本都过 ``esc()`` 再进 ``el()`` 的 innerHTML（V1.117 的教训）。
"""

from __future__ import annotations

__all__ = ["JS_WEEK"]

JS_WEEK = r"""async function renderWeek(root) {
  root.innerHTML = "";
  let w;
  try {
    w = await api("/api/week");
  } catch (e) {
    root.appendChild(el("div", "note", "周回顾读不出来：" + esc(e.message)));
    return;
  }
  root.appendChild(el("h2", null,
    "本周回顾 · " + esc(w.start) + " ~ " + esc(w.end)));
  root.appendChild(el("div", "meta", esc(w.disclaimer)));
  if (w.is_empty && !w.decisions.length && !w.promises.length && !w.turn_count) {
    root.appendChild(el("div", "note", "本周还没有可回顾的事实。直接说一句话就能新建。"));
    return;
  }

  // 分区渲染器：每个分区都**明说**空态，不让整段消失（消失就是静默）。
  const section = (title, rows, empty) => {
    root.appendChild(el("h3", null, title));
    if (!rows.length) {
      root.appendChild(el("div", "note", empty));
      return;
    }
    rows.forEach((row) => root.appendChild(el("div", "card", row)));
  };
  const label = (t) => (t.role ? "【" + esc(t.role) + "】" : "") + esc(t.title);
  const when = (t) => (t.when ? " " + esc(t.when.slice(5, 16)) : "");
  const day = (t) => (t.scheduled_for ? esc(t.scheduled_for) : "");

  section("本周结束 " + w.closed.length + " 件",
    w.closed.map((t) => label(t) + " —— " + esc(t.state) + when(t)),
    "（没有结束的事务）");

  section("卡在哪 · 本周顺延 " + w.slips.reduce((n, s) => n + s.times, 0) + " 次",
    w.slips.map((s) => esc(s.title) + " —— 顺延 " + s.times + " 次"),
    "本周没有顺延记录。");

  section("下周承诺 " + w.next_week.length + " 件",
    w.next_week.map((t) => day(t) + "  " + label(t)),
    "下周还没有排期。");

  section("没排期 " + w.unscheduled.length + " 件（记了但没进任何一天）",
    w.unscheduled.map((t) => label(t)), "没有悬空的事务。");

  section("在等 " + w.waiting.length + " 件",
    w.waiting.map((t) => esc(t.title) + " —— " + esc(t.waiting_on || "")),
    "没有在等的事。");

  section("成长目标（记下的自我期许）",
    w.growth_goals.map((g) => esc(g.text)),
    "还没记过 —— 用 /remember 记一条（如「今年把英语练到能开会」）。");

  // V1.133：三段「做过的事」，与事务分区分开摆，不混排、不打分。
  section("本周拍板 " + w.decisions.length + " 件（当时记下的原话）",
    w.decisions.map((d) => esc(d.conflict) +
      (d.reason ? " —— 因为：" + esc(d.reason) : "")),
    "没有拍过板。");

  section("本周许下的承诺 " + w.promises.length + " 件（仍未兑现）",
    w.promises.map((p) => esc(p.statement) +
      (p.promised_for ? " —— 承诺 " + esc(p.promised_for.slice(0, 10)) : "")),
    "没有新许下的承诺。");

  root.appendChild(el("div", "meta", w.turn_count
    ? "本周交给引擎 " + w.turn_count + " 轮"
    : "本周没有交给引擎的轮次。"));
  root.appendChild(el("div", "note", "这些是事实，不是建议 —— 要改什么，由你决定。"));
}"""
