"""اختبارات v4.8: حفظ/استرجاع إعدادات الواجهة في config.ini + سلامة التخطيط."""
from __future__ import annotations

import configparser
import importlib.util
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).with_name("fb_youtube_uploader_v49_pro.py")
spec = importlib.util.spec_from_file_location("pro48", MODULE_PATH)
assert spec and spec.loader
pro = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pro)


def test_ui_defaults_cover_all_keys():
    vals = pro.load_ui_values("لا-يوجد-ملف.ini")
    assert len(vals) == len(pro.UI_KEYS)
    assert vals[("SETTINGS", "ACCOUNT")] == "account1"
    assert vals[("WATCH", "INTERVAL_MINUTES")] == "10"
    assert vals[("AI", "MODEL")] == pro.GEMINI_MODELS[0]
    assert vals[("STUDIO", "CATEGORY")] == "25"


def test_ui_roundtrip_preserves_other_sections(tmp_path):
    cfg_file = tmp_path / "config.ini"
    # ملف موجود مسبقاً بمفاتيح لا تمسها الواجهة
    cfg_file.write_text(
        "[SETTINGS]\nVIDEO_DIRECTORY = D:/videos\nCLIENT_SECRETS_FILE = s.json\n",
        encoding="utf-8")

    values = pro.load_ui_values(str(cfg_file))
    values[("WATCH", "INTERVAL_MINUTES")] = "30"
    values[("STUDIO", "TITLE_PREFIX")] = "الوطنية"
    values[("AI", "STYLE")] = "إخباري رسمي"
    pro.save_ui_values(values, str(cfg_file))

    again = pro.load_ui_values(str(cfg_file))
    assert again[("WATCH", "INTERVAL_MINUTES")] == "30"
    assert again[("STUDIO", "TITLE_PREFIX")] == "الوطنية"
    assert again[("AI", "STYLE")] == "إخباري رسمي"
    # المسارات الأصلية لم تُمَس
    cfg = configparser.ConfigParser()
    cfg.read(str(cfg_file), encoding="utf-8")
    assert cfg.get("SETTINGS", "VIDEO_DIRECTORY") == "D:/videos"
    assert cfg.get("SETTINGS", "CLIENT_SECRETS_FILE") == "s.json"


def test_bool_values_roundtrip(tmp_path):
    cfg_file = tmp_path / "config.ini"
    values = pro.load_ui_values(str(cfg_file))
    values[("WATCH", "AUTO_START")] = "true"
    values[("STUDIO", "LOGO_ENABLED")] = "false"
    pro.save_ui_values(values, str(cfg_file))
    again = pro.load_ui_values(str(cfg_file))
    assert again[("WATCH", "AUTO_START")] == "true"
    assert again[("STUDIO", "LOGO_ENABLED")] == "false"


def test_ui_keys_complete_for_worker():
    """كل إعداد يعمل به العامل له مفتاح محفوظ — لا مفاجآت بعد إعادة الفتح."""
    saved = {(s, k) for (s, k) in pro.load_ui_values()}
    # (قسم، مفتاح) لكل إعداد يستخدمه العامل
    required = {
        ("SETTINGS", "ACCOUNT"), ("SETTINGS", "PRIVACY"), ("SETTINGS", "HEADLESS"),
        ("SETTINGS", "DELETE_AFTER_UPLOAD"), ("SETTINGS", "REMOVE_TAGS"),
        ("SETTINGS", "BETWEEN_VIDEOS"), ("SETTINGS", "DAILY_QUOTA"),
        ("SETTINGS", "MAX_ATTEMPTS"), ("SETTINGS", "MIN_DISK_GB"),
        ("WATCH", "INTERVAL_MINUTES"), ("WATCH", "FAST_SCRAPE"), ("WATCH", "AUTO_START"),
        ("STUDIO", "TITLE_PREFIX"), ("STUDIO", "TITLE_SUFFIX"), ("STUDIO", "CATEGORY"),
        ("STUDIO", "TITLE_OPTIMIZE"), ("STUDIO", "AUTO_SHORTS"), ("STUDIO", "AUTO_HASHTAGS"),
        ("STUDIO", "DEDUPE_CONTENT"), ("STUDIO", "CREDIT_LINE"),
        ("STUDIO", "EXTRA_DESCRIPTION"), ("STUDIO", "TAGS"),
        ("STUDIO", "LOGO_ENABLED"), ("STUDIO", "LOGO_PATH"), ("STUDIO", "LOGO_POSITION"),
        ("STUDIO", "LOGO_SCALE"), ("STUDIO", "LOGO_OPACITY"), ("STUDIO", "LOGO_COVER_OLD"),
        ("STUDIO", "MIN_DURATION"), ("STUDIO", "MAX_DURATION"),
        ("STUDIO", "SCHEDULE"), ("STUDIO", "SCHEDULE_MIN"), ("STUDIO", "SCHEDULE_MAX"),
        ("STUDIO", "COOKIES_FILE"), ("STUDIO", "COOKIES_ENABLED"),
        ("STUDIO", "TELEGRAM_TOKEN"), ("STUDIO", "TELEGRAM_CHAT_ID"),
        ("STUDIO", "TELEGRAM_ERRORS"), ("STUDIO", "PG_SCROLL"), ("STUDIO", "PG_LIMIT"),
        ("AI", "ENABLED"), ("AI", "MODEL"), ("AI", "STYLE"),
    }
    missing = required - saved
    assert not missing, f"مفاتيح غير محفوظة: {missing}"
