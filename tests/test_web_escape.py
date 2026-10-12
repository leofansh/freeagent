"""``esc()`` 的 HTML 转义契约守卫（``web/js_core.py``）。

为什么需要它
------------
这条 bug 是在**浏览器里**看出来的，不是测试发现的 —— 页面上写的是：

    改代码的说法：「让 opencode 改 <项目" “要做的事”」。

``esc()`` 把 ``>`` 转义成了 ``&quot;`` —— 那是**引号**的实体，不是 ``>`` 的。
于是 ``<项目>`` 先进 HTML 成 ``&lt;项目&quot;``，浏览器再解码成 ``<项目"``：
尖括号的**右半边凭空变成了引号**。凡是文本里带 ``>`` 的地方全中招。
Python 侧一直是对的（``test_capability_route`` 只断言子串），所以测试全绿。

``esc()`` 还被用在**属性**位置（``js_oc_selection`` 的 ``value="..."``、
``js_vision`` 的 ``href="..."``），所以真正的 ``"`` 也必须转义 —— 原实现
恰好漏了它、又把 ``>`` 错当成它，两个洞一起补。

静态守卫而不是跑 node：测试环境跑不起真 DOM，而这两个映射用源码就能钉死。
真机证据：浏览器实测「你能做什么」现在渲染出 ``<项目>``（不再是 ``<项目"``）。
"""
from __future__ import annotations

from freeagent.web.js_core import JS_CORE

#: 顺序即 esc() 里 replace 链的顺序 —— ``&`` 必须最先，否则后面注入的
#: 实体（如 ``&gt;``）会被再转一次成 ``&amp;gt;``。
MAPPINGS = (
    ('.replace(/&/g, "&amp;")', "& 转 &amp;，且必须最先做"),
    ('.replace(/</g, "&lt;")', "< 转 &lt;"),
    ('.replace(/>/g, "&gt;")', "> 转 &gt;"),
    ('.replace(/"/g, "&quot;")', '" 转 &quot;（esc 也用在属性里）'),
)


class TestEscMapsTheRightEntities:
    def test_every_mapping_is_present(self):
        for snippet, why in MAPPINGS:
            assert snippet in JS_CORE, f"esc() 缺了这条：{snippet} —— {why}"

    def test_gt_is_not_mistaken_for_a_quote(self):
        """回归：``>`` 曾被转义成 ``&quot;``，把 ``<项目>`` 渲染成 ``<项目"``。"""
        assert '.replace(/>/g, "&quot;")' not in JS_CORE, (
            '> 不能转成 &quot;（那是引号的实体）—— 会让 <项目> 显示成 <项目"'
        )

    def test_amp_is_escaped_first(self):
        idx = [JS_CORE.index(s) for s, _ in MAPPINGS]
        assert idx == sorted(idx), (
            "转义顺序必须是 & 最先 —— 否则先注入的 &gt; 会被 & 规则二次转义"
        )
