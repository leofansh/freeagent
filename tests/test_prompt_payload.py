"""``prompt_async`` 的**载荷形状** —— 尤其是 ``variant`` 在哪一层。

## 为什么这条文件独立

因为这是一个**真踩过、且没有任何报错**的 bug（2026-10-07，opencode 1.18.34）：

第一版把 ``variant`` 塞进 ``model`` 对象里，依据是 OpenAPI 里
``Session.model`` 的形状。而 ``UserMessage`` **不是**那个形状 ——
``variant`` 是它的**顶层**字段。

后果极其隐蔽：

- 服务端**不报错**（HTTP 204，与乱传一个不存在的档名表现完全一样）
- ``GET /session/{id}`` 回显 ``variant: "default"``
- 而同一个模型的 ``opencode run --variant high`` 回显 ``high``

也就是说「我在飞书里选了 High，实际没生效」，而且**查不出任何原因**。
单测当时全绿 —— 因为断言落在假的 server 上，只能验「我们构造了某个 dict」，
验不了「服务端认不认」。

所以这里除了形状断言，还锁一条**真机**对照（需要 opencode，默认跳过）。
"""

from __future__ import annotations

import json
import pathlib
import time

import pytest

from freeagent.services.opencode_server import OpenCodeServer


class _Capture:
    """记下最后发出去的路径与载荷。"""

    def __init__(self) -> None:
        self.path = ""
        self.payload: dict = {}

    def _must(self, path, method="GET", body=None):
        self.path, self.payload = path, body
        return {}

    def __call__(self, path, method="GET", body=None):
        self._must(path, method, body)


@pytest.fixture
def sent():
    """把 ``_must`` 换成记录器，返回它。"""
    cap = _Capture()
    server = OpenCodeServer.__new__(OpenCodeServer)  # 不走 __init__（不起进程）
    server._must = cap._must
    return cap


# ── 形状 ──────────────────────────────────────────────────────────────── #

def test_variant_is_top_level_not_inside_model(sent):
    """``variant`` 是 ``UserMessage`` 顶层字段。

    放错层级的症状是**静默失效**：服务端 204、会话回显 ``default``，
    而 CLI 的同一意图生效。两者对照才暴露得出来。
    """
    server = OpenCodeServer.__new__(OpenCodeServer)
    server._must = sent._must
    server.prompt_async("ses_1", "做点事",
                        model="opencode/fledge-alpha-free", variant="high")
    assert sent.payload["variant"] == "high", "必须在顶层"
    assert "variant" not in sent.payload["model"], "不能塞进 model 里"


def test_model_uses_modelID_not_id(sent):
    """``model`` 里是 ``modelID``。

    实测传 ``id`` 会被服务端 **400 拒绝**：
    ``{"message":"Missing key\\n  at [\\"model\\\"][\\\"modelID\\\"]"}``
    """
    server = OpenCodeServer.__new__(OpenCodeServer)
    server._must = sent._must
    server.prompt_async("ses_1", "b", model="opencode/fledge-alpha-free")
    assert sent.payload["model"] == {
        "providerID": "opencode", "modelID": "fledge-alpha-free"}


def test_model_is_partitioned_on_first_slash(sent):
    server = OpenCodeServer.__new__(OpenCodeServer)
    server._must = sent._must
    server.prompt_async("ses_1", "b", model="deepseek/deepseek-v4-pro")
    m = sent.payload["model"]
    assert (m["providerID"], m["modelID"]) == ("deepseek", "deepseek-v4-pro")


def test_absent_variant_is_omitted_entirely(sent):
    """省略 ≠ 空串。

    空串会被当成 variant 名去查、然后查不到。实测省略时服务端
    回显 ``default``，那是「模型基线」而不是「查不到」。
    """
    server = OpenCodeServer.__new__(OpenCodeServer)
    server._must = sent._must
    server.prompt_async("ses_1", "b", model="opencode/fledge-alpha-free")
    assert "variant" not in sent.payload


