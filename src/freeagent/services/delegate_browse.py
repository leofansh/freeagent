"""可授权根的目录浏览（设计文档 12.7.1.1 的服务层）。

「列出来 ≠ 已授权」：这个模块只回答「这个范围内有哪些目录、你点的那个
到底是不是范围内的目录」，**一次都不写 config.json** —— 授权仍由 Web 端点
走 POST ``allow_pick`` 完成。

五条实现约束与本模块的对应（每条都防一种具体的绕过手法）：

1. **只列目录不列文件** —— 委派的准入单位是「项目目录」，列文件只会
   诱导往文件级授权上漂（那需要另一套校验，不做）。
2. **绝对路径由服务端拼** —— 客户端只传「第几个根 + 根内相对路径」。
   收到绝对路径直接当非法（:func:`_reject_absolute`）：让客户端带绝对
   路径，等于让浏览接口变成任意路径读取器（路径里塞 ``..``、盘符、
   UNC 都是从这里进来的）。
3. **resolve() + relative_to() 验根内** —— 不用字符串前缀比较：前缀比对
   会被 ``C:\\allowed_evil`` 这种「同前缀不同目录」骗过。resolve 把
   ``..``、符号链接、8.3 短名全部归一后再比，:meth:`relative_to` 只
   允许「在根里」或「就是根本身」。
4. **符号链接连列表都不给出现** —— 链接 resolve 之后可能指向根外，若
   列出它、用户一点，校验再拒绝，用户看到的是「我明明看见了这个目录，
   却说不允许」（文案还答非所问）。所以列目录时就跳过，选择集 = 解析
   后仍在根内的真实目录。
5. **授权动作不在本模块** —— 浏览端点是 GET 只读；写入统一走
   ``endpoints_delegate_projects.allow_pick``（POST），复用同一套白名单
   校验。GET 别名不注册，防止浏览接口被当成授权接口用。

失败方向一律 fail-closed：任何判不清的情况（索引越界、根不存在、
选中的不是目录、指针像链接）都拒绝，且错误文案说清**下一步出路**
（因为所有异常都是 :class:`FreeAgentError` 子类，HTTP 层会原样带回去）。
"""

from __future__ import annotations

from pathlib import Path, PureWindowsPath
from typing import Any, Sequence

from ..domain import ValidationError

__all__ = ["enabled", "list_dirs", "pick_path", "pick_from_body"]


def enabled(roots: Sequence[str]) -> bool:
    """浏览功能开没开。**空列表就是关** —— 默认朝关（12.7.1.1）。"""
    return bool(roots)


def _root_at(roots: Sequence[str], index: object) -> Path:
    """把「第几个根」落成已 resolve 的目录，判不清就拒绝。"""
    # bool 是 int 的子类：True 会静默当成第 1 个根，这种「意外能用」
    # 的输入在安全边界上必须挡掉。
    if not isinstance(index, int) or isinstance(index, bool):
        raise ValidationError(f"根必须是第几个根的编号（整数），收到：{index!r}")
    if not 0 <= index < len(roots):
        raise ValidationError(
            f"根编号超出范围：{index}（共 {len(roots)} 个可授权根）"
        )
    root = Path(roots[index]).resolve()
    if not root.is_dir():
        # 根本身没了/不是目录：fail-closed，不退到「当前目录」之类兜底。
        raise ValidationError(f"可授权根不存在或不是目录：{roots[index]}")
    return root


def _reject_absolute(rel: str) -> None:
    """约束 2：客户端只许给根内**相对**路径，绝对/带盘符的都当非法。

    用 :class:`PureWindowsPath` 额外查一遍，是因为跨平台下
    ``C:\\x`` 在 POSIX 的 ``Path`` 眼里不是绝对路径 —— 宁可多拒一个
    畸形输入，也不能让盘符路径溜过去再靠 resolve 兜底。
    """
    if Path(rel).is_absolute() or PureWindowsPath(rel).drive or PureWindowsPath(rel).root:
        raise ValidationError(
            f"只接受根内的相对路径，不接受绝对路径：{rel!r}"
            "（相对路径从可授权根出发，由服务端拼接）"
        )


def _inside(root: Path, rel: str) -> Path:
    """约束 3：拼接 → resolve → relative_to 验仍在根内。"""
    _reject_absolute(rel)
    target = (root / rel).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise ValidationError(
            f"不在可授权根内：{rel!r}（根：{root}）"
            " —— 请从浏览结果里逐级进入，不要跨目录跳转"
        ) from None
    return target


def list_dirs(roots: Sequence[str], index: int, rel: str) -> list[str]:
    """列出根内某目录的**直接子目录名**（不含文件，约束 1）。

    符号链接连列都不列（约束 4，理由见模块 docstring）。排序按
    casefold，中英文混排时同机两次看到的顺序一致。
    """
    root = _root_at(roots, index)
    here = _inside(root, rel)
    try:
        children = list(here.iterdir())
    except OSError as exc:
        raise ValidationError(
            f"读不了目录 {here}：{exc.strerror or exc}"
        ) from exc
    names = sorted(
        (c.name for c in children if not c.is_symlink() and c.is_dir()),
        key=str.casefold,
    )
    return names


def pick_path(roots: Sequence[str], index: int, rel: str) -> str:
    """把用户点选的目录解析成**规范绝对路径**（写白名单前的最后校验）。

    比 :func:`list_dirs` 多三道（都 fail-closed）：

    - 选中项自身是符号链接 ⇒ 拒（列表里本就不出现，出现说明绕过了界面
      或 TOCTOU 竞态；链出去的链接是把范围开到根外的标准手法）；
    - resolve 后不存在 ⇒ 拒（白名单里放一条不存在的路径，会让委派
      在更深的地方以更难懂的方式失败）；
    - resolve 后不是目录 ⇒ 拒（准入单位是项目目录）。
    """
    root = _root_at(roots, index)
    _reject_absolute(rel)
    picked = root / rel
    if picked.is_symlink():
        raise ValidationError(
            f"选中的目录是符号链接，不接受：{rel!r}"
            "（链接可能指向可授权根之外）"
        )
    target = (picked).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise ValidationError(
            f"不在可授权根内：{rel!r}（根：{root}）"
        ) from None
    if not target.is_dir():
        raise ValidationError(f"选中的不是目录或已不存在：{rel!r}")
    return str(target)


def pick_from_body(roots: Sequence[str], pick: Any) -> str:
    """POST ``allow_pick`` 载荷 → 规范绝对路径（端点侧的入口）。

    载荷形状校验放在这里而不是端点里：它和 :func:`list_dirs` /
    :func:`pick_path` 是**同一道边界**，拆到两处写就会漂成「GET 严、
    POST 松」—— 而 POST 那头才是能写进白名单的（约束 5 的本意）。
    """
    if not isinstance(pick, dict):
        raise ValidationError("allow_pick 必须是 {root, path} 对象")
    unknown = sorted(set(pick) - {"root", "path"})
    if unknown:
        raise ValidationError(f"allow_pick 不认识这些字段：{', '.join(unknown)}")
    if not roots:
        # 浏览默认关：没圈范围时点选无从谈起，报错要说清是**没配置**
        # 而不是「路径非法」—— 否则用户会去查自己发的路径。
        raise ValidationError(
            "「可授权根」delegate.browse_roots 未配置（默认空 = 浏览关闭），"
            "没有可浏览选目录的范围"
        )
    rel = pick.get("path") or ""
    if not isinstance(rel, str):
        raise ValidationError("allow_pick.path 必须是字符串")
    return pick_path(roots, pick.get("root", 0), rel)
