"""اختبارات الدمج (v4.9) — تغطي التحصينات المرحّلة إلى fb_youtube_uploader_v49_pro.py.

تشمل: تصنيف أخطاء الرفع، التراجع الأسّي، استئناف الرفع، التحقق بعد الرفع،
إخفاء اسم الصفحة المصدر، توحيد رابط الريلز، الترتيب الحتمي، وفحص/تنظيف المقطع.

شغّل: python -m pytest -q test_merge_v49.py
"""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from googleapiclient.errors import HttpError

MODULE_PATH = Path(__file__).with_name("fb_youtube_uploader_v49_pro.py")
spec = importlib.util.spec_from_file_location("merged49", MODULE_PATH)
assert spec and spec.loader
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


# ─────────────────────────── أدوات مساعدة ───────────────────────────
class FakeResp:
    def __init__(self, status: int = 404, reason: str = "Not Found") -> None:
        self.status = status
        self.reason = reason


def _http_error(status: int, reason: str = "") -> HttpError:
    body = json.dumps({"error": {"errors": [{"reason": reason}]}}).encode() if reason else b"{}"
    return HttpError(FakeResp(status), body)


class FakeUploadRequest:
    def __init__(self, video_id: str = "youtube-video-id") -> None:
        self._video_id = video_id

    def next_chunk(self):
        return None, {"id": self._video_id}


class FlakyYouTube:
    """يرفع خطأً في أول N محاولة ثم ينجح."""

    def __init__(self, failures) -> None:
        self.failures = list(failures)
        self.calls = 0

    def videos(self):
        return self

    def insert(self, **_kwargs):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return FakeUploadRequest()


class ListYouTube:
    def __init__(self, items) -> None:
        self._items = items

    def videos(self):
        return self

    def list(self, **_kwargs):
        return self

    def execute(self):
        return {"items": self._items}


# ─────────────────────────── تصنيف الأخطاء والتراجع ───────────────────────────
def test_classify_quota_vs_rate_limit() -> None:
    assert app.classify_upload_error(_http_error(403, "quotaExceeded")) == "quota"
    assert app.classify_upload_error(_http_error(429, "rateLimitExceeded")) == "retryable"
    assert app.classify_upload_error(_http_error(403, "userRateLimitExceeded")) == "retryable"
    assert app.classify_upload_error(_http_error(503, "backendError")) == "retryable"


def test_classify_auth_and_fatal() -> None:
    assert app.classify_upload_error(_http_error(403, "")) == "auth"
    assert app.classify_upload_error(_http_error(401, "")) == "fatal"
    assert app.classify_upload_error(_http_error(400, "invalidMetadata")) == "fatal"


def test_backoff_grows_with_cap() -> None:
    assert 8 <= app._backoff_seconds(1) <= 12
    assert app._backoff_seconds(2) >= 24
    assert app._backoff_seconds(10) <= 304


# ─────────────────────────── الرفع: إعادة المحاولة والاستئناف ───────────────────────────
def test_upload_retries_rate_limit_then_succeeds(monkeypatch, tmp_path: Path) -> None:
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"video-bytes")
    monkeypatch.setattr(app.time, "sleep", lambda *_a: None)
    yt = FlakyYouTube([_http_error(429, "rateLimitExceeded"), _http_error(503, "")])
    assert app.upload_video(yt, {"file": str(f), "title": "t", "desc": "d"}) == "youtube-video-id"
    assert yt.calls == 3


def test_upload_quota_stops_immediately(monkeypatch, tmp_path: Path) -> None:
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"video-bytes")
    monkeypatch.setattr(app.time, "sleep", lambda *_a: None)
    yt = FlakyYouTube([_http_error(403, "quotaExceeded")])
    assert app.upload_video(yt, {"file": str(f), "title": "t", "desc": "d"}) == "QUOTA_ERROR"
    assert yt.calls == 1


def test_upload_auth_error_no_retry(monkeypatch, tmp_path: Path) -> None:
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"video-bytes")
    monkeypatch.setattr(app.time, "sleep", lambda *_a: None)
    yt = FlakyYouTube([_http_error(403, "")])
    assert app.upload_video(yt, {"file": str(f), "title": "t", "desc": "d"}) is None
    assert yt.calls == 1


class ResumingRequest:
    def __init__(self) -> None:
        self.calls = 0

    def next_chunk(self):
        self.calls += 1
        if self.calls == 1:
            raise _http_error(429, "rateLimitExceeded")
        return None, {"id": "vid-resumed"}


