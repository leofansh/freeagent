"""飞书通道页的端点测试（设计方案 12.7）。

重点不是「能返回 JSON」，而是**陈旧不能显示成「已连接」**：
进程被强杀时状态文件留在盘上，报健康比不报更坏 —— 它给出虚假的安心。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from freeagent.app import build_app
from freeagent.feishu.status import StatusReporter, status_path
from freeagent.web import endpoints_feishu
from freeagent.web.page import INDEX_HTML


@pytest.fixture()
def app(tmp_path: Path):
    a = build_app(tmp_path / "a.db")
    yield a
    a.close()


def _report(app, **fields) -> StatusReporter:
    r = StatusReporter(status_path(app.config.home), clock=time.time)
    r.update(**fields)
    return r


# --- 状态：三种情形必须分开 ----------------------------------------------- #
class TestFeishuStatus:
    def test_no_status_file_means_not_running(self, app):
        s = endpoints_feishu.feishu_status(app)
        assert s["status_file_exists"] is False
        assert s["running"] is False
        assert s["stale"] is True, "没有状态文件就该判陈旧，而不是「未知」"

    def test_fresh_connected_is_running(self, app):
        _report(app, state="ready", connected=True, bot_open_id="ou_b",
                dedup_entries=3, allowed_users_count=1, lock_port=8771)
        s = endpoints_feishu.feishu_status(app)
        assert s["running"] is True
        assert s["stale"] is False
        assert s["state"] == "ready"
        assert s["bot_open_id"] == "ou_b"
        assert s["dedup_entries"] == 3
        assert s["lock_port"] == 8771

    def test_stale_connected_is_NOT_running(self, app):
        """**核心回归**：陈旧的「已连接」绝不能算 running。

        这就是「对着尸体报健康」—— 文件还在盘上，但进程早就没了。
        """
        r = StatusReporter(status_path(app.config.home),
                           clock=lambda: time.time() - 10_000)
        r.update(state="ready", connected=True, bot_open_id="ou_b")
        s = endpoints_feishu.feishu_status(app)
        assert s["connected"] is True, "文件里确实写着连着"
        assert s["stale"] is True
        assert s["running"] is False, "陈旧必须压过 connected"
        assert s["age_seconds"] > 100

    def test_degraded_is_not_running_but_is_reported(self, app):
        """降级时进程活着、但群里 @ 不回 —— 界面必须能分开说。"""
        _report(app, state="degraded", connected=True, bot_open_id="")
        s = endpoints_feishu.feishu_status(app)
        assert s["running"] is False
        assert s["state"] == "degraded"
        assert s["stale"] is False, "它是新鲜的，只是功能降级"

    def test_connected_flag_alone_does_not_make_running(self, app):
        _report(app, state="starting", connected=False)
        s = endpoints_feishu.feishu_status(app)
        assert s["connected"] is False
        assert s["running"] is False

    def test_exposes_paths_for_diagnosis(self, app):
        """路径要露出来 —— 「状态读不到」时用户得知道该去看哪。"""
        s = endpoints_feishu.feishu_status(app)
        assert s["status_path"].endswith("bridge_status.json")
        assert s["stale_after_seconds"] > 0

    def test_last_error_is_passed_through(self, app):
        _report(app, state="ready", connected=True, last_error="端口 8771 被占用")
        assert endpoints_feishu.feishu_status(app)["last_error"] == "端口 8771 被占用"

    def test_missing_fields_become_empty_not_crash(self, app):
        _report(app, state="ready", connected=True)     # 其他字段都没给
        s = endpoints_feishu.feishu_status(app)
        assert s["bot_open_id"] == ""
        assert s["last_error"] == ""
        assert s["dedup_entries"] is None


# --- 日志 ---------------------------------------------------------------- #
class TestFeishuLog:
    def test_missing_log_explains_why(self, app):
        r = endpoints_feishu.feishu_log(app, {})
        assert r["exists"] is False
        assert r["lines"] == []
        assert "stdout" in r["notice"], (
            "必须说清「日志默认只写 stdout」—— 不说会被当成没出错"
        )

    def test_tail_only(self, app):
        p = status_path(app.config.home).parent / "feishu.log"
        p.write_text("\n".join(f"第 {i} 行" for i in range(1, 51)), encoding="utf-8")
        r = endpoints_feishu.feishu_log(app, {})
        assert r["exists"] is True
        assert r["total_lines"] == 50
        assert len(r["lines"]) == 50
        assert endpoints_feishu.feishu_log(app, {"lines": ["5"]})["lines"] == [
            f"第 {i} 行" for i in range(46, 51)
        ]

    def test_line_count_is_capped(self, app):
        p = status_path(app.config.home).parent / "feishu.log"
        p.write_text("\n".join(str(i) for i in range(9000)), encoding="utf-8")
        r = endpoints_feishu.feishu_log(app, {"lines": ["999999"]})
        assert len(r["lines"]) == endpoints_feishu.LOG_TAIL_MAX

    def test_bad_line_count_falls_back(self, app):
        p = status_path(app.config.home).parent / "feishu.log"
        p.write_text("a\nb\nc", encoding="utf-8")
        r = endpoints_feishu.feishu_log(app, {"lines": ["不是数字"]})
        assert r["lines"], "参数坏掉不该让整页报错"

    def test_empty_log_file(self, app):
        p = status_path(app.config.home).parent / "feishu.log"
        p.write_text("", encoding="utf-8")
        r = endpoints_feishu.feishu_log(app, {})
        assert r["exists"] is True and r["lines"] == []

    def test_undecodable_bytes_do_not_crash(self, app):
        p = status_path(app.config.home).parent / "feishu.log"
        p.write_bytes(b"ok\n\xff\xfe broken\n")
        assert endpoints_feishu.feishu_log(app, {})["exists"] is True


# --- 页面接线（防「后端做了、界面没接」的空壳）----------------------------- #
class TestFeishuPageWiring:
    def test_nav_has_feishu_tab(self):
        assert 'data-view="feishu"' in INDEX_HTML

    def test_single_spreader_in_nav(self):
        """之前手抖加了两个 spacer，导航会被推歪 —— 钉住。"""
        assert INDEX_HTML.count('class="spacer"') == 1

    def test_page_calls_both_endpoints(self):
        assert "/api/feishu/status" in INDEX_HTML
        assert "/api/feishu/log" in INDEX_HTML

    def test_render_function_present(self):
        assert "function renderFeishu" in INDEX_HTML

    def test_stale_is_shown_differently_from_connected(self):
        """界面**不许**把陈旧渲染成「已连接」。"""
        h = INDEX_HTML
        assert "已陈旧" in h, "必须有独立的「已陈旧」文案"
        seg = h[h.index("function feishuVerdict"):h.index("function feishuField")]
        assert "stale" in seg, "verdict 必须依据 stale 分支"

    def test_verdict_never_claims_connected_when_stale(self):
        """把这条钉死在源码上：stale 分支里不许出现「已连接」。"""
        h = INDEX_HTML
        seg = h[h.index("function feishuVerdict"):h.index("function feishuField")]
        stale_branch = seg[seg.index("if (s.stale)"):seg.index("if (s.state ===")]
        assert "已连接" not in stale_branch, (
            "陈旧分支里出现「已连接」= 对着尸体报健康"
        )

    def test_degraded_explains_group_gating(self):
        """降级时必须说清「群里 @ 不回、私聊正常」——否则用户不知道该做什么。"""
        h = INDEX_HTML
        seg = h[h.index("function feishuVerdict"):h.index("function feishuField")]
        deg = seg[seg.index('if (s.state === "degraded")'):seg.index("if (s.connected)")]
        assert "群里" in deg and "私聊" in deg

    def test_troubleshooting_path_is_listed(self):
        """用户要的是「有问题知道在哪」—— 所以排查顺序必须印在页面上。"""
        assert "出问题先按这个顺序看" in INDEX_HTML
        assert "doctor" in INDEX_HTML

    def test_no_external_resources(self):
        """12.6 的硬约束：断网必须照常能用。**零外部资源**。

        只查**会触发网络请求**的形态，不查 URL 文本本身 ——
        设置页有一句「必须以 http:// 或 https:// 开头」是**给人看的说明文字**，
        把它也算成外部资源就成了假警报（第一版就栽在这）。
        """
        import re
        for bad in ("<link", "<iframe", "<img", "cdn.", "unpkg", "jsdelivr",
                    "googleapis", "src=\"http", "src='http", "href=\"http://cdn"):
            assert bad not in INDEX_HTML, f"引入了外部资源：{bad}"
        # 真的会被浏览器去请求的属性，一律不许有
        for m in re.finditer(r'(?:src|href)\s*=\s*["\']([^"\']+)', INDEX_HTML):
            url = m.group(1)
            assert not url.startswith(("http://", "https://", "//")), (
                f"会去网络请求的地址：{url}"
            )

    def test_log_css_present(self):
        assert ".logbox" in INDEX_HTML and ".logline" in INDEX_HTML

    def test_page_is_one_document_with_scripts_last(self):
        """确认拼装结构没被改坏。

        注意结构是 ``markup + <style> + <script>``，所以 ``</html>`` 在
        **中间**而不是结尾 —— 第一版这里假设它在结尾，直接失败。
        """
        assert INDEX_HTML.startswith("<!DOCTYPE html>")
        assert INDEX_HTML.count("<html") == 1
        assert INDEX_HTML.count("</html>") == 1
        assert INDEX_HTML.count("<script>") == 1, "脚本应合并成一块，别散着"
        # script 必须在 </html> 之后 —— 所以 </html> 前面才是 script
        assert INDEX_HTML.index("</html>") < INDEX_HTML.index("<script>")
