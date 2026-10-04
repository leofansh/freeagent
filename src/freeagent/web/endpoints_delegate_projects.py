"""委派白名单：只读查询（GET）与**授权动作**（POST）。

设计文档 12.7.1。

## 为什么拆成两条

查是纯观测、零风险，可以随手做；授权不是。所以两条**长得不一样**——
否则「点一下刷新列表」和「点一下授予权限」就分不清了。

## 为什么授权必须是显式动作（安全硬约束）

**列表里出现一个项目 ≠ 授予权限。** 否则「在 OpenCode 里加一个项目」等于
「悄悄授权 FreeAgent 去改它」——而这种授权**不可审计**：事后没人说得清
它为什么能改那个目录。

所以路径取自 OpenCode（不让你手抄绝对路径），但**写进白名单必须你按一下**。

## 为什么只走 POST、不注册 GET 别名

理由同 12.7：浏览器预取、爬虫、`<img src>` 都会自动发 GET，能改权限的
操作绝不能挂在这种方法上。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..app import App
from ..config import load_config, save_config
from ..domain import FreeAgentError
from ..services.opencode_projects import list_projects, resolve
from .endpoints_feishu import state_home_for

__all__ = ["delegate_projects", "delegate_projects_save"]


def delegate_projects(app: App) -> dict[str, Any]:
    """可委派项目清单 = OpenCode 的项目 ∩ 已授权路径。

    ## 为什么不只给左边

    只给「OpenCode 知道什么」的话，界面上会出现一个你**点不进去**的项目 ——
    因为它没被授权。而那一行的路径一眼就能看见，用户会以为已经能用了。
    所以每行都带 ``allowed``，界面上直接灰掉未授权的。

    ## 为什么路径来自 OpenCode 而不是配置

    配置里存的是**路径**，OpenCode 存的是**路径 + 名字**。显示给用户的名字
    取自OpenCode（``project.name``，大小写不敏感匹配），路径也从它来 ——
    这样你不用手抄绝对路径，而**授权这个动作仍然是显式的**。
    """
    projects = list_projects()

    # 归一化后比：配置里写 ``D:/x/y``，OpenCode 可能写 ``D:\\x\\y`` 或带
    # 尾斜杠。不归一化的话「明明在白名单里」会被判成不在。
    cfg = load_config(state_home_for(app))
    allowed = {
        str(p).replace("\\", "/").rstrip("/").casefold()
        for p in cfg.delegate_projects
    }

    items = [
        {
            "name": p.display,
            "key": p.key,
            "path": p.worktree,
            "allowed": p.worktree.replace("\\", "/").rstrip("/").casefold()
            in allowed,
        }
        for p in projects
    ]

    return {
        "kind": "delegate_projects",
        "items": items,
        # 显式说明查不到时的原因，别让界面把「空列表」当成「你没有项目」。
        "opencode_found": bool(projects),
        #回显归一化后的路径，不回显 ``cfg.delegate_projects`` 原样 ——
        # 否则同一项会因写法不同（反斜杠 / 尾斜杠）在界面上显示成两条。
        "whitelist": sorted(
            p.replace("\\", "/").rstrip("/") for p in cfg.delegate_projects
        ),
    }


def _norm(path: str) -> str:
    """路径归一化：统一斜杠、去尾斜杠、转小写。

    比对时归一化，否则用户换个斜杠写法就会被判成「另一个项目」——
    而他明明授权过。
    """
    return path.replace("\\", "/").rstrip("/").casefold()


def _add(cfg, path: str):
    """加一个路径。**已在白名单里就返回 False**，不重复写。"""
    if any(_norm(p) == _norm(path) for p in cfg.delegate_projects):
        return cfg, False
    return replace(
        cfg, delegate_projects=tuple(cfg.delegate_projects) + (path,)
    ), True


def _revoke(cfg, name: str, projects):
    """撤销授权。返回 ``(新配置, 是否真的删掉了)``。

    ## 先按名字解析成路径，再比路径

    第一版直接拿 ``name`` 去比白名单里的**路径**，于是永远不相等 ——
    名字（``OpenMOS``）和路径（``D:/.../openmos``）根本不是一回事。
    测试 ``test_revoke_removes`` 当场抓住。

    ## 解析不到时按目录名兜底

    项目可能已经**从 OpenCode 里删掉了**，而白名单里那条还留着。这时候
    界面列不出它、用户也点不到撤 —— 而那条正是最该清掉的死配置。
    所以再按 ``worktree`` 的 basename 兜一次。
    """
    from ..services.opencode_projects import _basename

    hit = resolve(name, projects)
    before = cfg.delegate_projects
    if hit is not None:
        path_key = _norm(hit.worktree)
        kept = tuple(p for p in before if _norm(p) != path_key)
    else:
        target = _norm(name)
        kept = tuple(p for p in before if _norm(_basename(p)) != target)
    return replace(cfg, delegate_projects=kept), len(kept) != len(before)


def delegate_projects_save(app: App, body: dict[str, Any]) -> dict[str, Any]:
    """授权 / 撤销授权一个项目。返回**保存后的完整列表**。

    返回 GET 端点那一份而不是只回一句「好了」：省掉界面第二次请求，也避免
    「保存成功但回显的还是旧值」这种时序不一致（与
    :func:`feishu_config_save` 同一条理由）。
    """
    unknown = sorted(set(body) - {"allow", "revoke"})
    if unknown:
        raise FreeAgentError(
            f"不认识这些字段：{', '.join(unknown)}。只支持 allow / revoke。"
        )

    allow = body.get("allow")
    revoke = body.get("revoke")
    if allow and revoke:
        raise FreeAgentError("一次只能改一个项目（allow 与 revoke 不能同时给）")
    if not allow and not revoke:
        raise FreeAgentError("要给出 allow（项目名）或 revoke（项目名）")

    home = state_home_for(app)
    cfg = load_config(home)

    if allow:
        if not isinstance(allow, str):
            raise FreeAgentError("allow 必须是项目名字符串")
        hit = resolve(allow, list_projects())
        if hit is None:
            # 名字不认识时**列出可用的** —— 这是「用户可见入口」的关键一环。
            # 只说「不认识」等于让用户去猜能填什么。
            names = "、".join(p.display for p in list_projects()) or "（一个都没有）"
            raise FreeAgentError(f"不认识项目 {allow!r}。可用的是：{names}")
        cfg, changed = _add(cfg, hit.worktree)
        if not changed:
            raise FreeAgentError(f"{hit.display} 已经在白名单里了")
        save_config(cfg, home)
    else:
        if not isinstance(revoke, str):
            raise FreeAgentError("revoke 必须是项目名字符串")
        cfg, removed = _revoke(cfg, revoke, list_projects())
        if not removed:
            raise FreeAgentError(f"{revoke} 不在白名单里，没什么可撤")
        save_config(cfg, home)

    return delegate_projects(app)