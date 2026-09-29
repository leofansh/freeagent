"""飞书配置的读写端点（设计方案 12.7）。

**为什么写端点单独一个文件**：:mod:`endpoints_feishu` 是只读的，模块说明里
写了切分纪律「只读 / 写分开」—— 混在一起就得通读全文才能判断「这个函数会不会
动我的飞书配置」。这里会**改凭据**，必须是独立可审的一眼。

三条硬约束，少一条就出事：

1. **不回传明文 Secret**。GET 只给掩码。掩码本身够确认「是那个值」，
   不够拿去用 —— 见 :func:`~freeagent.feishu.secret_store.mask_secret`。
2. **空 Secret = 没改，不是删除**。这是最容易出事的地方：界面上一个没填过
   的输入框提交上来，如果当成「清空」，就会把用户**正在用的凭据抹掉**，而
   界面上还会显示「已保存」。删除必须显式要（``clear_secret``）。
3. **只接受白名单里的变量名**。透传任意 key 等于开 RCE 入口（``PYTHONPATH`` /
   ``EDITOR``），防线在 :data:`~freeagent.feishu.secret_store.ALLOWED_KEYS`。
"""

from __future__ import annotations

from typing import Any

from ..app import App
from ..domain import FreeAgentError
from ..feishu.config import MAX_ALLOWED, VALID_DOMAINS
from ..feishu.secret_store import (
    ALLOWED_KEYS,
    SECRET_KEYS,
    UnknownKeyError,
    describe_id,
    env_path,
    mask_secret,
    read_env,
    write_env,
)
from ..feishu.status import read_status, restart_required, status_path
from .endpoints_feishu import state_home_for

__all__ = ["feishu_config", "feishu_config_save"]

#: 端口合法范围。桥接只校验「是整数」（``bridge.lock_port_from_env``），
#: 越界的端口要等到 socket 绑定时才炸，那句报错没人看得懂。这里提前拦，
#: 属于**只会更早给出人话**、不会拒绝桥接本来能接受的东西。
_PORT_MIN = 1
_PORT_MAX = 65535

#: 必填项。**不做成「缺了就拒绝保存」** —— 用户可能正在分几次填，
#: 强行拦住只会让人填不完。缺什么如实报出来，界面显示「还差这几项」。
REQUIRED_KEYS = ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_ALLOWED_USERS")


def _check_domain(value: str) -> str:
    domain = value.strip().lower() or "feishu"
    if domain not in VALID_DOMAINS:
        raise FreeAgentError(
            f"FEISHU_DOMAIN 只能是 {' 或 '.join(sorted(VALID_DOMAINS))}，收到 {value!r}"
        )
    return domain


def _check_allowed(value: str) -> str:
    """白名单条目数与格式。

    上限复用 :data:`MAX_ALLOWED` —— 与 ``load_config`` 同一个来源，
    免得界面能填出一个桥接启动时才拒绝的值。
    """
    users = [part.strip() for part in value.split(",") if part.strip()]
    if len(users) > MAX_ALLOWED:
        raise FreeAgentError(
            f"白名单有 {len(users)} 个，超过上限 {MAX_ALLOWED}。"
            "本机个人助手不该授权这么多人，请检查是不是误填。"
        )
    # ``oc_`` 是**会话** id，不是人的 id。拦住它：填进去不会立刻报错，
    # 而是一直等到「提醒发不出去」才暴露（见 send_to_allowlist_entry）。
    #
    # 只拦这一种。裸 user_id（形如 2d1b7bec）**不拦** —— 它是合法且推荐的
    # 填法，只是需要应用申请对应权限。拦住它等于把正确配置说成错误。
    bad = [u for u in users if u.startswith("oc_")]
    if bad:
        raise FreeAgentError(
            f"这些看着是**会话 id**（oc_ 开头），不是人的 id：{', '.join(bad)}。"
            "白名单要填 ou_ 开头的 open_id，或租户级 user_id。"
        )
    return ",".join(users)


