"""配对码：治「第一次装时手里没有 ``open_id``」的死循环。

## 问题

白名单失败关闭（设计文档 11.9 边界 1）是对的：``FEISHU_ALLOWED_USERS``
为空就拒绝启动。但它把用户卡在死循环里 —— **要拿到 ``open_id`` 才能启动，
而拿到它的唯一正常途径是飞书事件，事件又只有已经在跑的桥接才会收到。**

于是第一次装的人只能去翻飞书开发者后台的日志抠出那串 id，再手抄进配置。

## 怎么解（而**不**削弱边界 1）

陌生**私聊**里 bot 回一条带配对码的卡片，用户把码粘回终端：

```text
> /pair 7KQF-2M9D
```

配对解决的是「**怎么拿到 open_id**」，**不是**「拿不到也放行」：

- 白名单判定一个字都没改，仍在 :meth:`ChannelService.handle` 的第一行
- 白名单为空仍然**拒绝启动**（``feishu/config.py::check_ready``）
- 配对码**不进**白名单、不产生任何命令执行、不过任何闸门
- 群里**不发**配对卡（群里发卡等于向全群确认「这里有个 bot」）

## 为什么短 TTL + 一次性

配对码是「谁能指挥本机」的门票，而它躺在**陌生人的聊天窗口**里。
所以：默认 10 分钟、用过即作废、**只存哈希**。

「只存哈希」的理由不是「显得更安全」，而是：这张表没有任何查询需求
（校验就是「拿码算哈希比对」），而明文落库等于**多一份可被读走的凭据**。

## 三种失败必须给三句可区分的话

「码不对」是一个死胡同：用户不知道是打错、过期、还是已经用过了。
而这三种的下一步完全不同 —— 重打 / 重新生成 / 重新私聊bot。
"""

from __future__ import annotations

import datetime
import hashlib
import secrets
from dataclasses import dataclass

__all__ = [
    "PairingStore",
    "PairingCode",
    "PairingError",
    "new_pairing_code",
    "PAIRING_TTL_SECONDS",
]

#: 配对码默认有效期。刻意短：它躺在陌生人的窗口里，
#: 而它的作用是「让对方证明自己知道终端上发生了什么」，不需要留着。
PAIRING_TTL_SECONDS = 600


def new_pairing_code() -> str:
    """生成一个**不可猜**的配对码。

    用 :mod:`secrets`（CSPRNG）而不是 ``uuid4().hex``：
    配对码的用途就是**授权**—— 拿到它并说出它，就等于证明自己知道终端上
    发生了什么。虽然后面还要比对哈希、不给哈希原值，但生成环节没理由
    用可预测的源。

    分组显示（``7KQF-2M9D``）纯粹为了**能被人抄下来**：
    一串 24 位无分隔字符既容易看错，也没法口述。
    """
    raw = secrets.token_urlsafe(9)  # 12 个字符，熵足够
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:12]}"


def _hash(code: str) -> str:
    """配对码 → 存进库的那一串。

    用 :func:`hmac.compare_digest` 比对（下面 :meth:`PairingStore.consume`
    里有），所以哈希不需要抗碰撞，只需要**不可逆** —— sha256 足够。
    """
    return hashlib.sha256(code.strip().encode("utf-8")).hexdigest()


class PairingError(Exception):
    """配对失败。``reason`` 是给用户看的一句话，必须可区分。"""


@dataclass(frozen=True, slots=True)
class PairingCode:
    """一条已签发的配对码。

    ⚠️ **这里有明文** —— 所以这个对象**只**在「刚签发、还没落库」那一瞬
    存在，签发完立刻丢掉。落库的只有 :attr:`code_hash`。
    """

    code: str
    open_id: str
    expires_at: datetime.datetime


