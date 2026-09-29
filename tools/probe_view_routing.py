"""量一量：真 LLM 选路 vs 关键词表，谁更准。

**这不是测试。** 它要真凭据、结果不确定，放进 CI 只会变成随机红灯。
它存在的理由是 12.1.1 那条规矩：**改路由之前先有数字**。

跑法::

    set PYTHONIOENCODING=utf-8
    python tools\\probe_view_routing.py

它读同一份黄金语料（``tests/test_routing_eval.py`` 里的 ``GOLDEN``，
不复制一份 —— 复制就会漂移），对**两条路径**各跑一遍：

1. 关键词表（``RuleBasedProvider``，离线）
2. 真模型选路（读 ``~/.freeagent/llm.env``）

然后并排打印。关注两件事：

- **路由准确率有没有真的涨** —— 涨了才说明这次改动值得
- **误写率是不是还是 0** —— 这是唯一不给下界、必须恒为 0 的指标
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from freeagent.app import build_app                       # noqa: E402
from freeagent.config import Config                       # noqa: E402
from freeagent.services.clock import FrozenClock          # noqa: E402
from freeagent.services.llm import build_provider         # noqa: E402
from freeagent.services.llm.rules import RuleBasedProvider  # noqa: E402

# 黄金语料与测试同源 —— 这里**不**另抄一份
from test_routing_eval import GOLDEN                      # noqa: E402

NOW = datetime(2026, 9, 26, 14, 0)


def make_app(llm, name: str, *, view_routing: bool = False):
    """建一套临时环境。

    ``view_routing`` **必须显式给**：生产默认是关的（实测没赢）。若跟着
    默认关，两条路径会跑同一套关键词表，探针就会报出「差值 0」——
    那个 0 不代表两者打平，只代表**被测的代码压根没跑**。
    """
    home = Path(tempfile.mkdtemp())
    app = build_app(
        home / f"{name}.db",
        clock=FrozenClock(NOW),
        llm=llm,
        config=Config(llm_view_routing=view_routing),
    )
    app.roles.create("工作项目A", note="销售 周报 客户")
    app.roles.create("家庭", note="孩子 学校 材料")
    for title in ("销售周报初稿", "整理客户名单", "修窗户螺丝"):
        t = app.tasks.create(title, [app.roles.list_roles()[0].id])
        app.tasks.schedule(t.id, app.clock.today())
    return app


def run(app):
    """返回 {问句: (route, inferred)}。同时盯住有没有写数据。"""
    before = {t.title for t in app.tasks.list_all()}
    roles_before = {r.name for r in app.roles.list_roles(include_inactive=True)}
    out = {}
    for case in GOLDEN:
        reply = app.chat.respond(case.text)
        out[case.text] = (reply.route, reply.inferred)
    after = {t.title for t in app.tasks.list_all()}
    roles_after = {r.name for r in app.roles.list_roles(include_inactive=True)}
    wrote = sorted(after - before)
    polluted = sorted(roles_after - roles_before)
    return out, wrote, polluted


def score(actual):
    route_ok = sum(1 for c in GOLDEN if actual[c.text][0] == c.route)
    inf_ok = sum(1 for c in GOLDEN if actual[c.text][1] == c.inferred)
    return route_ok, inf_ok


class CountingLLM:
    """数 ``select_view`` 被调了几次。

    为什么非要这个：**探针已经两次报出「差值 0」而其实没跑。**
    一次是 ``view_routing`` 默认关，两条路径跑的是同一张关键词表；
    一次是漏改调用点。两次的数字都「看起来像结论」。

    所以探针必须能证明被测代码真的跑了 —— 否则它只是个装饰。
    """

    def __init__(self, inner):
        self._inner = inner
        self.calls = 0

    @property
    def can_select_view(self):
        return getattr(self._inner, "can_select_view", False)

    def select_view(self, text, views):
        self.calls += 1
        return self._inner.select_view(text, views)

    def __getattr__(self, name):        # 其余能力原样转发
        return getattr(self._inner, name)


def main() -> int:
    print("=== 建两套环境（临时库，不碰真数据）===")
    rules_app = make_app(RuleBasedProvider(), "rules")

    try:
        cfg = Config()
        real = build_provider(cfg)
    except Exception as exc:
        print(f"!! 拿不到真模型：{exc}")
        print("   先在设置页配好 Key 再跑本探针。")
        return 2
    print(f"真模型：{type(real).__name__}  can_select_view="
          f"{getattr(real, 'can_select_view', '?')}")
    if not getattr(real, "can_select_view", False):
        print("!! 这个实现不参与选路，探针没有意义。")
        return 2
    counting = CountingLLM(real)
    llm_app = make_app(counting, "llm", view_routing=True)

    rules_actual, rules_wrote, rules_roles = run(rules_app)
    llm_actual, llm_wrote, llm_roles = run(llm_app)

    # **先证明被测代码真的跑了**，再报任何数字。
    #
    # 探针已经两次栽在这上面：一次是 ``view_routing`` 默认关、两次是漏改
    # 调用点，两次都报出了「差值 0」这种**看起来像结论**的东西。
    # 一个从没被执行过的路径和一个「与基线持平」的路径，数字长得一模一样。
    if counting.calls == 0:
        print("!! 真模型一次都没被问到 —— 这次的「差值」毫无意义，先修接线")
        return 2
    print(f"（接线自检：真模型被调用 {counting.calls} 次，数字有效）")

    n = len(GOLDEN)
    print("\n=== 并排对照（期望 → 关键词表 / 真模型）===")
    for case in GOLDEN:
        r_route, r_inf = rules_actual[case.text]
        l_route, l_inf = llm_actual[case.text]
        want = f"{case.route}{'*' if case.inferred else ''}"
        mark = lambda ok: "OK " if ok else "MISS"
        print(
            f"  {case.text!r:22} 期望 {want:14}"
            f" 关键词 {mark(r_route == case.route)}{r_route:11}"
            f" 模型 {mark(l_route == case.route)}{l_route}"
            + ("  (自报)" if l_inf else "")
        )

    r_ok, r_inf_ok = score(rules_actual)
    l_ok, l_inf_ok = score(llm_actual)
    print(f"\n  关键词表：路由 {r_ok}/{n} = {r_ok / n:.0%}   "
          f"自报 {r_inf_ok}/{n} = {r_inf_ok / n:.0%}")
    print(f"  真模型  ：路由 {l_ok}/{n} = {l_ok / n:.0%}   "
          f"自报 {l_inf_ok}/{n} = {l_inf_ok / n:.0%}")
    print(f"  差值    ：路由 {l_ok - r_ok:+d}   自报 {l_inf_ok - r_inf_ok:+d}")

    print("\n=== 误写（必须两套都是 0）===")
    for label, wrote, roles in (("关键词表", rules_wrote, rules_roles),
                                ("真模型", llm_wrote, llm_roles)):
        print(f"  {label}：建事务 {wrote or '无'}  建角色 {roles or '无'}")

    json.dump(
        {
            "route": {"rules": r_ok, "llm": l_ok, "total": n},
            "inferred": {"rules": r_inf_ok, "llm": l_inf_ok, "total": n},
            "llm_wrote": llm_wrote,
        },
        open(ROOT / "view_routing_probe.json", "w", encoding="utf-8"),
        ensure_ascii=False, indent=2,
    )
    print("\n（结果已写 view_routing_probe.json）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
