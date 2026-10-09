"""编程页签的两张动作卡：日常可选模型清单 + 派发事务。

## 为什么从 :mod:`js_oc_selection` 拆出来

四段下拉是「**读**什么、选什么」，这两张卡是「**做**什么」——
清单卡增删模型（写），派发卡建委派事务（写）。原先挤在同一个 JS
字符串里，于是 ``js_oc_selection.py`` 越过了 250 行那道守卫。

**跨文件调用为什么没问题**：:mod:`page` 把所有 JS 块拼进**同一个**
``<script>``，所以函数声明会提升、作用域是同一个。守卫
``tests/test_web.py`` 里的 ``len(blocks) == 1`` 守的正是「恰好一个
script 块」，所以拆分**不能**变成多个 script。

## 为什么清单默认折叠

85 个模型全列出来是一屏看不完的滚动条，而日常要勾的通常不到十个。
默认展开只会让人陷在一堆不需要的模型里 —— 所以默认折叠，只显示
「已勾 N 个」。

## 为什么派发卡要说「建了事务≠ 已经在跑」

执行器是独立进程，只跑「已确认且已 ``/start``」的事务。话术里不说清，
用户会以为建完就有人在改代码了 —— 而授权卡还在飞书等着他点。
"""

from __future__ import annotations

__all__ = ["JS_OC_CARDS"]

JS_OC_CARDS = r"""
// provider 分组用的显示名 → 回传值用 providerID。
// 不能只靠 value.split("/")[0] 当组名：用户看到的是 "OpenCode Zen"，
// 而载荷里是 "opencode"。混用会让界面出现两个看起来一样的组。
function ocGroupOf(options, modelValue) {
  const hit = options.find((o) => o.value === modelValue);
  if (!hit || !hit.hint) return "";
  // hint 形如「OpenCode Zen」或「OpenCode Zen · 3 档」
  return hit.hint.split("·")[0].trim();
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
    // 建事务时智能层要起标题/推断脉络（实测约 7 秒，网络差时更久）。
    // 不给进度文案的话，这就是「点了没反应」——按钮灰了但页面像死了。
    note.textContent = "正在建事务…（通常几秒，请稍候）";
    api("/api/oc/dispatch", {
      method: "POST",
      body: JSON.stringify({ brief: brief, project: data.selection.project }),
    }).then((r) => {
      // 成功回调自身也要防抛：这里一旦抛错，失败分支接不住（同一 .then
      // 里的两支互不兜底），症状就是「事务建好了但页面毫无反应」。
      try {
        note.innerHTML = '<strong>已建好事务</strong>：' + esc(r && r.title) +
          "（脉络：" + esc(r && r.role) + (r && r.role_by_default ? "，自动选的" : "") + "）";
        ta.value = "";
        // 「开始做」按钮：飞书动作卡上本来就有它（sender.py 的
        // _btn("开始做", "/start")），网页端却让人手敲 /start —— 设计文档
        // 11.9.8「点一下即可」在网页端一直没兑现。这里补上，调的是
        // 任务动作那条老路 /api/task/<id>/start（start 已在白名单里），
        // 所以后端不用新增端点。
        const row = el("div", "currow");
        const go = el("button", "btn primary", "开始做");
        const st = el("span", "hint", "");
        go.addEventListener("click", () => {
          go.disabled = true;
          st.textContent = "正在开始…";
          api("/api/task/" + encodeURIComponent(r.task_id) + "/start", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: "{}",
          }).then(() => {
            // 成功分支同样要防抛：两支互不兜底，抛了就是「点了没反应」。
            try {
              go.textContent = "已开始";
              st.textContent = "执行器会接手（它得在跑：python -m freeagent.delegate）";
              toast("已开始 —— 动手前的授权卡会发到飞书");
            } catch (err2) {
              st.textContent = "开始失败：" + err2.message;
              go.disabled = false;
            }
          }, (e2) => {
            st.textContent = "开始失败：" + (e2 && e2.message ? e2.message : e2);
            go.disabled = false;
          });
        });
        row.appendChild(go);
        row.appendChild(st);
        note.appendChild(row);
        toast("已建好事务");
      } catch (err) {
        note.innerHTML = '<span class="log-err">事务已建好，但反馈渲染出错：' +
          esc(err && err.message ? err.message : err) + "</span>";
        if (window.console && console.error) console.error("[freeagent]", err);
      }
      btn.disabled = false;
    }, (e) => {
      note.innerHTML = '<span class="log-err">' + esc(e.message) + "</span>";
      btn.disabled = false;
    });
  });
  box.appendChild(btn);
  box.appendChild(note);

  box.appendChild(el("div", "hint",
    "<strong>建了事务 ≠ 已经在跑。</strong> 建好后点「开始做」，执行器（独立进程）" +
    "才会接手 —— 它得在跑（python -m freeagent.delegate）。<br>" +
    "opencode 每要动手一次，仍然会在<strong>飞书</strong>给你一张授权卡。" +
    "代码不该在你没点过的情况下动。"));
  return box;
}
"""