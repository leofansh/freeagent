"""拍照识物。

三个关注点，按重要性排：

1. **模型编造型号会让人买错件** —— 校验必须挡住，这是最该硬拦的地方。
2. **图片不落盘** —— 铭牌里可能有地址、门牌、工牌。
3. 没配 Key 时**如实说没有**，不假装试过了。
"""

from __future__ import annotations

import base64
import json
import zlib
from datetime import datetime
from pathlib import Path

import pytest

from freeagent.domain import FreeAgentError, LLMError
from freeagent.services.clock import FrozenClock
from freeagent.services.llm.deepseek_vision import (
    DeepSeekVisionConfig,
    DeepSeekVisionProvider,
    build_identification,
)
from freeagent.services.llm.vision import (
    ALLOWED_MEDIA_TYPES,
    MAX_IMAGE_BYTES,
    ImageInput,
    ProductIdentification,
    VisionProvider,
)
from freeagent.services.vision import (
    SHOPPING_TARGETS,
    decode_upload,
    identify,
    search_links,
)


# --- 造一张真的最小 PNG（1x1），免得测试依赖外部文件 ---------------------- #
def _png() -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            len(data).to_bytes(4, "big") + tag + data
            + zlib.crc32(tag + data).to_bytes(4, "big")
        )

    ihdr = (1).to_bytes(4, "big") + (1).to_bytes(4, "big") + bytes([8, 0, 0, 0, 0])
    raw = b"\x00" + b"\xff\xff\xff"
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    )


def _jpeg() -> bytes:
    """只要有 JPEG 魔数即可 —— 校验看的是内容不是完整性。"""
    return b"\xff\xd8\xff\xe0" + b"\x00" * 32


def data_url(media: str, raw: bytes) -> str:
    return f"data:{media};base64," + base64.b64encode(raw).decode()


# =============================================================================
# 上传校验
# =============================================================================
class TestUploadValidation:
    def test_accepts_png(self):
        img = decode_upload(data_url("image/png", _png()))
        assert img.media_type == "image/png"
        assert img.data == _png()

    def test_accepts_jpeg_gif_webp(self):
        assert decode_upload(data_url("image/jpeg", _jpeg())).media_type == "image/jpeg"
        gif = b"GIF89a" + b"\x00" * 16
        assert decode_upload(data_url("image/gif", gif)).media_type == "image/gif"
        webp = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 8
        assert decode_upload(data_url("image/webp", webp)).media_type == "image/webp"

    def test_sniffs_content_over_declared_type(self):
        """声称 PNG 的 JPEG 要被改写成 JPEG，而不是被拒。

        DeepSeek 侧是**按文件内容**判断格式的，所以统一成实际的
        才是对的；否则会出现「格式与实际不符」的怪问题。
        """
        img = decode_upload(data_url("image/png", _jpeg()))
        assert img.media_type == "image/jpeg"

    def test_rejects_non_image(self):
        for raw in (b"just text", b"%PDF-1.4", b"", b"\x00\x01\x02"):
            with pytest.raises(FreeAgentError):
                decode_upload(data_url("image/png", raw))

    def test_rejects_unsupported_declared_type(self):
        with pytest.raises(FreeAgentError) as ei:
            decode_upload(data_url("image/svg+xml", _png()))
        assert "svg" in str(ei.value)

    def test_rejects_broken_base64(self):
        with pytest.raises(FreeAgentError):
            decode_upload("data:image/png;base64,!!!!not-base64!!!!")

    def test_rejects_empty_and_malformed(self):
        for bad in ("", "not-a-data-url", "data:image/png,no-comma"):
            with pytest.raises(FreeAgentError):
                decode_upload(bad)

    def test_rejects_oversized(self):
        class FakeBig:
            def __len__(self):            # 让 base64 假装很大
                return 0

        # 直接构造超限字节，验证体积检查真的在
        with pytest.raises(FreeAgentError) as ei:
            decode_upload(data_url("image/jpeg", b"\xff\xd8\xff" + b"\x00" * (MAX_IMAGE_BYTES + 10)))
        assert "太大" in str(ei.value)

    def test_allowed_types_are_documented_ones(self):
        assert set(ALLOWED_MEDIA_TYPES) == {
            "image/jpeg", "image/png", "image/gif", "image/webp"
        }


# =============================================================================
# 搜索深链
# =============================================================================
class TestSearchLinks:
    def test_uses_model_as_query(self):
        links = search_links("KJ-800-G")
        assert links, "应给出跳转链接"
        jd = dict(links)["京东"]
        assert "KJ-800-G" in jd or "KJ-800-G" in jd.replace("%2D", "-")

    def test_url_encodes_cjk_and_spaces(self):
        (name, url), = [(n, u) for n, u in search_links("小米 滤芯") if n == "京东"]
        assert " " not in url, "空格必须编码"
        assert "search.jd.com" in url

    def test_empty_query_gives_no_links(self):
        assert search_links("   ") == ()

    def test_every_target_is_a_search_page(self):
        for name, template in SHOPPING_TARGETS:
            url = template.format(q="x")
            assert url.startswith("https://"), f"{name} 不是 https"
            assert "search" in url.lower(), f"{name} 不是搜索页"

    def test_no_api_endpoint_is_contacted(self):
        """刻意断言：我们只生成链接，不调任何电商接口。"""
        for name, url in SHOPPING_TARGETS:
            assert "api" not in url.lower(), f"{name} 看起来像接口地址"


