"""
╔══════════════════════════════════════════════════════════════╗
║   FB → YouTube Uploader  v3.4 (Extraction & Media Hardening) ║
║   سحب ريلز/فيديوهات فيسبوك ورفعها إلى YouTube تلقائياً        ║
║   v3.4: استخراج أذكى (روابط دقيقة + كشف جدار الدخول)،         ║
║         عناوين/وصف نظيف بلا اسم الصفحة المصدر، فحص المقطع     ║
║         بـ ffprobe، وتغليف احترافي بـ ffmpeg (faststart +      ║
║         إزالة البيانات الوصفية المسرّبة)                       ║
╚══════════════════════════════════════════════════════════════╝
"""

import time, os, re, json, glob, logging, random, threading, queue, configparser, shutil, subprocess
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urlunparse, parse_qsl

import yt_dlp
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import StaleElementReferenceException

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.auth.transport.requests import Request
from googleapiclient.errors import HttpError

import tkinter as tk
from tkinter import messagebox, ttk, filedialog

# ════════════════════════════════════════════════════
# CONSTANTS & CONFIG
# ════════════════════════════════════════════════════
VERSION = "3.4"

BG      = "#0f0f1a"
CARD    = "#161625"
PANEL   = "#1c1c2e"
ACCENT  = "#3d5afe"
BORDER  = "#2a2a40"
TXT     = "#e0e0e0"
TXT2    = "#9fa8da"
INP     = "#1e1e32"
ERR     = "#ff5252"
SUCCESS = "#00e676"
WARN    = "#ffab40"
INFO    = "#40c4ff"

FB  = ("Segoe UI", 10)
FS  = ("Segoe UI", 12, "bold")
FSM = ("Segoe UI", 9)
FM  = ("Consolas", 10)

config = configparser.ConfigParser()
config.read('config.ini', encoding='utf-8')

SCOPES              = ["https://www.googleapis.com/auth/youtube.upload"]
VIDEO_DIRECTORY     = config.get('SETTINGS', 'VIDEO_DIRECTORY',     fallback='videos/')
CLIENT_SECRETS_FILE = config.get('SETTINGS', 'CLIENT_SECRETS_FILE', fallback='client_secrets.json')
COOKIES_FILE        = config.get('SETTINGS', 'COOKIES_FILE',        fallback='cookies.txt')
UPLOADED_IDS_FILE   = "uploaded_ids.txt"
ACCOUNTS_FILE       = "accounts.json"
STATS_FILE          = "stats.json"
UI_SETTINGS_FILE    = "ui_settings.json"
MAX_RETRIES         = 3
RETRY_DELAY         = 8

# أقل مهلة يقبلها YouTube عملياً لوقت النشر المجدول.
MIN_SCHEDULE_LEAD_MINUTES = 15
# حالات لا فائدة من إعادة المحاولة عليها (خطأ في الطلب/التفويض).
NON_RETRYABLE_UPLOAD_STATUSES = {400, 401, 404}

FFMPEG_BIN  = shutil.which("ffmpeg")
FFPROBE_BIN = shutil.which("ffprobe")

os.makedirs(VIDEO_DIRECTORY, exist_ok=True)

logging.basicConfig(
    filename="errors.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    encoding="utf-8",
)
logger = logging.getLogger("fb2yt")


class StopRequested(Exception):
    """يُرفع من داخل خيط العمل عندما يطلب المستخدم الإيقاف."""


# ════════════════════════════════════════════════════
# HELPERS — DATA & UTILS
# ════════════════════════════════════════════════════
def load_accounts():
    if os.path.exists(ACCOUNTS_FILE):
        try:
            with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list) and data:
                return data
        except (OSError, ValueError) as error:
            logger.warning("تعذر قراءة %s: %s", ACCOUNTS_FILE, error)
    return ["account1"]


def save_accounts(accounts):
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump(accounts, f, ensure_ascii=False)


def load_uploaded_ids():
    if not os.path.exists(UPLOADED_IDS_FILE):
        return set()
    try:
        with open(UPLOADED_IDS_FILE, "r", encoding="utf-8") as f:
            return {line.strip() for line in f if line.strip()}
    except OSError as error:
        logger.warning("تعذر قراءة %s: %s", UPLOADED_IDS_FILE, error)
        return set()


def save_uploaded_id(video_id):
    with open(UPLOADED_IDS_FILE, "a", encoding="utf-8") as f:
        f.write(f"{video_id}\n")
        f.flush()
        os.fsync(f.fileno())


def load_stats():
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError) as error:
            logger.warning("تعذر قراءة %s: %s", STATS_FILE, error)
    return {"total_uploaded": 0, "total_failed": 0,
            "total_downloaded": 0, "sessions": []}


def save_stats(stats):
    tmp_path = STATS_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, STATS_FILE)


def load_ui_settings():
    if os.path.exists(UI_SETTINGS_FILE):
        try:
            with open(UI_SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError) as error:
            logger.warning("تعذر قراءة %s: %s", UI_SETTINGS_FILE, error)
    return {}


def save_ui_settings(settings):
    try:
        with open(UI_SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
    except OSError as error:
        logger.warning("تعذر حفظ إعدادات الواجهة: %s", error)


def disk_free_gb():
    try:
        _total, _used, free = shutil.disk_usage(VIDEO_DIRECTORY)
        return round(free / (1024 ** 3), 2)
    except OSError as error:
        logger.warning("تعذر قراءة مساحة القرص: %s", error)
        return 0


def sanitize_account_name(name):
    """يمنع تسرب اسم الحساب إلى مسار ملف غير آمن."""
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", (name or "").strip())
    safe = safe.strip("._")
    return safe or "account1"


# ════════════════════════════════════════════════════
# HELPERS — TEXT SANITIZATION (العناوين/الوصف + إخفاء المصدر)
# ════════════════════════════════════════════════════
# الأحرف التي لا يقبلها YouTube فعلاً في العنوان (زائد محارف التحكم).
INVALID_TITLE_CHARS = re.compile(r"[<>\x00-\x1f]")
URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
MENTION_RE = re.compile(r"@\w+")
HASHTAG_RE = re.compile(r"#\w+")
# ضجيج إحصائي فيسبوكي يتسلل إلى العناوين الأصلية.
JUNK_STATS = re.compile(
    r"\b\d[\d.,]*\s*[kKmMbB]?\s*"
    r"(views?|مشاهدة|مشاهدات|likes?|إعجاب|تفاعل|تفاعلات|comments?|تعليق|تعليقات|"
    r"shares?|مشاركة|مشاركات|followers?|متابع|متابعين)\b",
    re.IGNORECASE,
)


def sanitize_public_text(text, page_name="", remove_hashtags=False,
                         remove_mentions=False, keep_newlines=False):
    """يُنظّف أي نص سيظهر علناً (عنوان/وصف):

    - يحذف الروابط وضجيج الإحصاءات الفيسبوكية ومحارف غير مقبولة.
    - يحذف اسم الصفحة المصدر بكل أشكاله (حالة مختلفة/بلا مسافات) حتى لا يظهر.
    - يحذف الهاشتاجات/المنشن اختيارياً.
    """
    if not text:
        return ""

    text = URL_RE.sub(" ", text)
    text = JUNK_STATS.sub(" ", text)
    if remove_hashtags:
        text = HASHTAG_RE.sub(" ", text)
    if remove_mentions:
        text = MENTION_RE.sub(" ", text)
    text = INVALID_TITLE_CHARS.sub(" ", text)

    # إخفاء اسم الصفحة المصدر بكل صيغه الشائعة
    if page_name:
        for variant in {page_name, page_name.replace(" ", ""), page_name.replace("_", " ")}:
            variant = variant.strip()
            if variant:
                text = re.sub(re.escape(variant), " ", text, flags=re.IGNORECASE)

    if keep_newlines:
        text = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines())
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
    else:
        text = re.sub(r"\s+", " ", text).strip()
    return text


