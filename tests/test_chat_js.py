"""``js_chat.js`` 的**类型契约**守卫。

为什么需要它
------------
这条 bug 是在**浏览器里**实测发现的，不是测试发现的：

    出错了：(items || []).map is not a function

原因是同一个参数被当成**两种类型**用：``chatItems()`` 收整份 reply，
而 ``addMsg()`` 存历史那行按数组用 ``(items || []).map(...)``。
于是问一句就抛错；反过来从历史恢复时传的是数组，``chatItems`` 读到
``undefined`` 就静默返回 null，**任务卡片全丢**、一声不吭。

全量测试（1400+）当时**全绿**：Python 侧 ``chat_payload`` 造的 ``items``
确实是数组，接口也对；错的是浏览器里那段 JS 从未被任何测试执行过。

所以这里守的不是「行为」，是**参数类型不许再混**。静态守卫而不是跑 node：
js_chat 依赖 ``$`` / api / history / openTask 等一堆宿主，跑起来成本高，
而这个 bug 的本质就是一个名字被当两种形状用 —— 形状用源码就能钉住。

真机证据：``tools/verify_e2e.py``（跨三入口）+ 浏览器实测（问句返回
可点卡片、无报错）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parent.parent / "src" / "freeagent" / "web" / "js_chat.py"
SOURCE = JS.read_text(encoding="utf-8")


def _body(fn_name: str) -> str:
    """取出某个函数的源码块（按大括号配平，够用即可）。"""
    start = SOURCE.index(f"function {fn_name}(")
    depth, i = 0, start
    while i < len(SOURCE):
        if SOURCE[i] == "{":
            depth += 1
        elif SOURCE[i] == "}":
            depth -= 1
            if depth == 0:
                return SOURCE[start:i + 1]
        i += 1
    raise AssertionError(f"{fn_name} 的大括号没配平")


class TestChatItemsTakesAnArray:
    def test_signature_is_items_not_reply(self):
        """形参必须叫 ``items`` —— 叫 ``reply`` 就是「这里要整份 reply」的信号。"""
        body = _body("chatItems")
        assert re.search(r"function chatItems\(\s*items\s*\)", body), (
            "chatItems 的形参不该再叫 reply —— 它收的是条目数组"
        )

    def test_never_reads_dot_items_off_its_argument(self):
        """收数组就**不许**再读 ``arg.items``（那正是「收整份 reply」的痕迹）。"""
        body = _body("chatItems")
        # 去掉形参那一行的 items 之后，不该再有 items.items / reply.items
        assert not re.search(r"\b\w+\.items\b", body), (
            f"chatItems 里还在读 xxx.items —— 它收的是数组，不该再解一层：{body[:200]}"
        )

    def test_iterates_the_argument_directly(self):
        assert "items.forEach" in _body("chatItems"), (
            "chatItems 应当直接遍历形参"
        )


class TestAddMsgCallSitesPassArrays:
    """每个 ``addMsg`` 调用点，第 4 个参数都必须是**数组或 null**。"""

    def _call_args(self) -> list[str]:
        out = []
        for m in re.finditer(r"\baddMsg\(", SOURCE):
            i, depth, arg, args = m.end(), 0, [], []
            while i < len(SOURCE):
                ch = SOURCE[i]
                if ch in "([{":
                    depth += 1
                elif ch in ")]}":
                    if depth == 0:
                        break
                    depth -= 1
                if ch == "," and depth == 0:
                    args.append("".join(arg).strip())
                    arg = []
                else:
                    arg.append(ch)
                i += 1
            out.append("".join(arg).strip())      # 最后一个参数
        return out

    def test_no_call_site_passes_a_bare_reply(self):
        """``addMsg(..., reply)`` 是本 bug 的原始形态 —— 只传裸 reply 一律红。

        允许的形态：省略 / ``null`` / ``[]`` / ``xxx.items`` / ``m.items``。
        """
        bad = [a for a in self._call_args()
               if a in ("reply", "r", "res", "data", "resp")]
        assert not bad, f"这些调用把整份 reply 当数组传了：{bad}"

    def test_send_call_site_passes_reply_items(self):
        """送问句那一处必须显式传 ``reply.items``。

        这是回归的**原始位置**：少写 ``.items`` 就会退回「整份 reply」。
        """
        assert re.search(
            r"addMsg\(\s*\"bot\"\s*,\s*reply\.text\s*,[^)]*?reply\.items",
            SOURCE,
        ), "送问句的调用点应当传 reply.items（数组），不是整份 reply"


class TestNoMapOnANonArray:
    def test_history_store_only_maps_an_array(self):
        """``(items || []).map`` 本身没错，错在传进来的可能不是数组。

        这里锁住 ``addMsg`` 里的形参用法：它要么是数组，要么 null。
        """
        body = _body("addMsg")
        m = re.search(r"items:\s*\(items\s*\|\|\s*\[\]\)\.map", body)
        assert m, "addMsg 存历史那行应当仍是 (items || []).map —— 若被改写请重新评估"
        # 传进 chatItems 的必须同一个形参（而不是形参与别的混用）
        assert re.search(r"chatItems\(\s*items\s*\)", body), (
            "chatItems 应当收 addMsg 的 items 形参本身"
        )


class TestStaleDateRefreshOnReplay:
    """C2：回放历史时把「今天」答复里的过期日期刷新成当天。

    日期是**生成时冻进文本**的（``{view.day}``），服务端从不重渲染已存
    文本 —— 10-06 生成的答复在 10-09 回放时仍写着「10-06 今天…」。
    修法在客户端：存 ``generated_at``，回放时若与当天不等且文本以它
    开头，就改写前缀。静态守卫（与本文件其余守卫同一理由）：跑不起
    真浏览器，但这段逻辑的形状用源码就能钉住。
    """

    def test_chatDay_helper_exists(self):
        """``chatDay`` 现在住在 ``js_chat_store.py``（为守住 250 行体积守卫拆的）。

        这里刻意**读新模块**而不是放宽到「页面里有就算」 —— 后者会让这条
        断言退化成「搜一下全文」，模块真被删了也照样绿。
        """
        from freeagent.web.js_chat_store import JS_CHAT_STORE

        assert "function chatDay(" in JS_CHAT_STORE, (
            "缺本地日历日助手 chatDay"
        )

    def test_addMsg_stores_generated_at(self):
        """存历史时必须带上 meta —— 否则回放无从知道日期该不该刷新。"""
        body = _body("addMsg")
        assert "generated_at" in body, "addMsg 需要读写 meta.generated_at"
        assert re.search(r"meta:\s*meta\s*\|\|\s*null", body), (
            "addMsg 存历史那行应带上 meta（没有 meta 的老存档存 null）"
        )

    def test_addMsg_refreshes_the_stale_date(self):
        """改写必须同时满足三个条件：是 bot、日期过期、文本以日期开头。"""
        body = _body("addMsg")
        assert "meta.generated_at !== chatDay()" in body, (
            "回放时要比对 generated_at 与当天"
        )
        assert "text.indexOf(meta.generated_at) === 0" in body, (
            "只改写以日期**开头**的文本 —— 日期出现在句中的不能动"
        )
        assert "chatDay() + text.slice(meta.generated_at.length)" in body, (
            "改写应当只替换日期前缀，其余文本原样保留"
        )

    def test_send_call_site_passes_reply_meta(self):
        """发问句那一处要把 reply 的 generated_at 传给 addMsg。"""
        assert re.search(
            r"addMsg\(\s*\"bot\"\s*,\s*reply\.text\s*,[^;]*?generated_at",
            SOURCE,
        ), "sendChat 收到答复后应把 generated_at 一并传给 addMsg"

    def test_replay_passes_stored_meta(self):
        """回放历史那一处必须把存档里的 meta 传回 addMsg。"""
        assert re.search(r"addMsg\(m\.who, m\.text, m\.cls, m\.items, false, m\.meta\)", SOURCE), (
            "renderChatShell 回放应传 m.meta"
        )


# =============================================================================
# 对话多选删除（js_chat_select.py）
# =============================================================================
from freeagent.web.js_chat_select import JS_CHAT_SELECT as _SELECT

_SELECT_SRC = _SELECT
_CHAT_SRC = JS.read_text(encoding="utf-8")


def _select_body(fn_name: str) -> str:
    start = _SELECT_SRC.index(f"function {fn_name}(")
    depth, i = 0, start
    while i < len(_SELECT_SRC):
        if _SELECT_SRC[i] == "{":
            depth += 1
        elif _SELECT_SRC[i] == "}":
            depth -= 1
            if depth == 0:
                return _SELECT_SRC[start:i + 1]
        i += 1
    raise AssertionError(f"{fn_name} 的大括号没配平")


class TestChatSelectWiring:
    """钩子必须在位 —— 少了它们，功能在界面上根本不出现。"""

    def test_module_registered_in_page(self):
        from freeagent.web.page import INDEX_HTML
        assert "chatPickPaint" in INDEX_HTML, (
            "js_chat_select 没进页面 —— 多选删除整块都不会出现"
        )

    def test_loadChat_calls_paint_hook(self):
        body = _chat_body("loadChat")
        assert "chatPickPaint();" in body, (
            "loadChat 末尾必须调 chatPickPaint —— 否则渲染出的消息没有复选框"
        )

    def test_build_bar_has_entry_button(self):
        body = _chat_body("buildBar")
        assert "chatEnterPick" in body, (
            "输入框工具条里要有「多选删除」入口"
        )

    def test_nav_leaves_pick_mode(self):
        from freeagent.web.js_boot import JS_BOOT
        assert 'chatPickExit(false)' in JS_BOOT, (
            "导航离开对话时要退掉选择模式（js_boot 的钩子）"
        )


class TestChatSelectSemantics:
    """删除语义：删所选 / 删到此为止 / 重渲染 / 落盘。"""

    def test_delete_persists_and_rerenders(self):
        body = _select_body("chatPickDelete")
        assert "chatSave(history_)" in body, "删除必须落盘（localStorage）"
        assert 'load("chat")' in body, "删除后必须整段重渲染，不局部摘 DOM"

    def test_delete_to_here_requires_exactly_one(self):
        body = _select_body("chatPickDeleteToHere")
        assert "chatPicked.size !== 1" in body, (
            "「删到此为止」的锚点必须唯一 —— 勾多条时不猜、不默认取最早"
        )
        assert "confirm(" in body, (
            "删一整段必须二次确认：这一刀下去删的是一条，是一段"
        )

    def test_delete_to_here_keeps_prefix(self):
        body = _select_body("chatPickDelete")
        assert "history_.slice(0, fromIndex)" in body, (
            "删到此为止 = 保留锚点之前的所有（不含锚点）"
        )

    def test_checkbox_stops_propagation(self):
        body = _select_body("chatPickPaint")
        assert "e.stopPropagation()" in body, (
            "勾选不许冒泡到消息里的事务卡片（那会打开任务详情）"
        )

    def test_toolbar_lives_outside_the_log(self):
        """工具条不能挂进 #chatlog —— 历史条目与 log 子节点 1:1，
        工具条混进去会让「第 N 条」在数组和界面里指的不是同一条。"""
        body = _select_body("chatPickPaint")
        assert 'wrap.insertBefore(bar, $("#chatlog"))' in body, (
            "工具条应插在 chat wrapper 里、log 之前 —— 不占 log 子节点"
        )

    def test_refs_rebuilt_after_delete(self):
        body = _select_body("chatPickDelete")
        assert "chatPickRebuildRefs()" in body, (
            "删完必须重建 lastItemIds —— 否则「第二个」指向已删的消息"
        )

    def test_paint_is_noop_outside_pick_mode(self):
        body = _select_body("chatPickPaint")
        assert "if (!chatPickMode) return;" in body, (
            "非选择模式必须 no-op（loadChat 无条件调它）"
        )


def _chat_body(fn_name: str) -> str:
    """从 js_chat.py 取函数体（与 _body 同逻辑，但读聊天源）。"""
    start = _CHAT_SRC.index(f"function {fn_name}(")
    depth, i = 0, start
    while i < len(_CHAT_SRC):
        if _CHAT_SRC[i] == "{":
            depth += 1
        elif _CHAT_SRC[i] == "}":
            depth -= 1
            if depth == 0:
                return _CHAT_SRC[start:i + 1]
        i += 1
    raise AssertionError(f"{fn_name} 的大括号没配平")
