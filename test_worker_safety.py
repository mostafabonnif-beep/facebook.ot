"""اختبارات وحدة للمنطق الأساسي في fb_youtube_uploader_v34.

لا تلمس هذه الاختبارات واجهة Tkinter ولا تتصل بـ Facebook أو YouTube.
شغّلها بـ:  python -m pytest -q
"""
from __future__ import annotations

import importlib.util
import json
import queue
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from googleapiclient.errors import HttpError

MODULE_PATH = Path(__file__).with_name("fb_youtube_uploader_v35.py")
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
        "anonymize_source": True,
        "optimize_video": False,
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


def _make_app(monkeypatch, links=None, settings=None):
    application = object.__new__(fixed_app.App)
    application._stop = threading.Event()
    application._uiq = queue.Queue()
    application.stats = {"total_uploaded": 0, "total_failed": 0,
                         "total_downloaded": 0, "sessions": []}
    application._log = lambda _message, replace_last=False: None
    monkeypatch.setattr(fixed_app, "load_uploaded_ids", lambda: set())
    monkeypatch.setattr(fixed_app, "get_youtube", lambda _account: (object(), None))
    monkeypatch.setattr(fixed_app, "init_driver", lambda _headless, _log: FakeDriver())
    monkeypatch.setattr(
        fixed_app, "get_reels",
        lambda _driver, _url, _scroll, _limit, _log, _stop: list(links or ["https://facebook.com/reel/123"]),
    )
    monkeypatch.setattr(fixed_app, "save_stats", lambda _stats: None)
    return application


def _drain_uiq(application):
    events = []
    while not application._uiq.empty():
        events.append(application._uiq.get_nowait())
    return events


# ─────────────────────────── اختبارات العامل ───────────────────────────
def test_worker_completes_without_accessing_tkinter_widgets(monkeypatch) -> None:
    application = _make_app(monkeypatch)
    saved_ids: list[str] = []
    monkeypatch.setattr(
        fixed_app, "download_video",
        lambda *_args, **_kwargs: {"file": "video.mp4", "title": "عنوان", "desc": "وصف"},
    )
    monkeypatch.setattr(fixed_app, "upload_video", lambda *_args, **_kwargs: "youtube-video-id")
    monkeypatch.setattr(fixed_app, "save_uploaded_id", saved_ids.append)

    application._worker(
        [{"url": "https://facebook.com/page/reels/", "name": "page", "scroll": 1, "limit": 1}],
        _settings(),
    )
    events = _drain_uiq(application)

    assert saved_ids == ["123"]
    assert ("progress", 100) in events
    assert events[-1] == ("finished", {"uploaded": 1, "failed": 0, "fatal": None, "stopped": False})
    assert application.stats["total_downloaded"] == 1
    assert application.stats["total_uploaded"] == 1
    assert len(application.stats["sessions"]) == 1


def test_worker_probes_and_finalizes_video_when_optimize_enabled(monkeypatch, tmp_path: Path) -> None:
    application = _make_app(monkeypatch)
    raw = tmp_path / "123.mp4"
    raw.write_bytes(b"raw-video")
    processed = tmp_path / "123.faststart.mp4"
    processed.write_bytes(b"processed-video")

    monkeypatch.setattr(
        fixed_app, "download_video",
        lambda *_a, **_k: {"file": str(raw), "title": "t", "desc": "d"},
    )
    monkeypatch.setattr(fixed_app, "probe_video", lambda *_a, **_k: (True, {"duration": 9.0}))
    monkeypatch.setattr(fixed_app, "finalize_video", lambda *_a, **_k: str(processed))

    uploaded_files = []
    monkeypatch.setattr(
        fixed_app, "upload_video",
        lambda _yt, video_data, *_a, **_k: (uploaded_files.append(video_data["file"]), "vid")[1],
    )
    monkeypatch.setattr(fixed_app, "save_uploaded_id", lambda _id: None)

    application._worker(
        [{"url": "https://facebook.com/page/reels/", "name": "page", "scroll": 1, "limit": 1}],
        _settings(optimize_video=True),
    )
    events = _drain_uiq(application)

    assert uploaded_files == [str(processed)]
    assert not raw.exists()  # الأصل حُذف بعد التغليف
    assert events[-1][1]["uploaded"] == 1