def test_agent_and_model_and_variant_coexist(sent):
    """三个字段互不覆盖 —— 实测模型优先于 agent，两者可同时生效。"""
    server = OpenCodeServer.__new__(OpenCodeServer)
    server._must = sent._must
    server.prompt_async("ses_1", "b", model="opencode/fledge-alpha-free",
                        agent="Sisyphus - ultraworker", variant="high")
    assert sent.payload["agent"] == "Sisyphus - ultraworker"
    assert sent.payload["variant"] == "high"
    assert sent.payload["model"]["modelID"] == "fledge-alpha-free"


def test_always_sends_something(sent):
    """第一版把 ``_must`` 那行丢过，于是 prompt_async 变成静默 no-op。

    症状：所有测试绿、真机不工作。所以这条断言盯的是「真的发出去了」。
    """
    server = OpenCodeServer.__new__(OpenCodeServer)
    server._must = sent._must
    server.prompt_async("ses_1", "做点事")
    assert sent.path.endswith("/prompt_async")
    assert sent.payload["parts"][0]["text"] == "做点事"


def test_directory_is_url_quoted(sent):
    server = OpenCodeServer.__new__(OpenCodeServer)
    server._must = sent._must
    server.prompt_async("ses_1", "b", directory="D:/PycharmProjects/my app")
    assert "directory=D%3A%2FPycharmProjects%2Fmy%20app" in sent.path


# ── 真机：服务端到底认不认 ─────────────────────────────────────────────── #

needs_opencode = pytest.mark.skipif(
    not (pathlib.Path("C:/Users/fanli/AppData/Roaming/npm/opencode.CMD").exists()
         or pathlib.Path("C:/Program Files/nodejs/opencode.cmd").exists()),
    reason="本机没装 opencode",
)


@pytest.mark.live
@needs_opencode
def test_server_actually_records_the_variant():
    """真机：发出去的 variant **被服务端记下来了**。

    这是唯一能证伪「variant 放错层级」的断言 —— 单测验不出，因为假的
    server 会照单全收。读回 ``GET /session/{id}`` 的 ``model.variant``。

    实测对照（2026-10-07）：

    - 放顶层 → 回显 ``high`` ✅
    - 塞进 ``model`` → 回显 ``default`` ❌（且 HTTP 204，无报错）
    - ``opencode run --variant high`` → 回显 ``high`` ✅
    """
    project = pathlib.Path("D:/PycharmProjects/freeagent")
    with OpenCodeServer.discovery(cwd=project) as oc:
        sid = oc.create_session()
        try:
            oc.prompt_async(sid, "只回复一行：OK",
                            model="opencode/fledge-alpha-free", variant="high")
            time.sleep(1.5)
            info = json.loads(oc.call(f"/session/{sid}")[1])
            assert (info.get("model") or {}).get("variant") == "high", (
                "服务端没记下 variant —— 它多半被放进了 model 对象里。"
                "对照：opencode run --variant high 是生效的。")
        finally:
            oc.abort(sid)


@pytest.mark.live
@needs_opencode
def test_omitting_variant_gives_the_model_baseline():
    """不给 variant = 模型基线（回显 ``default``），不是「查不到」。

    实测 4300/8475 个模型带 variants，且**没有一个带 ``default`` 档**，
    所以 ``default`` 是「未施加」而不是「某个默认档」。
    """
    project = pathlib.Path("D:/PycharmProjects/freeagent")
    with OpenCodeServer.discovery(cwd=project) as oc:
        sid = oc.create_session()
        try:
            oc.prompt_async(sid, "只回复一行：OK",
                            model="opencode/fledge-alpha-free")
            time.sleep(1.5)
            info = json.loads(oc.call(f"/session/{sid}")[1])
            assert (info.get("model") or {}).get("variant") == "default"
        finally:
            oc.abort(sid)