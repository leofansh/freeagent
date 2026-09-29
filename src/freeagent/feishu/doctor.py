"""飞书通道自检（doctor）：**回答「为什么 bot 不理我」**。

参考实现里三家都有这个东西 —— Hermes 的 ``hermes gateway setup``、
``lark-opencode-bridge`` 的 ``/doctor``。理由很实际：这套东西要配
App Secret、白名单、SDK，**每一处配错的表现都是「一句话都不回」**，
没有诊断信息就只能瞎猜。

## 默认**不联网**

自检默认只做**离线**检查（SDK 在不在、变量齐不齐、库能不能开）。
真去连飞书要加 ``--live`` —— 否则一个「查一下配置」的动作就会
产生网络请求、也可能被限流。

刻意不打印 secret 本身，只说「有没有」。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from .config import (
    ENV_ALLOWED,
    ENV_APP_ID,
    ENV_APP_SECRET,
    ENV_DOMAIN,
    ENV_POLL,
    ConfigError,
    FeishuConfig,
    load_config,
    state_home,
)

__all__ = [
    "Check",
    "check_identity",
    "check_lock_free",
    "run_checks",
    "format_report",
    "main",
]

#: 飞书 App Secret 的长度。用来识别「粘贴被截断」——
#: 否则接口只回一句没法定位的「app secret invalid」。
SECRET_LENGTH = 32

#: 身份检查这一项的名字。用 ``open_id`` 之外的词（比如「机器人」）会让人
#: 以为要填什么配置项，其实它是**自动探测**出来的。
CHECK_IDENTITY = "bot 身份（open_id）"

#: 锁检查这一项的名字。
CHECK_LOCK = "单实例锁"


@dataclass(frozen=True, slots=True)
class Check:
    """一项检查结果。``fatal`` 表示不修就完全用不了。"""

    name: str
    ok: bool
    detail: str
    fatal: bool = False


def check_sdk() -> Check:
    """SDK 装没装、什么版本。**唯一一处允许 import lark_oapi 的地方。**"""
    try:
        import lark_oapi  # noqa: F401
    except ImportError:
        return Check(
            "飞书 SDK", False,
            '未安装。装它：pip install ".[feishu]"（核心包不需要它）',
            fatal=True,
        )
    # 踩过的坑：读 ``lark_oapi.__version__`` —— 那个属性**不存在**，
    # 于是永远显示「未知版本」。而版本恰恰是排查「行为和文档不一致」
    # 时最该先确认的东西，报告里却拿不到。改从包元数据取。
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            text = f"已安装（{version('lark-oapi')}）"
        except PackageNotFoundError:
            text = "已安装（版本查不到）"
    except Exception:
        text = "已安装（版本查不到）"
    return Check("飞书 SDK", True, text)


def check_config(cfg: FeishuConfig) -> list[Check]:
    """配置齐不齐。逐项说清**缺了会怎样**，而不是只列名字。"""
    out: list[Check] = []
    out.append(Check(
        ENV_APP_ID, bool(cfg.app_id),
        f"已设置（{cfg.app_id}）" if cfg.app_id else "未设置，桥接无法连接",
        fatal=not cfg.app_id,
    ))
    out.append(Check(
        ENV_APP_SECRET, bool(cfg.app_secret),
        "已设置（不显示内容）" if cfg.app_secret
        else "未设置，桥接无法认证。**只从环境变量读**，不写进配置文件",
        fatal=not cfg.app_secret,
    ))
    # 长度在这里查，而不是在 .bat 里。
    # 踩过的坑：先在批处理里用 ``for /f`` 数长度 —— 它数的是**行**不是字符，
    # 而飞书密钥不含空格，于是 32 位密钥被数成 1，校验永远失败。
    # cmd 里做字符级切片极其易错，交给 Python 才安全。
    # 这一项很值：粘贴被截断时，接口只回一句无法定位的
    # 「app secret invalid」；长度不对则能立刻指出是粘贴问题。
    if cfg.app_secret:
        n = len(cfg.app_secret)
        out.append(Check(
            "App Secret 长度", n == SECRET_LENGTH,
            f"{n} 位（应为 {SECRET_LENGTH}）"
            + ("" if n == SECRET_LENGTH
               else "。长度不对通常说明**粘贴被截断或被改动过**，"
                    "请重新复制完整值再试"),
            fatal=n != SECRET_LENGTH,
        ))
    n = len(cfg.allowed_users)
    out.append(Check(
        ENV_ALLOWED, n > 0,
        f"已授权 {n} 人" if n else
        "为空 —— 没有任何人能指挥本机，桥接会拒绝启动",
        fatal=n == 0,
    ))
    domain_ok = cfg.domain in ("feishu", "lark")
    out.append(Check(
        ENV_DOMAIN, domain_ok,
        f"{cfg.domain} → {cfg.base_url}" if domain_ok
        else f"只能是 feishu 或 lark，收到 {cfg.domain!r}",
        fatal=not domain_ok,
    ))
    out.append(Check(
        ENV_POLL, True,
        f"提醒轮询 {cfg.reminder_poll:g} 秒" + ("（已关闭）" if cfg.reminder_poll <= 0 else ""),
    ))
    return out


def check_database(db_path=None) -> Check:
    """库能不能开、schema 是不是最新的。"""
    try:
        from ..app import build_app
        from ..storage.db import SCHEMA_VERSION

        app = build_app(db_path)
        try:
            version = int(
                app.conn.execute("PRAGMA user_version").fetchone()[0]
            )
            tasks = len(app.task_repo.list_all())
        finally:
            app.close()
    except Exception as exc:
        return Check("数据库", False, f"打不开：{type(exc).__name__}: {exc}", fatal=True)

    if version != SCHEMA_VERSION:
        return Check(
            "数据库", False,
            f"schema 是 v{version}，当前代码要 v{SCHEMA_VERSION}。"
            "build_app 本该自动迁移 —— 请报这个 issue",
        )
    return Check("数据库", True, f"正常（schema v{version}，{tasks} 条事务）")


def check_token(cfg: FeishuConfig) -> Check:
    """**联网**检查：凭据到底能不能换到 token。"""
    from .sender import FeishuError, FeishuSender

    try:
        token = FeishuSender(cfg).token()
    except FeishuError as exc:
        return Check(
            "凭据（联网）", False,
            f"换 token 失败：{exc}。检查 App ID/Secret 与应用是否已发布",
            fatal=True,
        )
    return Check("凭据（联网）", True, f"能换到 token（长度 {len(token)}，不显示内容）")


def check_identity(
    cfg: FeishuConfig,
    home=None,
    *,
    live: bool = False,
    now: float | None = None,
) -> Check:
    """bot 自己的 ``open_id`` 探到没。**群聊 @ 门控的唯一依据。**

    这一项值得单独列，因为它失败的症状最容易被误判：「私聊能回、群里
    @ 也不回」很容易被当成权限问题或事件订阅没配好，实际原因只是
    没探到 open_id。两者的修法完全不同，必须能一眼分开。

    离线时只读缓存 —— 「查一下配置」不该产生网络请求。
    """
    from .identity import IdentityError, load_cached_identity, resolve_identity

    try:
        cached = load_cached_identity(home, now=now)
    except OSError as exc:
        return Check(CHECK_IDENTITY, False, f"读缓存失败：{exc.strerror}")

    if cached is not None:
        name = f"（{cached.app_name}）" if cached.app_name else ""
        return Check(
            CHECK_IDENTITY, True,
            f"已缓存 {cached.open_id}{name}，群聊 @ 门控按它判定",
        )

    if not (live and cfg.app_id and cfg.app_secret):
        return Check(
            CHECK_IDENTITY, False,
            "还没探到。**私聊不受影响，但群里 @ 它不会回**"
            "（门控失败关闭）。加 --live 联网探一次并缓存",
            # **不是致命**：私聊还能用，只是群里不响应。标致命会让人
            # 以为整个通道都废了，反而不好定位。
        )

    from .sender import FeishuSender

    try:
        identity, note = resolve_identity(FeishuSender(cfg), home=home, now=now)
    except IdentityError as exc:
        return Check(CHECK_IDENTITY, False, f"探测失败：{exc}")
    except Exception as exc:                # 自检不该自己炸掉
        return Check(
            CHECK_IDENTITY, False, f"探测出错：{type(exc).__name__}: {exc}"
        )
    if identity is None:
        return Check(CHECK_IDENTITY, False, note)
    status = f"，启用状态 {identity.activate_status}" if identity.activate_status else ""
    return Check(
        CHECK_IDENTITY, True,
        f"{identity.open_id}（{identity.app_name or '无名称'}）{status}；{note}",
    )


def check_lock_free() -> Check:
    """锁端口空着吗？被占 = **已经有一个桥接在跑**。

    这条几乎没人会想到去查，但它是「bot 突然不响应」的头号原因：
    第二个桥接起不来（或者起来了但飞书只把事件投给第一个），
    现场表现和「权限坏了」一模一样。
    """
    from .bridge import AlreadyRunning, PortLock, lock_port_from_env

    try:
        port = lock_port_from_env()
    except ConfigError as exc:
        return Check(CHECK_LOCK, False, str(exc), fatal=True)
    lock = PortLock(port)
    try:
        lock.acquire()
    except AlreadyRunning as exc:
        return Check(
            CHECK_LOCK, False,
            f"{exc}。如果刚才那个就是你，那不用管；"
            f"否则只有第一个能收到事件，第二个会静默失效",
        )
    lock.release()
    return Check(CHECK_LOCK, True, f"端口 {port} 空闲（可以启动桥接）")


def run_checks(db_path=None, env=None, live: bool = False, home=None) -> list[Check]:
    """跑全部检查。``live=True`` 会真的去连飞书。"""
    checks: list[Check] = [check_sdk()]
    try:
        cfg = load_config(env)
    except ConfigError as exc:
        return checks + [Check("配置", False, str(exc), fatal=True)]

    # ``--db`` 指到哪儿，状态文件（身份缓存）就跟到哪儿 —— 和桥接同一套
    # 解析（``state_home``）。doctor 与 bridge 对「缓存在哪」看法不一致时，
    # 会出现「doctor 说没缓存、桥接说有」这种没法排查的分裂。
    resolved_home = home
    if resolved_home is None and db_path:
        resolved_home = state_home(Path(db_path).parent)

    try:
        cfg.check_ready()
    except ConfigError as exc:
        # 逐项照实列，而不是只丢一句「配置有问题」
        checks += check_config(cfg)
        checks.append(Check("配置整体", False, str(exc), fatal=True))
    else:
        checks += check_config(cfg)
        checks.append(Check("配置整体", True, "齐全"))

    checks.append(check_database(db_path))
    checks.append(check_identity(cfg, resolved_home, live=live))
    checks.append(check_lock_free())
    if live and cfg.app_id and cfg.app_secret:
        checks.append(check_token(cfg))
    return checks


def format_report(checks: list[Check]) -> str:
    lines = ["飞书通道自检", "=" * 46]
    for c in checks:
        mark = "OK  " if c.ok else ("致命" if c.fatal else "警告")
        lines.append(f"[{mark}] {c.name}：{c.detail}")
    fatal = [c for c in checks if c.fatal and not c.ok]
    lines.append("=" * 46)
    if fatal:
        lines.append(f"有 {len(fatal)} 项致命问题，先修上面标「致命」的。")
    else:
        lines.append("配置可用。启动：python -m freeagent.feishu.bridge")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="freeagent-feishu-doctor",
        description="检查飞书通道配置（默认不联网）",
    )
    parser.add_argument("--db", default=None)
    parser.add_argument(
        "--live", action="store_true",
        help="额外联网换一次 token（会产生网络请求）",
    )
    args = parser.parse_args(argv)
    checks = run_checks(db_path=args.db, live=args.live)
    print(format_report(checks))
    return 1 if any(c.fatal and not c.ok for c in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