class ResumeYouTube:
    def __init__(self) -> None:
        self.request = ResumingRequest()

    def videos(self):
        return self

    def insert(self, **_kwargs):
        return self.request


def test_upload_resumes_after_transient_chunk_failure(monkeypatch, tmp_path: Path) -> None:
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"video-bytes")
    monkeypatch.setattr(app.time, "sleep", lambda *_a: None)
    yt = ResumeYouTube()
    assert app.upload_video(yt, {"file": str(f), "title": "t", "desc": "d"}) == "vid-resumed"
    assert yt.request.calls == 2


def test_upload_missing_file_returns_none() -> None:
    assert app.upload_video(object(), {"file": "/no/such.mp4", "title": "t", "desc": "d"}) is None


def test_playlist_failure_does_not_duplicate_upload(monkeypatch, tmp_path: Path) -> None:
    """فشل إضافة قائمة التشغيل كان يعيد رفع الفيديو كاملاً — الآن لا."""
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"video-bytes")

    class PlaylistFails:
        def insert(self, **_kwargs):
            return self

        def execute(self):
            raise HttpError(FakeResp(404), b"{}")

    class YT:
        def videos(self):
            return self

        def insert(self, **_kwargs):
            return FakeUploadRequest()

        def playlistItems(self):
            return PlaylistFails()

    result = app.upload_video(YT(), {"file": str(f), "title": "t", "desc": "d"},
                              playlist_id="PL1")
    assert result == "youtube-video-id"


# ─────────────────────────── التحقق بعد الرفع ───────────────────────────
def test_verify_upload_warns_when_public_requested_but_private() -> None:
    messages: list[str] = []
    app.verify_upload(ListYouTube([{"status": {"privacyStatus": "private"}}]),
                      "vid", "public", None, messages.append)
    assert any("private" in m or "غير مُدقّق" in m for m in messages)


def test_verify_upload_accepts_schedule() -> None:
    messages: list[str] = []
    app.verify_upload(ListYouTube([{"status": {"privacyStatus": "private",
                                               "publishAt": "2026-09-13T10:00:00Z"}}]),
                      "vid", "private",
                      datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc), messages.append)
    assert any("الجدولة مقبولة" in m for m in messages)


def test_verify_upload_flags_missing_publish_at() -> None:
    messages: list[str] = []
    app.verify_upload(ListYouTube([{"status": {"privacyStatus": "private"}}]),
                      "vid", "private",
                      datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc), messages.append)
    assert any("لم يقبل وقت الجدولة" in m for m in messages)


def test_verify_upload_soft_on_failure() -> None:
    class Broken:
        def videos(self):
            raise RuntimeError("no scope")

    assert app.verify_upload(Broken(), "vid", "public", None, None) is None


# ─────────────────────────── إخفاء اسم الصفحة المصدر ───────────────────────────
def test_title_hides_page_name_and_junk() -> None:
    raw = "35K views · 576 reactions | جيجل حين تعانق الزعانف الأمواج | Elwatania TV"
    out = app.build_title(raw, ["Elwatania TV"])
    assert "views" not in out and "reactions" not in out
    assert "Elwatania" not in out
    assert "جيجل حين تعانق الزعانف الأمواج" in out


def test_sanitize_removes_page_name_variants_and_links() -> None:
    out = app.sanitize_public_text("ComedyClub يقدم https://fb.com/x مقطعاً", ["Comedy Club"])
    assert "comedyclub" not in out.lower()
    assert "http" not in out


def test_sanitize_keeps_hashtags_by_default() -> None:
    out = app.sanitize_public_text("رحلة & مغامرة #reel 2026")
    assert "&" in out and "#reel" in out


def test_clean_title_truncates_and_keeps_hash() -> None:
    title = app.clean_title("<b>عنوان</b>" + "ط" * 200)
    assert len(title) <= 100 and "<" not in title


# ─────────────────────────── الروابط والترتيب ───────────────────────────
def test_normalize_channel_url() -> None:
    assert app.normalize_channel_url("https://www.facebook.com/jireel") == \
        "https://www.facebook.com/jireel/reels"
    assert "sk=reels_tab" in app.normalize_channel_url(
        "https://www.facebook.com/profile.php?id=61554746552594")
    assert app.normalize_channel_url("https://www.facebook.com/x/reels") == \
        "https://www.facebook.com/x/reels"
    with pytest.raises(ValueError):
        app.normalize_channel_url("https://www.youtube.com/x")


