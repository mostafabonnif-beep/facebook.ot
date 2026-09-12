"""اختبارات وحدة للمنطق الأساسي في fb_youtube_uploader_v33.

لا تلمس هذه الاختبارات واجهة Tkinter ولا تتصل بـ Facebook أو YouTube.
شغّلها بـ:  python -m pytest -q
"""
from __future__ import annotations

import importlib.util
import queue
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from googleapiclient.errors import HttpError

MODULE_PATH = Path(__file__).with_name("fb_youtube_uploader_v33.py")
spec = importlib.util.spec_from_file_location("fb2yt", MODULE_PATH)
assert spec and spec.loader
fixed_app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixed_app)


# ─────────────────────────── أدوات مساعدة ───────────────────────────
class FakeDriver:
    def quit(self) -> None:
        return None


class FakeResp:
    def __init__(self, status: int = 404, reason: str = "Not Found") -> None:
        self.status = status
        self.reason = reason


class FakeUploadRequest:
    """يحاكي videos().insert(...) مع next_chunk()."""

    def __init__(self, video_id: str = "youtube-video-id") -> None:
        self._video_id = video_id
        self._done = False

    def next_chunk(self):
        if self._done:
            return None, {"id": self._video_id}
        self._done = True
        return None, {"id": self._video_id}


class FakePlaylistRequest:
    def __init__(self, fail: bool = False) -> None:
        self._fail = fail

    def execute(self):
        if self._fail:
            raise HttpError(FakeResp(), b"{}")
        return {}


class FakeVideos:
    def __init__(self, video_id: str = "youtube-video-id") -> None:
        self._video_id = video_id

    def insert(self, **_kwargs):
        return FakeUploadRequest(self._video_id)


class FakePlaylistItems:
    def __init__(self, fail: bool = False) -> None:
        self._fail = fail

    def insert(self, **_kwargs):
        return FakePlaylistRequest(self._fail)


class FakeYouTube:
    def __init__(self, playlist_fails: bool = False) -> None:
        self._videos = FakeVideos()
        self._playlist = FakePlaylistItems(fail=playlist_fails)

    def videos(self):
        return self._videos

    def playlistItems(self):
        return self._playlist


def _settings(**overrides) -> dict:
    base = {
        "account": "channel",
        "headless": True,
        "remove_tags": False,
        "newest_first": True,
        "extra_description": "",
        "schedule_enabled": False,
        "min_delay": 20,
        "max_delay": 60,
        "privacy": "public",
        "tags": ["Facebook", "Reel"],
        "delete_after_upload": False,
        "between_videos": 0,
        "cookies_file": "",
    }
    base.update(overrides)
    return base


# ─────────────────────────── اختبارات العامل ───────────────────────────
def test_worker_completes_without_accessing_tkinter_widgets(monkeypatch) -> None:
    application = object.__new__(fixed_app.App)
    application._stop = threading.Event()
    application._uiq = queue.Queue()
    application.stats = {"total_uploaded": 0, "total_failed": 0, "total_downloaded": 0, "sessions": []}
    application._log = lambda _message, replace_last=False: None

    saved_ids: list[str] = []
    monkeypatch.setattr(fixed_app, "load_uploaded_ids", lambda: set())
    monkeypatch.setattr(fixed_app, "get_youtube", lambda _account: (object(), None))
    monkeypatch.setattr(fixed_app, "init_driver", lambda _headless, _log: FakeDriver())
    monkeypatch.setattr(
        fixed_app,
        "get_reels",
        lambda _driver, _url, _scroll, _limit, _log, _stop: ["https://facebook.com/reel/123"],
    )
    monkeypatch.setattr(
        fixed_app,
        "download_video",
        lambda *_args, **_kwargs: {"file": "video.mp4", "title": "عنوان", "desc": "وصف"},
    )
    monkeypatch.setattr(fixed_app, "upload_video", lambda *_args, **_kwargs: "youtube-video-id")
    monkeypatch.setattr(fixed_app, "save_uploaded_id", saved_ids.append)
    monkeypatch.setattr(fixed_app, "save_stats", lambda _stats: None)

    application._worker(
        [{"url": "https://facebook.com/page/reels/", "name": "page", "scroll": 1, "limit": 1}],
        _settings(),
    )

    events = []
    while not application._uiq.empty():
        events.append(application._uiq.get_nowait())

    assert saved_ids == ["123"]
    assert ("progress", 100) in events
    assert events[-1] == ("finished", {"uploaded": 1, "failed": 0, "fatal": None, "stopped": False})
    # الإحصاءات الحقيقية يجب أن تُحدَّث هذه المرة
    assert application.stats["total_downloaded"] == 1
    assert application.stats["total_uploaded"] == 1
    assert len(application.stats["sessions"]) == 1


