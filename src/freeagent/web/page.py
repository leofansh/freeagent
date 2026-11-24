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
from .js_nav import JS_NAV
from .js_chat_search import JS_CHAT_SEARCH
from .js_chat_shell import JS_CHAT_SHELL
from .js_chat_sessions import JS_CHAT_SESSIONS
from .js_chat_projects import JS_CHAT_PROJECTS
from .js_chat_intake import JS_CHAT_INTAKE
from .js_detail import JS_DETAIL
from .js_contacts import JS_CONTACTS
from .js_promises import JS_PROMISES
from .js_pending import JS_PENDING
from .js_nudge import JS_NUDGE
from .js_model import JS_MODEL
from .js_week import JS_WEEK
from .js_harness import JS_HARNESS
from .js_onboard import JS_ONBOARD
from .js_artifact import JS_ARTIFACT
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
from .js_settings_groups import JS_SETTINGS_GROUPS
from .js_vision import JS_VISION
from .js_workbench import JS_WORKBENCH
from .js_workbench_card import JS_WORKBENCH_CARD
from .js_workbench_dirs import JS_WORKBENCH_DIRS
from .js_workbench_projects import JS_WORKBENCH_PROJECTS
from .js_workbench_page import JS_WORKBENCH_PAGE
from .js_workbench_intake import JS_WORKBENCH_INTAKE
from .js_workbench_rail import JS_WORKBENCH_RAIL
from .js_workbench_side import JS_WORKBENCH_SIDE
from .js_workbench_files import JS_WORKBENCH_FILES
from .js_workbench_shell import JS_WORKBENCH_SHELL
from .markup import PAGE_MARKUP
from .style import STYLE_CSS
from .style_contrast import STYLE_CONTRAST_CSS
from .style_layout import STYLE_LAYOUT_CSS
from .style_nav import STYLE_NAV_CSS
from .style_workbench import STYLE_WORKBENCH_CSS
from .style_usability import STYLE_USABILITY_CSS
from .style_chat import STYLE_CHAT_CSS

__all__ = ["INDEX_HTML"]