def test_safe_normalize_never_raises() -> None:
    assert app._safe_normalize_channel_url("https://youtube.com/x") == "https://youtube.com/x"


def test_order_reels_dedupes_newest_first() -> None:
    links = ["https://www.facebook.com/reel/100", "https://www.facebook.com/reel/300",
             "https://www.facebook.com/reel/100", "https://www.facebook.com/reel/200"]
    assert app.order_reels(links, True) == [
        "https://www.facebook.com/reel/300",
        "https://www.facebook.com/reel/200",
        "https://www.facebook.com/reel/100",
    ]


def test_sanitize_account_name_blocks_traversal() -> None:
    assert app.sanitize_account_name("../../etc/passwd") == "etc_passwd"
    assert app.sanitize_account_name("") == "account1"


# ─────────────────────────── فحص وتنظيف المقطع ───────────────────────────
def test_validate_media_rejects_missing_file(tmp_path: Path) -> None:
    ok, info = app.validate_media(str(tmp_path / "none.mp4"))
    assert ok is False
    assert info.get("error") == "missing-file"


def test_validate_media_is_lenient_when_probe_fails(tmp_path: Path) -> None:
    """ملف ليس فيديو حقيقياً: ffprobe يفشل → نعتبره مقبولاً (لا نرفض بسبب أداة الفحص)."""
    fake = tmp_path / "not-a-video.mp4"
    fake.write_bytes(b"not a video")
    ok, _info = app.validate_media(str(fake))
    assert ok is True


def test_validate_media_without_ffprobe_is_lenient(monkeypatch, tmp_path: Path) -> None:
    fake = tmp_path / "x.mp4"
    fake.write_bytes(b"x")
    monkeypatch.setattr(app.shutil, "which", lambda _name: None)
    ok, _info = app.validate_media(str(fake))
    assert ok is True


def test_finalize_video_without_ffmpeg_returns_original(monkeypatch, tmp_path: Path) -> None:
    f = tmp_path / "v.mp4"
    f.write_bytes(b"video")
    monkeypatch.setattr(app.shutil, "which", lambda _name: None)
    assert app.finalize_video(str(f)) == str(f)


def test_finalize_video_success_returns_cleaned(monkeypatch, tmp_path: Path) -> None:
    f = tmp_path / "v.mp4"
    f.write_bytes(b"video")
    out = tmp_path / "v.clean.mp4"

    monkeypatch.setattr(app.shutil, "which", lambda _name: "/fake/ffmpeg")

    def fake_run(cmd, **_kwargs):
        out.write_bytes(b"cleaned")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(app.subprocess, "run", fake_run)
    assert app.finalize_video(str(f)) == str(out)


def test_finalize_video_failure_returns_original(monkeypatch, tmp_path: Path) -> None:
    f = tmp_path / "v.mp4"
    f.write_bytes(b"video")
    monkeypatch.setattr(app.shutil, "which", lambda _name: "/fake/ffmpeg")
    monkeypatch.setattr(app.subprocess, "run",
                        lambda *_a, **_k: SimpleNamespace(returncode=1, stderr="boom"))
    assert app.finalize_video(str(f)) == str(f)


# ─────────────────────────── التكامل: تنزيل يحجب اسم الصفحة ───────────────────────────
def test_download_hides_page_and_uploader_names(monkeypatch, tmp_path: Path) -> None:
    """تنزيل محاكى: العنوان الخام يحمل اسم الصفحة والرافع — يجب ألا يظهر أي منهما."""
    video = tmp_path / "123.mp4"
    video.write_bytes(b"video")

    class FakeYDL:
        def __init__(self, _opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def extract_info(self, _url, download=True):
            return {
                "id": "123",
                "title": "12K views | عنوان المقطع | Elwatania TV",
                "description": "وصف من Elwatania TV مع رابط https://fb.com/x",
                "uploader": "Elwatania TV",
                "duration": 30,
                "filepath": str(video),
            }

        def prepare_filename(self, _info):
            return str(video)

    monkeypatch.setattr(app.yt_dlp, "YoutubeDL", FakeYDL)
    data = app.download_video("https://facebook.com/reel/123", "الوطنية TV")
    assert data is not None
    assert "Elwatania" not in data["title"]
    assert "الوطنية" not in data["title"]
    assert "views" not in data["title"]
    assert "Elwatania" not in data["desc"]
    assert "http" not in data["desc"]
