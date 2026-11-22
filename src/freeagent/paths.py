"""状态目录的路径解析 —— **只此一份**。

## 为什么单独一个模块

``config.json`` 和 ``llm.env`` 必须落在**同一个目录**，而 ``config.py`` 又要读
``llm.env``（取 Key）。如果让 ``llm_env`` 通过 ``from .config import config_path``
去拿路径，就形成 ``config -> llm_env -> config`` 的环。

运行时那个环其实是惰性 import、不会炸，但静态检查会报（``reportImportCycles``），
而且**它会诱导下一个人写出真环**：哪天某个模块在**模块级**就 import 了 ``config``，
整条链就会在 import 期炸掉，且报错信息指向一个看起来无辜的模块。

所以把解析逻辑下沉到这个**叶子**模块（只依赖标准库），``config`` 和 ``llm_env``
都从它拿 —— 两边互不知晓，环在结构上就不可能形成。

## 复制路径解析为什么会出事

漂移的表现是「界面写 A 目录、运行时读 B 目录」，于是配置永远不生效，
而且**没有任何报错**。这类 bug 极难查，所以宁可多一个模块也不复制那五行。

## 改名（V1.140）

产品名从 FreeAgent 改成「值守 / Steward」，状态目录随之从 ``~/.freeagent``
改成 ``~/.steward``、环境变量从 ``FREEAGENT_HOME`` 改成 ``STEWARD_HOME``。

旧名不能一刀切掉：切掉就是让已经配过的人**静默失效** —— 他们设的
``FREEAGENT_HOME`` 不再生效，库和配置一起读不到，而界面只会显示「空」，
一个错都不报。所以这里保留两个旧名分支，命中时往 stderr **打一行明示**，
同一条只打一次。这不叫兜底容错：兜底是「出错了假装没事」，这里是
「还有一个更旧的意图正在生效，告诉用户它正在生效、以及怎么迁过去」。

``home`` 参数显式给值时**一律照用**，不探测旧目录 —— 显式指定就是最高优先级，
探测只服务「调用方什么都没说」的情况。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = [
    "DEFAULT_DIR_NAME",
    "HOME_ENV",
    "LEGACY_DIR_NAME",
    "LEGACY_HOME_ENV",
    "state_dir",
    "state_file",
]

#: 默认状态目录名。``STEWARD_HOME`` 没设时用它。
DEFAULT_DIR_NAME = ".steward"

#: 状态目录名的环境变量。库和配置、密钥文件都跟着它走。
HOME_ENV = "STEWARD_HOME"

#: 改名前的目录名（FreeAgent 时代的 ``~/.freeagent``）。只用于「沿用旧数据」判断。
LEGACY_DIR_NAME = ".freeagent"

#: 改名前的环境变量名。仍然认，命中时明示一次。
LEGACY_HOME_ENV = "FREEAGENT_HOME"

#: 已经提示过的消息 key —— 同一条明示只打一次，不然每读一次配置就刷一行。
_seen_notices: set[str] = set()


def _notice_once(key: str, message: str) -> None:
    """往 stderr 说一次。**不是日志**：这是要让用户看见的，别吞进 log 文件。"""
    if key in _seen_notices:
        return
    _seen_notices.add(key)
    print(message, file=sys.stderr)


def _has_db(directory: Path) -> bool:
    """那个目录里是否已经有一份库。

    看 ``agent.db`` 而不是「目录存在」：目录会被 :func:`resolve_db_path` 的
    ``mkdir`` 先建出来，一个空目录**不能**当成「用户已经在用新目录」——
    否则升级后第一次启动就把旧库甩开，而用户看到的只是「事务全没了」。
    看文件才是「真的用上了」。
    """
    try:
        return (directory / "agent.db").exists()
    except OSError:
        return False


def state_dir(home: str | Path | None = None) -> Path:
    """状态目录（不放文件名）。

    ``home`` 为空时依次：``STEWARD_HOME`` > 旧名 ``FREEAGENT_HOME`` >
    「旧目录里已有库就沿用旧目录」> ``~/.steward``。
    目录不存在**不创建** —— 只返回预期路径，让调用方决定要不要建。
    """
    if home is not None:
        return Path(home)
    # ``strip()`` 不是洁癖，是必需的。踩过的坑：批处理里写
    # ``set FREEAGENT_HOME=C:\data ``（``&&`` 前的空格被算进值里），
    # 或者从 ``.env`` 复制时带上尾随空格 —— Windows 会把路径组件末尾的
    # 空格**剥掉**，于是 ``mkdir`` 建的目录名和后面用的路径对不上：
    # ``is_dir()`` 说存在、``os.access(W_OK)`` 说可写，sqlite 却报
    # ``unable to open database file``。那句话本身不含任何线索，
    # 排查成本极高。空白值则直接当没设，回落到默认目录。
    env = (os.environ.get(HOME_ENV) or "").strip()
    if env:
        return Path(env)
    legacy_env = (os.environ.get(LEGACY_HOME_ENV) or "").strip()
    if legacy_env:
        _notice_once(
            "legacy-env",
            f"[值守] 检测到旧环境变量 {LEGACY_HOME_ENV}={legacy_env}，本次沿用它。"
            f" 改成 {HOME_ENV} 之后这条提示就不会再出现。",
        )
        return Path(legacy_env)
    new = Path.home() / DEFAULT_DIR_NAME
    legacy = Path.home() / LEGACY_DIR_NAME
    if not _has_db(new) and _has_db(legacy):
        _notice_once(
            "legacy-dir",
            f"[值守] 数据目录仍是 {legacy}（沿用既有数据，没丢）。"
            f" 全新安装会用 {new}；要把旧数据搬过去请明确下令，本版不自动搬。",
        )
        return legacy
    return new


def state_file(home: str | Path | None, name: str) -> Path:
    """状态目录下的某个文件。``config.json`` / ``llm.env`` / ``feishu.env`` 都走它。"""
    return state_dir(home) / name