INDEX_HTML = (
    PAGE_MARKUP
    + "<style>\n" + STYLE_CSS + "\n" + STYLE_CONTRAST_CSS + "\n" + STYLE_LAYOUT_CSS + "\n" + STYLE_NAV_CSS + "\n"
    + STYLE_WORKBENCH_CSS + "\n" + STYLE_USABILITY_CSS + "\n"
    + STYLE_CHAT_CSS + "\n</style>\n"
    + "<script>\n"
    # 顺序要紧：core 提供 helper，vision/chat 依赖它，boot 最后接线并启动。
    + "\n\n".join(
        (
            JS_CORE,
            # nav 提供「更多」下拉开合与 MENU_VIEWS；boot 的 load() 引用
            # closeMore / MENU_VIEWS，所以排在 boot 之前（同一块脚本内运行期调用）。
            JS_NAV,
            JS_VISION,
            # chat_store 提供 chatDay/chatLoad/chatSave/clearHistory 与
            # lastItemIds —— JS_CHAT 与 JS_CHAT_SELECT 都直接用它们，
            # 所以排在 chat 之前。
            JS_CHAT_STORE,
            JS_CHAT,
            # chat_search 提供 chatSearchOpen（buildBar 的搜索按钮 onclick 用），
            # 依赖 JS_CHAT 建立的 #chatlog / .msg .bubble 结构。
            JS_CHAT_SEARCH,
            JS_CHAT_SHELL,
            # chat_sessions 提供 chatSessionsPaint/chatSessionsBoot（两级会话栏与
            # 历史迁移），chatRailPaint 转调它；只依赖 core helper 与会话端点，
            # 声明提升让顺序无关，仍按依赖写在 chat_shell 之后。
            JS_CHAT_SESSIONS,
            # chat_projects 提供 chatProjMount（chatBarControls 挂载项目按钮时调），
            # 与工作台共用端点、只另画视图层；声明提升让顺序无关，仍按依赖写。
            JS_CHAT_PROJECTS,
            # chat_intake 提供 chatIntakeMount（chatBarControls 挂派活按钮时调），
            # 复用 /api/agent/intake 认项目+brief，确认闸门仍交 chatDelegate。
            JS_CHAT_INTAKE,
            # onboard 提供 renderChatWelcome（renderChatShell 空对话时调）。
            # 只依赖 core 的 $/el/esc 与 chat 建立的 #chatlog；声明提升让
            # 顺序无关，仍按依赖写在 chat 之后。
            JS_ONBOARD,
            # chat_select 只依赖 JS_CHAT 的 history_/chatSave 与 JS_CORE 的
            # el/toast/load；loadChat 末尾会调它的 chatPickPaint，所以必须
            # 在 JS_BOOT 之前进页面。
            JS_CHAT_SELECT,
            # settings_llm 在 settings 之前：后者要调前者的 renderLlmSection。
            # （函数声明会提升，所以其实反了也能跑，但顺序按依赖写，别让人猜。）
            JS_SETTINGS_LLM,
            # settings_groups 提供分组标题 helper（renderSettings 里调）。
            JS_SETTINGS_GROUPS,
            JS_SETTINGS,
            JS_DETAIL,
            # contacts 提供 renderContacts/openContact（关系视图，boot 分发
            # data-view=contacts 用）。只用 JS_CORE 的 el/esc/api/toast/load，
            # 但必须在 JS_BOOT 之前进页面。
            JS_CONTACTS,
            # promises 提供 renderPromises/settle（承诺视图，boot 分发
            # data-view=promises 用）。只用 JS_CORE 的 helper + js_detail 的
            # post，但必须在 JS_BOOT 之前进页面。
            JS_PROMISES,
            # pending 提供 renderPending/openClarify/clarify（待处理视图，
            # boot 分发 data-view=pending 用）。只用 JS_CORE helper + js_detail
            # 的 post，但必须在 JS_BOOT 之前进页面。
            JS_PENDING,
            # nudge 提供 renderNudge/openDraft/copyDraft（催办视图，boot
            # 分发 data-view=nudge 用）。只用 JS_CORE helper + js_detail 的
            # api，但必须在 JS_BOOT 之前进页面。
            JS_NUDGE,
            # model 提供 renderModel（画像视图，boot 分发 data-view=model 用）。
            # 只用 JS_CORE 的 helper + js_contacts 的 openContact，必须在
            # JS_BOOT 之前进页面。
            JS_MODEL,
            # week 提供 renderWeek（boot 分发 data-view=week 用）。只用 JS_CORE
            # 的 el/esc/api，但必须在 JS_BOOT 之前进页面。
            JS_WEEK,
            # harness（V1.143）提供 renderHarness（boot 分发 data-view=harness
            # 用）。只依赖 JS_CORE 的 el/esc/api，必须在 JS_BOOT 之前进页面。
            JS_HARNESS,
            # artifact 提供 artifactBar（验收按钮），由 JS_DETAIL 的
            # renderTask 调用 —— 用的是 js_detail 里的 post/toast/openTask。
            # 声明提升让顺序无关，写成依赖序。
            JS_ARTIFACT,
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
            # delegate_live 提供 mountLivePanel（委派总览页最上面那块面板）。
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
            # 工作台（21.4 / V1.114）：state → card → page 是三段依赖序
            # （paint 会调 wbAsk，page 会调 wbSend/wbPaint）。用 JS_CORE 的
            # el/esc/api/toast 与 JS_DETAIL 的 post，必须在 JS_BOOT 之前
            # （boot 会调 wbStopPoll / loadWorkbench）。
            JS_WORKBENCH,
            # workbench_side 提供 wbSidePaint（wbPaint 末尾会调它填 #wb-side），
            # 只依赖 core 的 $/el/esc；放在 workbench 之后、page 装配之前。
            JS_WORKBENCH_SIDE,
            JS_WORKBENCH_CARD,
            JS_WORKBENCH_DIRS,
            JS_WORKBENCH_PROJECTS,
            # workbench_rail（V1.139）读 projects 那四个状态变量，画左栏与
            # 「累积了什么」，故排在它后面、装配页之前。
            JS_WORKBENCH_RAIL,
            # workbench_intake 提供 wbIntakeMount（page 装配时调），只依赖
            # core 的 el/$/toast 与 detail 的 post；排在 rail 之后、装配页之前。
            JS_WORKBENCH_INTAKE,
            # workbench_files（V1.142）提供 wbSideMount（把右栏拆成产物块与
            # 文件块）与 wbFilesPaint（wbLoadProjects 末尾调）；shell 提供
            # wbRunNote / wbZenBoot（装配末尾调）。两者都只依赖 core 的
            # $/el/esc/api 与 side 的 wbReveal，排在装配页之前。
            JS_WORKBENCH_FILES,
            JS_WORKBENCH_SHELL,
            JS_WORKBENCH_PAGE,
            JS_BOOT,
        )
    )
    + "\n</script>\n"
)