class PairingStore:
    """配对码的读写。**不缓存** —— 桥接与终端是两个进程，随时会写。"""

    def __init__(self, conn, clock=None) -> None:
        self._conn = conn
        self._clock = clock or datetime.datetime.now

    # -- 签发 --------------------------------------------------------------- #
    def issue(
        self, open_id: str, *, ttl_seconds: int = PAIRING_TTL_SECONDS
    ) -> PairingCode:
        """给 ``open_id`` 签发一个配对码，返回**明文**（只此一次）。

        刻意**不返回「去查表拿码」的能力**：明文只在签发这一刻存在，
        落库的是哈希。丢了就重新签发一个 —— 它本来就是短 TTL 的一次性码。
        """
        open_id = open_id.strip()
        if not open_id:
            raise PairingError("没有对方身份，签不了码")
        code = new_pairing_code()
        now = self._clock()
        expires = now + datetime.timedelta(seconds=ttl_seconds)
        self._conn.execute(
            "INSERT INTO pairing_codes (code_hash, open_id, created_at, expires_at)"
            " VALUES (?, ?, ?, ?)",
            (_hash(code), open_id, now.isoformat(), expires.isoformat()),
        )
        self._conn.commit()
        return PairingCode(code=code, open_id=open_id, expires_at=expires)

    # -- 消费 --------------------------------------------------------------- #
    def consume(self, code: str) -> str:
        """核销一个配对码，返回它对应的 ``open_id``。

        **一次性**：用过即作废（写 ``used_at``），第二次来一律拒绝。

        三种失败各给一句**可区分**的话（见模块 docstring）：不存在 /
        已过期 / 已用过。调用方把这句话原样回给用户。
        """
        raw = (code or "").strip()
        if not raw:
            raise PairingError("没给码")

        row = self._conn.execute(
            "SELECT open_id, expires_at, used_at FROM pairing_codes WHERE code_hash = ?",
            (_hash(raw),),
        ).fetchone()
        if row is None:
            raise PairingError(
                "这个码不认 —— 重新给bot 发一条私聊，它会发一张带新码的卡"
            )
        open_id, expires_at, used_at = row

        # 顺序要紧：先看「用过」再看「过期」。一个**又过期又用过**的码，
        # 若先报过期，用户会以为「等一会儿就行」，而它其实早就废了。
        if used_at:
            raise PairingError(
                f"这个码已经用过了（{used_at}）—— 配对码只能用一次。"
                "重新给 bot 发一条私聊拿新码"
            )

        now = self._clock()
        try:
            expires = datetime.datetime.fromisoformat(expires_at)
        except ValueError as exc:  # pragma: no cover - 库坏了
            raise PairingError("这个码的记录读不出来，生成库出问题了") from exc
        if now > expires:
            raise PairingError(
                f"这个码过期了（{expires:%H:%M:%S} 过期）—— "
                "重新给 bot 发一条私聊拿新码"
            )

        self._conn.execute(
            "UPDATE pairing_codes SET used_at = ? WHERE code_hash = ?",
            (now.isoformat(), _hash(raw)),
        )
        self._conn.commit()
        return str(open_id)

    # -- 清扫 --------------------------------------------------------------- #
    def purge_expired(self, *, keep_used_seconds: int = 86400) -> int:
        """清掉「早就过期」与「早就用过」的码，返回清掉几条。

        为什么连**用过的**也清：它们已经没有用途，留着只是让表无限涨。
        保留一天是为了「刚配完就查为什么成功」这件事还有据可查。

        **刻意不自动清未过期的** —— 那些是还能用的凭证，
        被清扫等于让正在配对的人莫名失败。
        """
        now = self._clock()
        used_cut = (now - datetime.timedelta(seconds=keep_used_seconds)).isoformat()
        cur = self._conn.execute(
            "DELETE FROM pairing_codes"
            " WHERE expires_at < ? OR (used_at IS NOT NULL AND used_at < ?)",
            (now.isoformat(), used_cut),
        )
        self._conn.commit()
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0