def test_worker_skips_invalid_video(monkeypatch, tmp_path: Path) -> None:
    application = _make_app(monkeypatch)
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"x")
    monkeypatch.setattr(
        fixed_app, "download_video",
        lambda *_a, **_k: {"file": str(broken), "title": "t", "desc": "d"},
    )
    monkeypatch.setattr(
        fixed_app, "probe_video",
        lambda *_a, **_k: (False, {"problems": ["لا يوجد مسار فيديو"]}),
    )
    monkeypatch.setattr(
        fixed_app, "upload_video",
        lambda *_a, **_k: pytest.fail("يجب ألا يُرفع مقطع غير صالح"),
    )
    monkeypatch.setattr(fixed_app, "save_uploaded_id", lambda _id: None)

    application._worker(
        [{"url": "https://facebook.com/page/reels/", "name": "page", "scroll": 1, "limit": 1}],
        _settings(optimize_video=True),
    )
    events = _drain_uiq(application)
    assert events[-1][1]["uploaded"] == 0
    assert events[-1][1]["failed"] == 1
    assert not broken.exists()  # نظّف الملف المعطوب


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
        fixed_app, "get_reels",
        lambda _driver, _url, _scroll, _limit, _log, _stop: ["https://facebook.com/reel/123"],
    )
    monkeypatch.setattr(fixed_app, "download_video", lambda *_a, **_k: pytest.fail("يجب ألا يُنزَّل مقطع مرفوع سابقاً"))
    monkeypatch.setattr(fixed_app, "save_stats", lambda _stats: None)

    application._worker(
        [{"url": "https://facebook.com/page/reels/", "name": "page", "scroll": 1, "limit": 1}],
        _settings(),
    )
    events = _drain_uiq(application)
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
    events = _drain_uiq(application)
    assert events[-1][1]["stopped"] is True
    assert events[-1][1]["uploaded"] == 0


# ─────────────────────────── توحيد رابط الصفحة ───────────────────────────
def test_normalize_channel_url_vanity_page() -> None:
    assert fixed_app.normalize_channel_url("https://www.facebook.com/jireel") == \
        "https://www.facebook.com/jireel/reels"


def test_normalize_channel_url_profile_adds_reels_tab() -> None:
    result = fixed_app.normalize_channel_url("https://www.facebook.com/profile.php?id=61554746552594")
    assert "sk=reels_tab" in result
    assert "id=61554746552594" in result


def test_normalize_channel_url_keeps_reels_and_sk_params() -> None:
    reels = "https://www.facebook.com/jireel/reels"
    assert fixed_app.normalize_channel_url(reels) == reels
    profile = "https://www.facebook.com/profile.php?id=123&sk=reels_tab"
    assert fixed_app.normalize_channel_url(profile) == profile


def test_normalize_channel_url_adds_scheme_and_rejects_non_facebook() -> None:
    assert fixed_app.normalize_channel_url("facebook.com/jireel").startswith("https://facebook.com")
    with pytest.raises(ValueError):
        fixed_app.normalize_channel_url("https://www.youtube.com/x")
    with pytest.raises(ValueError):
        fixed_app.normalize_channel_url("")


def test_extract_reel_id() -> None:
    assert fixed_app.extract_reel_id("https://www.facebook.com/reel/4581978605351760") == "4581978605351760"
    assert fixed_app.extract_reel_id("https://www.facebook.com/watch/videos/123456") == "123456"
    assert fixed_app.extract_reel_id("https://www.facebook.com/page") is None


# ─────────────────────────── تنقية النص (إخفاء المصدر) ───────────────────────────
def test_sanitize_removes_page_name_everywhere() -> None:
    text = fixed_app.sanitize_public_text("صفحة نكتة يومية تقدم: مقطع من نكتةيومية مضحك", "نكتة يومية")
    assert "نكتة" not in text
    assert "مضحك" in text