def test_worker_skips_already_uploaded_ids(monkeypatch) -> None:
    application = object.__new__(fixed_app.App)
    application._stop = threading.Event()
    application._uiq = queue.Queue()
    application.stats = {"total_uploaded": 0, "total_failed": 0, "total_downloaded": 0}
    application._log = lambda _message, replace_last=False: None

    monkeypatch.setattr(fixed_app, "load_uploaded_ids", lambda: {"123"})
    monkeypatch.setattr(fixed_app, "get_youtube", lambda _account: (object(), None))
    monkeypatch.setattr(fixed_app, "init_driver", lambda _headless, _log: FakeDriver())
    monkeypatch.setattr(
        fixed_app,
        "get_reels",
        lambda _driver, _url, _scroll, _limit, _log, _stop: ["https://facebook.com/reel/123"],
    )
    monkeypatch.setattr(fixed_app, "download_video", lambda *_a, **_k: pytest.fail("يجب ألا يُنزَّل مقطع مرفوع سابقاً"))
    monkeypatch.setattr(fixed_app, "save_stats", lambda _stats: None)

    application._worker(
        [{"url": "https://facebook.com/page/reels/", "name": "page", "scroll": 1, "limit": 1}],
        _settings(),
    )

    events = []
    while not application._uiq.empty():
        events.append(application._uiq.get_nowait())
    assert events[-1][1]["uploaded"] == 0


def test_worker_stops_when_stop_event_is_set(monkeypatch) -> None:
    application = object.__new__(fixed_app.App)
    application._stop = threading.Event()
    application._stop.set()
    application._uiq = queue.Queue()
    application.stats = {"total_uploaded": 0, "total_failed": 0, "total_downloaded": 0}
    application._log = lambda _message, replace_last=False: None
    monkeypatch.setattr(fixed_app, "load_uploaded_ids", lambda: set())
    monkeypatch.setattr(fixed_app, "get_youtube", lambda _account: (object(), None))
    monkeypatch.setattr(fixed_app, "save_stats", lambda _stats: None)

    application._worker(
        [{"url": "https://facebook.com/page/reels/", "name": "page", "scroll": 1, "limit": 1}],
        _settings(),
    )
    events = []
    while not application._uiq.empty():
        events.append(application._uiq.get_nowait())
    assert events[-1][1]["stopped"] is True
    assert events[-1][1]["uploaded"] == 0


# ─────────────────────────── عنوان الفيديو ───────────────────────────
def test_clean_title_keeps_hashtags_and_ampersand_by_default() -> None:
    title = fixed_app.clean_title("رحلة & مغامرة #reel 2026", remove_hashtags=False)
    assert "&" in title
    assert "#reel" in title


def test_clean_title_removes_hashtags_when_requested() -> None:
    title = fixed_app.clean_title("رحلة #reel #shorts", remove_hashtags=True)
    assert "#" not in title


def test_clean_title_strips_angle_brackets_and_truncates() -> None:
    title = fixed_app.clean_title("<b>عنوان</b>" + "ط" * 200)
    assert "<" not in title and ">" not in title
    assert len(title) <= 100


def test_clean_title_returns_fallback_for_empty() -> None:
    assert fixed_app.clean_title("") == "Facebook Reel"
    assert fixed_app.clean_title("   ") == "Facebook Reel"


# ─────────────────────────── ترتيب الروابط ───────────────────────────
def test_order_reels_dedupes_and_puts_newest_first() -> None:
    links = [
        "https://www.facebook.com/reel/100",
        "https://www.facebook.com/reel/300",
        "https://www.facebook.com/reel/100",
        "https://www.facebook.com/reel/200",
    ]
    ordered = fixed_app.order_reels(links, newest_first=True)
    assert ordered == [
        "https://www.facebook.com/reel/300",
        "https://www.facebook.com/reel/200",
        "https://www.facebook.com/reel/100",
    ]


