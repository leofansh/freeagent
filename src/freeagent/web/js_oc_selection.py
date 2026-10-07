"""编程页签：四段下拉 + 日常可选模型清单。

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

## 清单区为什么**默认折叠**

85 个模型全列出来是一屏看不完的滚动条，而日常要勾的通常不到十个。
默认展开只会让人陷在一堆不需要的模型里 —— 所以默认折叠，只显示
「已勾 N 个」。
"""

from __future__ import annotations

__all__ = ["JS_OC_SELECTION"]

JS_OC_SELECTION = r"""
// 四段的展示顺序 = 依赖顺序（档位依附于模型）。与后端
// endpoints_oc_selection.oc_selection_save 的校验顺序**必须一致** ——
// 那不是巧合：顺序错了，「选模型 + 选 High」这个组合就会提交失败。
const OC_STAGES = ["project", "agent", "model", "variant"];
const OC_STAGE_LABEL = {
  project: "项目", agent: "工作模式", model: "模型", variant: "推理档位",
};

// provider 分组用的显示名 → 回传值用 providerID。
// 不能只靠 value.split("/")[0] 当组名：用户看到的是 "OpenCode Zen"，
// 而载荷里是 "opencode"。混用会让界面出现两个看起来一样的组。
function ocGroupOf(options, modelValue) {
  const hit = options.find((o) => o.value === modelValue);
  if (!hit || !hit.hint) return "";
  // hint 形如「OpenCode Zen」或「OpenCode Zen · 3 档」
  return hit.hint.split("·")[0].trim();
}

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

// 日常可选模型清单：每个模型一个开关（对应 Desktop「管理模型」那个页面）。
//
// 刻意**默认折叠**：85 个模型是一屏看不完的滚动条，而日常要勾的不到十个。
function ocCurationView(curated, repaint) {
  const box = el("div", "card");
  const on = curated.models || [];
  const available = curated.available || [];
  const head =
    '<div class="row1"><span class="title">日常可选模型</span>' +
    '<span class="meta">' + (curated.configured
      ? "已勾 " + on.length + " 个"
      : "没挑过 —— 上面给的是全部 " + available.length + " 个") + "</span></div>";
  box.innerHTML = head;
  if (!available.length) {
    box.appendChild(el("div", "note",
      "现在读不到可用的模型（OpenCode 没起来，或没有已连接凭据的 provider）。"));
    return box;
  }

  const details = el("details", "");
  if (curated.configured) details.open = true;
  const sum = el("summary", "", curated.configured
    ? "增删（已勾 " + on.length + " 个）"
    : "挑几个放进日常清单");
  details.appendChild(sum);

  const toggle = (o, isOn) => {
    api("/api/oc/curation", {
      method: "POST",
      body: JSON.stringify({ action: isOn ? "add" : "remove", model: o.value }),
    }).then(repaint, (e) => toast("改不了：" + e.message));
  };

  // 按 provider 分组：用户认的是 provider（它在 OpenCode 里要单独登录），
  // 而 85 个模型平铺着根本看不出「哪些要凭据」。
  const groups = {};
  available.forEach((o) => {
    const g = ocGroupOf(available, o.value);
    (groups[g || "其他"] = groups[g || "其他"] || []).push(o);
  });
  Object.keys(groups).sort().forEach((g) => {
    details.appendChild(el("div", "hint",
      esc(g) + "（" + groups[g].length + " 个）"));
    const list = el("div", "currow");
    groups[g].forEach((o) => {
      const id = "occ-" + o.value.replace(/[^a-z0-9]/gi, "-");
      const isOn = on.indexOf(o.value) >= 0;
      const item = el("label", "curopt");
      item.innerHTML = '<input type="checkbox" id="' + id + '"' +
        (isOn ? " checked" : "") + "><span>" + esc(o.label) + "</span>" +
        (o.hint ? '<span class="tag">' + esc(o.hint) + "</span>" : "");
      item.querySelector("input").addEventListener("change", (e) =>
        toggle(o, e.target.checked));
      list.appendChild(item);
    });
    details.appendChild(list);
  });

  box.appendChild(details);
  box.appendChild(el("div", "hint",
    "清单<strong>只影响 FreeAgent 的选择器</strong>（飞书 + 这里）。" +
    "OpenCode Desktop 里仍然是你自己的那份 —— 两边各管各的，" +
    "FreeAgent 不会去改 OpenCode 的配置（那是 Desktop 的文件，" +
    "两个进程抢一个配置文件迟早互相覆盖）。<br>" +
    "「连接提供商 / 配 API Key」也不在这里做：" +
    "OpenCode 那个接口会返回明文 Key，而这个页面没有鉴权（只监听回环）。" +
    "配凭据请在 OpenCode 里做。"));
  return box;
}

function ocDispatchView(data, root, repaint) {
  const box = el("div", "card");
  box.innerHTML = '<div class="row1"><span class="title">要做什么</span>' +
    '<span class="meta">建一条委派事务</span></div>';

  const ta = el("textarea", "");
  ta.id = "ocbrief";
  ta.rows = 2;
  ta.placeholder = "一句话说清，例如：把 README 的用法那节补上";
  ta.style.width = "100%";
  box.appendChild(ta);

  const btn = el("button", "btn primary", "开始");
  btn.id = "ocgo";
  const note = el("div", "hint", "");
  btn.addEventListener("click", () => {
    const brief = ta.value.trim();
    if (!brief) { toast("先说一句要做的事"); return; }
    if (/[\r\n]/.test(brief)) {
      // 服务端也会挡，但这里先说 —— 免得用户莫名其妙看到失败。
      toast("需求要写成一行（换行会让 opencode 判成复杂任务然后失败）");
      return;
    }
    btn.disabled = true;
    api("/api/oc/dispatch", {
      method: "POST",
      body: JSON.stringify({ brief: brief, project: data.selection.project }),
    }).then((r) => {
      note.innerHTML = '<strong>已建好事务</strong>：' + esc(r.title) +
        "（脉络：" + esc(r.role) + (r.role_by_default ? "，自动选的" : "") + "）" +
        "<br>" + esc(r.next);
      ta.value = "";
      btn.disabled = false;
      toast("已建好事务");
    }, (e) => {
      note.innerHTML = '<span class="log-err">' + esc(e.message) + "</span>";
      btn.disabled = false;
    });
  });
  box.appendChild(btn);
  box.appendChild(note);

  box.appendChild(el("div", "hint",
    "<strong>建了事务 ≠ 已经在跑。</strong> 执行器是独立进程，只跑「已确认且已 /start」" +
    "的事务 —— 所以建好之后还要两步（上面会写出来）。<br>" +
    "opencode 每要动手一次，仍然会在<strong>飞书</strong>给你一张授权卡。" +
    "代码不该在你没点过的情况下动。"));
  return box;
}

function renderOcSelection(data, root) {
  // **清空再画**。刻意不在这里清 —— 调用方 :func:`loadOcSelection` 会清，
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