def test_sanitize_removes_page_name_case_insensitive_latin() -> None:
    text = fixed_app.sanitize_public_text("BEST OF Comedy Club weekly", "Comedy Club")
    assert "comedy club" not in text.lower()


def test_sanitize_handles_real_facebook_title_pattern() -> None:
    """النمط الحقيقي لعناوين فيسبوك: إحصاءات · إحصاءات | العنوان | اسم الصفحة."""
    raw = ("35K views · 576 reactions | موسم جني الطماطم الصناعية "
           "بقالمة.. مصدر رزق موسمي | Elwatania TV")
    out = fixed_app.build_title(raw, ["Elwatania TV"])
    assert "views" not in out
    assert "reactions" not in out
    assert "Elwatania" not in out
    assert not out.startswith("·") and not out.endswith("|")
    assert "موسم جني الطماطم" in out


def test_sanitize_accepts_list_of_names() -> None:
    out = fixed_app.sanitize_public_text("Elwatania TV يقدم من قناة elwatania", ["Elwatania TV", "elwatania"])
    assert "elwatania" not in out.lower()


def test_sanitize_strips_edge_separators() -> None:
    assert fixed_app.sanitize_public_text("· | عنوان نظيف |") == "عنوان نظيف"
    assert fixed_app.sanitize_public_text("— عنوان —") == "عنوان"


def test_sanitize_removes_page_name_without_spaces_variant() -> None:
    text = fixed_app.sanitize_public_text("Best of ComedyClub weekly", "Comedy Club")
    assert "comedyclub" not in text.lower()


def test_sanitize_removes_urls_and_stats_junk() -> None:
    raw = "شاهد المزيد https://fb.com/x ‎2.3M views و 45 تفاعل"
    out = fixed_app.sanitize_public_text(raw)
    assert "http" not in out
    assert "views" not in out.lower()
    assert "شاهد" in out


def test_sanitize_removes_mentions_when_requested() -> None:
    out = fixed_app.sanitize_public_text("فيديو من @SomePage اليوم", remove_mentions=True)
    assert "@SomePage" not in out


def test_sanitize_keeps_newlines_for_description() -> None:
    out = fixed_app.sanitize_public_text("سطر أول\n\n\nسطر ثانٍ", keep_newlines=True)
    assert "سطر أول" in out and "سطر ثانٍ" in out
    assert "\n\n\n" not in out


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


def test_build_title_never_leaks_page_name() -> None:
    title = fixed_app.build_title("MyPage was live. funny cat video", "MyPage")
    assert "MyPage" not in title
    assert "funny cat video" in title


def test_build_title_truncates_on_word_boundary() -> None:
    long_title = " ".join(["word"] * 40)
    title = fixed_app.build_title(long_title)
    assert len(title) <= 100
    assert not title.endswith("wor")  # لم ينشطر منتصف كلمة


def test_build_description_hides_page_and_links_keeps_extra() -> None:
    desc = "MyPage يقدم مقطعاً https://fb.com/x"
    out = fixed_app.build_description(desc, "MyPage", extra="تابع قناتنا")
    assert "MyPage" not in out
    assert "http" not in out
    assert "تابع قناتنا" in out
    assert len(out) <= 5000


def test_build_description_without_anonymize_keeps_page_name() -> None:
    out = fixed_app.build_description("مقطع من MyPage", "MyPage", anonymize=False)
    assert "MyPage" in out


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


# ─────────────────────────── فحص المقطع وتغليفه ───────────────────────────
def test_probe_video_rejects_missing_file(tmp_path: Path) -> None:
    ok, info = fixed_app.probe_video(str(tmp_path / "none.mp4"))
    assert ok is False
    assert info.get("error") == "missing-file"


def test_probe_video_passes_without_ffprobe(monkeypatch, tmp_path: Path) -> None:
    f = tmp_path / "v.mp4"
    f.write_bytes(b"video")
    monkeypatch.setattr(fixed_app, "FFPROBE_BIN", None)
    ok, _info = fixed_app.probe_video(str(f))
    assert ok is True