def test_order_reels_keeps_discovery_order_when_disabled() -> None:
    links = ["https://www.facebook.com/reel/5", "https://www.facebook.com/reel/9"]
    assert fixed_app.order_reels(links, newest_first=False) == links


# ─────────────────────────── الحساب والجدولة ───────────────────────────
def test_sanitize_account_name_blocks_path_traversal() -> None:
    assert fixed_app.sanitize_account_name("../../etc/passwd") == "etc_passwd"
    assert fixed_app.sanitize_account_name("my channel!") == "my_channel"
    assert fixed_app.sanitize_account_name("") == "account1"


def test_clamp_publish_at_enforces_minimum_lead() -> None:
    now = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    too_soon = now + timedelta(minutes=2)
    clamped = fixed_app.clamp_publish_at(too_soon, now=now)
    assert clamped == now + timedelta(minutes=fixed_app.MIN_SCHEDULE_LEAD_MINUTES)

    later = now + timedelta(hours=3)
    assert fixed_app.clamp_publish_at(later, now=now) == later


# ─────────────────────────── اكتشاف ملف التنزيل ───────────────────────────
def test_resolve_downloaded_file_prefers_existing_merged_mp4(tmp_path: Path) -> None:
    merged = tmp_path / "reel.mp4"
    merged.write_bytes(b"video")

    class FakeYdl:
        def prepare_filename(self, _info):
            return str(tmp_path / "reel.webm")

    assert fixed_app._resolve_downloaded_file({}, FakeYdl()) == str(merged)


def test_resolve_downloaded_file_returns_none_when_missing(tmp_path: Path) -> None:
    class FakeYdl:
        def prepare_filename(self, _info):
            return str(tmp_path / "ghost.webm")

    assert fixed_app._resolve_downloaded_file({}, FakeYdl()) is None


# ─────────────────────────── الرفع ───────────────────────────
def test_upload_rejects_missing_local_file() -> None:
    data = {"file": "/no/such/file.mp4", "title": "t", "desc": "d"}
    assert fixed_app.upload_video(FakeYouTube(), data, log_cb=None) is None


def test_playlist_failure_does_not_duplicate_upload(tmp_path: Path) -> None:
    video_file = tmp_path / "clip.mp4"
    video_file.write_bytes(b"video-bytes")
    data = {"file": str(video_file), "title": "t", "desc": "d"}

    youtube = FakeYouTube(playlist_fails=True)
    result = fixed_app.upload_video(youtube, data, playlist_id="PL123", log_cb=None)
    # الرفع نجح رغم فشل الإضافة لقائمة التشغيل، ولا يُعاد رفعه.
    assert result == "youtube-video-id"


def test_upload_returns_quota_error_on_403(tmp_path: Path) -> None:
    video_file = tmp_path / "clip.mp4"
    video_file.write_bytes(b"video-bytes")
    data = {"file": str(video_file), "title": "t", "desc": "d"}

    class QuotaVideos:
        def insert(self, **_kwargs):
            raise HttpError(FakeResp(403, "Forbidden"), b"{}")

    class QuotaYouTube:
        def videos(self):
            return QuotaVideos()

    assert fixed_app.upload_video(QuotaYouTube(), data, log_cb=None) == "QUOTA_ERROR"


# ─────────────────────────── تخزين المعرّفات ───────────────────────────
def test_uploaded_ids_roundtrip(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "uploaded_ids.txt"
    monkeypatch.setattr(fixed_app, "UPLOADED_IDS_FILE", str(target))
    fixed_app.save_uploaded_id("111")
    fixed_app.save_uploaded_id("222")
    assert fixed_app.load_uploaded_ids() == {"111", "222"}


def test_get_youtube_reports_missing_secrets(tmp_path: Path) -> None:
    service, error = fixed_app.get_youtube("acc", secrets_file=str(tmp_path / "missing.json"))
    assert service is None
    assert error and "مفقود" in error
