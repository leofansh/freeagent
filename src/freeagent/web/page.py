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
from .js_chat_select import JS_CHAT_SELECT
from .js_chat_store import JS_CHAT_STORE
from .js_core import JS_CORE
from .js_detail import JS_DETAIL
from .js_feishu import JS_FEISHU
from .js_feishu_config import JS_FEISHU_CONFIG
from .js_delegate_projects import JS_DELEGATE_PROJECTS
from .js_delegate_live import JS_DELEGATE_LIVE
from .js_delegate_live_poll import JS_DELEGATE_LIVE_POLL
from .js_services import JS_SERVICES
from .js_oc_cards import JS_OC_CARDS
from .js_oc_selection import JS_OC_SELECTION
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
            # chat_store 提供 chatDay/chatLoad/chatSave/clearHistory 与
            # lastItemIds —— JS_CHAT 与 JS_CHAT_SELECT 都直接用它们，
            # 所以排在 chat 之前。
            JS_CHAT_STORE,
            JS_CHAT,
            # chat_select 只依赖 JS_CHAT 的 history_/chatSave 与 JS_CORE 的
            # el/toast/load；loadChat 末尾会调它的 chatPickPaint，所以必须
            # 在 JS_BOOT 之前进页面。
            JS_CHAT_SELECT,
            # settings_llm 在 settings 之前：后者要调前者的 renderLlmSection。
            # （函数声明会提升，所以其实反了也能跑，但顺序按依赖写，别让人猜。）
            JS_SETTINGS_LLM,
            JS_SETTINGS,
            JS_DETAIL,
            JS_FEISHU,
            JS_FEISHU_CONFIG,
            # delegate_projects 只用 JS_FEISHU_* 里已有的 el/esc/api/toast，
            # 但它自己提供 delegateProjectsView / delegateRestartBanner，
            # 必须在 JS_BOOT 之前进页面 —— boot 里会调用它们。
            JS_DELEGATE_PROJECTS,
            # oc_cards / oc_selection 只用 JS_CORE 里的 el/esc/api/toast，但
            # boot 会调 loadOcSelection，所以必须在 JS_BOOT 之前。
            # 两块之间顺序按依赖写（声明会提升，反了也能跑，别让人猜）。
            JS_OC_CARDS,
            JS_OC_SELECTION,
            # delegate_live 提供 mountLivePanel（编程页签最上面那块面板）。
            # 它只用 JS_CORE 的 el/esc/api/toast，但 oc_selection 会调它 ——
            # 声明提升让顺序无关，这里按依赖写在 oc_selection 之后。
            JS_DELEGATE_LIVE,
            # delegate_live_poll 是「什么时候画」那一半（轮询/签名/不重建 DOM）。
            # 它调 js_delegate_live 里的 liveItemView，也被 oc_selection 的
            # mountLivePanel 用到 —— 声明提升让顺序无关，写成依赖序。
            JS_DELEGATE_LIVE_POLL,
            # services 提供 loadServices（boot 分发 data-view=services 用），
            # 必须在 JS_BOOT 之前进页面。
            JS_SERVICES,
            JS_BOOT,
        )
    )
    + "\n</script>\n"
)
