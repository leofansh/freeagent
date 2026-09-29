"""密钥文件引擎：``KEY=VALUE`` 文本文件的读 / 洗 / 写 / 掩码。

这个模块**不含任何产品知识** —— 不知道飞书、不知道 DeepSeek。
它只做一件事：把「一份只允许固定几个键的密钥文件」这件事做对。
产品侧各自声明自己的文件名与白名单（见 ``feishu.secret_store``、
``llm_env``），逻辑只有这一份。

## 为什么不各写一份

清洗粘连行、识别 ``***`` 占位符、容忍 BOM、保权限原子写 —— 这些都是
**踩过坑才写出来的**，而它们和「哪个产品的哪个键」毫无关系。复制一份的
代价不是多几行代码，而是**两份实现从此各自演化**：一边修了「值里恰好含
``API_KEY=`` 片段被误切」，另一边没有，那个 bug 就在那里等着。

## 三条硬约束（少一条就出事）

1. **变量名必须是白名单**，不接受自定义 key。
   往 ``PYTHONPATH`` / ``LD_PRELOAD`` / ``EDITOR`` 里塞一个路径，下次起
   子进程就在 ``main()`` **之前**加载攻击者的代码；而
   ``^[A-Za-z_]\\w*$`` 这种正则**挡不住这些** —— 它们本来就合规。
   所以必须「只认列出的那几个名字」，黑名单是不够的（Hermes 用的就是
   黑名单，那是它更弱的地方）。

2. **读盘必须容忍 BOM**。PowerShell 写出的 ``.env`` 带 BOM，用 ``utf-8``
   读会让第一个变量名变成 ``\\ufeffFEISHU_APP_ID`` 而**静默失效** ——
   配置看起来写了，用它的功能却当没有。

3. **每次读都清洗**。两种已知损坏形态必须自愈：两对 ``KEY=VALUE``
   粘在一行（中间少了换行）、以及被打断留下的 ``KEY=***``。
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Iterable
from pathlib import Path

__all__ = [
    "SecretFile",
    "UnknownKeyError",
    "mask_secret",
    "PLACEHOLDER_VALUES",
    "reject_non_ascii",
]

#: 变量名形状校验。**它只是防呆，不是安全边界** —— ``PYTHONPATH`` 完全合规，
#: 真正的边界是 :class:`SecretFile` 的白名单。
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: 被打断的写入会留下这个。算「未配置」，不能让界面显示成「已配置」。
PLACEHOLDER_VALUES = frozenset({"", "***", "***REDACTED***", "<redacted>"})


class UnknownKeyError(ValueError):
    """要写的变量名不在白名单里。"""


def mask_secret(value: str) -> str:
    """掩码：头 4 + 尾 4（够确认「是那个值」），其余不露。

    刻意不显示长度 —— 长度本身也是信息（有些密钥是定长的），而这里没有
    非要报长度的理由。

    短于 8 位时头尾会重叠、等于全露，所以退化成 ``****``。
    """
    if not value:
        return ""
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}…{value[-4:]}"


def reject_non_ascii(key: str, value: str) -> str:
    """值里有非 ASCII 就**报错**，而不是照发。

    抄 Hermes（``_check_non_ascii_credential``）的一条：凭据里的非 ASCII
    几乎**总是粘贴事故** —— 全角引号、中文输入法带出的字符、零宽字符、
    不换行空格。它们肉眼完全看不出，而服务端只会回一个 401，
    于是用户会以为是 Key 过期了，反复重贴同一个错的值。

    报错要说清是哪一行出的问题，不然用户不知道要改哪个。
    """
    bad = sorted({c for c in value if ord(c) > 127})
    if bad:
        shown = "、".join(repr(c) for c in bad[:5])
        raise ValueError(
            f"{key} 里有非 ASCII 字符：{shown}。"
            "这几乎总是粘贴时混进来的（全角引号、输入法字符、零宽字符），"
            "肉眼分不出但服务端不认。请重新复制粘贴。"
        )
    return value


class SecretFile:
    """一份「只允许固定几个键」的密钥文件。

    :param path: 文件位置
    :param allowed_keys: **白名单**。只有这些键会被读出、也只有这些能被写。
    :param ascii_only: 值里出现非 ASCII 就拒绝（见 :func:`reject_non_ascii`）

    ``read()`` 读不到文件时返回空字典 —— 「没配」不是错误，要让界面能显示
    「未配置」而不是抛异常。
    """

    def __init__(
        self,
        path: Path,
        allowed_keys: Iterable[str],
        *,
        ascii_only: bool = True,
    ) -> None:
        self.path = path
        self.allowed_keys = frozenset(allowed_keys)
        self.ascii_only = ascii_only

    # -- 读 ---------------------------------------------------------------- #
    def read(self) -> dict[str, str]:
        try:
            raw = self.path.read_text(encoding="utf-8-sig", errors="replace")
        except (OSError, UnicodeDecodeError):
            return {}
        return self._parse(raw.splitlines())

    # -- 写 ---------------------------------------------------------------- #
    def write(self, values: dict[str, str]) -> Path:
        """写入。**只接受白名单里的 key**。

        未知 key 抛 :class:`UnknownKeyError` —— 不静默忽略。静默忽略的话，
        调用方以为写成功了，实际没写，而界面上会显示「已保存」。
        """
        unknown = sorted(set(values) - self.allowed_keys)
        if unknown:
            raise UnknownKeyError(
                f"不允许写这些变量：{', '.join(unknown)}。"
                f"只支持 {', '.join(sorted(self.allowed_keys))}。"
            )
        for key, value in values.items():
            if not _NAME_RE.match(key):       # 双重保险：白名单之外的形状也挡
                raise UnknownKeyError(f"变量名不合法：{key!r}")
            if self.ascii_only and value:
                reject_non_ascii(key, value)

        self.path.parent.mkdir(parents=True, exist_ok=True)

        # 先读现有内容（未提及的键原样保留），再覆盖要改的。
        try:
            existing = self.path.read_text(encoding="utf-8-sig", errors="replace")
        except (OSError, UnicodeDecodeError):
            existing = ""
        lines = self._sanitize(existing.splitlines())

        # 区分三件事：**要改成 X**、**要删掉**（传了空串）、**没提到**。
        # 踩过的坑：原先用 `if v != ""` 把空串一起排除，于是「清空」退化成了
        # 「没提到」——旧值原样留着，界面显示「已保存」但配置根本没删。
        touched = set(values)
        wanted = {k: v for k, v in values.items() if v != ""}
        deleted = touched - set(wanted)

        merged: list[str] = []
        seen: set[str] = set()
        for line in lines:
            if "=" not in line or line.startswith("#"):
                merged.append(line)
                continue
            key = line.partition("=")[0].strip()
            if key in deleted:
                if key not in seen:
                    seen.add(key)        # 记下「已处理」，避免粘连行里再写回来
                continue                   # 删掉这一行
            if key not in wanted:
                merged.append(line)         # 没提到的，原样留着
                continue
            if key in seen:
                continue                    # 粘连清洗后可能出现重复，只留第一次
            merged.append(f"{key}={wanted[key]}")
            seen.add(key)
        for key, value in wanted.items():
            if key not in seen:
                merged.append(f"{key}={value}")
                seen.add(key)
        if merged and not merged[-1].endswith("\n"):
            merged.append("")
        body = "\n".join(merged).strip("\n") + "\n"

        # 原子写 + 保留原权限（Hermes 踩过：mkstemp 出的文件是 0600，
        # 直接 replace 会把用户原有的宽松权限改掉）。
        original_mode = None
        try:
            original_mode = self.path.stat().st_mode
        except OSError:
            pass
        fd, tmp = tempfile.mkstemp(
            dir=str(self.path.parent), suffix=".tmp", prefix="secrets_"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
            if original_mode is not None:
                os.chmod(tmp, original_mode)
            os.replace(tmp, self.path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return self.path

    # -- 内部 -------------------------------------------------------------- #
    def _sanitize(self, lines: list[str]) -> list[str]:
        """把已知的两种损坏形态修好。返回清洗后的行。

        - 粘连：``A=1B=2`` → ``A=1`` / ``B=2``
        - 占位符：``A=***`` → 丢掉这一行（视为未配置）

        拆分**只在已知变量名上**做。不限定名字的话，值里恰好含
        ``LM_API_KEY=`` 这种片段时会被切开，文件直接损坏（Hermes 也踩过）。
        """
        out: list[str] = []
        names = sorted(self.allowed_keys, key=len, reverse=True)
        for raw in lines:
            line = raw.rstrip("\r\n")
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                out.append(line)
                continue

            # 找出所有「已知名=」的位置。用「长的名字优先」排序，避免
            # GLM_API_KEY= 被 LM_API_KEY= 切开这种后缀包含造成的损坏。
            positions: list[tuple[int, str]] = []
            for name in names:
                needle = name + "="
                start = 0
                while True:
                    idx = stripped.find(needle, start)
                    if idx < 0:
                        break
                    positions.append((idx, name))
                    start = idx + len(needle)
            if not positions:
                out.append(line)
                continue
            positions.sort()
            # 丢掉被别的匹配完全包住的（后缀包含）
            kept: list[tuple[int, str]] = []
            for pos in positions:
                if any(
                    o[0] < pos[0]
                    and o[0] + len(o[1]) + 1 >= pos[0] + len(pos[1]) + 1
                    and o != pos
                    for o in positions
                ):
                    continue
                kept.append(pos)
            kept.sort()
            for i, (idx, name) in enumerate(kept):
                end = kept[i + 1][0] if i + 1 < len(kept) else len(stripped)
                out.append(f"{name}={stripped[idx + len(name) + 1:end]}")
        return out

    def _parse(self, lines: list[str]) -> dict[str, str]:
        """行 → 键值。**占位符当未配置**。"""
        result: dict[str, str] = {}
        for line in self._sanitize(lines):
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key not in self.allowed_keys:
                continue                  # 别人写的变量原样留着，但我们不认
            value = value.strip()
            if value in PLACEHOLDER_VALUES:
                continue                  # 被打断的写入：算没配
            result[key] = value
        return result
