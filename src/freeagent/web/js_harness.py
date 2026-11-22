"""Web「Harness」视图（V1.143）：它为你累积了什么 —— 四块只读回显。

为什么独立成文件：``js_boot`` 是视图分发表，每个视图各占一个渲染模块；
``js_model``（画像）已经是先例。这里只调 ``/api/harness``，**不自算任何计数**
—— 数字与「读不到」的分辨全在服务层（12.1.1）。

## 两条呈现纪律

1. **缺键 ≠ 0**：``workload`` 里没有那个键就是「读不出来」，界面上写「读不到」。
   把它画成 0 等于告诉人「你没有未结束的事务」，那是替人下结论（V1.139 同一条）。
2. **动态文本一律 ```esc()```**：目录、备注、记忆原文都来自磁盘或库，
   名字里可以带尖括号（V1.117 的坑）。
"""

from __future__ import annotations

__all__ = ["JS_HARNESS"]

JS_HARNESS = r"""function harnessNum(v) {
  // 缺键（undefined）是「读不出来」，null 也是；0 是可信的 0。
  return (v === undefined || v === null) ? "读不到" : String(v);
}

function harnessSize(n) {
  if (n == null) return "大小读不到";
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  return (n / 1048576).toFixed(1) + " MB";
}

function harnessRuleLine(rule) {
  if (!rule) return "规矩：这个工作区没记目录，引擎读不到项目规矩";
  if (!rule.exists) return "规矩：这个目录里没有 AGENTS.md —— 引擎没有项目规矩可读";
  const bits = [];
  // 行数没数完时不重复一遍「没数完」：原因已经由服务层的 note 说清
  // （「只读了前 8 KB」），这里只报大小，免同一句话在界面上出现两遍。
  if (rule.lines == null) bits.push("AGENTS.md 在（" + harnessSize(rule.size) + "）");
  else bits.push("AGENTS.md 共 " + rule.lines + " 行（" + harnessSize(rule.size) + "）");
  if (rule.updated_at) bits.push("最后改于 " + rule.updated_at.slice(0, 10));
  if (rule.note) bits.push(rule.note);
  return "规矩：" + bits.join(" · ");
}

async function renderHarness(root) {
  root.innerHTML = "";
  let d;
  try {
    d = await api("/api/harness");
  } catch (e) {
    root.innerHTML = '<div class="note">Harness 读不出来：' + esc(e.message) + "</div>";
    return;
  }
  root.appendChild(el("h2", null, "Harness · 你累积起来的东西"));
  root.appendChild(el("div", "meta",
    "四块：项目与规矩 / 记忆 / 事务与承诺 / 能力与设置。全部是已有事实的回显，"
    + "这一页不改任何东西 —— 要改，回它原来那个入口。"));

  // ① 项目与规矩：一个工作区一张卡，规矩只报文件事实，不解析内容。
  root.appendChild(el("h3", null, "项目与规矩"));
  if (!d.projects.length) {
    root.appendChild(el("div", "note",
      "还没有工作区档案 —— 去工作台选一个目录，它才会开始累积。"));
  } else {
    d.projects.forEach((p) => {
      const card = el("div", "card");
      card.appendChild(el("div", "title", esc(p.name || p.directory)));
      card.appendChild(el("div", "meta", esc(p.directory)));
      const bits = [
        p.turns == null ? "轮次读不到"
          : (p.turns ? "跑过 " + p.turns + " 轮" : "还没跑过"),
      ];
      if (p.note) bits.push("备注：" + p.note);
      card.appendChild(el("div", "meta", esc(bits.join(" · "))));
      card.appendChild(el("div", "meta", esc(harnessRuleLine(p.rule))));
      root.appendChild(card);
    });
  }

  // ② 记忆：条数能精确就精确，只能「至少」就写「至少」。
  root.appendChild(el("h3", null, "记忆（core 记忆层）"));
  const total = d.memory_total == null
    ? "至少 " + d.memory.length + " 条"
    : d.memory.length + " 条";
  root.appendChild(el("div", "meta", "共 " + total + "，最近的在下面"));
  if (!d.memory.length) {
    root.appendChild(el("div", "note",
      "还没记过任何偏好 / 约定 / 修正。对话里说「记住：……」就会进到这里。"));
  } else {
    d.memory.slice(0, 20).forEach((m) => {
      const when = (m.created_at || "").slice(0, 10);
      const line = "[" + (m.kind || "记忆") + "] " + m.content + (when ? " · " + when : "");
      root.appendChild(el("div", "meta", esc(line)));
    });
  }

  // ③ 事务与承诺：只报数，动作在别的页。
  root.appendChild(el("h3", null, "事务与承诺"));
  const w = d.workload || {};
  root.appendChild(el("div", "meta",
    "未结束事务 " + harnessNum(w.open_tasks)
    + " · 未兑现承诺 " + harnessNum(w.open_promises)
    + " · 待处理 " + harnessNum(w.pending)));
  root.appendChild(el("div", "note",
    "三项都只是计数 —— 要动手去「今天」「承诺」「待处理」那三页。"));

  // ④ 能力与设置：能力没有就说不有；设置只报当前值。
  root.appendChild(el("h3", null, "能力与设置"));
  if (!d.skills.available) {
    root.appendChild(el("div", "note", esc(d.skills.reason)));
  }
  const s = d.settings || {};
  root.appendChild(el("div", "meta", "模型层：" + esc(s.provider || "未知")));
  root.appendChild(el("div", "meta",
    "凭据：" + (s.key_configured == null
      ? "读不到"
      : (s.key_configured ? "已配置" : "未配置（现在走规则层，离线可用）"))));
  root.appendChild(el("div", "meta",
    "授权档：" + (s.mode === "auto_dir" ? "本目录内自动" : "逐次授权（默认）")));
  if (s.data_dir) root.appendChild(el("div", "meta", "数据目录：" + esc(s.data_dir)));

  if (d.notes && d.notes.length) {
    root.appendChild(el("div", "note", "读的时候有几处不顺：" + esc(d.notes.join("；"))));
  }
}"""