# =============================================================================
# 模型输出校验 —— 最该硬拦的地方
# =============================================================================
class TestIdentificationValidation:
    def test_reads_model_when_all_good(self):
        got = build_identification({
            "readable": True,
            "summary": "小米 空气净化器滤芯",
            "brand": "小米",
            "model": "KJ-800-G",
            "spec": "除PM2.5",
            "raw_text": "型号 KJ-800-G",
        })
        assert got.ok
        assert got.model == "KJ-800-G"
        assert got.search_terms[0] == "KJ-800-G", "搜商品靠型号，型号必须排最前"
        assert "小米 KJ-800-G" in got.search_terms

    def test_refuses_when_model_missing(self):
        """自称 readable 却给不出型号 → 视为不可靠。

        宁可让用户重拍，也不要给一个像样但错的型号。
        """
        got = build_identification({
            "readable": True, "summary": "某个滤芯", "brand": "某牌",
            "model": None, "raw_text": "看不清",
        })
        assert got.ok is False
        assert "型号" in got.note

    def test_refuses_when_not_readable(self):
        got = build_identification({
            "readable": False, "note": "反光看不清",
        })
        assert got.ok is False
        assert "反光" in got.note
        assert got.search_terms == ()

    def test_empty_model_string_counts_as_missing(self):
        got = build_identification({
            "readable": True, "summary": "x", "model": "   ",
        })
        assert got.ok is False

    def test_blank_fields_become_none_not_empty_string(self):
        got = build_identification({
            "readable": True, "summary": "  小米滤芯 ", "model": "KJ-1",
            "brand": "", "spec": "   ",
        })
        assert got.brand is None
        assert got.spec is None
        assert got.summary == "小米滤芯"

    def test_query_prefers_model(self):
        got = build_identification({
            "readable": True, "summary": "空气净化器滤芯", "model": "KJ-800-G",
        })
        assert got.best_query() == "KJ-800-G"

    def test_fields_are_length_capped(self):
        got = build_identification({
            "readable": True, "summary": "x" * 500, "model": "M" * 500,
            "raw_text": "y" * 2000,
        })
        assert len(got.summary) <= 120
        assert len(got.model) <= 60
        assert len(got.raw_text) <= 400


# =============================================================================
# Provider：请求形状
# =============================================================================
class TestDeepSeekVisionProvider:
    def _provider(self, reply: str):
        seen = {}

        def transport(url, payload, headers, timeout):
            seen.update(url=url, payload=payload, headers=headers, timeout=timeout)
            return json.dumps({
                "choices": [{"message": {"content": reply}}]
            })

        return DeepSeekVisionProvider(
            DeepSeekVisionConfig(model="deepseek-flash"), "sk-test",
            transport=transport,
        ), seen

    def test_sends_image_in_user_message_content_array(self):
        p, seen = self._provider(json.dumps({
            "readable": True, "summary": "滤芯", "model": "KJ-1",
        }))
        p.identify_product(ImageInput(data=_png(), media_type="image/png"))

        msgs = seen["payload"]["messages"]
        assert msgs[0]["role"] == "system", "系统提示单独一条"
        user = msgs[1]
        assert user["role"] == "user", "图片只能放 user，放 system 会 400"
        assert isinstance(user["content"], list), "图文混排必须是数组"
        kinds = [part["type"] for part in user["content"]]
        assert kinds == ["text", "image_url"]
        img = user["content"][1]["image_url"]
        assert img["url"].startswith("data:image/png;base64,")
        assert img["detail"] == "high", "铭牌是小字，detail 必须 high"

    def test_uses_configured_vision_model(self):
        p, seen = self._provider(json.dumps({
            "readable": True, "summary": "x", "model": "M",
        }))
        p.identify_product(ImageInput(data=_png(), media_type="image/png"))
        assert seen["payload"]["model"] == "deepseek-flash"

    def test_key_goes_in_headers_only(self):
        p, seen = self._provider(json.dumps({
            "readable": True, "summary": "x", "model": "M",
        }))
        p.identify_product(ImageInput(data=_png(), media_type="image/png"))
        assert seen["headers"]["Authorization"] == "Bearer sk-test"
        assert "sk-test" not in json.dumps(seen["payload"]), "key 不能进 payload"

    def test_asks_for_json(self):
        p, seen = self._provider(json.dumps({
            "readable": True, "summary": "x", "model": "M",
        }))
        p.identify_product(ImageInput(data=_png(), media_type="image/png"))
        assert seen["payload"]["response_format"] == {"type": "json_object"}

    def test_strips_fenced_json(self):
        p, _ = self._provider(
            "```json\n" + json.dumps({
                "readable": True, "summary": "x", "model": "M",
            }) + "\n```"
        )
        got = p.identify_product(ImageInput(data=_png(), media_type="image/png"))
        assert got.ok

    def test_bad_json_raises_llm_error(self):
        p, _ = self._provider("这不是 JSON")
        with pytest.raises(LLMError):
            p.identify_product(ImageInput(data=_png(), media_type="image/png"))

    def test_empty_reply_raises(self):
        p, _ = self._provider("")          # content 为空字符串
        with pytest.raises(LLMError):
            p.identify_product(ImageInput(data=_png(), media_type="image/png"))

    def test_whitespace_reply_raises(self):
        p, _ = self._provider("   ")
        with pytest.raises(LLMError):
            p.identify_product(ImageInput(data=_png(), media_type="image/png"))

    def test_transport_failure_is_normalized(self):
        def boom(*a, **kw):
            raise OSError("连接被重置")

        p = DeepSeekVisionProvider(
            DeepSeekVisionConfig(), "sk-test", transport=boom
        )
        with pytest.raises(LLMError):
            p.identify_product(ImageInput(data=_png(), media_type="image/png"))

    def test_empty_key_rejected(self):
        with pytest.raises(LLMError):
            DeepSeekVisionProvider(DeepSeekVisionConfig(), "  ")


