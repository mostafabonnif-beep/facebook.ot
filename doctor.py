"""doctor.py — فحص جاهزية بيئة FB → YouTube Uploader.

شغّله قبل البرنامج ليعطيك تقريراً واضحاً بما ينقص:

    py doctor.py        (Windows)
    python3 doctor.py   (Linux/macOS)

لا يحتاج أي مكتبة خارجية، ولا يتصل بالإنترنت، ولا يقرأ أي أسرار (يفحص وجودها وبنيّتها فقط).
"""

import json
import os
import shutil
import sys

OK, WARN, BAD, INFO = "✅", "⚠️ ", "❌", "ℹ️ "

results = []


def check(name, status, detail="", fix=""):
    results.append((status, name, detail, fix))
    line = f"{status} {name}"
    if detail:
        line += f" — {detail}"
    print(line)
    if fix and status in (BAD, WARN):
        print(f"     ↳ {fix}")


def _config_value(section, key, fallback):
    import configparser
    cfg = configparser.ConfigParser()
    cfg.read("config.ini", encoding="utf-8")
    return cfg.get(section, key, fallback=fallback)


print("=" * 62)
print("  فحص جاهزية FB → YouTube Uploader")
print("=" * 62)

# 1) إصدار Python
major, minor = sys.version_info[:2]
version_text = f"{major}.{minor}.{sys.version_info[2]}"
if (major, minor) >= (3, 11):
    check("إصدار Python", OK, version_text)
elif (major, minor) == (3, 10):
    check("إصدار Python", WARN, f"{version_text} — yt-dlp أسقط دعم 3.10 ومكتبات Google ستتبعه قريباً",
          "ثبّت Python 3.12 من python.org")
else:
    check("إصدار Python", BAD, f"{version_text} غير مدعوم",
          "ثبّت Python 3.11 أو أحدث")

# 2) المكتبات
missing = []
for module, package in [("yt_dlp", "yt-dlp"), ("selenium", "selenium"),
                        ("googleapiclient", "google-api-python-client"),
                        ("google_auth_oauthlib", "google-auth-oauthlib")]:
    try:
        __import__(module)
    except ImportError:
        missing.append(package)
if missing:
    check("مكتبات Python", BAD, "ناقصة: " + ", ".join(missing),
          "py -m pip install -r requirements.txt")
else:
    check("مكتبات Python", OK, "كل المكتبات المطلوبة موجودة")

# 3) Tkinter (الواجهة)
try:
    import tkinter  # noqa: F401
    check("واجهة Tkinter", OK, "مثبتة")
except ImportError:
    check("واجهة Tkinter", BAD, "غير مثبتة",
          "Windows: أعد تثبيت Python مع خيار tcl/tk — Linux: sudo apt install python3-tk")

# 4) متصفح Chrome/Chromium
browsers = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"]
found_browser = next((shutil.which(b) for b in browsers if shutil.which(b)), None)
if not found_browser:
    for candidate in [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]:
        if os.path.exists(candidate):
            found_browser = candidate
            break
if found_browser:
    check("متصفح Chrome", OK, os.path.basename(found_browser))
else:
    check("متصفح Chrome", BAD, "غير موجود",
          "ثبّت Google Chrome (Selenium يجلب chromedriver تلقائياً بعدها)")

# 5) ffmpeg / ffprobe
for tool in ("ffmpeg", "ffprobe"):
    path = shutil.which(tool)
    if path:
        check(tool, OK, path)
    else:
        check(tool, WARN, "غير موجود",
              "نزّله من ffmpeg.org وأضفه إلى PATH (بدونه يتخطى فحص/تحسين المقطع)")

# 6) client_secrets.json
secrets_file = _config_value("SETTINGS", "CLIENT_SECRETS_FILE", "client_secrets.json")
if not os.path.exists(secrets_file):
    check("ملف تفويض Google", BAD, f"{secrets_file} مفقود",
          "أنشئ OAuth client نوع Desktop في Google Cloud ونزّل الملف بجانب البرنامج")
else:
    try:
        with open(secrets_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        kind = "installed" if "installed" in data else ("web" if "web" in data else None)
        if kind and data[kind].get("client_id") and data[kind].get("client_secret"):
            note = "نوع Desktop (مناسب)" if kind == "installed" else "نوع Web — قد لا يناسب الربط المحلي"
            check("ملف تفويض Google", OK if kind == "installed" else WARN, note)
        else:
            check("ملف تفويض Google", BAD, "البنية غير مكتملة", "أعد تنزيل ملف OAuth")
    except (ValueError, OSError) as error:
        check("ملف تفويض Google", BAD, f"غير صالح: {error}", "أعد تنزيل ملف OAuth")

# 7) ملف كوكيز فيسبوك (لاكتشاف الريلز)
cookies_file = _config_value("SETTINGS", "COOKIES_FILE", "cookies.txt")
if os.path.exists(cookies_file):
    size = os.path.getsize(cookies_file)
    check("كوكيز فيسبوك", OK if size > 100 else WARN, f"{cookies_file} ({size} بايت)")
else:
    check("كوكيز فيسبوك", WARN, f"{cookies_file} غير موجود (فيسبوك يطلب دخولاً لعرض الريلز)",
          "صدّر كوكيز فيسبوك بإضافة Get cookies.txt LOCALLY بعد تسجيل الدخول")

# 8) ربط قناة YouTube (ملف التوكن)
tokens = sorted(n for n in os.listdir(".") if n.endswith("_token.json"))
if tokens:
    check("ربط قناة YouTube", OK, "تم العثور على: " + ", ".join(tokens))
else:
    check("ربط قناة YouTube", INFO, "لا يوجد ملف توكن — سيُطلب الربط عند أول تشغيل (ضغطة Allow)")

# 9) config.ini
if os.path.exists("config.ini"):
    check("ملف config.ini", OK, "موجود")
else:
    check("ملف config.ini", INFO, "غير موجود — ستُستخدم القيم الافتراضية",
          "انسخ config.ini.example إلى config.ini إن أردت تخصيص المسارات")

# 10) صلاحية الكتابة والمساحة
try:
    probe = ".doctor_write_test"
    with open(probe, "w", encoding="utf-8") as f:
        f.write("ok")
    os.remove(probe)
    free_gb = round(shutil.disk_usage(".").free / (1024 ** 3), 1)
    check("الكتابة على القرص", OK, f"المساحة الحرة {free_gb} GB")
except OSError as error:
    check("الكتابة على القرص", BAD, str(error), "شغّل البرنامج من مجلد تملك صلاحية الكتابة عليه")

print("=" * 62)
bad = sum(1 for s, *_ in results if s == BAD)
warn = sum(1 for s, *_ in results if s == WARN)
if bad:
    print(f"النتيجة: ❌ {bad} مشكلة تمنع التشغيل، و{warn} تحذير — راجع البنود أعلاه.")
elif warn:
    print(f"النتيجة: ⚠️ لا مانع من التشغيل، لكن هناك {warn} تحذير يُفضّل معالجته.")
else:
    print("النتيجة: ✅ البيئة جاهزة للتشغيل.")
print("=" * 62)

sys.exit(1 if bad else 0)