def test_probe_video_validates_streams(monkeypatch, tmp_path: Path) -> None:
    f = tmp_path / "v.mp4"
    f.write_bytes(b"video")
    monkeypatch.setattr(fixed_app, "FFPROBE_BIN", "/fake/ffprobe")

    def fake_run_valid(*_args, **_kwargs):
        payload = {
            "streams": [{"codec_type": "video", "codec_name": "h264", "width": 720, "height": 1280}],
            "format": {"duration": "12.5", "size": "204800"},
        }
        return SimpleNamespace(stdout=json.dumps(payload), returncode=0)

    monkeypatch.setattr(fixed_app.subprocess, "run", fake_run_valid)
    ok, info = fixed_app.probe_video(str(f))
    assert ok is True
    assert info["vcodec"] == "h264"
    assert info["height"] == 1280

    def fake_run_broken(*_args, **_kwargs):
        payload = {"streams": [], "format": {"duration": "0", "size": "100"}}
        return SimpleNamespace(stdout=json.dumps(payload), returncode=0)

    monkeypatch.setattr(fixed_app.subprocess, "run", fake_run_broken)
    ok, info = fixed_app.probe_video(str(f))
    assert ok is False
    assert "لا يوجد مسار فيديو" in info["problems"]


def test_finalize_video_without_ffmpeg_returns_original(monkeypatch, tmp_path: Path) -> None:
    f = tmp_path / "v.mp4"
    f.write_bytes(b"video")
    monkeypatch.setattr(fixed_app, "FFMPEG_BIN", None)
    assert fixed_app.finalize_video(str(f)) == str(f)


def test_finalize_video_success_returns_processed(monkeypatch, tmp_path: Path) -> None:
    f = tmp_path / "v.mp4"
    f.write_bytes(b"video")
    out = tmp_path / "v.faststart.mp4"
    monkeypatch.setattr(fixed_app, "FFMPEG_BIN", "/fake/ffmpeg")

    def fake_run(cmd, **_kwargs):
        out.write_bytes(b"processed")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(fixed_app.subprocess, "run", fake_run)
    assert fixed_app.finalize_video(str(f)) == str(out)


def test_finalize_video_failure_returns_original(monkeypatch, tmp_path: Path) -> None:
    f = tmp_path / "v.mp4"
    f.write_bytes(b"video")
    monkeypatch.setattr(fixed_app, "FFMPEG_BIN", "/fake/ffmpeg")
    monkeypatch.setattr(
        fixed_app.subprocess, "run",
        lambda *_a, **_k: SimpleNamespace(returncode=1, stderr="boom"),
    )
    assert fixed_app.finalize_video(str(f)) == str(f)
    assert not (tmp_path / "v.faststart.mp4").exists()


# ─────────────────────────── كوكيز المتصفح ───────────────────────────
def test_parse_netscape_cookie_file(tmp_path: Path) -> None:
    f = tmp_path / "cookies.txt"
    f.write_text(
        "# Netscape HTTP Cookie File\n"
        ".facebook.com\tTRUE\t/\tTRUE\t1893456000\tc_user\t123\n"
        ".facebook.com\tTRUE\t/\tTRUE\t1893456000\txs\tabc\n"
        "badline\n",
        encoding="utf-8",
    )
    cookies = fixed_app._parse_cookie_file(str(f))
    assert len(cookies) == 2
    assert cookies[0]["name"] == "c_user"
    assert cookies[1]["expiry"] == 1893456000


def test_parse_json_cookie_file(tmp_path: Path) -> None:
    f = tmp_path / "cookies.json"
    f.write_text(json.dumps([
        {"name": "c_user", "value": "123", "domain": ".facebook.com",
         "path": "/", "secure": True, "expirationDate": 1893456000},
    ]), encoding="utf-8")
    cookies = fixed_app._parse_cookie_file(str(f))
    assert len(cookies) == 1
    assert cookies[0]["name"] == "c_user"
    assert cookies[0]["expiry"] == 1893456000


