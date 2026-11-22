"""真机验收矩阵的守卫（`docs/真机验收矩阵.md`）。

## 为什么一张 Markdown 表值得配守卫

因为它记的是**唯一一件没有测试能替它保证的事**：「人真的点过没有」。
代码写完有三千多条测试兜着；点没点过只有人知道，而人的记忆会漂 ——
本仓此前把同一句话（「停止按钮仍未验」）抄在六处散文里，改一次要改六处，
于是必然有地方烂掉。收拢成一张表以后，新的失效方式变成「表慢慢变旧」：
有人验完了忘了改、有人删了一格、摘要数字留在三个月前。

四条规矩把这三件事都变成**报红**，而不是靠人复查：

1. **摘要数字由格子重算** —— 数不对就是错。
2. **每个 ✅ 必须有日期与复查落点** —— 没有复查方式的 ✅ 只是让别人
   不好意思怀疑它。
3. **每个 ⬜ 必须有「怎么验」与「为什么不能顺带证明」** —— 后半句挡的是
   那句最常见的合理化：「旁边那格验过了，这格应该一样」。
4. **编号全集冻结**（借 OpenHands `@spec` 的纪律：只增不重排，作废用删除线）
   —— 删一格必须来这份测试里显式划掉，于是它一定出现在 diff 里。

⚠️ 这份测试**不判断格子填得对不对**（那需要人去点），只判断它没有腐坏。
两者的区别很重要：它守的是「这张表还能被信任」，不是「这张表说的事都对」。
"""
from __future__ import annotations

import pathlib
import re

import pytest

DOC = pathlib.Path(__file__).resolve().parent.parent / "docs" / "真机验收矩阵.md"

#: 编号全集。**只增不重排**；作废请用删除线并在此注明，不要复用编号。
KNOWN_IDS = frozenset(
    [f"A{i}" for i in range(1, 15)]      # 表 1：动作 × 通道；A13/A14 本会话内允许此类（V1.138）
    + [f"B{i}" for i in range(1, 6)]     # 表 2：闸门自身行为
    + [f"W{i}" for i in range(1, 6)]     # 表 3：工作台（V1.114）的卡与停止；W5 兜底捞卡（V1.128）
    + [f"D{i}" for i in range(1, 5)]     # 附录：已不在链路上的项目级闸门
)

STATUS = "✅⬜✖➖"
CELL_RE = re.compile(rf"([{STATUS}])\s*`([A-Z]\d+)`")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
SUMMARY_RE = re.compile(
    r"\*\*(主表|附录)\s*(\d+)\s*格：✅\s*(\d+)\s*·\s*⬜\s*(\d+)\s*·\s*✖\s*(\d+)"
    r"\s*·\s*➖\s*(\d+)\*\*"
)


def _text() -> str:
    assert DOC.exists(), f"矩阵不见了：{DOC}"
    return DOC.read_text(encoding="utf-8")


def _section(title: str) -> str:
    """取 ``## <title>`` 到下一个同级标题之间的正文。"""
    text = _text()
    hit = re.search(rf"^## {re.escape(title)}.*?$(.*?)(?=^## |\Z)",
                    text, re.MULTILINE | re.DOTALL)
    assert hit is not None, f"找不到这一节：{title}"
    return hit.group(1)


def _rows(section: str) -> list[list[str]]:
    """把表格行拆成单元格；跳过分隔行。"""
    out: list[list[str]] = []
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if all(set(c) <= set("-: ") for c in cells):
            continue
        out.append(cells)
    return out


def _cells() -> dict[str, str]:
    """编号 → 状态（全文件扫一遍）。"""
    return {m.group(2): m.group(1) for m in CELL_RE.finditer(_text())}


class TestMatrixShape:
    def test_doc_exists(self):
        assert DOC.exists(), "docs/真机验收矩阵.md 是审批链唯一的真源，不能少"

    def test_no_duplicate_cell_ids(self):
        ids = [m.group(2) for m in CELL_RE.finditer(_text())]
        dupes = {i for i in ids if ids.count(i) > 1}
        assert not dupes, (
            f"这些编号出现了不止一次：{sorted(dupes)} —— 一格两个状态，"
            "读的人无从判断以哪个为准")

    def test_known_ids_match_the_doc(self):
        """双向对账：多一个（忘了登记）少一个（悄悄删了）都报红。"""
        got = set(_cells())
        assert got - KNOWN_IDS == set(), (
            f"表里多了没登记的编号：{sorted(got - KNOWN_IDS)} —— 加格子要同时"
            "来这份测试登记，否则下一次有人删掉它时没人会注意到")
        assert KNOWN_IDS - got == set(), (
            f"登记的编号在表里不见了：{sorted(KNOWN_IDS - got)} —— 不许悄悄删格"
            "（作废请用删除线并在测试里注明），因为那正是这张表最容易腐坏的方式")

    def test_status_markers_are_from_the_closed_set(self):
        for sid in _cells():
            assert re.search(rf"[{STATUS}]", sid) is None  # 编号里不该带记号

    def test_summary_counts_are_recomputed(self):
        """摘要那两行是给人看的唯一数字 —— 它必须由格子算出来。"""
        cells = _cells()
        wanted = {"主表": "ABW", "附录": "D"}
        found = {m.group(1): m for m in SUMMARY_RE.finditer(_text())}
        assert set(found) == set(wanted), (
            "摘要行格式变了或丢了一行；格式是 "
            "`**主表 N 格：✅ a · ⬜ b · ✖ c · ➖ d**`")
        for name, prefixes in wanted.items():
            mine = [s for i, s in cells.items() if i[0] in prefixes]
            m = found[name]
            stated = [int(m.group(i)) for i in range(2, 7)]
            actual = [len(mine)] + [mine.count(s) for s in STATUS]
            assert stated == actual, (
                f"{name} 摘要与实际不符：写了 {stated}，数出来 {actual}"
                "（顺序：总 / ✅ / ⬜ / ✖ / ➖）")


