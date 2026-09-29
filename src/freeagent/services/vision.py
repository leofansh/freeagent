"""拍照识物 → 建任务 / 一键去搜。

**为什么不接电商 API**
--------------------
读铭牌是难点，搜商品不是：型号 ``KJ-800-G`` 复制到京东搜索框 3 秒就有结果，
而**京东自己的搜索页比任何 API 都好用** —— 有你的登录态、优惠券、评价筛选、
价格曲线。API 只能给字段。

所以这里只生成**深链**：点一下跳到京东/淘宝的真实搜索结果页。
零 API、零密钥、零资质，顺带保住这个工具「本地、断网可用」的属性。

**图片不落盘**
--------------
铭牌照片里可能有地址、门牌、工牌、手机号。识别完只剩文字，图片字节
用完即弃 —— 不写进数据库、不写进临时文件、不进日志。
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from urllib.parse import quote

from ..domain import FreeAgentError
from .llm.vision import (
    ALLOWED_MEDIA_TYPES,
    MAX_IMAGE_BYTES,
    ImageInput,
    ProductIdentification,
    VisionProvider,
)

__all__ = [
    "VisionResult",
    "identify",
    "decode_upload",
    "search_links",
    "SHOPPING_TARGETS",
]

#: 跳转目标。**只给搜索页深链，不调任何接口。**
SHOPPING_TARGETS: tuple[tuple[str, str], ...] = (
    ("京东", "https://search.jd.com/Search?keyword={q}"),
    ("淘宝", "https://s.taobao.com/search?q={q}"),
    ("什么值得买", "https://www.smzdm.com/search/?c=home&s={q}"),
)

#: data URL 形如 ``data:image/jpeg;base64,XXXX``
_DATA_URL_RE = re.compile(
    r"^data:(?P<media>image/[a-zA-Z0-9.+-]+);base64,(?P<data>.+)$", re.S
)

#: 图像文件魔数。**DeepSeek 侧按内容判断格式、不看扩展名**，
#: 所以我们也必须看内容 —— 只信客户端说的 Content-Type 等于不设防。
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def _sniff(data: bytes) -> str | None:
    for magic, media in _MAGIC:
        if data.startswith(magic):
            return media
    # WebP: "RIFF....WEBP"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


@dataclass(frozen=True, slots=True)
class VisionResult:
    """识别结果 + 跳转链接。"""

    identification: ProductIdentification
    links: tuple[tuple[str, str], ...] = ()
    query: str | None = None

    @property
    def ok(self) -> bool:
        return self.identification.ok


def decode_upload(data_url: str) -> ImageInput:
    """把浏览器传来的 data URL 解成 :class:`ImageInput`，**顺带做安全校验**。

    校验顺序刻意是「先看魔数，再看声明的类型」：声称自己是 PNG 的 JPEG
    会在这里被改写成 JPEG，而不是被拒 —— 因为 DeepSeek 是按内容判断的，
    统一了就不会出现「格式与实际不符」的怪问题。
    """
    if not data_url:
        raise FreeAgentError("没有收到图片")
    match = _DATA_URL_RE.match(data_url.strip())
    if not match:
        raise FreeAgentError("图片格式不对（要 JPEG / PNG / GIF / WebP）")
    try:
        raw = base64.b64decode(match.group("data"), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise FreeAgentError("图片编码坏了，重新选一次") from exc
    if not raw:
        raise FreeAgentError("图片是空的")
    if len(raw) > MAX_IMAGE_BYTES:
        mb = len(raw) / (1024 * 1024)
        raise FreeAgentError(f"图片太大了（{mb:.1f} MB，上限 8 MB）")

    actual = _sniff(raw)
    if actual is None:
        raise FreeAgentError("这不是 JPEG / PNG / GIF / WebP 图片")
    declared = match.group("media").lower()
    # 声明与实际不一致时不报错，用实际的 —— 但仍要确保它在白名单里
    if declared not in ALLOWED_MEDIA_TYPES:
        raise FreeAgentError(f"不支持的图片类型：{declared}")
    return ImageInput(data=raw, media_type=actual, name=None)


def search_links(query: str) -> tuple[tuple[str, str], ...]:
    """按搜索词生成跳转链接。**不做任何联网请求。**"""
    q = query.strip()
    if not q:
        return ()
    encoded = quote(q, safe="")
    return tuple((name, url.format(q=encoded)) for name, url in SHOPPING_TARGETS)


def identify(
    provider: VisionProvider | None, data_url: str
) -> VisionResult:
    """端到端：校验上传 → 调模型 → 拼链接。

    顺序是刻意的：**先解码校验，再问有没有能力**。一个 9 MB 的请求
    不该先收到「没配 Key」——那既答非所问，也白烧一次解码。

    ``provider`` 为 ``None`` 表示**没配置视觉能力**。这时必须明确说清，
    而不是返回一个「识别失败」假装试过了。
    """
    image = decode_upload(data_url)
    if provider is None:
        raise FreeAgentError(
            "没配置视觉模型，拍照识别用不了。"
            "请设置 DEEPSEEK_API_KEY 后重启服务。"
        )
    result = provider.identify_product(image)
    query = result.best_query() if result.ok else None
    return VisionResult(
        identification=result,
        links=search_links(query) if query else (),
        query=query,
    )


def as_task_payload(identification: ProductIdentification) -> dict:
    """转成建单时要用的标题/意图。"""
    bits = [identification.summary]
    if identification.model:
        bits.append(f"型号 {identification.model}")
    if identification.spec:
        bits.append(identification.spec)
    return {
        "title": identification.summary or "买配件",
        "intent": "；".join(bits),
    }