def test_load_cookies_into_driver(tmp_path: Path) -> None:
    f = tmp_path / "cookies.txt"
    f.write_text(".facebook.com\tTRUE\t/\tTRUE\t1893456000\tc_user\t123\n", encoding="utf-8")

    class CookieDriver:
        def __init__(self):
            self.cookies = []
            self.visited = []

        def get(self, url):
            self.visited.append(url)

        def add_cookie(self, cookie):
            self.cookies.append(cookie)

    driver = CookieDriver()
    added = fixed_app.load_cookies_into_driver(driver, str(f))
    assert added == 1
    assert driver.cookies[0]["name"] == "c_user"


# ─────────────────────────── استخراج الروابط ───────────────────────────
class FakeElement:
    def __init__(self, href):
        self._href = href

    def get_attribute(self, name):
        return self._href if name == "href" else None


class ReelDriver:
    def __init__(self, links, current_url="https://www.facebook.com/page/reels"):
        self.links = links
        self.current_url = current_url
        self.scrolls = 0

    def get(self, _url):
        return None

    def execute_script(self, _script):
        self.scrolls += 1

    def find_elements(self, by, value):
        from selenium.webdriver.common.by import By
        if by == By.CSS_SELECTOR and value == "input[name='email'], form[action*='login']":
            return []
        return [FakeElement(l) for l in self.links]

    def quit(self):
        return None


def test_get_reels_collects_unique_links_in_order() -> None:
    driver = ReelDriver([
        "https://www.facebook.com/reel/200?__cft__=1",
        "https://www.facebook.com/reel/100",
        "https://www.facebook.com/reel/200",
        "https://www.facebook.com/page/photos",  # ليس ريلاً
    ])
    links = fixed_app.get_reels(driver, "https://www.facebook.com/page/reels", 2, 10)
    assert links == [
        "https://www.facebook.com/reel/200",
        "https://www.facebook.com/reel/100",
    ]


def test_get_reels_detects_login_wall() -> None:
    driver = ReelDriver([], current_url="https://www.facebook.com/login.php")
    links = fixed_app.get_reels(driver, "https://www.facebook.com/page/reels", 2, 10)
    assert links == []


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
    assert result == "youtube-video-id"


def test_upload_returns_quota_error_on_quota_403(tmp_path: Path) -> None:
    video_file = tmp_path / "clip.mp4"
    video_file.write_bytes(b"video-bytes")
    data = {"file": str(video_file), "title": "t", "desc": "d"}
    quota = HttpError(FakeResp(403, "Forbidden"),
                      b'{"error":{"errors":[{"reason":"quotaExceeded"}]}}')

    class QuotaVideos:
        def insert(self, **_kwargs):
            raise quota

    class QuotaYouTube:
        def videos(self):
            return QuotaVideos()

    assert fixed_app.upload_video(QuotaYouTube(), data, log_cb=None) == "QUOTA_ERROR"


# ─────────────────────────── تصنيف أخطاء الرفع (v3.5) ───────────────────────────
def _http_error(status: int, reason: str) -> HttpError:
    body = json.dumps({"error": {"errors": [{"reason": reason}]}}).encode() if reason else b"{}"
    return HttpError(FakeResp(status), body)


def test_classify_quota_error() -> None:
    assert fixed_app.classify_upload_error(_http_error(403, "quotaExceeded")) == "quota"
    assert fixed_app.classify_upload_error(_http_error(403, "dailyLimitExceeded")) == "quota"


def test_classify_rate_limit_is_retryable() -> None:
    """429 وتحديد المعدل مؤقتان — كانا يوقفان العملية خطأً في السابق."""
    assert fixed_app.classify_upload_error(_http_error(429, "rateLimitExceeded")) == "retryable"
    assert fixed_app.classify_upload_error(_http_error(403, "userRateLimitExceeded")) == "retryable"
    assert fixed_app.classify_upload_error(_http_error(503, "backendError")) == "retryable"