def _check_lock_port(value: str) -> str:
    raw = value.strip()
    if not raw:
        return ""
    try:
        port = int(raw)
    except ValueError as exc:
        raise FreeAgentError(f"FEISHU_LOCK_PORT 必须是整数，收到 {raw!r}") from exc
    if not _PORT_MIN <= port <= _PORT_MAX:
        raise FreeAgentError(
            f"FEISHU_LOCK_PORT 必须在 {_PORT_MIN}-{_PORT_MAX} 之间，收到 {port}"
        )
    return str(port)


#: 每个键的校验器。**没列在这里的键一律原样写入** ——
#: 刻意不硬编一个「全都要有校验」的框架：白名单已经挡住了危险的名字
#: （见模块说明第 3 条），剩下这几个都是纯字符串，值不值得单独校验
#: 要看它错起来会不会静默。
_VALIDATORS = {
    "FEISHU_DOMAIN": _check_domain,
    "FEISHU_ALLOWED_USERS": _check_allowed,
    "FEISHU_LOCK_PORT": _check_lock_port,
}


def _validate(values: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in values.items():
        check = _VALIDATORS.get(key)
        out[key] = check(value) if check is not None else value
    return out


def feishu_config(app: App) -> dict[str, Any]:
    """当前配置。**Secret 只给掩码。**"""
    home = state_home_for(app)
    path = env_path(home)
    values = read_env(home)
    secret = values.get("FEISHU_APP_SECRET", "")
    allowed_raw = [
        part.strip() for part in values.get("FEISHU_ALLOWED_USERS", "").split(",")
        if part.strip()
    ]

    return {
        "kind": "feishu_config",
        "env_path": str(path),
        "env_file_exists": path.exists(),
        # 非秘密的值原样给，秘密的换成掩码。
        "values": {
            key: (mask_secret(value) if key in SECRET_KEYS else value)
            for key, value in values.items()
        },
        "secret_configured": bool(secret),
        "missing": [key for key in REQUIRED_KEYS if not values.get(key)],
        "allowed_keys": sorted(ALLOWED_KEYS),
        "secret_keys": sorted(SECRET_KEYS),
        # 逐条说明白名单条目**大概**是什么形状。填白名单最大的坑是
        # 「不知道该填什么」—— 界面直接给判断，用户照着填而不是猜。
        "allowed_entries": [
            {"id": entry, "kind": describe_id(entry)} for entry in allowed_raw
        ],
        "restart_required": restart_required(read_status(status_path(home)), home=home),
    }


def feishu_config_save(app: App, body: dict[str, Any]) -> dict[str, Any]:
    """保存配置。**返回保存后的完整状态**，界面据此重绘。

    返回 GET 端点那一份而不是只回一句「好了」：省掉界面第二次请求，
    也避免「保存成功但回显的还是旧值」这种时序不一致。
    """
    unknown = sorted(set(body) - ALLOWED_KEYS - {"clear_secret"})
    if unknown:
        raise FreeAgentError(
            f"不认识这些字段：{', '.join(unknown)}。"
            f"只支持 {', '.join(sorted(ALLOWED_KEYS))} 和 clear_secret。"
        )

    values: dict[str, str] = {}
    for key in ALLOWED_KEYS:
        if key not in body:
            continue                        # 没提到 = 不改
        raw = body[key]
        if not isinstance(raw, str):
            raise FreeAgentError(f"{key} 必须是字符串")
        if key in SECRET_KEYS and not raw.strip():
            # 关键：空 Secret 当「没改」。见模块说明第 2 条。
            # 真要删得显式来 —— 那个操作太危险，不该由一个空输入框触发。
            continue
        values[key] = raw

    clear = body.get("clear_secret")
    if clear:
        values["FEISHU_APP_SECRET"] = ""    # 交给 write_env 执行删除

    home = state_home_for(app)
    try:
        write_env(_validate(values), home)
    except UnknownKeyError as exc:
        raise FreeAgentError(str(exc)) from exc
    return feishu_config(app)
