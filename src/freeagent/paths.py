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
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["state_dir", "state_file", "DEFAULT_DIR_NAME"]

#: ``FREEAGENT_HOME`` 没设时的默认目录名。
DEFAULT_DIR_NAME = ".freeagent"

#: 状态目录名的环境变量。库和配置、密钥文件都跟着它走。
HOME_ENV = "FREEAGENT_HOME"


def state_dir(home: str | Path | None = None) -> Path:
    """状态目录（不放文件名）。

    ``home`` 为空时用 ``FREEAGENT_HOME``，再退回 ``~/.freeagent``。
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
    return Path(env) if env else Path.home() / DEFAULT_DIR_NAME


def state_file(home: str | Path | None, name: str) -> Path:
    """状态目录下的某个文件。``config.json`` / ``llm.env`` / ``feishu.env`` 都走它。"""
    return state_dir(home) / name