def test_classify_forbidden_is_auth_and_bad_request_is_fatal() -> None:
    assert fixed_app.classify_upload_error(_http_error(403, "")) == "auth"
    assert fixed_app.classify_upload_error(_http_error(400, "invalidMetadata")) == "fatal"
    assert fixed_app.classify_upload_error(_http_error(401, "")) == "fatal"


def test_backoff_grows() -> None:
    d1 = fixed_app._backoff_seconds(1)
    d2 = fixed_app._backoff_seconds(2)
    assert 8 <= d1 <= 12
    assert d2 >= 24
    assert fixed_app._backoff_seconds(10) <= 304  # بحد أقصى ~300 + رجّة


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


def test_upload_retries_rate_limit_then_succeeds(monkeypatch, tmp_path: Path) -> None:
    video_file = tmp_path / "clip.mp4"
    video_file.write_bytes(b"video-bytes")
    data = {"file": str(video_file), "title": "t", "desc": "d"}
    monkeypatch.setattr(fixed_app.time, "sleep", lambda *_a: None)

    youtube = FlakyYouTube([_http_error(429, "rateLimitExceeded"),
                            _http_error(503, "")])
    result = fixed_app.upload_video(youtube, data, log_cb=None)
    assert result == "youtube-video-id"
    assert youtube.calls == 3  # فشلتان ثم نجاح


def test_upload_does_not_retry_on_auth_error(monkeypatch, tmp_path: Path) -> None:
    video_file = tmp_path / "clip.mp4"
    video_file.write_bytes(b"video-bytes")
    data = {"file": str(video_file), "title": "t", "desc": "d"}
    monkeypatch.setattr(fixed_app.time, "sleep", lambda *_a: None)

    youtube = FlakyYouTube([_http_error(403, "")])
    assert fixed_app.upload_video(youtube, data, log_cb=None) is None
    assert youtube.calls == 1  # لا إعادة محاولة على خطأ صلاحيات


def test_upload_does_not_retry_on_quota(monkeypatch, tmp_path: Path) -> None:
    video_file = tmp_path / "clip.mp4"
    video_file.write_bytes(b"video-bytes")
    data = {"file": str(video_file), "title": "t", "desc": "d"}
    monkeypatch.setattr(fixed_app.time, "sleep", lambda *_a: None)

    youtube = FlakyYouTube([_http_error(403, "quotaExceeded")])
    assert fixed_app.upload_video(youtube, data, log_cb=None) == "QUOTA_ERROR"
    assert youtube.calls == 1


# ─────────────────────────── التحقق بعد الرفع (v3.5) ───────────────────────────
class ListYouTube:
    def __init__(self, items) -> None:
        self._items = items

    def videos(self):
        return self

    def list(self, **_kwargs):
        return self

    def execute(self):
        return {"items": self._items}


def test_verify_upload_warns_when_public_requested_but_private() -> None:
    messages: list[str] = []
    youtube = ListYouTube([{"status": {"privacyStatus": "private"}}])
    status = fixed_app.verify_upload(youtube, "vid", "public", None, messages.append)
    assert status == {"privacyStatus": "private"}
    assert any("غير مُدقّق" in m or "private" in m for m in messages)


def test_verify_upload_accepts_schedule() -> None:
    messages: list[str] = []
    youtube = ListYouTube([{"status": {"privacyStatus": "private",
                                       "publishAt": "2026-09-13T10:00:00Z"}}])
    fixed_app.verify_upload(youtube, "vid", "private",
                            datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc), messages.append)
    assert any("الجدولة مقبولة" in m for m in messages)


def test_verify_upload_flags_missing_publish_at() -> None:
    messages: list[str] = []
    youtube = ListYouTube([{"status": {"privacyStatus": "private"}}])
    fixed_app.verify_upload(youtube, "vid", "private",
                            datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc), messages.append)
    assert any("لم يقبل وقت الجدولة" in m for m in messages)


def test_verify_upload_is_soft_on_failure() -> None:
    class BrokenYouTube:
        def videos(self):
            raise RuntimeError("no scope")

    assert fixed_app.verify_upload(BrokenYouTube(), "vid", "public", None, None) is None


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
