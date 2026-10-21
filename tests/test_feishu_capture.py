"""捕获摩擦：飞书图片/文件收下而非丢弃（设计方案 11.9.10 / 20.1.8）。

不破契约：非文字消息**不建事务**——收下的是附件引用 + 下载字节，落盘到
状态目录 ``attachments/``；事务仍要用户后处理时显式发起。这样
``tests/test_doc_contract.py::test_user_docs_must_state_when_the_unsupported_hint_is_sent``
要求的「不建事务」前提依然成立。
"""

import tempfile
from pathlib import Path

from freeagent.feishu.bridge import FeishuBridge
from freeagent.feishu.events import AttachmentRef, IncomingMessage


class _FakeDedup:
    def __init__(self):
        self.seen = set()

    def is_duplicate(self, key):
        if key in self.seen:
            return True
        self.seen.add(key)
        return False


class _FakeChannel:
    def __init__(self):
        self.dedup = _FakeDedup()
        self.allowed = {"ou_a"}

    def is_allowed(self, ids):
        return bool(ids & self.allowed)

    def allowlist_mismatch(self, ids):
        return f"不在白名单: {ids}"


class _FakeSender:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def send_text(self, chat_id, text):
        self.sent.append((chat_id, text))

    def download_media(self, key, *, media_type, downloader=None):
        if self.fail:
            raise RuntimeError("no network")
        return b"IMGDATA-bytes"


def _bridge(fail=False, home=None):
    if home is None:
        home = Path(tempfile.mkdtemp())
    return FeishuBridge(
        _FakeChannel(), _FakeSender(fail=fail), config=None, home=home,
    ), home


def _msg(**kw):
    base = dict(
        event_id="e1", chat_id="oc_x", sender_open_id="ou_a", text="",
        is_group=False, mentioned=True, unsupported_type="image",
        message_id="m1", attachment=None,
    )
    base.update(kw)
    return IncomingMessage(**base)


def test_capture_image_download_success():
    b, home = _bridge()
    b._capture_unsupported(
        _msg(attachment=AttachmentRef("image", "k1", "image", 0)),
    )
    assert any("已收下" in t for _, t in b.sender.sent), b.sender.sent
    files = list(home.glob("attachments/*"))
    assert files and files[0].read_bytes() == b"IMGDATA-bytes", files


def test_capture_download_failure_records_reference():
    b, home = _bridge(fail=True)
    b._capture_unsupported(
        _msg(attachment=AttachmentRef("file", "k2", "报表.pdf", 1234)),
    )
    assert any("下载不下来" in t for _, t in b.sender.sent), b.sender.sent
    metas = list(home.glob("attachments/*.meta.json"))
    assert metas, "下载失败时没记下引用"


def test_capture_rich_text_still_replies_and_drops_nothing():
    b, home = _bridge()
    b._capture_unsupported(_msg(unsupported_type="post"))
    assert any("我只处理文字" in t for _, t in b.sender.sent), b.sender.sent


def test_capture_blocked_when_not_allowlisted():
    b, home = _bridge()
    b._capture_unsupported(
        _msg(sender_open_id="ou_b",
             attachment=AttachmentRef("image", "k3", "image", 0)),
    )
    assert not b.sender.sent


def test_capture_dedup_no_double_capture():
    b, home = _bridge()
    att = AttachmentRef("image", "k4", "image", 0)
    b._capture_unsupported(_msg(event_id="dup", message_id="", attachment=att))
    first = len(b.sender.sent)
    b._capture_unsupported(_msg(event_id="dup", message_id="", attachment=att))
    assert len(b.sender.sent) == first, "重复事件被二次收下"


def test_extract_attachment_from_image_and_file_content():
    from freeagent.feishu.events import _extract_attachment

    img = _extract_attachment("image", '{"image_key": "im_abc"}')
    assert img is not None and img.type == "image" and img.key == "im_abc"

    fle = _extract_attachment(
        "file", '{"file_key": "fi_xyz", "file_name": "a.pdf", "file_size": 99}',
    )
    assert fle is not None and fle.key == "fi_xyz" and fle.name == "a.pdf"
    assert fle.size == 99

    assert _extract_attachment("post", "{}") is None