def _truncate_smart(text, limit=100):
    """يقص عند حدود كلمة حتى لا ينشطر منتصف كلمة عربية/إنجليزية."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut[:-1]:
        cut = cut.rsplit(" ", 1)[0]
    cut = cut.rstrip(" .،,;:-—|·")
    return cut or text[:limit]


def build_title(title, page_name="", remove_hashtags=False, anonymize=True):
    """يبني عنواناً نظيفاً جاهزاً للنشر، بلا اسم الصفحة المصدر."""
    text = sanitize_public_text(
        title,
        page_name if anonymize else "",
        remove_hashtags=remove_hashtags,
    )
    text = _truncate_smart(text, 100)
    return text or "Facebook Reel"


def clean_title(title, page_name="", remove_hashtags=False):
    """غلاف توافقي فوق build_title (إخفاء المصدر مفعّل دائماً)."""
    return build_title(title, page_name, remove_hashtags, anonymize=True)


def build_description(description, page_name="", extra="", anonymize=True,
                      remove_hashtags=False):
    """يبني وصفاً نظيفاً: يحذف روابط واسم الصفحة المصدر والمنشن، ويحافظ على الأسطر."""
    base = sanitize_public_text(
        description or "",
        page_name if anonymize else "",
        remove_hashtags=remove_hashtags,
        remove_mentions=anonymize,
        keep_newlines=True,
    )
    parts = [part for part in (base, (extra or "").strip()) if part]
    return "\n\n".join(parts)[:5000]


# ════════════════════════════════════════════════════
# HELPERS — URL & EXTRACTION (استخراج الروابط)
# ════════════════════════════════════════════════════
FACEBOOK_HOSTS = ("facebook.com", "fb.com")
REELS_SEGMENTS = {"reel", "reels", "videos", "watch"}


def normalize_channel_url(url):
    """يوحّد رابط الصفحة ليشير إلى تبويب الريلز ويصلح الروابط الشائعة.

    - `facebook.com/name` → `facebook.com/name/reels`
    - `facebook.com/profile.php?id=X` → يضيف `sk=reels_tab`
    - الروابط التي تشير أصلاً إلى reel/reels/videos تُترك كما هي.
    """
    url = (url or "").strip().strip('"').strip("'").strip()
    if not url:
        raise ValueError("رابط الصفحة فارغ")
    if "://" not in url:
        url = "https://" + url

    parts = urlparse(url)
    host = parts.netloc.lower().split(":")[0]
    if not any(host == h or host.endswith("." + h) for h in FACEBOOK_HOSTS):
        raise ValueError(f"ليس رابط فيسبوك: {url}")

    path, query = parts.path, parts.query
    segments = [s for s in path.split("/") if s]
    lowered = {s.lower() for s in segments}

    # رابط يشير إلى ريلز/فيديوهات أو يحمل sk بالفعل — اتركه كما هو.
    if lowered & {"reel", "reels", "videos"} or "sk" in {k.lower() for k, _ in parse_qsl(query)}:
        return urlunparse(parts)

    if segments and segments[0].lower() == "profile.php":
        query = f"{query}&sk=reels_tab" if query else "sk=reels_tab"
    elif len(segments) == 1:
        path = f"/{segments[0]}/reels"

    return urlunparse((parts.scheme, parts.netloc, path, parts.params, query, parts.fragment))


def extract_reel_id(url):
    """يستخرج معرّف الفيديو/الريل الرقمي من الرابط."""
    match = re.search(r"/(?:reel|videos)/(\d+)", url)
    return match.group(1) if match else None


def order_reels(links, newest_first=True):
    """يزيل التكرار بترتيب الاكتشاف ثم يرتب حسب رقم المعرّف (الأحدث أولاً)."""
    unique = list(dict.fromkeys(links))
    if newest_first:
        unique.sort(key=lambda u: int(extract_reel_id(u) or 0), reverse=True)
    return unique


def clamp_publish_at(publish_at, now=None):
    """يضمن أن وقت النشر المجدول يسبقه هامش كافٍ يقبله YouTube."""
    now = now or datetime.now(timezone.utc)
    earliest = now + timedelta(minutes=MIN_SCHEDULE_LEAD_MINUTES)
    if publish_at < earliest:
        return earliest
    return publish_at


def _cleanup_artifacts(*paths):
    """يحذف الملفات المؤقتة والمجزأة الناتجة عن محاولة تنزيل فاشلة."""
    for path in paths:
        if not path:
            continue
        stem = os.path.splitext(path)[0]
        targets = {path, stem + ".part", stem + ".ytdl"}
        targets.update(glob.glob(stem + ".f*"))
        targets.update(glob.glob(stem + ".*.part"))
        targets.update(glob.glob(stem + ".*.ytdl"))
        for target in targets:
            try:
                if os.path.isfile(target):
                    os.remove(target)
            except OSError as error:
                logger.warning("تعذر حذف ملف مؤقت %s: %s", target, error)


# ════════════════════════════════════════════════════
# HELPERS — MEDIA PROCESSING (برمجة المقطع)
# ════════════════════════════════════════════════════
def probe_video(path, log_cb=None):
    """يفحص المقطع بـ ffprobe قبل الرفع.

    يعيد (صالح؟, معلومات). الفحص «متساهل»: غياب ffprobe أو فشله لا يمنع الرفع.
    """
    if not os.path.isfile(path):
        return False, {"error": "missing-file"}
    if not FFPROBE_BIN:
        if log_cb:
            log_cb("⚠️ ffprobe غير مثبت؛ سيتخطى فحص المقطع.")
        return True, {}

    try:
        result = subprocess.run(
            [FFPROBE_BIN, "-v", "error",
             "-show_entries", "format=duration,size:stream=codec_type,codec_name,width,height",
             "-of", "json", path],
            capture_output=True, text=True, timeout=60,
        )
        data = json.loads(result.stdout or "{}")
    except Exception as error:
        logger.warning("فشل فحص ffprobe لـ %s: %s", path, error)
        if log_cb:
            log_cb("⚠️ تعذر فحص المقطع؛ سيكمل دون فحص.")
        return True, {}

    streams = data.get("streams") or []
    has_video = any(s.get("codec_type") == "video" for s in streams)
    duration = float((data.get("format") or {}).get("duration") or 0)
    size = int((data.get("format") or {}).get("size") or 0)

    problems = []
    if not has_video:
        problems.append("لا يوجد مسار فيديو")
    if duration <= 0:
        problems.append("مدة غير صالحة")
    if size < 10 * 1024:
        problems.append("حجم صغير جداً")
    if problems:
        return False, {"problems": problems, "duration": duration, "size": size}

    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    return True, {
        "duration": duration, "size": size,
        "width": video.get("width"), "height": video.get("height"),
        "vcodec": video.get("codec_name"),
    }


def finalize_video(path, log_cb=None):
    """يعيد تغليف المقطع قبل الرفع:
    - faststart (الوصفية في المقدمة) لتسريع معالجة YouTube.
    - حذف كل البيانات الوصفية المسرّبة (اسم المحرر/الصفحة/البرنامج) حفاظاً على إخفاء المصدر.
    ينسخ التدفقات دون إعادة ترميز؛ عند الفشل يعيد الملف الأصلي.
    """
    if not FFMPEG_BIN:
        if log_cb:
            log_cb("⚠️ ffmpeg غير مثبت؛ سيرفع المقطع كما هو.")
        return path

    out_path = os.path.splitext(path)[0] + ".faststart.mp4"
    cmd = [
        FFMPEG_BIN, "-y", "-v", "error", "-i", path,
        "-c", "copy", "-movflags", "+faststart",
        "-map_metadata", "-1",
        "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact",
        out_path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode == 0 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
            if log_cb:
                log_cb("🛠 حُسّن المقطع: faststart + إزالة البيانات الوصفية.")
            return out_path
        logger.warning("فشل ffmpeg على %s: %s", path, (proc.stderr or "")[:500])
        _cleanup_artifacts(out_path)
        return path
    except Exception:
        logger.exception("فشل غير متوقع في ffmpeg على %s", path)
        _cleanup_artifacts(out_path)
        return path


# ════════════════════════════════════════════════════
# HELPERS — SELENIUM (المتصفح والكوكيز)
# ════════════════════════════════════════════════════
def init_driver(headless=True, log_cb=None):
    options = Options()
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-infobars")
    options.add_argument("--lang=ar")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    options.add_argument(f"user-agent={ua}")

    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
    else:
        options.add_argument("--start-maximized")

    try:
        driver = webdriver.Chrome(options=options)
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        })
        return driver
    except Exception as error:
        logger.exception("فشل تشغيل المتصفح")
        if log_cb:
            log_cb(f"❌ فشل تشغيل المتصفح: {error}")
        return None


def _parse_cookie_file(path):
    """يقرأ ملف كوكيز بصيغة Netscape (cookies.txt) أو JSON (تصدير Cookie-Editor)."""
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()

    stripped = content.strip()
    if stripped.startswith("["):
        try:
            data = json.loads(stripped)
            cookies = []
            for c in data:
                cookies.append({
                    "name": c.get("name"),
                    "value": c.get("value"),
                    "domain": c.get("domain", ".facebook.com"),
                    "path": c.get("path", "/"),
                    "secure": bool(c.get("secure", True)),
                    "expiry": int(c["expirationDate"]) if c.get("expirationDate") else None,
                })
            return cookies
        except (ValueError, TypeError):
            pass

    cookies = []
    for line in content.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.rstrip("\n").split("\t")
        if len(parts) != 7:
            continue
        domain, _flag, cpath, secure, expiry, name, value = parts
        cookies.append({
            "name": name, "value": value, "domain": domain, "path": cpath,
            "secure": secure.upper() == "TRUE",
            "expiry": int(expiry) if expiry.isdigit() else None,
        })
    return cookies


def load_cookies_into_driver(driver, cookies_file, log_cb=None):
    """يحقن كوكيز فيسبوك في جلسة Selenium (لرؤية كل الريلز بدل أول صفحة فقط)."""
    if not cookies_file or not os.path.isfile(cookies_file):
        return 0
    try:
        cookies = _parse_cookie_file(cookies_file)
    except OSError as error:
        logger.warning("تعذر قراءة ملف الكوكيز %s: %s", cookies_file, error)
        return 0
    if not cookies:
        if log_cb:
            log_cb("⚠️ ملف الكوكيز فارغ أو بصيغة غير معروفة.")
        return 0

    try:
        driver.get("https://www.facebook.com/")
    except Exception:
        pass
    added = 0
    for cookie in cookies:
        payload = {k: v for k, v in cookie.items() if v is not None}
        if not payload.get("name"):
            continue
        try:
            driver.add_cookie(payload)
            added += 1
        except Exception:
            continue
    if log_cb:
        log_cb(f"🍪 حُقنت {added} كوكيز في المتصفح.")
    return added


def get_reels(driver, fb_page, scroll_times, video_limit, log_cb=None, stop_event=None):
    """يجمع روابط الريلز/الفيديوهات بترتيب الاكتشاف دون تكرار.

    - يستخدم محددات دقيقة (روابط /reel/ و /videos/) بدل كل الروابط.
    - يكشف جدار تسجيل الدخول بدل العودة بقائمة فارغة صامتة.
    """
    try:
        driver.get(fb_page)
        try:
            WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.TAG_NAME, "a")))
        except Exception:
            pass
        time.sleep(4)

        # كشف جدار تسجيل الدخول
        current_url = (driver.current_url or "").lower()
        login_form = driver.find_elements(By.CSS_SELECTOR, "input[name='email'], form[action*='login']")
        if "login" in current_url or login_form:
            if log_cb:
                log_cb("🔐 فيسبوك يطلب تسجيل الدخول هنا؛ فعّل ملف الكوكيز من «خيارات متقدمة».")
            return []

        links = {}
        last_count = 0
        stall_count = 0

        for i in range(scroll_times):
            if stop_event is not None and stop_event.is_set():
                raise StopRequested()
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(3)

            for el in driver.find_elements(By.CSS_SELECTOR, 'a[href*="/reel/"], a[href*="/videos/"]'):
                try:
                    href = el.get_attribute("href")
                    if not href:
                        continue
                    clean = href.split("?")[0].split("&")[0]
                    if extract_reel_id(clean):
                        links.setdefault(clean, None)
                except StaleElementReferenceException:
                    continue
                except Exception:
                    continue

            if log_cb:
                log_cb(f"📜 تمرير {i + 1}/{scroll_times} — {len(links)} رابط")

            if len(links) == last_count:
                stall_count += 1
                if stall_count >= 3:
                    break
            else:
                stall_count = 0

            last_count = len(links)
            if len(links) >= video_limit:
                break

        if not links and log_cb:
            log_cb("⚠️ لم يُعثر على روابط. إن كانت الصفحة تتطلب تسجيل دخول، فعّل ملف الكوكيز من «خيارات متقدمة».")
        return list(links.keys())[:video_limit]
    except StopRequested:
        raise
    except Exception as error:
        logger.exception("خطأ في استخراج الروابط من %s", fb_page)
        if log_cb:
            log_cb(f"❌ خطأ في استخراج الروابط: {error}")
        return []


def get_youtube(account_name, secrets_file=None):
    """يربط حساب YouTube المحدد ويعيد (الخدمة، رسالة الخطأ)."""
    sf = secrets_file or CLIENT_SECRETS_FILE
    token_file = f"{sanitize_account_name(account_name)}_token.json"
    creds = None

    if os.path.exists(token_file):
        try:
            creds = Credentials.from_authorized_user_file(token_file, SCOPES)
        except (OSError, ValueError) as error:
            logger.warning("تعذر قراءة رمز الحساب %s: %s", token_file, error)
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as error:
                logger.warning("تعذر تجديد رمز %s: %s", account_name, error)
                creds = None

        if not creds:
            if not os.path.exists(sf):
                return None, f"ملف {os.path.basename(sf)} مفقود"
            try:
                flow = InstalledAppFlow.from_client_secrets_file(sf, SCOPES)
                creds = flow.run_local_server(port=0)
            except Exception as error:
                logger.exception("فشل تفويض الحساب %s", account_name)
                return None, f"فشل تفويض YouTube: {error}"

        with open(token_file, "w", encoding="utf-8") as f:
            f.write(creds.to_json())

    try:
        return build("youtube", "v3", credentials=creds), None
    except Exception as error:
        logger.exception("فشل إنشاء عميل YouTube")
        return None, f"فشل الاتصال بـ YouTube: {error}"


def _downloaded_candidates(info, ydl=None):
    """كل المسارات المحتملة لملف التنزيل (تُستخدم للحذف والتقاط الملف)."""
    if info is None:
        return []
    candidates = []
    for item in info.get("requested_downloads") or []:
        if isinstance(item, dict):
            candidates.extend([item.get("filepath"), item.get("_filename")])
    candidates.extend([info.get("filepath"), info.get("_filename")])
    if ydl is not None:
        try:
            candidates.append(ydl.prepare_filename(info))
        except Exception:
            pass
    return [c for c in candidates if c]


def _resolve_downloaded_file(info, ydl):
    """يعثر على ملف الفيديو الفعلي بعد الدمج، لا على الاسم المتوقع فقط."""
    for candidate in _downloaded_candidates(info, ydl):
        if os.path.isfile(candidate):
            return candidate
        merged = os.path.splitext(candidate)[0] + ".mp4"
        if os.path.isfile(merged):
            return merged
    return None


def download_video(url, page_name="", log_cb=None, retries=MAX_RETRIES,
                   remove_tags=False, stop_event=None, cookies_file=None,
                   anonymize=True):
    ydl_opts = {
        "format": "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "outtmpl": os.path.join(VIDEO_DIRECTORY, "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
        "retries": 10,
        "socket_timeout": 60,
    }
    if cookies_file and os.path.isfile(cookies_file):
        ydl_opts["cookiefile"] = cookies_file

    def stop_hook(_status):
        if stop_event is not None and stop_event.is_set():
            raise StopRequested()

    ydl_opts["progress_hooks"] = [stop_hook]

    for attempt in range(1, retries + 1):
        info = None
        file_path = None
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                file_path = _resolve_downloaded_file(info, ydl)
                if not file_path:
                    raise FileNotFoundError("اكتمل الاستخراج لكن لم يُعثر على ملف MP4 الناتج.")

                title = build_title(info.get("title", ""), page_name, remove_tags, anonymize)
                description = build_description(
                    info.get("description") or "", page_name, anonymize=anonymize
                )
                size_mb = round(os.path.getsize(file_path) / (1024 ** 2), 1)
                if log_cb:
                    log_cb(f"✅ تحميل ناجح: {title[:40]}... ({size_mb} MB)")
                logger.info("اكتمل تنزيل الفيديو: %s", file_path)
                return {
                    "file": file_path,
                    "title": title,
                    "desc": description,
                    "size_mb": size_mb,
                    "duration": info.get("duration", 0),
                }
        except StopRequested:
            _cleanup_artifacts(file_path)
            _cleanup_artifacts(*_downloaded_candidates(info))
            raise
        except Exception as error:
            logger.exception("فشل تنزيل الفيديو %s في المحاولة %s", url, attempt)
            _cleanup_artifacts(file_path, *_downloaded_candidates(info))
            if attempt < retries:
                if log_cb:
                    log_cb(f"⚠️ فشل التحميل: {error}. إعادة المحاولة ({attempt}/{retries})…")
                time.sleep(RETRY_DELAY)
            elif log_cb:
                log_cb(f"❌ فشل التحميل نهائياً: {error}")
    return None


def _insert_to_playlist(youtube, playlist_id, video_id, log_cb=None):
    """إضافة الفيديو لقائمة تشغيل — خطوة اختيارية لا تُفسد نجاح الرفع."""
    try:
        youtube.playlistItems().insert(
            part="snippet",
            body={"snippet": {
                "playlistId": playlist_id,
                "resourceId": {"kind": "youtube#video", "videoId": video_id},
            }},
        ).execute()
        return True
    except HttpError as error:
        logger.warning("رُفع الفيديو %s لكن تعذّرت إضافته لقائمة التشغيل: %s", video_id, error)
        if log_cb:
            log_cb(f"⚠️ رُفع الفيديو لكن تعذّرت إضافته لقائمة التشغيل: {error}")
        return False


def upload_video(youtube, video_data, privacy="public", playlist_id=None, tags=None,
                 category="22", sched_time=None, log_cb=None, retries=MAX_RETRIES):
    body = {
        "snippet": {
            "title": video_data["title"],
            "description": video_data["desc"],
            "tags": [tag.strip() for tag in (tags or ["Facebook", "Reel"]) if tag.strip()],
            "categoryId": category,
        },
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    if sched_time:
        body["status"]["privacyStatus"] = "private"
        body["status"]["publishAt"] = sched_time.astimezone(timezone.utc).isoformat()

    if not os.path.isfile(video_data["file"]):
        message = f"ملف الفيديو غير موجود بعد التنزيل: {video_data['file']}"
        logger.error(message)
        if log_cb:
            log_cb(f"❌ {message}")
        return None

    for attempt in range(1, retries + 1):
        try:
            media = MediaFileUpload(video_data["file"], chunksize=1024 * 1024, resumable=True)
            request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
            response = None
            while response is None:
                status, response = request.next_chunk()
                if status and log_cb:
                    log_cb(f"⬆️ جاري الرفع: {int(status.progress() * 100)}%", replace_last=True)

            vid_id = response.get("id")
            if not vid_id:
                raise RuntimeError("لم يرجع YouTube معرّف الفيديو بعد الرفع.")
            if log_cb:
                log_cb(f"🚀 تم الرفع بنجاح! ID: {vid_id}")
            # الإضافة لقائمة التشغيل بعد نجاح الرفع، وخارج مسار إعادة المحاولة.
            if playlist_id:
                _insert_to_playlist(youtube, playlist_id, vid_id, log_cb)
            return vid_id
        except HttpError as error:
            logger.exception("خطأ YouTube API في المحاولة %s", attempt)
            status_code = error.resp.status if error.resp else 0
            if status_code in [403, 429]:
                if log_cb:
                    log_cb(f"❌ رفض YouTube الرفع (HTTP {status_code}). تحقق من الحصة والصلاحيات.")
                return "QUOTA_ERROR"
            message = f"خطأ YouTube HTTP {status_code}: {error}"
            if status_code in NON_RETRYABLE_UPLOAD_STATUSES:
                logger.error("خطأ غير قابل لإعادة المحاولة: %s", message)
                if log_cb:
                    log_cb(f"❌ {message}")
                return None
        except Exception as error:
            logger.exception("خطأ غير متوقع أثناء رفع الفيديو في المحاولة %s", attempt)
            message = f"خطأ أثناء الرفع: {error}"

        if attempt < retries:
            if log_cb:
                log_cb(f"⚠️ {message}. إعادة المحاولة ({attempt}/{retries})…")
            time.sleep(RETRY_DELAY)
        elif log_cb:
            log_cb(f"❌ فشل الرفع نهائياً: {message}")
    return None


# ════════════════════════════════════════════════════
# UI COMPONENTS (STYLISH)
# ════════════════════════════════════════════════════
class Btn(tk.Button):
    def __init__(self, master, text, command, color=ACCENT, **kwargs):
        super().__init__(master, text=text, command=command, bg=color, fg="white", font=FB,
                         relief="flat", padx=15, pady=6, activebackground=color,
                         activeforeground="white", cursor="hand2", **kwargs)
        self.default_bg = color
        self.bind("<Enter>", lambda e: self.config(bg=self._lighten(color)))
        self.bind("<Leave>", lambda e: self.config(bg=color))

    def set_color(self, color):
        self.default_bg = color
        self.config(bg=color)

    def _lighten(self, hex):
        hex = hex.lstrip('#')
        rgb = tuple(int(hex[i:i + 2], 16) for i in (0, 2, 4))
        new_rgb = tuple(min(255, c + 30) for c in rgb)
        return '#%02x%02x%02x' % new_rgb


class PEntry(tk.Entry):
    def __init__(self, master, ph="", **kwargs):
        super().__init__(master, bg=INP, fg=TXT, insertbackground=ACCENT, relief="flat",
                         font=FB, highlightthickness=1, highlightbackground=BORDER, **kwargs)
        self.ph = ph
        self._on = False
        if ph:
            self._put()
            self.bind("<FocusIn>", self._clear)
            self.bind("<FocusOut>", self._put)

    def _put(self, _=None):
        if not self.get():
            self.insert(0, self.ph)
            self.config(fg=TXT2)
            self._on = True

    def _clear(self, _=None):
        if self._on:
            self.delete(0, "end")
            self.config(fg=TXT)
            self._on = False

    def set_val(self, text):
        """يضبط القيمة ويتعامل مع نص العنصر النائب."""
        self._on = False
        self.delete(0, "end")
        if text is None or str(text) == "":
            self._put()
        else:
            self.config(fg=TXT)
            self.insert(0, str(text))

    def val(self):
        return "" if self._on else self.get()


def sec(master, text, icon=""):
    f = tk.Frame(master, bg=master["bg"])
    f.pack(fill="x", pady=(12, 6))
    tk.Label(f, text=f"{icon}  {text}", bg=master["bg"], fg=ACCENT, font=FS).pack(side="left")
    tk.Frame(f, bg=BORDER, height=1).pack(side="left", fill="x", expand=True, padx=(10, 0))


def mk_card(master):
    return tk.Frame(master, bg=CARD, highlightthickness=1, highlightbackground=BORDER)


class StatsCard:
    def __init__(self, master):
        self.f = tk.Frame(master, bg=BG)
        self.f.pack(fill="x", pady=10)
        self.vars = {
            "total_uploaded": tk.StringVar(value="0"),
            "total_failed": tk.StringVar(value="0"),
            "total_downloaded": tk.StringVar(value="0"),
            "disk_free": tk.StringVar(value="0 GB"),
        }
        cols = [("مرفوع الإجمالي", "total_uploaded", SUCCESS),
                ("فشل الإجمالي", "total_failed", ERR),
                ("تحميل الإجمالي", "total_downloaded", INFO),
                ("مساحة القرص", "disk_free", WARN)]
        for t, v, c in cols:
            cf = mk_card(self.f)
            cf.pack(side="left", fill="both", expand=True, padx=5)
            tk.Label(cf, text=t, bg=CARD, fg=TXT2, font=FSM).pack(pady=(10, 2))
            tk.Label(cf, textvariable=self.vars[v], bg=CARD, fg=c,
                     font=("Segoe UI Semibold", 16)).pack(pady=(0, 10))
        self.refresh()

    def refresh(self, s=None):
        if s is None:
            s = load_stats()
        self.vars["total_uploaded"].set(str(s.get("total_uploaded", 0)))
        self.vars["total_failed"].set(str(s.get("total_failed", 0)))
        self.vars["total_downloaded"].set(str(s.get("total_downloaded", 0)))
        self.vars["disk_free"].set(f"{disk_free_gb()} GB")


# ════════════════════════════════════════════════════
# MAIN APP (V3.4)
# ════════════════════════════════════════════════════
class App:
    def __init__(self, root):
        self.root = root
        self.root.title(f"🎬 FB → YouTube Uploader v{VERSION} (Extraction & Media)")
        self.root.configure(bg=BG)
        self.root.geometry("1050x900")

        self._stop = threading.Event()
        self._thread = None
        self._logq = queue.Queue()
        self._uiq = queue.Queue()
        self._last_log_replace = False
        self._last_progress = 0.0

        self.accounts = load_accounts()
        self.stats = load_stats()
        self.saved = load_ui_settings()

        self._apply_styles()
        self._build_header()
        self._build_nb()
        self._build_footer()
        self._restore_settings()
        self._poll()

    def _apply_styles(self):
        s = ttk.Style(); s.theme_use("clam")
        s.configure("TProgressbar", troughcolor=INP, background=ACCENT, borderwidth=0, thickness=12)
        s.configure("TNotebook", background=BG, borderwidth=0)
        s.configure("TNotebook.Tab", background=CARD, foreground=TXT2, padding=[20, 10], font=FB)
        s.map("TNotebook.Tab", background=[("selected", ACCENT)], foreground=[("selected", "white")])
        s.configure("Treeview", background=CARD, foreground=TXT, rowheight=32,
                    fieldbackground=CARD, font=FB, borderwidth=0)
        s.configure("Treeview.Heading", background=PANEL, foreground=ACCENT, font=FS, relief="flat")
        s.map("Treeview", background=[("selected", ACCENT)])
        s.configure("TCombobox", fieldbackground=INP, background=CARD, foreground=TXT,
                    selectbackground=ACCENT, font=FB)
        s.configure("TCheckbutton", background=BG, foreground=TXT, font=FB)

    def _build_header(self):
        h = tk.Frame(self.root, bg=PANEL, pady=16)
        h.pack(fill="x")
        inner = tk.Frame(h, bg=PANEL); inner.pack(padx=24)
        tk.Label(inner, text="🚀", bg=PANEL, fg=ACCENT, font=("Segoe UI", 26)).pack(side="left", padx=(0, 15))
        tl = tk.Frame(inner, bg=PANEL); tl.pack(side="left")
        tk.Label(tl, text="FB → YouTube Advanced", bg=PANEL, fg=TXT,
                 font=("Segoe UI Semibold", 18, "bold")).pack(anchor="w")
        tk.Label(tl, text=f"استخراج أذكى • برمجة مقطع احترافية • إخفاء المصدر  •  v{VERSION}", bg=PANEL, fg=TXT2,
                 font=FSM).pack(anchor="w")
        Btn(inner, "📊 الإحصائيات", lambda: self.nb.select(self.t_dash), color="#303050").pack(side="right")
        tk.Frame(self.root, bg=ACCENT, height=2).pack(fill="x")

    def _build_nb(self):
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=16, pady=(12, 0))

        self.t_pages   = tk.Frame(self.nb, bg=BG)
        self.t_yt      = tk.Frame(self.nb, bg=BG)
        self.t_opts    = tk.Frame(self.nb, bg=BG)
        self.t_log     = tk.Frame(self.nb, bg=BG)
        self.t_accs    = tk.Frame(self.nb, bg=BG)
        self.t_dash    = tk.Frame(self.nb, bg=BG)

        self.nb.add(self.t_pages, text="  📄 الصفحات  ")
        self.nb.add(self.t_yt,    text="  📺 يوتيوب  ")
        self.nb.add(self.t_opts,  text="  ⚙️ خيارات متقدمة  ")
        self.nb.add(self.t_log,   text="  📋 السجل  ")
        self.nb.add(self.t_accs,  text="  👤 الحسابات  ")
        self.nb.add(self.t_dash,  text="  📊 لوحة التحكم  ")

        self._tab_pages()
        self._tab_youtube()
        self._tab_options()
        self._tab_log()
        self._tab_accounts()
        self._tab_dashboard()

    def _tab_pages(self):
        p = self.t_pages
        out = tk.Frame(p, bg=BG); out.pack(fill="both", expand=True, padx=20, pady=10)
        sec(out, "قائمة الصفحات المستهدفة", "📄")
        tf = mk_card(out); tf.pack(fill="both", expand=True, pady=(0, 10))
        cols = ("الرابط", "اسم الصفحة", "تمرير", "حد")
        self.ptree = ttk.Treeview(tf, columns=cols, show="headings", height=8, selectmode="browse")
        for c, w in zip(cols, [400, 180, 80, 80]):
            self.ptree.heading(c, text=c)
            self.ptree.column(c, width=w, anchor="center")
        vsb = ttk.Scrollbar(tf, orient="vertical", command=self.ptree.yview)
        self.ptree.configure(yscrollcommand=vsb.set)
        self.ptree.pack(side="left", fill="both", expand=True, padx=2, pady=2)
        vsb.pack(side="right", fill="y")

        sec(out, "إضافة صفحة جديدة", "➕")
        fm = mk_card(out); fm.pack(fill="x", pady=(0, 8))
        inn = tk.Frame(fm, bg=CARD, padx=14, pady=12); inn.pack(fill="x")
        r1 = tk.Frame(inn, bg=CARD); r1.pack(fill="x", pady=4)
        tk.Label(r1, text="رابط الصفحة:", bg=CARD, fg=TXT, width=15, anchor="e").pack(side="left")
        self.pg_url = PEntry(r1, ph="https://www.facebook.com/...", width=60)
        self.pg_url.pack(side="left", padx=10, ipady=4)

        r2 = tk.Frame(inn, bg=CARD); r2.pack(fill="x", pady=4)
        tk.Label(r2, text="اسم الصفحة:", bg=CARD, fg=TXT, width=15, anchor="e").pack(side="left")
        self.pg_name = PEntry(r2, ph="اختياري — يُخفى من العنوان والوصف", width=25)
        self.pg_name.pack(side="left", padx=10, ipady=4)
        tk.Label(r2, text="تمرير:", bg=CARD, fg=TXT).pack(side="left")
        self.pg_scroll = PEntry(r2, width=6); self.pg_scroll.insert(0, "3")
        self.pg_scroll.pack(side="left", padx=8, ipady=4)
        tk.Label(r2, text="حد الفيديوهات:", bg=CARD, fg=TXT).pack(side="left")
        self.pg_limit = PEntry(r2, width=6); self.pg_limit.insert(0, "10")
        self.pg_limit.pack(side="left", padx=8, ipady=4)

        br = tk.Frame(inn, bg=CARD); br.pack(fill="x", pady=(10, 0))
        Btn(br, "➕ إضافة", self._page_add, ACCENT).pack(side="left")
        Btn(br, "🗑 حذف", self._page_del, ERR).pack(side="left", padx=10)
        Btn(br, "📋 استيراد CSV", self._import_pages, "#37474f").pack(side="right")

    def _page_add(self):
        url = self.pg_url.val().strip()
        if not url:
            return
        try:
            sc, lm = int(self.pg_scroll.get()), int(self.pg_limit.get())
            if sc < 1 or lm < 1:
                raise ValueError
            tag = "odd" if len(self.ptree.get_children()) % 2 else "even"
            self.ptree.insert("", "end", values=(url, self.pg_name.val().strip(), sc, lm), tags=(tag,))
            self.pg_url.delete(0, "end"); self.pg_url._put()
        except ValueError:
            messagebox.showerror("خطأ", "أدخل أرقاماً صحيحة أكبر من صفر")

    def _page_del(self):
        s = self.ptree.selection()
        if s:
            self.ptree.delete(s[0])

    def _import_pages(self):
        path = filedialog.askopenfilename(filetypes=[("CSV", "*.csv"), ("Text", "*.txt")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    p = [x.strip() for x in line.split(",")]
                    if p and p[0].startswith("http"):
                        self.ptree.insert("", "end", values=(
                            p[0],
                            p[1] if len(p) > 1 else "",
                            p[2] if len(p) > 2 else 3,
                            p[3] if len(p) > 3 else 10,
                        ))
        except OSError as error:
            messagebox.showerror("خطأ", f"تعذر قراءة الملف: {error}")

    def _get_pages(self):
        return [{"url": v[0], "name": v[1], "scroll": int(v[2]), "limit": int(v[3])}
                for v in [self.ptree.item(i, "values") for i in self.ptree.get_children()]]

    def _tab_youtube(self):
        out = tk.Frame(self.t_yt, bg=BG)
        out.pack(fill="both", expand=True, padx=20, pady=10)
        sec(out, "إعدادات القناة", "📺")
        c = mk_card(out); c.pack(fill="x", pady=(0, 10))
        inn = tk.Frame(c, bg=CARD, padx=14, pady=12); inn.pack(fill="x")
        r1 = tk.Frame(inn, bg=CARD); r1.pack(fill="x", pady=4)
        tk.Label(r1, text="الحساب النشط:", bg=CARD, fg=TXT, width=15, anchor="e").pack(side="left")
        self.acc_var = tk.StringVar(value=self.accounts[0] if self.accounts else "account1")
        self.acc_combo = ttk.Combobox(r1, textvariable=self.acc_var, values=self.accounts,
                                      state="readonly", width=30)
        self.acc_combo.pack(side="left", padx=10, ipady=3)

        sec(out, "تفاصيل الفيديو", "📤")
        c2 = mk_card(out); c2.pack(fill="x")
        inn2 = tk.Frame(c2, bg=CARD, padx=14, pady=12); inn2.pack(fill="x")

        r2 = tk.Frame(inn2, bg=CARD); r2.pack(fill="x", pady=4)
        tk.Label(r2, text="الخصوصية:", bg=CARD, fg=TXT, width=15, anchor="e").pack(side="left")
        self.priv_var = tk.StringVar(value="public")
        for v, t in [("public", "🌍 عام"), ("private", "🔒 خاص"), ("unlisted", "🔗 غير مدرج")]:
            ttk.Radiobutton(r2, text=t, variable=self.priv_var, value=v).pack(side="left", padx=10)

        r3 = tk.Frame(inn2, bg=CARD); r3.pack(fill="x", pady=6)
        tk.Label(r3, text="الوسوم (Tags):", bg=CARD, fg=TXT, width=15, anchor="e").pack(side="left")
        self.tags_e = PEntry(r3, width=50); self.tags_e.insert(0, "Facebook,Reel,Shorts")
        self.tags_e.pack(side="left", padx=10, ipady=4)

        r4 = tk.Frame(inn2, bg=CARD); r4.pack(fill="x", pady=6)
        tk.Label(r4, text="وصف إضافي:", bg=CARD, fg=TXT, width=15, anchor="e").pack(side="left", anchor="n")
        self.desc_e = tk.Text(inn2, bg=INP, fg=TXT, height=4, width=55, relief="flat",
                              highlightthickness=1, highlightbackground=BORDER)
        self.desc_e.pack(pady=5, padx=(120, 0))

    def _tab_options(self):
        out = tk.Frame(self.t_opts, bg=BG)
        out.pack(fill="both", expand=True, padx=20, pady=10)
        sec(out, "إعدادات التشغيل الذكي", "⚙️")
        c = mk_card(out); c.pack(fill="x", pady=(0, 10))
        inn = tk.Frame(c, bg=CARD, padx=14, pady=12); inn.pack(fill="x")

        self.headless_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(inn, text="تشغيل المتصفح في الخلفية (Headless)",
                        variable=self.headless_var).pack(anchor="w", pady=4)

        self.del_after_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(inn, text="حذف الفيديو من الجهاز بعد الرفع",
                        variable=self.del_after_var).pack(anchor="w", pady=4)

        self.remove_tags_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(inn, text="تنظيف العنوان من الهاشتاجات الأصلية",
                        variable=self.remove_tags_var).pack(anchor="w", pady=4)

        self.newest_first_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(inn, text="البدء بالأحدث أولاً (ترتيب حسب معرّف الفيديو)",
                        variable=self.newest_first_var).pack(anchor="w", pady=4)

        r1 = tk.Frame(inn, bg=CARD); r1.pack(fill="x", pady=10)
        tk.Label(r1, text="تأخير بين الفيديوهات (ثواني):", bg=CARD, fg=TXT).pack(side="left")
        self.delay_e = PEntry(r1, width=8); self.delay_e.insert(0, "10")
        self.delay_e.pack(side="left", padx=10)

        sec(out, "الخصوصية وجودة المقطع", "🛡️")
        cp = mk_card(out); cp.pack(fill="x", pady=(0, 10))
        inp = tk.Frame(cp, bg=CARD, padx=14, pady=12); inp.pack(fill="x")

        self.anonymize_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(inp, text="إخفاء هوية المصدر (حذف اسم الصفحة من العنوان والوصف والبيانات الوصفية)",
                        variable=self.anonymize_var).pack(anchor="w", pady=4)

        self.optimize_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(inp, text="فحص المقطع وتحسينه قبل الرفع (ffprobe + ffmpeg faststart + إزالة البيانات الوصفية)",
                        variable=self.optimize_var).pack(anchor="w", pady=4)

        sec(out, "ملف كوكيز فيسبوك (اختياري)", "🍪")
        ck = mk_card(out); ck.pack(fill="x", pady=(0, 10))
        ink = tk.Frame(ck, bg=CARD, padx=14, pady=12); ink.pack(fill="x")
        rk = tk.Frame(ink, bg=CARD); rk.pack(fill="x")
        self.cookies_e = PEntry(rk, ph="cookies.txt (Netscape أو JSON — لرؤية كل الريلز)", width=52)
        self.cookies_e.pack(side="left", ipady=4)
        Btn(rk, "📂 اختيار", self._pick_cookies, "#455a64").pack(side="left", padx=8)

        sec(out, "الجدولة العشوائية", "🕐")
        c2 = mk_card(out); c2.pack(fill="x")
        inn2 = tk.Frame(c2, bg=CARD, padx=14, pady=12); inn2.pack(fill="x")
        self.sched_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(inn2, text="تفعيل جدولة الرفع (لتجنب الحظر)",
                        variable=self.sched_var).pack(anchor="w")

        r2 = tk.Frame(inn2, bg=CARD); r2.pack(fill="x", pady=8)
        tk.Label(r2, text="تأخير (دقيقة) من:", bg=CARD, fg=TXT).pack(side="left")
        self.min_d = PEntry(r2, width=6); self.min_d.insert(0, "20")
        self.min_d.pack(side="left", padx=5)
        tk.Label(r2, text="إلى:", bg=CARD, fg=TXT).pack(side="left")
        self.max_d = PEntry(r2, width=6); self.max_d.insert(0, "60")
        self.max_d.pack(side="left", padx=5)
        tk.Label(inn2, text=f"ملاحظة: أصغر وقت نشر مجدول يقبله YouTube هو {MIN_SCHEDULE_LEAD_MINUTES} دقيقة من الآن.",
                 bg=CARD, fg=TXT2, font=FSM).pack(anchor="w")

    def _pick_cookies(self):
        path = filedialog.askopenfilename(filetypes=[("Cookies", "*.txt;*.json"), ("All", "*.*")])
        if path:
            self.cookies_e.set_val(path)

    def _tab_log(self):
        out = tk.Frame(self.t_log, bg=BG)
        out.pack(fill="both", expand=True, padx=14, pady=10)
        lf = tk.Frame(out, bg=INP, highlightbackground=BORDER, highlightthickness=1)
        lf.pack(fill="both", expand=True)
        self.log_txt = tk.Text(lf, bg=INP, fg=TXT, font=FM, wrap="word", state="disabled",
                               relief="flat", padx=10, pady=8)
        vsb = ttk.Scrollbar(lf, orient="vertical", command=self.log_txt.yview)
        self.log_txt.configure(yscrollcommand=vsb.set)
        self.log_txt.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.log_txt.tag_config("ok", foreground=SUCCESS)
        self.log_txt.tag_config("err", foreground=ERR)
        self.log_txt.tag_config("warn", foreground=WARN)
        self.log_txt.tag_config("info", foreground=INFO)
        self.log_txt.tag_config("ts", foreground="#555570")

    def _log(self, msg, replace_last=False):
        self._logq.put((msg, replace_last))

    def _poll(self):
        """ينفذ كل تحديثات Tkinter من الخيط الرئيسي فقط."""
        while not self._logq.empty():
            msg, repl = self._logq.get_nowait()
            self.log_txt.configure(state="normal")
            ts = datetime.now().strftime("%H:%M:%S")
            if repl and self._last_log_replace:
                self.log_txt.delete("end-2l", "end-1c")
            tag = "ok" if "✅" in msg else "err" if "❌" in msg else "warn" if "⚠️" in msg else "info"
            self.log_txt.insert("end", f"[{ts}] ", "ts")
            self.log_txt.insert("end", f"{msg}\n", tag)
            self.log_txt.see("end")
            self.log_txt.configure(state="disabled")
            self._last_log_replace = repl

        while not self._uiq.empty():
            event, payload = self._uiq.get_nowait()
            if event == "progress":
                self._last_progress = payload
                self.prog_var.set(payload)
                self.pct_lbl.config(text=f"{payload}%")
            elif event == "finished":
                self._finish_worker(payload)
        self.root.after(100, self._poll)

    def _tab_accounts(self):
        out = tk.Frame(self.t_accs, bg=BG)
        out.pack(fill="both", expand=True, padx=20, pady=10)
        sec(out, "إدارة الحسابات", "👤")
        c = mk_card(out); c.pack(fill="x", pady=(0, 10))
        inn = tk.Frame(c, bg=CARD, padx=14, pady=12); inn.pack(fill="x")
        self.acc_lb = tk.Listbox(inn, bg=INP, fg=TXT, font=FB, relief="flat", height=8)
        self.acc_lb.pack(fill="x", pady=8)
        for a in self.accounts:
            self.acc_lb.insert("end", a)

        r = tk.Frame(inn, bg=CARD); r.pack(fill="x")
        self.new_acc_e = PEntry(r, ph="اسم الحساب", width=30)
        self.new_acc_e.pack(side="left", ipady=4)
        Btn(r, "➕ إضافة", self._acc_add, ACCENT).pack(side="left", padx=10)
        Btn(r, "🗑 حذف", self._acc_del, ERR).pack(side="left")

    def _acc_add(self):
        n = self.new_acc_e.val().strip()
        if n and n not in self.accounts:
            self.accounts.append(n); save_accounts(self.accounts)
            self.acc_lb.insert("end", n); self.acc_combo["values"] = self.accounts
        self.new_acc_e.delete(0, "end"); self.new_acc_e._put()

    def _acc_del(self):
        s = self.acc_lb.curselection()
        if s:
            n = self.acc_lb.get(s[0]); self.accounts.remove(n); save_accounts(self.accounts)
            self.acc_lb.delete(s[0]); self.acc_combo["values"] = self.accounts

    def _tab_dashboard(self):
        out = tk.Frame(self.t_dash, bg=BG)
        out.pack(fill="both", expand=True, padx=20, pady=10)
        sec(out, "إحصائيات الأداء", "📊")
        self.stats_card = StatsCard(out)
        Btn(out, "🔄 تحديث البيانات", lambda: self.stats_card.refresh(), color="#455a64").pack(pady=10)

    def _build_footer(self):
        tk.Frame(self.root, bg=BORDER, height=1).pack(fill="x")
        foot = tk.Frame(self.root, bg=PANEL, pady=15)
        foot.pack(fill="x", side="bottom")
        inn = tk.Frame(foot, bg=PANEL); inn.pack(padx=25, fill="x")

        pg_row = tk.Frame(inn, bg=PANEL); pg_row.pack(fill="x", pady=(0, 8))
        self.prog_var = tk.DoubleVar()
        ttk.Progressbar(pg_row, variable=self.prog_var, orient="horizontal",
                        mode="determinate").pack(side="left", fill="x", expand=True, padx=(0, 15))
        self.pct_lbl = tk.Label(pg_row, text="0%", bg=PANEL, fg=ACCENT, font=FS, width=5)
        self.pct_lbl.pack(side="right")

        ctrl = tk.Frame(inn, bg=PANEL); ctrl.pack(fill="x")
        self.status_lbl = tk.Label(ctrl, text="⏸ جاهز للبدء", bg=PANEL, fg=TXT2, font=FB)
        self.status_lbl.pack(side="left")

        self.start_btn = Btn(ctrl, "▶ ابدأ العمل", self._do_start, SUCCESS)
        self.start_btn.pack(side="right")
        self.stop_btn = Btn(ctrl, "⏹ إيقاف", self._do_stop, ERR)
        self.stop_btn.pack(side="right", padx=10)
        self.stop_btn.config(state="disabled")

    # ── استمرارية الإعدادات ──────────────────────────────
    def _restore_settings(self):
        s = self.saved
        if not s:
            return
        try:
            if s.get("account"):
                self.acc_var.set(s["account"])
            self.headless_var.set(bool(s.get("headless", True)))
            self.del_after_var.set(bool(s.get("delete_after_upload", True)))
            self.remove_tags_var.set(bool(s.get("remove_tags", False)))
            self.newest_first_var.set(bool(s.get("newest_first", True)))
            self.anonymize_var.set(bool(s.get("anonymize_source", True)))
            self.optimize_var.set(bool(s.get("optimize_video", True)))
            self.sched_var.set(bool(s.get("schedule_enabled", False)))
            self.priv_var.set(s.get("privacy", "public"))
            if s.get("tags"):
                self.tags_e.set_val(s["tags"])
            if s.get("between_videos") is not None:
                self.delay_e.set_val(s["between_videos"])
            if s.get("min_delay") is not None:
                self.min_d.set_val(s["min_delay"])
            if s.get("max_delay") is not None:
                self.max_d.set_val(s["max_delay"])
            if s.get("cookies_file"):
                self.cookies_e.set_val(s["cookies_file"])
            if s.get("extra_description"):
                self.desc_e.insert("1.0", s["extra_description"])
        except Exception:
            logger.exception("تعذر استعادة إعدادات الواجهة")

    def _do_stop(self):
        self._stop.set()
        self.status_lbl.config(text="⏹ جاري الإيقاف...", fg=ERR)

    def _capture_worker_settings(self):
        """يقرأ عناصر Tkinter مرة واحدة قبل بدء الخيط الخلفي."""
        try:
            min_delay = int(self.min_d.get() or 20)
            max_delay = int(self.max_d.get() or 60)
            between_videos = int(self.delay_e.get() or 10)
        except ValueError as error:
            raise ValueError("أدخل أرقاماً صحيحة للتأخيرات والجدولة.") from error
        if min_delay < 1 or max_delay < min_delay or between_videos < 0:
            raise ValueError("تحقق من نطاق الجدولة والتأخير بين الفيديوهات.")
        return {
            "account": self.acc_var.get().strip(),
            "headless": self.headless_var.get(),
            "remove_tags": self.remove_tags_var.get(),
            "newest_first": self.newest_first_var.get(),
            "anonymize_source": self.anonymize_var.get(),
            "optimize_video": self.optimize_var.get(),
            "extra_description": self.desc_e.get("1.0", "end").strip(),
            "schedule_enabled": self.sched_var.get(),
            "min_delay": min_delay,
            "max_delay": max_delay,
            "privacy": self.priv_var.get(),
            "tags": self.tags_e.get().split(","),
            "delete_after_upload": self.del_after_var.get(),
            "between_videos": between_videos,
            "cookies_file": self.cookies_e.val().strip() or COOKIES_FILE,
        }

    def _do_start(self):
        pages = self._get_pages()
        if not pages:
            messagebox.showerror("خطأ", "أضف صفحة واحدة على الأقل")
            return
        try:
            settings = self._capture_worker_settings()
        except ValueError as error:
            messagebox.showerror("خطأ في الإعدادات", str(error))
            return
        if not settings["account"]:
            messagebox.showerror("خطأ", "اختر حساب YouTube نشطاً أولاً")
            return
        save_ui_settings(settings)
        self._last_progress = 0.0
        self.prog_var.set(0)
        self.pct_lbl.config(text="0%")
        self._stop.clear()
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status_lbl.config(text="🚀 جاري التنفيذ...", fg=SUCCESS)
        self.nb.select(self.t_log)
        self._thread = threading.Thread(
            target=self._worker,
            args=(pages, settings),
            daemon=True,
            name="facebook-youtube-worker",
        )
        self._thread.start()

    def _worker(self, pages, settings):
        """لا يلمس عناصر Tkinter؛ كل الرسائل تمر عبر الطوابير الآمنة."""
        up_count, fail_count, skipped = 0, 0, 0
        downloaded = 0
        fatal_error = None
        stopped = False
        try:
            uploaded_ids = load_uploaded_ids()
            yt, error = get_youtube(settings["account"])
            if not yt:
                fatal_error = error or "تعذر ربط حساب YouTube."
                self._log(f"❌ خطأ: {fatal_error}")
                return

            self._log("✅ متصل بـ YouTube API")
            all_reels = []
            for page in pages:
                if self._stop.is_set():
                    stopped = True
                    break
                try:
                    page_url = normalize_channel_url(page["url"])
                except ValueError as error:
                    self._log(f"❌ {error}")
                    fail_count += 1
                    continue
                self._log(f"🔍 فحص: {page_url}")
                driver = init_driver(settings["headless"], self._log)
                if not driver:
                    fail_count += 1
                    continue
                try:
                    cookies_file = settings.get("cookies_file")
                    if cookies_file:
                        load_cookies_into_driver(driver, cookies_file, self._log)
                    links = get_reels(driver, page_url, page["scroll"], page["limit"],
                                      self._log, self._stop)
                except StopRequested:
                    stopped = True
                    break
                finally:
                    driver.quit()
                links = order_reels(links, settings.get("newest_first", True))
                for link in links:
                    source_id = extract_reel_id(link) or link
                    if source_id not in uploaded_ids:
                        all_reels.append({"url": link, "id": source_id, "page": page["name"]})
                    else:
                        skipped += 1
                # لا نكرر نفس الفيديو إن ظهر في أكثر من صفحة
                seen = set()
                all_reels = [r for r in all_reels if not (r["id"] in seen or seen.add(r["id"]))]

            if not all_reels:
                self._log(f"⚠️ لا توجد فيديوهات جديدة. (تم تخطي {skipped} مرفوع سابقاً)")
                return

            total = len(all_reels)
            self._log(f"🎯 تم العثور على {total} فيديو جديد (تخطي {skipped} مكرر)")
            schedule_time = clamp_publish_at(datetime.now(timezone.utc) + timedelta(minutes=10))
            for index, reel in enumerate(all_reels, start=1):
                if self._stop.is_set():
                    stopped = True
                    self._log("⚠️ أوقف المستخدم العملية بعد العنصر الجاري.")
                    break

                self._uiq.put(("progress", int(((index - 1) / total) * 100)))
                self._log(f"⬇️ [{index}/{total}] جاري التحميل...")
                try:
                    video_data = download_video(
                        reel["url"], reel["page"], self._log, MAX_RETRIES,
                        settings["remove_tags"], self._stop, settings.get("cookies_file"),
                        settings.get("anonymize_source", True),
                    )
                except StopRequested:
                    stopped = True
                    self._log("⏹ أُوقف التنزيل الجاري.")
                    break
                if not video_data:
                    fail_count += 1
                    continue
                downloaded += 1

                # فحص المقطع وتغليفه قبل الرفع (حماية من المقاطع المعطوبة)
                if settings.get("optimize_video", True):
                    ok, _probe = probe_video(video_data["file"], self._log)
                    if not ok:
                        self._log(f"⚠️ المقطع غير صالح ({', '.join(_probe.get('problems', []))})؛ سيتخطى.")
                        _cleanup_artifacts(video_data["file"])
                        fail_count += 1
                        continue
                    processed = finalize_video(video_data["file"], self._log)
                    if processed and processed != video_data["file"] and os.path.isfile(processed):
                        old_file = video_data["file"]
                        video_data["file"] = processed
                        try:
                            os.remove(old_file)
                        except OSError:
                            pass
                        video_data["size_mb"] = round(os.path.getsize(processed) / (1024 ** 2), 1)

                if settings["extra_description"]:
                    video_data["desc"] += f"\n\n{settings['extra_description']}"

                publish_at = None
                if settings["schedule_enabled"]:
                    schedule_time += timedelta(
                        minutes=random.randint(settings["min_delay"], settings["max_delay"])
                    )
                    publish_at = clamp_publish_at(schedule_time)
                    self._log(f"🕐 جدولة الرفع: {publish_at.strftime('%H:%M')} UTC")

                self._log(f"⬆️ رفع: {video_data['title'][:40]}...")
                result = upload_video(
                    yt, video_data, settings["privacy"], None, settings["tags"],
                    "22", publish_at, self._log,
                )
                if result == "QUOTA_ERROR":
                    fatal_error = "توقفت العملية لأن YouTube رفض الرفع أو نفدت الحصة."
                    break
                if result:
                    save_uploaded_id(reel["id"])
                    uploaded_ids.add(reel["id"])
                    up_count += 1
                    if settings["delete_after_upload"]:
                        try:
                            os.remove(video_data["file"])
                        except OSError as error:
                            logger.warning("تعذر حذف الملف المحلي: %s", error)
                            self._log(f"⚠️ رُفع الفيديو لكن تعذر حذف الملف المحلي: {error}")
                else:
                    fail_count += 1

                self._uiq.put(("progress", int((index / total) * 100)))
                if index < total and not self._stop.is_set():
                    time.sleep(settings["between_videos"])
        except StopRequested:
            stopped = True
            self._log("⏹ تم إيقاف العملية بناءً على طلب المستخدم.")
        except Exception as error:
            logger.exception("توقف عامل الرفع بصورة غير متوقعة")
            fatal_error = f"خطأ غير متوقع: {error}"
            self._log(f"❌ {fatal_error}")
        finally:
            self.stats["total_uploaded"] = self.stats.get("total_uploaded", 0) + up_count
            self.stats["total_failed"] = self.stats.get("total_failed", 0) + fail_count
            self.stats["total_downloaded"] = self.stats.get("total_downloaded", 0) + downloaded
            sessions = self.stats.setdefault("sessions", [])
            sessions.append({
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "uploaded": up_count,
                "failed": fail_count,
                "downloaded": downloaded,
                "skipped": skipped,
                "pages": len(pages),
                "stopped": stopped,
            })
            del sessions[:-50]
            try:
                save_stats(self.stats)
            except OSError:
                logger.exception("تعذر حفظ الإحصاءات")
            self._log(f"🏁 انتهى! مرفوع: {up_count}, فاشل: {fail_count}, محمّل: {downloaded}")
            self._uiq.put(("finished", {
                "uploaded": up_count, "failed": fail_count,
                "fatal": fatal_error, "stopped": stopped,
            }))

    def _finish_worker(self, result):
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        if not result.get("fatal"):
            target = 100 if not result.get("stopped") else int(self._last_progress)
            self.prog_var.set(target)
            self.pct_lbl.config(text=f"{target}%")
        self.stats_card.refresh()
        if result.get("fatal"):
            self.status_lbl.config(text="❌ توقفت العملية؛ راجع السجل", fg=ERR)
            messagebox.showerror("تعذرت العملية", result["fatal"] + "\n\nراجع ملف errors.log للتفاصيل.")
        elif result.get("stopped"):
            self.status_lbl.config(text="⏹ أُوقفت العملية", fg=WARN)
        elif result.get("failed"):
            self.status_lbl.config(text="⚠️ اكتملت مع أخطاء؛ راجع السجل", fg=WARN)
        else:
            self.status_lbl.config(text="✅ اكتملت العملية", fg=SUCCESS)


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
