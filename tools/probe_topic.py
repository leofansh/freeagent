"""探针：飞书**私聊**里到底能不能用「话题」当会话容器？

这不是测试 —— 它**真的往你的飞书发消息**。跑法：

    set PYTHONIOENCODING=utf-8
    python tools\\probe_topic.py            # 真跑（发 5~6 条消息到你私聊）
    python tools\\probe_topic.py --dry-run  # 只造请求、不发

## 为什么必须先探，不能先写代码

「一个话题 = 一个会话」是飞书官方的标准用法（话题概述里写的就是
「每个话题对应一个工单信息」）。但**我们要用的四件事，文档一件都没覆盖**：

1. 私聊（p2p）能不能开话题 —— 文档只讲了群；
2. ``interactive`` 授权卡能不能作为话题回复；
3. **CardKit 2.0 实体卡**（``{"type":"card","data":{"card_id":…}}``）能不能进话题；
4. V1.88 的 **PATCH 折叠终态**在话题消息上是否同样生效。

这四条全绿才能把「话题隔离」写进契约（同 12.7.2 的纪律：真机自证之前
不落规范性文字）。有一条红，就退到降级方案（卡面带委派短号 + 进度卡分组）。

## 判据是「飞书回了什么」，不是「请求没报错」

每一步都读响应里的 ``code``（飞书 HTTP 200 也可以是失败）**和**
``data.thread_id``（**开了话题才会有**）。所以 S2 的真实判据不是
「回复成功」，而是「回复成功**且** thread_id 非空」—— 前者只证明消息发出去了。

## 它会改你的聊天，但只改自己造的那几条

根消息是探针自己的卡，收尾会 PATCH 成一行收据（不留墓碑，同 11.16）。
中途那些测试卡会留在话题里 —— 那正是要看的证据，别手动删。
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from freeagent.feishu.config import ConfigError, load_config          # noqa: E402
from freeagent.feishu.sender import (                                  # noqa: E402
    FeishuError,
    FeishuSender,
    _progress_card,
    _settled_card,
    _tool_card,
)

#: 探针自己的根卡。刻意**没有按钮**：根卡只当话题的锚点，不需要可点。
#
# 为什么不直接复用 ``_tool_card``：那张卡有 allow/deny 按钮，点下去会真的
# 走桥接的 ``_card_action``。探针不该给用户一个「点了会发生什么我也不知道」
# 的按钮 —— 判据是「卡片能不能进话题」，没有按钮的卡一样能验。
PROBE_TITLE = "【探针】话题能力验证 · 根消息"


def _root_card() -> dict:
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": "grey",
            "title": {"tag": "plain_text", "content": PROBE_TITLE},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": (
                "这条消息是**话题的根**。探针跑完会把它改写成一行收据。\n"
                "下面的测试卡请**不要点** —— 它们不是真授权。"
            )}},
            {"tag": "note", "elements": [{"tag": "plain_text", "content": (
                "tools/probe_topic.py · 验的是私聊能不能开话题"
            )}]},
        ],
    }


class Probe:
    """一步步打，一步步判。

    刻意**不用** ``FeishuSender`` 的公开方法：那些方法把 ``reply_in_thread``
    写死成 ``False``（11.16 的决定，那时没有话题）。探针要验的恰恰是这个
    参数，所以它直接拼请求 —— 但卡片 JSON **复用** sender 里的构造器
    （``_tool_card`` / ``_progress_card`` / ``_settled_card``），否则验的是
    一张我自己编的卡，结论不作数。
    """

    def __init__(self, sender: FeishuSender, to: str, *, dry_run: bool) -> None:
        self.sender = sender
        self.to = to
        self.dry_run = dry_run
        self.base = sender.config.base_url
        self.results: list[tuple[str, bool, str]] = []

    # -- 底层 ------------------------------------------------------------- #
    def _api(
        self, path: str, payload: dict | None, *, method: str = "POST",
        what: str = "",
    ) -> dict:
        """打一发，返回 ``data``（dict）。

        失败抛 :class:`FeishuError` —— **带 code 和 msg**。探针里每个 code
        都是一条结论（9999… 是权限、230xxx 是消息、200830 是卡片结构），
        把它吞掉等于把唯一有用的线索吞掉。
        """
        if self.dry_run:
            print(f"  [dry-run] {method} {path}")
            print(f"            payload={json.dumps(payload, ensure_ascii=False)[:400]}")
            return {"message_id": f"dry_{uuid.uuid4().hex[:8]}",
                    "thread_id": "omt_dryrun", "card_id": "dry_card"}
        raw = self.sender._transport(  # noqa: SLF001 - 探针就是要打裸接口
            f"{self.base}{path}", payload,
            {"Authorization": f"Bearer {self.sender.token()}"},
            self.sender.timeout, method=method,
        )
        try:
            body = json.loads(raw)
        except ValueError as exc:
            raise FeishuError(f"{what}：响应不是 JSON（{exc}）") from exc
        if not isinstance(body, dict):
            raise FeishuError(f"{what}：响应不是对象")
        if body.get("code") not in (0, None):
            raise FeishuError(
                f"{what}：code={body.get('code')} msg={body.get('msg')}"
            )
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    def _reply(
        self, root: str, *, msg_type: str, content: dict, in_thread: bool | None,
        what: str,
    ) -> dict:
        """回复一条消息。``in_thread=None`` = **不传这个字段**（验默认行为）。"""
        payload: dict = {
            "msg_type": msg_type,
            "content": json.dumps(content, ensure_ascii=False),
        }
        if in_thread is not None:
            payload["reply_in_thread"] = in_thread
        return self._api(f"/open-apis/im/v1/messages/{root}/reply", payload,
                         what=what)

    # -- 每一步 ----------------------------------------------------------- #
    def step(self, key: str, title: str, fn) -> dict:
        print(f"\n{key} {title}")
        try:
            data = fn()
        except FeishuError as exc:
            print(f"   RED  {exc}")
            self.results.append((key, False, f"{title}：{exc}"))
            return {}
        except Exception as exc:                      # noqa: BLE001 - 探针要跑完
            print(f"   RED  {type(exc).__name__}: {exc}")
            self.results.append((key, False, f"{title}：{exc}"))
            return {}
        self.results.append((key, True, title))
        return data

    def run(self) -> int:
        print("=" * 78)
        print("飞书话题探针 · 私聊能不能用「话题」当会话容器")
        print(f"  收件人：{self.to[:8]}…（open_id 前 8 位）")
        print(f"  模式：{'dry-run（不发消息）' if self.dry_run else '真跑（会发消息到你飞书）'}")
        print("=" * 78)

        # S1 根消息：一张无按钮的 1.0 卡，作为话题锚点。
        self.step("S1", "发根卡到私聊", lambda: self._s1())
        root = self.root_id
        if not root:
            print("\n根消息都没发出去，后面的都无从谈起。先查凭据/权限。")
            return 1
        print(f"   OK  root={root}")

        # S2 判据一：私聊 reply_in_thread=true 到底给不给 thread_id。
        self.step("S2", "文本回复 + reply_in_thread=true（判据①：私聊能否开话题）",
                  lambda: self._s2(root))
        thread = self.thread_id

        # S3 判据二：授权卡（带按钮）能不能作为话题回复。
        self.step("S3", "授权卡进话题（判据②：interactive 卡）",
                  lambda: self._s3(root))

        # S4 判据三：CardKit 2.0 实体卡能不能进话题 —— 这条最要紧，
        # 因为我们的进度卡全是实体卡，不能进就等于话题方案无法承载进度。
        self.step("S4", "CardKit 实体卡进话题（判据③：{type:card,card_id}）",
                  lambda: self._s4(root))

        # S5 判据四：实体卡在话题里还能不能流式更新。
        self.step("S5", "话题内流式更新实体卡（判据④）", lambda: self._s5())

        # S6 附带：不传 reply_in_thread 时默认行为（决定路由要不要兜底）。
        self.step("S6", "话题内再回复、且**不传** reply_in_thread（看默认归属）",
                  lambda: self._s6(root))

        # S7 判据五：V1.88 的 PATCH 折叠在话题消息上是否生效。
        self.step("S7", "话题内 PATCH 折叠成终态收据（判据⑤）", lambda: self._s7())

        # S8 判据六：能不能按 thread_id 拉全量（审计视图能不能白送）。
        self.step("S8", "按 thread 拉消息列表（判据⑥：审计视图）", lambda: self._s8())

        self._s9_cleanup()
        return self._summary()

    # -- 各步实现 ---------------------------------------------------------- #
    root_id = ""
    thread_id = ""
    card_msg_id = ""
    card_id = ""

    def _s1(self) -> dict:
        # **按形状判 receive_id_type**，别写死 open_id：白名单同时接受
        # ``ou_…``（open_id）和租户级裸 ``user_id``（如 ``2d1b7bec``），
        # 写死会让后一种直接吃 HTTP 400 code=99992351（本次真跑第一发就是
        # 这么红的）。生产路径由 ``FeishuSender._receive_id_type`` 统一判断，
        # 探针复用它 —— 抄一份就会漂移，而漂移的表现是「探针测不出真问题」。
        id_type = self.sender._receive_id_type(self.to)  # noqa: SLF001
        data = self._api(
            f"/open-apis/im/v1/messages?receive_id_type={id_type}",
            {
                "receive_id": self.to,
                "msg_type": "interactive",
                "content": json.dumps(_root_card(), ensure_ascii=False),
            },
            what="发根卡",
        )
        self.root_id = str(data.get("message_id") or "")
        if not self.root_id:
            raise FeishuError("响应里没有 message_id")
        return data

    def _s2(self, root: str) -> dict:
        data = self._reply(
            root, msg_type="text", in_thread=True, what="话题文本回复",
            content={"text": "【探针】这条是想开话题的文本回复。若它出现在话题里、"
                             "且下面还有卡片，说明私聊支持话题。"},
        )
        self.thread_id = str(data.get("thread_id") or "")
        if not self.thread_id:
            raise FeishuError(
                "回复成功但 thread_id 为空 —— 私聊没开话题（或该参数在 p2p 被忽略）"
            )
        print(f"        thread_id={self.thread_id}")
        return data

    def _s3(self, root: str) -> dict:
        card = _tool_card(
            "【探针】授权卡形状（别点）",
            "验证带按钮的 interactive 卡能否作为话题回复。"
            "动作：probe（不是真委派）。",
            "probe-not-a-credential",
        )
        data = self._reply(root, msg_type="interactive", content=card,
                           in_thread=True, what="授权卡进话题")
        self._s3_msg_id = str(data.get("message_id") or "")
        self._same_thread("授权卡", str(data.get("thread_id") or ""))
        return data

    def _s4(self, root: str) -> dict:
        raw = self.sender._transport(  # noqa: SLF001
            f"{self.base}/open-apis/cardkit/v1/cards",
            {
                "type": "card_json",
                "data": json.dumps(_progress_card("【探针】流式进度卡", "probe"),
                                   ensure_ascii=False),
                "uuid": str(uuid.uuid4()),
                "sequence": 1,
            },
            {"Authorization": f"Bearer {self.sender.token()}"},
            self.sender.timeout,
        ) if not self.dry_run else json.dumps(
            {"code": 0, "data": {"card_id": "dry_card"}})
        body = json.loads(raw)
        if body.get("code") not in (0, None):
            raise FeishuError(f"建卡片实体：code={body.get('code')} "
                              f"msg={body.get('msg')}")
        card_id = str((body.get("data") or {}).get("card_id") or "")
        if not card_id:
            raise FeishuError("建卡片实体成功但没回 card_id")
        self.card_id = card_id

        data = self._reply(
            root, msg_type="interactive",
            content={"type": "card", "data": {"card_id": card_id}},
            in_thread=True, what="实体卡进话题",
        )
        self.card_msg_id = str(data.get("message_id") or "")
        self._same_thread("实体卡", str(data.get("thread_id") or ""))
        return data

    def _s5(self) -> dict:
        self._api(
            f"/open-apis/cardkit/v1/cards/{self.card_id}"
            f"/elements/progress_text/content",
            {"uuid": str(uuid.uuid4()),
             "content": "**话题内更新成功。**\n这条文字是第二次写入 —— 说明实体卡"
                        "进了话题之后仍然可以流式更新。",
             "sequence": 2},
            method="PUT", what="话题内流式更新",
        )
        return {}

    def _s6(self, root: str) -> dict:
        data = self._reply(
            root, msg_type="text", in_thread=None, what="默认归属回复",
            content={"text": "【探针】这条**没传** reply_in_thread —— 看它默认进不进同一个话题。"},
        )
        got = str(data.get("thread_id") or "")
        if not got:
            print("        → 默认**不进**话题（thread_id 空）：以后发消息必须显式带参数")
        elif got == self.thread_id:
            print("        → 默认自动进同一话题（thread_id 与 S2 相同）")
        else:
            print(f"        → 进了一个**新**话题 {got}（与 S2 不同！）")
        return data

    def _s7(self) -> dict:
        if not self.card_msg_id and not self.thread_id:
            raise FeishuError("没有可折叠的消息（S3/S4 都失败了）")
        # 优先折叠 S3 那张授权卡 —— 它才是 V1.88 真实要折叠的东西。
        target = self._s3_msg_id or self.card_msg_id
        if not target:
            raise FeishuError("没有可折叠的卡片消息 id")
        self._api(
            f"/open-apis/im/v1/messages/{target}",
            {"content": json.dumps(
                _settled_card("【探针】授权卡", "已折叠 · 话题内 PATCH 生效"),
                ensure_ascii=False)},
            method="PATCH", what="话题内折叠终态",
        )
        return {}

    def _s8(self) -> dict:
        if not self.thread_id:
            raise FeishuError("没有 thread_id（S2 就没开成话题）")
        items = self._api(
            "/open-apis/im/v1/messages"
            f"?container_id_type=thread&container_id={self.thread_id}"
            "&sort_type=ByCreateTimeAsc&page_size=20",
            None, method="GET", what="按话题拉消息",
        )
        got = items.get("items")
        n = len(got) if isinstance(got, list) else 0
        print(f"        → 话题里共 {n} 条消息")
        if n < 2:
            raise FeishuError(f"只拉到 {n} 条 —— 话题容器没把这串消息收进去")
        return items

    def _s9_cleanup(self) -> None:
        """把根卡改写成一行收据。**不撤回**（撤回留墓碑，V1.88 定案过）。"""
        if not self.root_id:
            return
        try:
            self._api(
                f"/open-apis/im/v1/messages/{self.root_id}",
                {"content": json.dumps(_settled_card(
                    "话题探针", "已跑完 · 见上方话题里的测试卡"),
                    ensure_ascii=False)},
                method="PATCH", what="收尾改写根卡",
            )
            print("\n根卡已改写成收据（没撤回，所以不会留墓碑）。")
        except FeishuError as exc:
            print(f"\n根卡收尾失败（不影响结论）：{exc}")

    # -- 判据小工具 -------------------------------------------------------- #
    _s3_msg_id = ""

    def _same_thread(self, what: str, got: str) -> None:
        if not got:
            raise FeishuError(f"{what}回复没有 thread_id —— 没进话题")
        if self.thread_id and got != self.thread_id:
            raise FeishuError(f"{what}进了另一个话题（{got} ≠ {self.thread_id}）")
        print(f"        thread_id={got}")

    def _summary(self) -> int:
        red = [r for r in self.results if not r[1]]
        print()
        print("=" * 78)
        for key, ok, msg in self.results:
            print(f"  {'OK  ' if ok else 'RED '} {key:3s} {msg}")
        print("-" * 78)
        if red:
            print(f"**{len(red)} 项红。话题方案按现有形态不可行 —— 走降级方案：**")
            print("  卡面带委派短号（#3f2a · openmos）+ 进度卡按委派分组，")
            print("  不引入话题，也不用改通道形态。")
            print("红在哪就说明哪一层不支持，别把它写成「差一点就行」。")
        else:
            print("**7 项全绿 —— 私聊话题可用，话题隔离可以进契约。**")
            print("下一步：发送层加 reply_in_thread + 落 thread_id（tasks 加列走 _MIGRATIONS），")
            print("路由按事件的 root_id / thread_id 归属到委派。")
            print("注意：真机走一遍「用户往话题里说话」，确认事件里 root_id 有值 ——")
            print("      这条探针验不了（要人发消息），但它决定路由能不能做。")
        print("=" * 78)
        return 1 if red else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="探飞书私聊的话题能力")
    ap.add_argument("--to", default="", help="收件人 open_id（默认取白名单第一个）")
    ap.add_argument("--dry-run", action="store_true", help="只造请求，不发消息")
    args = ap.parse_args(argv)

    try:
        cfg = load_config()
        cfg.check_ready()
    except ConfigError as exc:
        print(f"配置不完整：{exc}")
        print("（探针要用真实凭据：FEISHU_APP_ID / FEISHU_APP_SECRET / "
              "FEISHU_ALLOWED_USERS，或 ~/.freeagent/feishu.env）")
        return 2

    to = args.to.strip() or sorted(cfg.allowed_users)[0]
    if not to.startswith("ou_"):
        # 白名单也接受租户级裸 user_id，那种情况下面板收不到「话题」之外的东西，
        # 但这里**不拦** —— 探针允许用 user_id 试一次，失败会如实报 code。
        print(f"提示：{to[:6]}… 不是 ou_ 开头，可能是 user_id；失败时先换 open_id 再试。")

    sender = FeishuSender(cfg)
    return Probe(sender, to, dry_run=args.dry_run).run()


if __name__ == "__main__":
    raise SystemExit(main())
