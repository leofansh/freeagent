"""页面内联 JS 的**语法**必须合法（发布前闸门）。

## 为什么这道闸非有不可

这些 JS 是**原样字符串**注入 ``<script>`` 的。一旦有一处语法错误，整个
script 块就不执行 —— 于是聊天、任务列表、设置**全都死掉**，而页面白白地
渲染出来、看着「加载了但什么都不动」。

Python 侧**测不出来**��``compileall`` 只管 Python。所以这条必须在发布前
单独验——而它只在我手动跑过一次时被发现有价值（不是发现了错，是确认没错）。

刻意**不 mock**、直接 ``node --check``：它验的是「这段 JS 能不能被解析」，
而不是「某几个函数名在不在」。函数名存在但语法坏掉，一样是白屏。
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from freeagent.web.page import INDEX_HTML

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="没装 node，跳过 JS 语法检查"
)


def _js(tmp_path) -> Path:
    blocks = re.findall(r"<script>(.*?)</script>", INDEX_HTML, re.S)
    assert blocks, "页面里没有 <script> 块"
    p = tmp_path / "page.js"
    p.write_text("\n;\n".join(blocks), encoding="utf-8")
    return p


def test_inline_js_parses(tmp_path):
    """整块 JS 必须能被 node 解析——语法错 = 整个界面没反应。"""
    r = subprocess.run(["node", "--check", str(_js(tmp_path))],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert r.returncode == 0, (
        "页面 JS 语法错误（会导致整页交互失效）：\n"
        + (r.stderr or "")[:1500]
    )


def test_no_escaped_quotes_leaked_into_js(tmp_path):
    """``r\"\"\"...\"\"\"`` 里写成 ``\\"`` 会原样进 JS 并让语法坏掉。

    这条是上一条的**根因**检查：Python 侧看着正常，浏览器侧炸掉。
    """
    js = _js(tmp_path).read_text(encoding="utf-8")
    leaked = re.findall(r'\\"', js)
    assert not leaked, (
        f"JS 里混进了 {len(leaked)} 处转义引号，说明 raw string 边界写错了"
    )


def test_new_views_are_actually_present():
    """委派白名单那两个函数必须真的在页面里，而不是「代码写了没接上」。

    这是「断言落在错的通道上」的另一种形态：函数定义得再漂亮，没被拼进
    页面就是死代码，而 Python 测试不会告诉你。
    """
    for name in ("delegateProjectsView", "delegateRestartBanner",
                 "paintDelegate"):
        assert name in INDEX_HTML, f"{name} 不在页面里 —— 界面不会出现这块"