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
