"""编程页签：四段下拉 + 这一页的渲染与加载。

## 为什么这个页签与飞书共用一份选项

它读的是 ``/api/oc/options``，那个端点转手
:mod:`freeagent.services.oc_discovery` —— 与飞书那四张卡**同一个来源**。

所以「在 Web 里验过的」就是「飞书在跑的」。若 Web 自己查一遍 OpenCode，
两边会给出不同的集合，而症状是「Web 上选得到、飞书里选不到」，
没人知道该信哪个。

## 为什么模型用**原生 select** 而不是按钮

飞书没有下拉框，只能拆成按钮卡分页 —— 那是妥协。浏览器有原生
``<select>``，能做到 Desktop 那个样子：按 provider 分组、显示档位数、
一个搜索框（``<datalist>`` 由浏览器提供输入过滤）。

## 两张动作卡不在这里

「日常可选模型」与「派发事务」是**动作**、不是选择器，挪到
:mod:`js_oc_cards` 了（原先挤在一起，把这个文件顶过了 250 行守卫）。
两张卡由本文件的 :func:`renderOcSelection` 调用—— 同属一个
``<script>``，所以调用得到。
"""

from __future__ import annotations

__all__ = ["JS_OC_SELECTION"]

JS_OC_SELECTION = r"""
// 四段的展示顺序 = 依赖顺序（档位依附于模型）。与后端
// endpoints_oc_selection_write.oc_selection_save 的校验顺序**必须一致** ——
// 那不是巧合：顺序错了，「选模型 + 选 High」这个组合就会提交失败。
const OC_STAGES = ["project", "agent", "model", "variant"];
const OC_STAGE_LABEL = {
  project: "项目", agent: "工作模式", model: "模型", variant: "推理档位",
};

function ocOptionLabel(o) {
  // 显示名 + 档位数。不显示 providerID：那是内部标识。
  return o.hint ? o.label + "（" + o.hint + "）" : o.label;
}

function ocSelect(stage, options, value, note, onChange) {
  const wrap = el("div", "field");
  const id = "ocsel-" + stage;
  wrap.innerHTML = '<label for="' + id + '">' +
    esc(OC_STAGE_LABEL[stage]) + "</label>";
  const sel = el("select", "");
  sel.id = id;
  // 空选项 = 「用默认」。刻意保留为空，而不是自动选第一个 ——
  // 自动选中会让用户以为「没选就是它」，而默认与第一个往往是不同的模型。
  //
  // 但**只在选项里没有空值时才加**：「推理档位」段服务端本来就给了
  // 「不指定（用模型默认）」（value 为空），再加一个就会有两个 value 相同的
  // 选项 —— 而用户点哪个都是「不指定」，于是那个下拉看着像有重复项。
  const hasEmpty = options.some((o) => !o.value);
  sel.innerHTML = (hasEmpty ? "" : '<option value="">（用默认）</option>') +
    options.map((o) =>
      '<option value="' + esc(o.value) + '"' +
      (o.value === value ? " selected" : "") + ">" +
      esc(ocOptionLabel(o)) + "</option>").join("");
  sel.addEventListener("change", () => onChange(stage, sel.value));
  wrap.appendChild(sel);
  if (note) {
    wrap.appendChild(el("div", "hint", esc(note)));
  }
  return wrap;
}

function renderOcSelection(data, root) {
  // **清空再画**。刻意不在这里清 —— 调用方 loadOcSelection 会清，
  // 而清单的 repaint 回调也走它，所以只有一个地方负责清。
  //
  // 第一版的 repaint 直接调本函数，于是每勾一个模型就**叠一份**：
  // 界面上出现两份清单（实测 85 → 170 个开关），而
  // ``getElementById`` 返回的是**第一份**，于是「清单已生效」也看不出来。
  // 症状是「勾了没反应」—— 而真实原因是自己把自己画了两遍。
  root.appendChild(el("div", "title", "编程"));
  root.appendChild(el("div", "hint",
    "这四段决定「交给 opencode 时用哪个项目、哪个工作模式、" +
    "哪个模型、哪个推理档」。跟飞书里那四张卡<strong>是同一份数据</strong>，" +
    "所以在飞书选过的这里也一样。<br>" +
    "这个页签主要是<strong>配置与试验</strong>：" +
    "真正干活时工具授权卡仍走飞书 —— 代码不该在你没点过的情况下动。"));

  if (!data.discovery_ok) {
    root.appendChild(el("div", "note",
      "连不上 OpenCode，所以下面都是空的。先确认它能起来" +
      "（命令行 <code>opencode --version</code> 有输出），再点右上「刷新」。"));
  }

  const card = el("div", "card");
  const sel = (stage) => (data[stage] || {}).options || [];

  // 换模型后档位要跟着重画 —— 而**档位选项是服务端按当前模型算出来的**，
  // 所以这里**必须重拉**，不能拿旧数据本地重画。
  //
  // 第一版就是本地重画，于是换个模型之后档位下拉**始终只有「不指定」** ——
  // 界面看不出任何错误，只是「怎么选不了 High」。而那正是这个页签存在的理由。
  //
  // 重拉不贵：选项来自同一个快照（:mod:`oc_discovery` 缓存 30 秒），
  // 所以不会多起一个 opencode 进程。
  const saveOc = (next) => {
    api("/api/oc/selection", {
      method: "POST",
      body: JSON.stringify({ selection: next }),
    }).then(() => loadOcSelection(root), (e) => toast("没记住：" + e.message));
  };

  const paint = (staged) => {
    const s = staged || data.selection;
    card.replaceChildren();
    OC_STAGES.forEach((stage) => {
      card.appendChild(ocSelect(stage, sel(stage), s[stage],
        stage === "model" ? (data.model || {}).note : "",
        (changed, value) => {
          const next = Object.assign({}, s);
          next[changed] = value;
          // 改了模型就清掉旧档位：Big Pickle 没有档，留着会让用户
          // 以为「选了 High 却没生效」。
          if (changed === "model") next.variant = "";
          saveOc(next);
        }));
    });
  };

  paint(data.selection);
  root.appendChild(card);

  // 工作模式失效：当前选的模式在 OpenCode 侧已经不存在（项目级 agent 被删、
  // 或 OpenCode 升级后内置 agent 改名）。必须显式点出来，否则派发会静默
  // 退化成用别的模式（设计文档 11.13.8 / 11.14）。
  if (data.agent_warning) {
    root.appendChild(el("div", "note warn", esc(data.agent_warning)));
  }

  // 派发卡紧跟选择卡 —— 顺序就是操作顺序：先定「用哪个」，再说「做什么」。
  root.appendChild(ocDispatchView(data, root));

  root.appendChild(ocCurationView(data.curated, () => loadOcSelection(root)));

  const box = el("div", "card");
  box.innerHTML = '<div class="row1"><span class="title">现在这一套</span></div>';
  box.appendChild(el("div", "hint",
    OC_STAGES.map((k) => esc(OC_STAGE_LABEL[k]) + "：" +
      esc(data.selection[k] || "（默认）")).join("<br>")));
  root.appendChild(box);
}

function loadOcSelection(root) {
  root.appendChild(el("div", "loading", "正在问 opencode 有哪些项目 / 模型…"));
  api("/api/oc/options").then((d) => {
    root.replaceChildren();
    renderOcSelection(d, root);
  }, (e) => {
    root.replaceChildren(el("div", "note", "读不到选项：" + e.message));
  });
}
"""