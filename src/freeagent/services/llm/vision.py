"""视觉能力：读一张照片，说出这是什么。

**刻意独立于文本智能层**（``provider.py``），有三个原因：

1. 用的**不是同一个模型** —— 文本可能配 ``deepseek-pro``，视觉只能用
   ``deepseek-flash``。混在一个 provider 里就必须让用户为记一句话
   也配一个视觉模型。
2. **只有一个功能用得上** —— 拍照记一笔。把它塞进 ``LLMProvider``
   会让每个实现都被迫实现一个跟文本无关的方法。
3. **可能整个不可用** —— 没配 Key 时是「没有这个能力」，而不是
   「有个能力返回失败」。

图像**只读进内存，不落盘**。识别完只剩文字；照片里可能有你的地址、
门牌、工牌，留在磁盘上就是长期隐私泄露。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

__all__ = [
    "ImageInput",
    "ProductIdentification",
    "VisionProvider",
    "ALLOWED_MEDIA_TYPES",
    "MAX_IMAGE_BYTES",
]

#: 只接受这四种。DeepSeek 侧也只支持这几种，且**按文件内容判断**格式，
#: 不看扩展名 —— 所以我们也不能靠客户端说的 ``image/png`` 蒙混过关。
ALLOWED_MEDIA_TYPES: tuple[str, ...] = (
    "image/jpeg", "image/png", "image/gif", "image/webp",
)

#: 单张上限。留出 base64 膨胀（~4/3）和 JSON 开销。
MAX_IMAGE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ImageInput:
    """一张待识别的图片。

    ``data`` 是**原始字节**，不是 data URL —— 让调用方决定怎么编码，
    别让传输格式渗进契约。
    """

    data: bytes
    media_type: str
    name: str | None = None

    @property
    def size_hint(self) -> str:
        mb = self.data.__len__() / (1024 * 1024)
        return f"{self.data.__len__() / 1024:.0f} KB" if mb < 1 else f"{mb:.1f} MB"

    def data_url(self) -> str:
        """拼成 base64 data URL（DeepSeek 接受的三种方式之一）。"""
        import base64

        return (
            f"data:{self.media_type};base64,"
            + base64.b64encode(self.data).decode("ascii")
        )


@dataclass(frozen=True, slots=True)
class ProductIdentification:
    """识别结果。

    刻意区分 ``model``（型号）和 ``spec``（规格）—— **搜商品靠型号**。
    「小米」「空气净化器」「滤芯」这些词在电商搜索里几乎没有区分度，
    真正的区分度全在型号上。识别不出来时 ``model`` 就是 ``None``，
    界面据此决定给不给「搜这个型号」按钮。
    """

    ok: bool
    summary: str
    #: 识别所需的搜索词，**按区分度从高到低**。第一个通常是型号。
    search_terms: tuple[str, ...] = ()
    brand: str | None = None
    model: str | None = None
    spec: str | None = None
    #: 铭牌上的原始文字。让人能自己核对，而不是只能信模型。
    raw_text: str | None = None
    #: 识别不可靠时的说明 —— 必须说清哪里没把握。
    note: str | None = None

    def best_query(self) -> str | None:
        """拿哪个词去搜。型号优先，其次完整 summary。"""
        if self.search_terms:
            return self.search_terms[0]
        if self.model:
            return self.model
        return self.summary or None


@runtime_checkable
class VisionProvider(Protocol):
    """读图识物。**只读不猜** —— 认不出就说认不出。"""

    name: str

    def identify_product(self, image: ImageInput) -> ProductIdentification:
        """从照片里读出品牌 / 型号 / 规格。

        认不清时返回 ``ok=False`` 并在 ``note`` 里说清原因，
        **绝不允许编一个像样的型号** —— 编出来的型号会让人买错件。
        """
        ...