class TestEvidenceDiscipline:
    def test_every_verified_cell_has_a_dated_evidence_row(self):
        """✅ 的判据是「有日期 + 能复查」，不是「我记得点过」。"""
        cells = _cells()
        verified = {i for i, s in cells.items() if s == "✅"}
        rows = _rows(_section("已验格子的证据"))
        covered: set[str] = set()
        for cells_row in rows:
            ids = {m.group(1) for m in
                   re.finditer(r"`([A-Z]\d+)`", " ".join(cells_row))}
            if not ids:
                continue
            assert DATE_RE.search(" ".join(cells_row)), (
                f"证据行没有日期：{cells_row} —— 没有日期的证据无法复查，"
                "它与「我记得」没有区别")
            body = " ".join(cells_row[2:]).strip()
            assert len(body) >= 20, (
                f"证据行太短，写不出「去哪儿查」：{cells_row}")
            covered |= ids
        assert verified - covered == set(), (
            f"这些格子标了 ✅ 却没有证据行：{sorted(verified - covered)}")

    def test_no_blank_cell_is_listed_as_verified(self):
        """⬜ 混进证据表 = 把「没验」写成「验过」，比不记更坏。"""
        cells = _cells()
        for cells_row in _rows(_section("已验格子的证据")):
            for m in re.finditer(r"`([A-Z]\d+)`", " ".join(cells_row)):
                assert cells[m.group(1)] == "✅", (
                    f"{m.group(1)} 在证据表里，但状态是 "
                    f"{cells[m.group(1)]} —— 要么补验，要么把它从证据表删掉")


class TestBlankCellsHaveARecipe:
    def test_every_blank_or_missing_cell_has_a_how_to_verify_row(self):
        """空格子必须写「点什么、看到什么算过」——否则它永远空着。

        第二个非空要求是「为什么不能顺带证明」：这一栏挡的是那句最常见的
        合理化（「旁边那格验过了，这格应该一样」），而那正是本仓
        「允许/拒绝验过 ≠ 停止按钮验过」踩出来的教训。
        """
        cells = _cells()
        blank = {i for i, s in cells.items() if s in "⬜✖"}
        rows = _rows(_section("空格子怎么验"))
        covered: set[str] = set()
        for cells_row in rows:
            ids = {m.group(1) for m in
                   re.finditer(r"`([A-Z]\d+)`", " ".join(cells_row))}
            if not ids:
                continue
            assert len(cells_row) >= 3, f"这一行少了「为什么」那一栏：{cells_row}"
            assert len(cells_row[1]) >= 10 and len(cells_row[2]) >= 10, (
                f"「怎么验」或「为什么不能顺带证明」写得太短：{cells_row}")
            covered |= ids
        # ✖ 的「怎么验」= 它缺在哪（附录那条），不在这张表里
        unimplemented = {i for i, s in cells.items() if s == "✖"}
        assert (blank - unimplemented) - covered == set(), (
            f"这些空格子没有「怎么验」：{sorted((blank - unimplemented) - covered)}")

    def test_unimplemented_cells_point_at_the_gap(self):
        """✖ 与 ⬜ 是两件事：一个是「没做」，一个是「没验」。

        混着写会让「没挡」被排进验收计划 —— 排进去也只会验出「果然没挡」，
        然后被当成一条别人的 bug。所以 ✖ 必须写明缺在哪。
        """
        cells = _cells()
        text = _text()
        for sid, state in cells.items():
            if state != "✖":
                continue
            pattern = rf"`{sid}`[^\n]*未实现"
            assert re.search(pattern, text), (
                f"{sid} 标了 ✖（未实现）却没写它缺在哪；"
                "「没做」和「没验」必须一眼分得开")
