"""单页界面。

刻意**零外部资源**（无 CDN、无字体文件、无框架）—— 这是个本地工具，
断网也必须照常能用。

两条不能违反的设计约束：
1. 每条排序信号的**理由**必须显示，且标注「启发式提示，不是评分」；
2. 视图切换只是**换个角度读同一份数据**，不给「筛选掉某些事」的选择权
   （那等于替用户做取舍）。

这个文件只负责**拼装**：HTML 骨架、样式、脚本分块放在同目录的
``markup`` / ``style`` / ``js_*`` 模块里。

刻意用「拼起来」而不是「拆成很多文件再各自维护」—— 单页界面天生是一份
文档，拆得太细反而没人能一眼看完。分块只为单文件别超过 250 行。
拼装结果在 ``tests/test_web.py`` 里有逐字节等价的守卫。
"""

from __future__ import annotations

from .js_boot import JS_BOOT
from .js_chat import JS_CHAT
from .js_core import JS_CORE
from .js_detail import JS_DETAIL
from .js_feishu import JS_FEISHU
from .js_feishu_config import JS_FEISHU_CONFIG
from .js_delegate_projects import JS_DELEGATE_PROJECTS
from .js_feishu_control import JS_FEISHU_CONTROL
from .js_settings import JS_SETTINGS
from .js_settings_llm import JS_SETTINGS_LLM
from .js_vision import JS_VISION
from .markup import PAGE_MARKUP
from .style import STYLE_CSS

__all__ = ["INDEX_HTML"]

INDEX_HTML = (
    PAGE_MARKUP
    + "<style>\n" + STYLE_CSS + "\n</style>\n"
    + "<script>\n"
    # 顺序要紧：core 提供 helper，vision/chat 依赖它，boot 最后接线并启动。
    + "\n\n".join(
        (
            JS_CORE,
            JS_VISION,
            JS_CHAT,
            # settings_llm 在 settings 之前：后者要调前者的 renderLlmSection。
            # （函数声明会提升，所以其实反了也能跑，但顺序按依赖写，别让人猜。）
            JS_SETTINGS_LLM,
            JS_SETTINGS,
            JS_DETAIL,
            JS_FEISHU,
            JS_FEISHU_CONFIG,
            JS_FEISHU_CONTROL,
            # delegate_projects 只用 JS_FEISHU_* 里已有的 el/esc/api/toast，
            # 但它自己提供 delegateProjectsView / delegateRestartBanner，
            # 必须在 JS_BOOT 之前进页面 —— boot 里会调用它们。
            JS_DELEGATE_PROJECTS,
            JS_BOOT,
        )
    )
    + "\n</script>\n"
)