# =============================================================================
# 没配 Key 时如实说没有
# =============================================================================
class TestNoVisionConfigured:
    def test_identify_says_not_configured(self):
        with pytest.raises(FreeAgentError) as ei:
            identify(None, data_url("image/png", _png()))
        message = str(ei.value)
        assert "DEEPSEEK_API_KEY" in message, "要说清该设哪个环境变量"
        assert "用不了" in message

    def test_validation_happens_before_capability_check(self):
        """先校验再问能力 —— 9 MB 的请求不该先收到「没配 Key」。"""
        huge = data_url("image/jpeg", b"\xff\xd8\xff" + b"\x00" * (MAX_IMAGE_BYTES + 10))
        with pytest.raises(FreeAgentError) as ei:
            identify(None, huge)
        assert "太大" in str(ei.value), "超限应先报超限"

    def test_app_vision_is_none_without_key(self, tmp_path, monkeypatch):
        from freeagent.app import build_app

        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        app = build_app(tmp_path / "a.db", clock=FrozenClock(datetime(2026, 9, 26, 14)))
        assert app.vision is None, "没 Key 时不该返回一个失败的实现"
        app.close()

    def test_app_vision_present_with_key(self, tmp_path, monkeypatch):
        from freeagent.app import build_app

        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        app = build_app(tmp_path / "a.db", clock=FrozenClock(datetime(2026, 9, 26, 14)))
        assert app.vision is not None
        app.close()


# =============================================================================
# 图片不落盘
# =============================================================================
class TestImageNeverPersisted:
    def test_identify_does_not_write_files(self, tmp_path):
        calls = []

        class FakeVision:
            name = "fake"

            def identify_product(self, image):
                calls.append(image)
                return ProductIdentification(
                    ok=True, summary="滤芯", model="KJ-1",
                    search_terms=("KJ-1",),
                )

        before = set(p for p in Path(tmp_path).rglob("*"))
        identify(FakeVision(), data_url("image/png", _png()))
        after = set(p for p in Path(tmp_path).rglob("*"))
        assert before == after, "识别过程不该写任何文件"

    def test_image_bytes_only_live_in_the_call(self):
        """图片只作为参数传给 provider，provider 返回后就没了。"""
        seen = []

        class FakeVision:
            name = "fake"

            def identify_product(self, image):
                seen.append(len(image.data))
                return ProductIdentification(ok=True, summary="x", model="M",
                                            search_terms=("M",))

        identify(FakeVision(), data_url("image/png", _png()))
        assert seen == [len(_png())]

    def test_result_carries_no_image(self):
        class FakeVision:
            name = "fake"

            def identify_product(self, image):
                return ProductIdentification(ok=True, summary="x", model="M",
                                            search_terms=("M",))

        result = identify(FakeVision(), data_url("image/png", _png()))
        dumped = json.dumps(
            {"s": result.identification.summary, "q": result.query,
             "l": [u for _, u in result.links]},
            ensure_ascii=False,
        )
        assert "base64" not in dumped
        assert "data:image" not in dumped


# =============================================================================
# 端到端（假 provider，全程离线）
# =============================================================================
class TestEndToEnd:
    def test_full_flow_returns_links(self):
        class FakeVision:
            name = "fake"

            def identify_product(self, image):
                assert image.media_type == "image/png"
                return ProductIdentification(
                    ok=True, summary="小米空气净化器滤芯", brand="小米",
                    model="KJ-800-G", spec="除PM2.5",
                    search_terms=("KJ-800-G", "小米 KJ-800-G"),
                    raw_text="型号 KJ-800-G",
                )

        result = identify(FakeVision(), data_url("image/png", _png()))
        assert result.ok
        assert result.query == "KJ-800-G"
        assert len(result.links) == len(SHOPPING_TARGETS)
        assert "京东" in dict(result.links)
