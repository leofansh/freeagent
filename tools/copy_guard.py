"""文案守卫：窄规则扫描 + 基线棘轮（V1.94 / 设计文档 12.12）。

## 为什么需要它

V1.91 那次在 ``_HELP`` 里**手工**找出 6 处 ``**`` —— 飞书 ``send_text`` 是
纯文本，星号会原样印给用户。人工找到了那 6 处，找不到第 7 处；而下一次
是谁写下第 7 处都不知道。

## 形状：语法守卫 + 基线棘轮（借自 openwork 的 ui-guardrails）

* 规则必须**窄且可静态判定**：只认「纯文本出口的字面量里出现了 markdown
  记号」。判断不了的一律不判 —— 一个会误报的守卫，两周内就会被人关掉。
* 基线按「规则 + 文件 + 归一化见证串的哈希」记次数，**不看行号**：
  挪代码不该要求刷基线。
* **只准变少**：删掉违规永远不用刷基线；新增、复制到新文件、次数变多 → 红。
* **存量不是批准。** 基线里有一条，意思是「这条先欠着」，不是「这样写可以」。

## 用法

    python tools/copy_guard.py                  # 检查（CI / 测试里跑这个）
    python tools/copy_guard.py --update-baseline # 只用于「删干净了」之后收账

退出码：0 = 没有新增；1 = 有新增，或基线里没有的违规。
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

#: 纯文本出口的函数名。``_say`` 是 CLI/飞书回复的唯一出口，
#: ``send_text`` 是飞书文本消息的发送入口 —— 两者都不解析 markdown。
PLAIN_TEXT_CALLS = frozenset({"_say", "send_text"})

#: 这些记号在纯文本里会**原样印出来**，用户看到的是字面量。
MARKDOWN_MARKS = ("**", "```", "](")

RULES = {
    "plain-text-markdown": "纯文本出口（_say / send_text）里出现 markdown 记号",
    "plain-text-newline": "plain_text 组件的正文里有换行",
}

#: 基线文件与源码根。两个都能被 CLI 覆盖（测试用临时目录）。
BASELINE_NAME = "copy-baseline.json"


@dataclass(frozen=True)
class Violation:
    rule: str
    file: str            # 相对仓库根，正斜杠
    line: int
    sample: str          # 归一化后截断，给人和基线看
    witness: str         # sample 的哈希，基线按它记账（不含行号）


def _normalize(text: str) -> str:
    """压掉所有空白差异 —— 重新缩进、换行重排都不该改变见证串。"""
    return " ".join(text.split())


def _witness(text: str) -> str:
    return hashlib.sha1(_normalize(text).encode("utf-8")).hexdigest()[:12]


def _plain_text_bodies(tree: ast.AST) -> list[tuple[int, str]]:
    """``{"tag": "plain_text", "content": "..."}`` 里的正文。"""
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        tag = None
        body = None
        for key, value in zip(node.keys, node.values):
            if not isinstance(key, ast.Constant):
                continue
            if key.value == "tag" and isinstance(value, ast.Constant):
                tag = value.value
            elif key.value == "content" and isinstance(value, ast.Constant):
                body = value.value
        if tag == "plain_text" and isinstance(body, str):
            out.append((node.lineno, body))
    return out


def _plain_text_arguments(tree: ast.AST) -> list[tuple[int, str]]:
    """``self._say("...")`` / ``send_text("...")`` 的字面量实参。"""
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if name not in PLAIN_TEXT_CALLS:
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                out.append((node.lineno, arg.value))
    return out


def scan_file(path: Path, root: Path) -> list[Violation]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []                     # 读不了的文件不猜：守卫不负责判它
    rel = path.relative_to(root).as_posix()
    found: list[Violation] = []
    for line, text in _plain_text_arguments(tree):
        if any(mark in text for mark in MARKDOWN_MARKS):
            found.append(Violation(
                "plain-text-markdown", rel, line,
                _normalize(text)[:60], _witness(text),
            ))
    for line, text in _plain_text_bodies(tree):
        if "\n" in text:
            found.append(Violation(
                "plain-text-newline", rel, line,
                _normalize(text)[:60], _witness(text),
            ))
    return found


def scan(root: Path, package: Path | None = None) -> list[Violation]:
    """扫一遍 ``package``（默认 ``root/src/freeagent``）下的所有 .py。"""
    target = package or (root / "src" / "freeagent")
    out: list[Violation] = []
    for path in sorted(target.rglob("*.py")):
        out.extend(scan_file(path, root))
    return out


def counts(violations: list[Violation]) -> dict[tuple[str, str, str], int]:
    """按「规则 + 文件 + 见证串」记账。**键里没有行号** —— 那是刻意的。"""
    out: dict[tuple[str, str, str], int] = {}
    for item in violations:
        key = (item.rule, item.file, item.witness)
        out[key] = out.get(key, 0) + 1
    return out


def load_baseline(path: Path) -> dict[tuple[str, str, str], int]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        (entry["rule"], entry["file"], entry["witness"]): int(entry["count"])
        for entry in data.get("entries", [])
    }


def dump_baseline(violations: list[Violation]) -> str:
    tally = counts(violations)
    samples: dict[tuple[str, str, str], str] = {}
    for item in violations:
        samples.setdefault((item.rule, item.file, item.witness), item.sample)
    entries = [
        {
            "rule": rule,
            "file": file,
            "witness": witness,
            "count": count,
            "sample": samples.get((rule, file, witness), ""),
        }
        for (rule, file, witness), count in sorted(tally.items())
    ]
    return json.dumps(
        {"version": 1, "entries": entries}, ensure_ascii=False, indent=2
    ) + "\n"


def compare(
    violations: list[Violation], baseline: dict[tuple[str, str, str], int]
) -> tuple[list[Violation], list[tuple[str, str, str]], int]:
    """返回值：新增的违规、该收账的基线条目、变少的总数。

    「新增」的定义：基线里没有这一条，**或者**这次出现的次数比基线多。
    后者是关键 —— 否则在同一个文件里再复制一份同样的违规就能绕过。
    """
    tally = counts(violations)
    fresh: list[Violation] = []
    seen: dict[tuple[str, str, str], int] = {}
    for item in violations:
        key = (item.rule, item.file, item.witness)
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > baseline.get(key, 0):
            fresh.append(item)
    stale = [key for key in baseline if key not in tally]
    removed = sum(baseline.values()) - sum(tally.values())
    return fresh, stale, removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="文案守卫（窄规则 + 基线棘轮）")
    parser.add_argument("--root", default=".", help="仓库根目录")
    parser.add_argument("--update-baseline", action="store_true")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    baseline_path = root / "tools" / BASELINE_NAME
    violations = scan(root)

    if args.update_baseline:
        before = load_baseline(baseline_path)
        fresh, _stale, _removed = compare(violations, before)
        baseline_path.write_text(dump_baseline(violations), encoding="utf-8")
        print(f"基线已写入 {baseline_path}（{len(counts(violations))} 条账）")
        for item in fresh:
            print(f"  + {item.file}:{item.line}  {item.rule}  {item.sample}")
        return 0

    baseline = load_baseline(baseline_path)
    fresh, stale, removed = compare(violations, baseline)
    for item in fresh:
        print(f"{item.file}:{item.line}  {item.rule}  {item.sample}")
    for rule, file, witness in stale:
        print(f"（基线里这条已经修掉了，可以 --update-baseline 收账）"
              f"  {rule}  {file}  {witness}")
    if fresh:
        print(
            f"\n{len(fresh)} 处新增文案违规。存量是欠账，不是批准；"
            f"也不要把基线刷一遍来放行新写的。"
        )
        return 1
    if removed:
        print(f"违规比基线少 {removed} 处（变少不用刷基线）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
