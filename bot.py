"""
بوت تحميل فيديوهات متطور — أزرار ملونة + دعم واسع للمواقع
المحركات: yt-dlp (1700+ موقع) ← تقليد متصفح للمواقع المحمية ← gallery-dl (صور/ألبومات) ← كاشف فيديو في أي صفحة
"""
import os, re, time, uuid, shutil, asyncio, tempfile, sqlite3, threading, logging
import socket, ipaddress, hashlib, subprocess, urllib.request, urllib.error, copy, inspect
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import urlparse, urljoin

import yt_dlp
from yt_dlp.extractor import list_extractor_classes
from pyrogram import Client, filters, idle
from pyrogram.errors import MessageNotModified, FloodWait
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto

try:
    from pyrogram.enums import ButtonStyle as S
    COLORS = True
except ImportError:
    COLORS = False
    class S: PRIMARY = SUCCESS = DANGER = None

try:
    import gallery_dl.extractor as gdx
    HAS_GALLERY = shutil.which("gallery-dl") is not None
except ImportError:
    gdx, HAS_GALLERY = None, False

IMPERSONATE = None   # تقليد متصفح Chrome لتخطي حماية Cloudflare وأخواتها
try:
    import curl_cffi  # noqa: F401
    from yt_dlp.networking.impersonate import ImpersonateTarget
    IMPERSONATE = ImpersonateTarget.from_str("chrome")
except Exception:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vidbot")

try:    # event loop أسرع (لازم يتفعّل قبل إنشاء الـ Client)
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    HAS_UVLOOP = True
except Exception:
    HAS_UVLOOP = False

# ───────────── الإعدادات ─────────────
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
PROXY = os.getenv("PROXY")
MAX_DL = int(os.getenv("MAX_PARALLEL", "4"))
MAX_UP = int(os.getenv("MAX_UPLOADS", "3"))
PER_BATCH = int(os.getenv("PER_BATCH", "3"))
MAX_PLAYLIST = int(os.getenv("MAX_PLAYLIST", "30"))
MAX_GALLERY = int(os.getenv("MAX_GALLERY", "30"))
MAX_ACTIVE_BATCHES = int(os.getenv("MAX_ACTIVE_BATCHES", "2"))
CONN = int(os.getenv("CONNECTIONS", "16"))
FRAGMENTS = int(os.getenv("FRAGMENTS", "16"))             # أجزاء HLS/DASH بالتوازي
UPLOAD_WORKERS = int(os.getenv("UPLOAD_WORKERS", "12"))   # طلبات رفع متوازية للملف الواحد (الافتراضي في المكتبة 4)
MAX_LINKS, MAX_ITEMS = 10, 50
MAX_PROBES = int(os.getenv("MAX_PROBES", "4"))            # فحص روابط متزامن (كان من غير حد)
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", "30"))         # عناصر/يوم لكل مستخدم عادي (0 = بلا حد). الأدمن مستثنى
RATE_SECONDS = float(os.getenv("RATE_SECONDS", "3"))      # أقل فاصل بين رسالتين روابط من نفس المستخدم
HISTORY_DAYS = int(os.getenv("HISTORY_DAYS", "90"))
MAX_CACHE_ROWS = int(os.getenv("MAX_CACHE_ROWS", "50000"))
# قائمة المسموح لهم: فاضية = البوت مفتوح للكل (لكن الكوتة اليومية بتفضل شغالة)
MAX_DURATION = int(os.getenv("MAX_DURATION_MIN", "180")) * 60   # أقصى مدة فيديو بالثواني (0 = بلا حد)
MIN_FREE_MB = int(os.getenv("MIN_FREE_MB", "3000"))           # أقل مساحة فاضية على القرص قبل بدء تحميل جديد
ALLOWED_IDS = {int(x) for x in os.getenv("ALLOWED_USERS", "").split(",") if x.strip()}
MAX_SIZE = 2000 * 1024 * 1024
DB_PATH = os.getenv("DB_PATH") or ("/data/bot.db" if os.path.isdir("/data") else "bot.db")
# مواقع محجوبة (محتوى مخالف لقواعد تليجرام). القايمة الأساسية ثابتة في الكود،
# ومتغير BLOCKED_DOMAINS (فاصلة) بيضيف عليها بس — مينفعش يتلغي الحجب بمتغير.
_DEFAULT_BLOCK = ("pornhub.com,xvideos.com,xnxx.com,xhamster.com,redtube.com,youporn.com,"
                  "spankbang.com,tube8.com,eporner.com,motherless.com")
BLOCKED = tuple(dict.fromkeys(
    x.strip().lower() for x in (_DEFAULT_BLOCK + "," + os.getenv("BLOCKED_DOMAINS", "")).split(",") if x.strip()))
VALID_PREFS = {"ask", "best", "1080", "720", "480", "360", "audio"}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")

COOKIES_FILE = None
if os.getenv("COOKIES_TXT"):
    COOKIES_FILE = os.path.join(tempfile.gettempdir(), "cookies.txt")
    with os.fdopen(os.open(COOKIES_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:   # الكوكيز = جلسة حساب
        f.write(os.environ["COOKIES_TXT"])
HAS_ARIA2 = shutil.which("aria2c") is not None
SITE_COUNT = sum(1 for _ in list_extractor_classes())

def tune_upload(workers):
    """يرفع عدد طلبات الرفع المتوازية للملف الكبير (المكتبة ثابتة على 4) — أكبر تأثير على سرعة الرفع."""
    try:
        import pyrogram.methods.advanced.save_file as sf
        src, old = inspect.getsource(sf), "workers_count = 4 if is_big else 1"
        if old not in src:
            log.warning("upload tuning skipped: pattern not found (library version changed)")
            return False
        ns = {"__name__": sf.__name__ + "_tuned", "__file__": sf.__file__}
        exec(compile(src.replace(old, f"workers_count = {int(workers)} if is_big else 1"), sf.__file__, "exec"), ns)
        Client.save_file = ns["SaveFile"].save_file
        return True
    except Exception as e:
        log.warning("upload tuning failed: %s", e)
        return False

UPLOAD_TUNED = tune_upload(UPLOAD_WORKERS) if UPLOAD_WORKERS > 4 else False

# max_concurrent_transmissions: بدونها المكتبة بترفع ملف واحد بس في نفس الوقت (الافتراضي 1)
app = Client("vidbot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN,
             in_memory=True, workers=32, max_concurrent_transmissions=max(MAX_UP, 1))
DL_SEM = asyncio.Semaphore(MAX_DL)
UP_SEM = asyncio.Semaphore(MAX_UP)
PROBE_SEM = asyncio.Semaphore(MAX_PROBES)
last_req: dict[int, float] = {}
URL_RE = re.compile(r"https?://[^\s<>\"']+")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# ───────────── قاعدة البيانات ─────────────
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db_lock = threading.Lock()
for _pragma in ("PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL"):
    try: db.execute(_pragma)
    except sqlite3.DatabaseError: pass
db.executescript("""
CREATE TABLE IF NOT EXISTS cache(key TEXT, mode TEXT, file_id TEXT, PRIMARY KEY(key, mode));
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, first_seen INTEGER,
                                 downloads INTEGER DEFAULT 0, quality TEXT DEFAULT 'ask');
CREATE TABLE IF NOT EXISTS history(user_id INTEGER, title TEXT, mode TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS banned(id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS usage(user_id INTEGER, day TEXT, n INTEGER DEFAULT 0, PRIMARY KEY(user_id, day));
""")
for _alter in ("ALTER TABLE users ADD COLUMN quality TEXT DEFAULT 'ask'",
               "ALTER TABLE cache ADD COLUMN ts INTEGER DEFAULT 0"):
    try:
        db.execute(_alter); db.commit()
    except sqlite3.OperationalError:
        pass

def db_exec(q, args=()):
    with db_lock:
        cur = db.execute(q, args); db.commit(); return cur.fetchall()

def cache_get(key, mode):
    r = db_exec("SELECT file_id FROM cache WHERE key=? AND mode=?", (key, mode))
    return r[0][0] if r else None

def cache_put(key, mode, fid):
    db_exec("INSERT OR REPLACE INTO cache(key, mode, file_id, ts) VALUES (?,?,?,?)",
            (key, mode, fid, int(time.time())))

def touch_user(uid):
    db_exec("INSERT OR IGNORE INTO users(id, first_seen) VALUES (?,?)", (uid, int(time.time())))

def record(uid, title, mode):
    db_exec("UPDATE users SET downloads = downloads + 1 WHERE id=?", (uid,))
    db_exec("INSERT INTO history VALUES (?,?,?,?)", (uid, title[:80], mode, int(time.time())))

def today():
    return time.strftime("%Y-%m-%d", time.gmtime())

def quota_left(uid):
    if is_admin(uid) or DAILY_LIMIT <= 0: return 10 ** 9
    r = db_exec("SELECT n FROM usage WHERE user_id=? AND day=?", (uid, today()))
    return max(DAILY_LIMIT - (r[0][0] if r else 0), 0)

def quota_add(uid, n):
    """بيزوّد (أو بيرجّع لو n سالب) استهلاك اليوم."""
    if is_admin(uid) or DAILY_LIMIT <= 0 or n == 0: return
    db_exec("INSERT INTO usage(user_id, day, n) VALUES (?,?,MAX(?,0)) "
            "ON CONFLICT(user_id, day) DO UPDATE SET n = MAX(n + ?, 0)", (uid, today(), n, n))

def cleanup_db():
    now = int(time.time())
    db_exec("DELETE FROM history WHERE ts < ?", (now - HISTORY_DAYS * 86400,))
    db_exec("DELETE FROM usage WHERE day < ?", (time.strftime("%Y-%m-%d", time.gmtime(now - 7 * 86400)),))
    db_exec("DELETE FROM cache WHERE rowid IN (SELECT rowid FROM cache ORDER BY ts DESC LIMIT -1 OFFSET ?)",
            (MAX_CACHE_ROWS,))

def clean_stale_tmp(max_age=3600):
    """بقايا تحميلات اتقطعت (Crash / إعادة تشغيل) — بتتمسح عشان القرص ما يمتلاش."""
    tmp, now = tempfile.gettempdir(), time.time()
    for n in os.listdir(tmp):
        if not n.startswith(("dl_", "speed_")): continue
        path = os.path.join(tmp, n)
        try:
            if now - os.path.getmtime(path) > max_age:
                shutil.rmtree(path, ignore_errors=True) if os.path.isdir(path) else os.remove(path)
        except OSError:
            pass

async def cleanup_loop():
    while True:
        cut = time.time() - 3600
        for k in [k for k, v in last_req.items() if v < cut]: last_req.pop(k, None)
        try:
            await asyncio.to_thread(cleanup_db)
            await asyncio.to_thread(clean_stale_tmp)
        except Exception as e: log.warning("cleanup failed: %s", e)
        await asyncio.sleep(6 * 3600)

def get_pref(uid):
    r = db_exec("SELECT quality FROM users WHERE id=?", (uid,))
    return (r[0][0] if r and r[0][0] else "ask")

def set_pref(uid, q):
    if q not in VALID_PREFS: return
    db_exec("UPDATE users SET quality=? WHERE id=?", (q, uid))

# ───────────── الموديلات ─────────────
@dataclass
class Item:
    url: str
    title: str
    key: str
    engine: str = "ytdlp"     # ytdlp | gallery
    referer: str = ""
    status: str = "wait"      # wait | dl | up | done | err
    detail: str = ""
    info: object = field(default=None, repr=False)   # نتيجة الفحص (بتتستخدم تاني عشان نوفر استخراج تاني)

@dataclass
class Batch:
    id: str
    user_id: int
    chat_id: int
    items: list
    created: float = field(default_factory=time.time)
    mode: str = "720"
    cancelled: bool = False
    done: bool = False
    started: bool = False
    started_at: float = 0.0
    msg: object = None
    sem: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(PER_BATCH))

batches: dict[str, Batch] = {}
awaiting: dict[int, str] = {}
bc_pending: dict[int, tuple] = {}

# رسائل أخطاء مفهومة بالعربي
ERR_MAP = [
    (r"sign in to confirm|not a bot", "يوتيوب طالب تأكيد إنك مش بوت — لازم cookies أو proxy"),
    (r"private video|video is private|this account is private", "الفيديو/الحساب خاص"),
    (r"confirm your age|age[- ]restricted|inappropriate for some users", "محتوى متقيد بالسن — محتاج cookies"),
    (r"login required|log in|logged in|rate-limit reached|requires? authentication|cookies", "الموقع طالب تسجيل دخول — محتاج cookies"),
    (r"\bdrm\b|widevine", "محمي بـ DRM ومينفعش يتحمل"),
    (r"geo[- ]?restrict|not available in your country|blocked .* in your country", "محجوب في بلد السيرفر"),
    (r"live event|is live|premieres in|will begin in", "ده بث مباشر/لسه ما بدأش — حاول لما يخلص"),
    (r"unsupported url|no video|there is no video|no media|مفيش فيديو", "مفيش فيديو قابل للتحميل في الرابط ده"),
    (r"unavailable|removed|deleted|does not exist|not found|404", "الفيديو مش متاح أو اتحذف"),
    (r"429|too many requests", "الموقع حظر السيرفر مؤقتًا — حاول بعد شوية"),
    (r"timed out|timeout", "الموقع اتأخر في الرد — حاول تاني"),
]

def clean_err(e):
    s = ANSI_RE.sub("", str(e)).replace("ERROR:", "").strip()
    for rx, msg in ERR_MAP:
        if re.search(rx, s, re.I): return msg
    return s[:150]

def mode_label(m):
    return {"best": "أعلى جودة", "audio": "MP3", "ask": "اسألني كل مرة"}.get(m, f"{m}p")

def is_admin(uid):
    return uid in ADMIN_IDS

def is_banned(uid):
    return bool(db_exec("SELECT 1 FROM banned WHERE id=?", (uid,)))

def is_allowed(uid):
    if uid in ADMIN_IDS: return True
    if is_banned(uid): return False
    return not ALLOWED_IDS or uid in ALLOWED_IDS

def is_blocked(url):
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in BLOCKED)

def is_safe_url(url):
    """يمنع الروابط الداخلية (localhost / الشبكة الخاصة / metadata) عشان السيرفر ما يتستغلش.
    بيتطبق على: رابط المستخدم، وكل رابط بيتلقط من جوه صفحة، وكل Redirect بنتبعه بنفسنا."""
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname: return False
        for *_, sa in socket.getaddrinfo(p.hostname, None):
            ip = ipaddress.ip_address(sa[0].split("%")[0])
            if getattr(ip, "ipv4_mapped", None): ip = ip.ipv4_mapped
            if not ip.is_global or ip.is_multicast:      # is_global بتغطي private/loopback/link-local/CGNAT/reserved
                return False
        return True
    except Exception:
        return False

def url_ok(url):
    return is_safe_url(url) and not is_blocked(url)

# ───────────── الأزرار الملونة ─────────────
def B(text, data, style=S.PRIMARY):
    if COLORS and style is not None:
        return InlineKeyboardButton(text, callback_data=data, style=style)
    return InlineKeyboardButton(text, callback_data=data)

def KB(*rows):
    return InlineKeyboardMarkup([list(r) for r in rows])

def menu_kb(uid):
    rows = [
        [B("➕ تحميل فيديو", "dl", S.SUCCESS)],
        [B("📊 تحميلاتي", "me"), B("⚙️ الإعدادات", "st")],
        [B("🌐 المواقع المدعومة", "sites"), B("❓ المساعدة", "hp")],
    ]
    if is_admin(uid):
        rows.append([B("🛠 لوحة الأدمن", "ad", S.DANGER)])
    return KB(*rows)

def back_kb():
    return KB([B("🏠 القائمة الرئيسية", "m")])

def picker_kb(bid):
    return KB(
        [B("🏆 أعلى جودة", f"q|{bid}|best", S.SUCCESS)],
        [B("🎬 1080p", f"q|{bid}|1080"), B("🎬 720p", f"q|{bid}|720")],
        [B("🎬 480p", f"q|{bid}|480"), B("🎬 360p", f"q|{bid}|360")],
        [B("🎧 صوت MP3", f"q|{bid}|audio")],
        [B("❌ إلغاء", f"x|{bid}", S.DANGER)],
    )

def cancel_kb(b):
    return KB([B("🛑 إلغاء", f"c|{b.id}", S.DANGER)])

def after_kb():
    return KB([B("➕ تحميل جديد", "dl", S.SUCCESS)], [B("🏠 القائمة الرئيسية", "m")])

def settings_kb(uid):
    cur = get_pref(uid)
    def mk(v, label):
        sel = v == cur
        return B(("✅ " if sel else "") + label, f"st|{v}", S.SUCCESS if sel else S.PRIMARY)
    return KB(
        [mk("ask", "🤔 اسألني كل مرة")],
        [mk("best", "🏆 أعلى جودة")], [mk("1080", "1080p"), mk("720", "720p")],
        [mk("480", "480p"), mk("360", "360p")], [mk("audio", "🎧 MP3")],
        [B("🏠 القائمة الرئيسية", "m")],
    )

def admin_kb():
    return KB(
        [B("📊 إحصائيات", "ad|st"), B("📢 إذاعة", "ad|bc", S.SUCCESS)],
        [B("⚡ اختبار السرعة", "ad|sp", S.SUCCESS)],
        [B("🗑 مسح الكاش", "ad|cc", S.DANGER)],
        [B("🏠 القائمة الرئيسية", "m")],
    )

# ───────────── yt-dlp (مع تقليد المتصفح) ─────────────
RETRY_IMP = re.compile(r"403|forbidden|cloudflare|just a moment|captcha|429|impersonat|"
                       r"unable to download webpage|unsupported url|timed out|challenge", re.I)

def base_opts(referer=None):
    o = {"quiet": True, "no_warnings": True, "retries": 10, "fragment_retries": 10,
         "socket_timeout": 30, "concurrent_fragment_downloads": FRAGMENTS, "noprogress": True}
    if COOKIES_FILE: o["cookiefile"] = COOKIES_FILE
    if PROXY: o["proxy"] = PROXY
    if referer: o["http_headers"] = {"Referer": referer, "User-Agent": UA}
    return o

def ydl_run(url, opts, download):
    """يجرب عادي، ولو اتحجب يعيد بتقليد متصفح Chrome."""
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            return y.extract_info(url, download=download)
    except Exception as e:
        if IMPERSONATE and RETRY_IMP.search(str(e)):
            o2 = dict(opts); o2["impersonate"] = IMPERSONATE
            o2.pop("external_downloader", None); o2.pop("external_downloader_args", None)
            try:
                with yt_dlp.YoutubeDL(o2) as y:
                    return y.extract_info(url, download=download)
            except Exception:
                pass
        raise

YT_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")

def is_single_video(url):
    """فيديو يوتيوب مفرد (مش Playlist). المواقع التانية بيقررها yt-dlp نفسه."""
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if not any(host == h or host.endswith("." + h) for h in YT_HOSTS): return False
    if host.endswith("youtu.be") or p.path.startswith(("/shorts/", "/live/", "/embed/")): return True
    return p.path == "/watch" and "v=" in p.query

def probe_ytdlp(url, referer=None):
    single = is_single_video(url)
    o = base_opts(referer)
    o.update(extract_flat="in_playlist", noplaylist=single, playlistend=MAX_PLAYLIST)
    info = ydl_run(url, o, False)
    items, ptitle = [], None
    if info.get("_type") == "playlist":
        ptitle = info.get("title")
        for e in info.get("entries") or []:
            if not e: continue
            u, eid = e.get("url") or e.get("webpage_url"), e.get("id")
            ie = (e.get("ie_key") or info.get("extractor_key") or "x").lower()
            if not u or not u.startswith("http"):
                if ie == "youtube" and eid: u = f"https://www.youtube.com/watch?v={eid}"
                else: continue
            if not url_ok(u): continue
            items.append(Item(u, e.get("title") or u, f"{ie}:{eid or u}", referer=referer or ""))
            if len(items) >= MAX_PLAYLIST: break
    else:
        ie = (info.get("extractor_key") or "x").lower()
        items.append(Item(url, info.get("title") or url, f"{ie}:{info.get('id') or url}",
                          referer=referer or "", info=info if info.get("formats") else None))
    return items, ptitle

# ───────────── كاشف الفيديو في أي صفحة ─────────────
OG_RE = re.compile(r"""<meta[^>]+(?:property|name)=["']og:video(?::secure_url|:url)?["'][^>]+content=["']([^"']+)""", re.I)
SRC_RE = re.compile(r"""<(?:video|source)[^>]+src=["']([^"']+)""", re.I)
MEDIA_RE = re.compile(r"""https?://[^\s"'<>\\]+?\.(?:m3u8|mpd|mp4|webm|mov|mkv)(?:\?[^\s"'<>\\]*)?""", re.I)
IFRAME_RE = re.compile(r"""<iframe[^>]+src=["']([^"']+)""", re.I)
TITLE_RE = re.compile(r"""<meta[^>]+property=["']og:title["'][^>]+content=["']([^"']+)|<title[^>]*>([^<]+)""", re.I)

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw): return None

_REDIRECT_CODES = (301, 302, 303, 307, 308)

def _get_once(url):
    """طلب واحد من غير ما يتبع Redirect تلقائيًا. بيرجّع (نص, لينك_التحويل_أو_None)."""
    try:
        if IMPERSONATE:
            from curl_cffi import requests as cr
            r = cr.get(url, impersonate="chrome", timeout=20, allow_redirects=False,
                       proxies={"http": PROXY, "https": PROXY} if PROXY else None)
            if r.status_code in _REDIRECT_CODES and r.headers.get("location"):
                return "", r.headers["location"]
            return r.text[:3_000_000], None
    except Exception as e:
        log.debug("curl_cffi fetch failed: %s", e)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=20) as r:
            return r.read(3_000_000).decode("utf-8", "ignore"), None
    except urllib.error.HTTPError as e:
        if e.code in _REDIRECT_CODES and e.headers.get("Location"):
            return "", e.headers["Location"]
        raise

def fetch_html(url):
    """بيجيب الصفحة وبيفحص كل Redirect (لحد 5) قبل ما يتبعه — عشان تحويلة لعنوان داخلي ما تعدّيش."""
    cur = url
    for _ in range(6):
        if not url_ok(cur): raise RuntimeError("unsafe or blocked url")
        text, loc = _get_once(cur)
        if loc is None: return text
        cur = urljoin(cur, loc)
    raise RuntimeError("too many redirects")

def sniff(url):
    txt = fetch_html(url).replace("\\/", "/").replace("&amp;", "&")
    cands = OG_RE.findall(txt) + SRC_RE.findall(txt) + MEDIA_RE.findall(txt) + IFRAME_RE.findall(txt)
    seen, urls = set(), []
    for c in cands:
        c = urljoin(url, c.strip())
        if c.startswith("http") and c not in seen and c != url and url_ok(c):   # SSRF: كل مرشح لازم يعدّي الفحص
            seen.add(c); urls.append(c)
    m = TITLE_RE.search(txt)
    title = (m.group(1) or m.group(2)).strip() if m else ""
    for c in urls[:8]:
        try:
            items, _ = probe_ytdlp(c, referer=url)
        except Exception:
            continue
        if items:
            it = items[0]
            it.title = title[:100] or it.title
            it.referer = url
            it.key = "sniff:" + hashlib.sha1(c.encode()).hexdigest()[:16]
            return [it]
    raise RuntimeError("no video found in page")

# ───────────── gallery-dl (صور وألبومات) ─────────────
def gallery_supported(url):
    if not (HAS_GALLERY and gdx): return False
    try: return gdx.find(url) is not None
    except Exception: return False

def do_gallery(url, outdir):
    cmd = ["gallery-dl", "-q", "-D", outdir, "-f", "{num:>03}.{extension}",
           "--range", f"1-{MAX_GALLERY}"]
    if COOKIES_FILE: cmd += ["-C", COOKIES_FILE]
    if PROXY: cmd += ["--proxy", PROXY]
    r = subprocess.run(cmd + [url], capture_output=True, text=True, timeout=600)
    files = sorted(os.path.join(outdir, f) for f in os.listdir(outdir)
                   if os.path.isfile(os.path.join(outdir, f)) and not f.endswith((".json", ".txt", ".part")))
    if not files:
        raise RuntimeError((r.stderr or "no media").strip()[-300:])
    return files

# ───────────── محرك الاكتشاف (بالترتيب) ─────────────
def probe(url):
    """yt-dlp ← gallery-dl ← كاشف الصفحة. لو الكل فشل بيرجّع خطأ yt-dlp الأصلي."""
    try:
        return probe_ytdlp(url)
    except Exception as first:
        if gallery_supported(url):
            host = (urlparse(url).hostname or "").replace("www.", "")
            return [Item(url, f"📷 {host}", "gal:" + hashlib.sha1(url.encode()).hexdigest()[:16],
                         engine="gallery")], None
        try:
            return sniff(url), None
        except Exception as e2:
            log.info("all engines failed for %s | ytdlp: %s | sniff: %s", url, first, e2)
            raise first

def check_limits(info):
    """بيرفض البث المباشر والفيديوهات الأطول من الحد قبل ما نصرف باندويث."""
    if not info: return
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        raise RuntimeError("live event")                      # بيتحوّل لرسالة عربي من ERR_MAP
    dur = info.get("duration")
    if MAX_DURATION and dur and dur > MAX_DURATION:
        raise RuntimeError(f"الفيديو أطول من الحد المسموح ({MAX_DURATION // 60} دقيقة)")

def _match_filter(info, *, incomplete=False):
    if not incomplete: check_limits(info)
    return None

# H.264 أولًا: تليجرام (iOS/Desktop) ساعات مش بيشغّل VP9/AV1 جوه الـ MP4
SZ = "[filesize<?1900M]"

def fmt_for(mode):
    if mode == "audio": return "bestaudio/best"
    if mode == "best":
        return (f"bv*[vcodec^=avc1]{SZ}+ba[ext=m4a]/bv*{SZ}+ba[ext=m4a]/bv*{SZ}+ba/b")
    h = f"[height<={mode}]"
    return (f"bv*{h}[vcodec^=avc1]{SZ}+ba[ext=m4a]/bv*{h}{SZ}+ba/b{h}{SZ}/b{h}/b")

FALLBACK = {"best": ["720", "480", "360"], "1080": ["720", "480", "360"], "720": ["480", "360"],
            "480": ["360"], "360": [], "audio": []}

def do_download_fit(it, mode, outdir):
    """لو الملف عدّى 2GB بينزل جودة تلقائيًا بدل ما يفشل. بيرجّع (مسار, info, الجودة_الفعلية)."""
    for m in [mode] + FALLBACK.get(mode, []):
        path, info = do_download(it, m, outdir)
        if os.path.getsize(path) <= MAX_SIZE:
            return path, info, m
        log.info("file too big in %s for %s, trying lower quality", m, it.url)
        try: os.remove(path)
        except OSError: pass
    raise RuntimeError("الملف أكبر من 2GB حتى بأقل جودة")

def do_download(it, mode, outdir):
    o = base_opts(it.referer or None)
    o.update(noplaylist=True, outtmpl=f"{outdir}/%(title).60s.%(ext)s")
    if HAS_ARIA2:
        o["external_downloader"] = {"default": "aria2c", "dash": "native",
                                    "m3u8": "native", "m3u8_native": "native"}
        o["external_downloader_args"] = {"aria2c": [
            "-x", str(CONN), "-s", str(CONN), "-k", "1M", "--min-split-size=1M",
            "--file-allocation=none", "--disable-ipv6=true", "--connect-timeout=10",
            "--max-tries=5", "--retry-wait=1", "--summary-interval=0",
            "--console-log-level=error"]}
    o["format"] = fmt_for(mode)
    o["match_filter"] = _match_filter
    if mode == "audio":
        o["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                                "preferredquality": "192"}]
    else:
        o["merge_output_format"] = "mp4"
    info = None
    if it.info:                    # نستخدم نتيجة الفحص: بنوفر 2-5 ثواني استخراج
        check_limits(it.info)      # بره الـ try عشان الرفض ما يتحوّلش لإعادة استخراج
        try:
            with yt_dlp.YoutubeDL(o) as y:
                info = y.process_ie_result(copy.deepcopy(it.info), download=True)
        except Exception as e:
            log.info("reuse probe info failed, re-extracting: %s", e)
            for f in os.listdir(outdir):
                try: os.remove(os.path.join(outdir, f))
                except OSError: pass
            info = None
    if info is None:
        info = ydl_run(it.url, o, True)
    files = [os.path.join(outdir, f) for f in os.listdir(outdir)]
    files = [f for f in files if os.path.isfile(f) and not f.endswith((".aria2", ".part", ".ytdl"))]
    if not files:
        raise RuntimeError("التحميل فشل (مفيش ملف ناتج)")
    return max(files, key=os.path.getsize), info

def dir_size(d):
    t = 0
    for f in os.listdir(d):
        try: t += os.path.getsize(os.path.join(d, f))
        except OSError: pass
    return t

async def make_thumb(path, out):
    for ss in ("1", "0"):          # الفيديو القصير (<1 ثانية) مفيهوش فريم عند الثانية 1
        try:
            p = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-ss", ss, "-i", path, "-frames:v", "1", "-vf", "scale=320:-1", out,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await p.wait()
        except Exception:
            return None
        if os.path.exists(out): return out
    return None

async def with_retry(fn, *a, tries=3, **kw):
    """إعادة محاولة للرفع: FloodWait بيستنى المدة المطلوبة، وأي خطأ شبكة بيتعاد بفاصل متزايد."""
    for i in range(tries):
        try:
            return await fn(*a, **kw)
        except FloodWait as e:
            if i == tries - 1: raise
            await asyncio.sleep(min(e.value + 1, 60))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if i == tries - 1: raise
            log.info("send failed (%s), retry %d", e, i + 1)
            await asyncio.sleep(2 * (i + 1))

async def wait_disk(poll=3, tries=40):
    """مانع إننا نبدأ تحميل جديد والقرص شبه ممتلئ (تحميلات متزامنة + دمج فيديو/صوت بياخدوا مساحة)."""
    for _ in range(tries):
        if shutil.disk_usage(tempfile.gettempdir()).free > MIN_FREE_MB * 1024 * 1024: return
        await asyncio.sleep(poll)
    raise RuntimeError("السيرفر ممتلئ مؤقتًا — حاول بعد شوية")

# ───────────── الإرسال ─────────────
async def send_media(b, it, src, info, thumb=None, progress=None):
    info = info or {}
    cap = (info.get("title") or it.title)[:200]
    if b.mode == "audio":
        m = await app.send_audio(b.chat_id, src, caption=cap,
                                 duration=int(info.get("duration") or 0),
                                 title=info.get("title"), performer=info.get("uploader"),
                                 progress=progress)
        return m.audio.file_id if m.audio else None
    m = await app.send_video(b.chat_id, src, caption=cap,
                             duration=int(info.get("duration") or 0),
                             width=info.get("width") or 0, height=info.get("height") or 0,
                             thumb=thumb, supports_streaming=True, progress=progress)
    if m.video: return m.video.file_id
    return m.document.file_id if m.document else None

IMG_EXT, VID_EXT = {".jpg", ".jpeg", ".png", ".webp"}, {".mp4", ".mov", ".mkv", ".webm", ".m4v"}

async def send_gallery(b, it, files):
    cap, ext = it.title[:200], lambda f: os.path.splitext(f)[1].lower()
    imgs = [f for f in files if ext(f) in IMG_EXT and os.path.getsize(f) <= 10 * 1024 * 1024]
    vids = [f for f in files if ext(f) in VID_EXT]
    gifs = [f for f in files if ext(f) == ".gif"]
    docs = [f for f in files if f not in imgs and f not in vids and f not in gifs]
    first = True
    for i in range(0, len(imgs), 10):
        chunk = imgs[i:i + 10]
        try:
            if len(chunk) == 1:
                await app.send_photo(b.chat_id, chunk[0], caption=cap if first else None)
            else:
                await app.send_media_group(b.chat_id, [InputMediaPhoto(p, caption=cap if (first and j == 0) else None)
                                                       for j, p in enumerate(chunk)])
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
        except Exception:
            docs += chunk
        first = False
    for v in vids:
        await app.send_video(b.chat_id, v, caption=cap if first else None, supports_streaming=True); first = False
    for g in gifs:
        await app.send_animation(b.chat_id, g, caption=cap if first else None); first = False
    for d in docs:
        await app.send_document(b.chat_id, d, caption=cap if first else None); first = False

# ───────────── معالجة عنصر واحد ─────────────
async def run_item(b, it):
    async with b.sem:
        if b.cancelled:
            it.status, it.detail = "err", "أُلغي"
            return
        try:
            if it.engine == "ytdlp":
                fid = cache_get(it.key, b.mode)
                if fid:
                    try:
                        it.status, it.detail = "up", "فوري ⚡"
                        await send_media(b, it, fid, None)
                        it.status = "done"; record(b.user_id, it.title, b.mode)
                        return
                    except Exception as e:
                        log.warning("cache miss for %s: %s", it.key, e)

            outdir = tempfile.mkdtemp(prefix="dl_")
            try:
                if it.engine == "gallery":
                    async with DL_SEM:
                        it.status, it.detail = "dl", "بيحمل الصور..."
                        files = await asyncio.to_thread(do_gallery, it.url, outdir)
                    if b.cancelled: raise RuntimeError("أُلغي")
                    async with UP_SEM:
                        it.status, it.detail = "up", f"{len(files)} ملف"
                        await with_retry(send_gallery, b, it, files, tries=2)
                    it.status = "done"; record(b.user_id, it.title, "gallery")
                    return

                async with DL_SEM:
                    it.status, it.detail = "dl", "بيبدأ..."
                    await wait_disk()
                    task = asyncio.create_task(asyncio.to_thread(do_download_fit, it, b.mode, outdir))
                    last, t0 = 0, time.time()
                    while not task.done():
                        await asyncio.sleep(1.5)
                        size, now = dir_size(outdir), time.time()
                        speed = (size - last) / max(now - t0, 0.1)
                        last, t0 = size, now
                        it.detail = f"{size/1e6:.0f}MB • {speed/1e6:.1f}MB/s"
                    path, info, used = await task
                if b.cancelled: raise RuntimeError("أُلغي")
                if used != b.mode: log.info("quality lowered %s -> %s for %s", b.mode, used, it.url)

                async with UP_SEM:
                    it.status, it.detail = "up", "0%"
                    tick = {"t": 0}
                    async def prog(cur, total):
                        if time.time() - tick["t"] > 1.5:
                            tick["t"] = time.time()
                            it.detail = f"{cur/total*100:.0f}%"
                    thumb = None if b.mode == "audio" else await make_thumb(path, f"{outdir}/thumb.jpg")
                    fid = await with_retry(send_media, b, it, path, info, thumb, prog)
                if fid: cache_put(it.key, b.mode, fid)
                it.status = "done"; record(b.user_id, it.title, b.mode)
            finally:
                it.info = None
                shutil.rmtree(outdir, ignore_errors=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            it.status, it.detail = "err", clean_err(e)
            log.warning("item failed %s: %s", it.url, e)

# ───────────── شاشة الحالة ─────────────
def render(b, final=False):
    c = Counter(i.status for i in b.items)
    txt = (f"📦 {len(b.items)} عنصر • {mode_label(b.mode)}\n"
           f"✅ {c['done']}   ⬇️ {c['dl']}   ⬆️ {c['up']}   ⏳ {c['wait']}   ❌ {c['err']}\n")
    active = [i for i in b.items if i.status in ("dl", "up")]
    if active: txt += "\n"
    for i in active[:6]:
        txt += f"{'⬇️' if i.status == 'dl' else '⬆️'} {i.detail} — {i.title[:35]}\n"
    errs = [i for i in b.items if i.status == "err"]
    if errs: txt += "\n"
    for i in errs[-4:]:
        txt += f"❌ {i.title[:28]}: {i.detail[:70]}\n"
    if final: txt += f"\n🏁 خلصنا في {int(time.time() - b.started_at)} ثانية ⚡"
    elif b.cancelled: txt += "\n🛑 جاري الإلغاء بعد العناصر الجارية..."
    return txt

async def safe_edit(msg, text, kb=None):
    try: await msg.edit_text(text, reply_markup=kb)
    except MessageNotModified: pass
    except FloodWait as e: await asyncio.sleep(min(e.value, 15))
    except Exception as e: log.debug("edit failed: %s", e)

async def run_batch(b):
    b.started = True
    b.started_at = time.time()
    tasks = [asyncio.create_task(run_item(b, it)) for it in b.items]
    async def loop():
        while not b.done:
            await safe_edit(b.msg, render(b), cancel_kb(b))
            await asyncio.sleep(3)
    ui = asyncio.create_task(loop())
    await asyncio.gather(*tasks, return_exceptions=True)
    b.done = True
    ui.cancel()
    quota_add(b.user_id, -sum(1 for i in b.items if i.status == "err"))   # الفاشل ما يتحسبش
    await safe_edit(b.msg, render(b, final=True), after_kb())
    batches.pop(b.id, None)

async def begin(b, answer=None):
    """نقطة بدء واحدة لأي دفعة: بتطبق الكوتة اليومية وبعدها بتشغّل الدفعة."""
    left = quota_left(b.user_id)
    if left <= 0:
        batches.pop(b.id, None)
        await safe_edit(b.msg, f"🚫 خلصت حدك اليومي ({DAILY_LIMIT} عنصر). جرّب بكرة 🙂", back_kb())
        return
    trimmed = len(b.items) > left
    if trimmed:
        b.items = b.items[:left]
    quota_add(b.user_id, len(b.items))
    if trimmed:
        try: await app.send_message(b.chat_id, f"⚠️ الحد اليومي: هحمّل أول {left} عنصر بس.")
        except Exception: pass
    asyncio.create_task(run_batch(b))

def gc_batches():
    for k, v in list(batches.items()):
        if not v.started and time.time() - v.created > 1800:
            batches.pop(k, None)

# ───────────── الشاشات ─────────────
def welcome_text(name):
    return (f"🎬 أهلاً {name}!\n\n"
            f"أنا بوت تحميل الفيديوهات والصور من أكتر من {SITE_COUNT} موقع.\n"
            "ابعتلي أي لينك (أو أكتر) مباشرة، أو استخدم الأزرار 👇")

HELP = ("❓ ازاي أستخدم البوت؟\n\n"
        "1️⃣ ابعت لينك (أو لينكات كتير في رسالة واحدة)\n"
        "2️⃣ اختار الجودة من الأزرار\n"
        "3️⃣ استنى وهيوصلك الفيديو\n\n"
        "📋 لينك Playlist بيتحمّل كله\n"
        "📷 لينكات الصور والألبومات بتتبعت صور\n"
        "🔎 لو الموقع مش معروف، بدوّر على الفيديو جوه الصفحة لوحدي\n"
        "⚡ أي فيديو اتحمل قبل كده بيتبعت فورًا\n"
        "⚙️ من الإعدادات تثبّت جودة افتراضية وتتخطى سؤال الجودة")

def sites_text():
    return (f"🌐 المواقع المدعومة\n\nبيشتغل مع أكتر من {SITE_COUNT} موقع، منهم:\n\n"
            "🎥 يوتيوب • تيك توك • انستجرام • فيسبوك • تويتر/X • ثريدز • ريديت • لينكدإن\n"
            "🎬 فيميو • ديلي موشن • تويتش • بيليبيلي • رومبل • Streamable • VK • OK.ru\n"
            "🎧 ساوند كلاود • باندكامب • ميكس كلاود (صوت)\n"
            "📷 بنترست • تمبلر • فليكر • صور انستجرام وتويتر وريديت (ألبومات)\n"
            "🔎 أي صفحة فيها فيديو، حتى لو الموقع مش في القايمة\n\n"
            "⚠️ مش بيشتغل مع المحتوى المحمي بـ DRM (نتفلكس، ديزني+، شاهد VIP...) "
            "ولا المحتوى الخاص، ومحجوب المحتوى المخالف لقواعد تليجرام.")

async def show(target, text, kb):
    if hasattr(target, "data"):
        await safe_edit(target.message, text, kb)
    else:
        await target.reply_text(text, reply_markup=kb)

def _dl_stream(n):
    req = urllib.request.Request(f"https://speed.cloudflare.com/__down?bytes={n}", headers={"User-Agent": UA})
    got = 0
    with urllib.request.urlopen(req, timeout=30) as r:
        while True:
            chunk = r.read(1 << 20)
            if not chunk: break
            got += len(chunk)
    return got

def speed_download_test(streams=4, size=25 * 1024 * 1024):
    from concurrent.futures import ThreadPoolExecutor
    t = time.time()
    with ThreadPoolExecutor(streams) as ex:
        total = sum(ex.map(_dl_stream, [size] * streams))
    return total / (time.time() - t) / 1e6          # MB/s

_speed_running = False

async def speed_test(cq):
    """يقيس سرعة التحميل من النت وسرعة الرفع لتليجرام من السيرفر اللي البوت شغال عليه."""
    global _speed_running
    if _speed_running:
        return await cq.answer("الاختبار شغال دلوقتي", show_alert=True)
    _speed_running = True
    msg, path = cq.message, None
    again = KB([B("🔄 اختبار تاني", "ad|sp", S.SUCCESS)], [B("🔙 رجوع", "ad")])
    try:
        await safe_edit(msg, "⚡ بقيس سرعة التحميل (100MB)...")
        try:
            dl = await asyncio.to_thread(speed_download_test)
            dl_txt = f"{dl:.1f} MB/s (≈ {dl*8:.0f} Mbps)"
        except Exception as e:
            dl, dl_txt = None, f"فشل ({clean_err(e)[:40]})"
        await safe_edit(msg, f"⬇️ التحميل: {dl_txt}\n\n⚡ بقيس سرعة الرفع لتليجرام (40MB)...")
        fd, path = tempfile.mkstemp(prefix="speed_", suffix=".bin")
        with os.fdopen(fd, "wb") as f:
            for _ in range(40): f.write(os.urandom(1 << 20))
        t = time.time()
        m = await app.send_document(msg.chat.id, path, caption="speed test")
        up = 40 * 1.048576 / (time.time() - t)
        try: await m.delete()
        except Exception: pass
        verdict = ("🟢 ممتازة" if up >= 15 else "🟡 كويسة" if up >= 6 else "🔴 بطيئة — جرّب region تاني أو VPS قريب من تليجرام")
        await safe_edit(msg, f"⚡ نتيجة اختبار السرعة\n\n⬇️ التحميل: {dl_txt}\n⬆️ الرفع لتليجرام: "
                             f"{up:.1f} MB/s (≈ {up*8:.0f} Mbps)\n\nالرفع: {verdict}\n"
                             f"(رفع متوازي: {UPLOAD_WORKERS if UPLOAD_TUNED else 4})", again)
    except Exception as e:
        await safe_edit(msg, f"❌ الاختبار فشل: {clean_err(e)}", again)
    finally:
        _speed_running = False
        if path and os.path.exists(path): os.remove(path)

async def broadcast(admin_cq, chat_id, msg_id):
    ids = [r[0] for r in db_exec("SELECT id FROM users")]
    ok = bad = 0
    for u in ids:
        for _ in range(2):
            try:
                await app.copy_message(u, chat_id, msg_id); ok += 1; break
            except FloodWait as e:
                await asyncio.sleep(e.value + 1)
            except Exception:
                bad += 1; break
        await asyncio.sleep(0.05)
    await safe_edit(admin_cq.message, f"📢 خلصت الإذاعة\n✅ وصلت: {ok}\n❌ فشلت: {bad}", admin_kb())

# ───────────── الهاندلرز ─────────────
@app.on_message(filters.command("start") & filters.private)
async def on_start(_, m):
    if not is_allowed(m.from_user.id):
        return await m.reply_text("🔒 البوت ده خاص. كلّم صاحبه علشان يفعّلك.")
    touch_user(m.from_user.id)
    awaiting.pop(m.from_user.id, None)
    await m.reply_text(welcome_text(m.from_user.first_name or ""), reply_markup=menu_kb(m.from_user.id))

@app.on_message(filters.command(["ban", "unban"]) & filters.private)
async def on_ban(_, m):
    if not is_admin(m.from_user.id): return
    cmd = m.command[0].lower()
    try: target = int(m.command[1])
    except (IndexError, ValueError):
        return await m.reply_text(f"الاستخدام: /{cmd} <user_id>")
    if target in ADMIN_IDS:
        return await m.reply_text("مينفعش تحظر أدمن.")
    if cmd == "ban":
        db_exec("INSERT OR IGNORE INTO banned(id) VALUES (?)", (target,))
        txt = f"🚫 اتحظر: {target}"
    else:
        db_exec("DELETE FROM banned WHERE id=?", (target,))
        txt = f"✅ اتفك الحظر عن: {target}"
    await m.reply_text(txt)

@app.on_message(filters.private & ~filters.command(["start", "ban", "unban"]))
async def on_message(_, m):
    uid = m.from_user.id
    if not is_allowed(uid):
        return await m.reply_text("🔒 البوت ده خاص. كلّم صاحبه علشان يفعّلك.")
    touch_user(uid); gc_batches()

    if awaiting.get(uid) == "bc" and is_admin(uid):
        awaiting.pop(uid)
        bc_pending[uid] = (m.chat.id, m.id)
        n = db_exec("SELECT COUNT(*) FROM users")[0][0]
        return await m.reply_text(f"📢 هتتبعت الرسالة دي لـ {n} مستخدم. تأكيد؟",
                                  reply_markup=KB([B("✅ تأكيد الإرسال", "ad|bc|ok", S.SUCCESS)],
                                                  [B("❌ إلغاء", "ad|bc|no", S.DANGER)]))

    urls = list(dict.fromkeys(u.rstrip(".,;:!?)]}،؛") for u in URL_RE.findall(m.text or "")))[:MAX_LINKS]
    if not urls:
        return await m.reply_text("ابعت لينك صحيح 🙂 أو ارجع للقائمة 👇", reply_markup=menu_kb(uid))
    now = time.time()
    if not is_admin(uid) and now - last_req.get(uid, 0) < RATE_SECONDS:
        return await m.reply_text("⏳ على مهلك شوية، ابعت الرابط التالي بعد ثواني.")
    last_req[uid] = now
    if quota_left(uid) <= 0:
        return await m.reply_text(f"🚫 خلصت حدك اليومي ({DAILY_LIMIT} عنصر). جرّب بكرة 🙂", reply_markup=back_kb())
    blocked = [u for u in urls if is_blocked(u)]
    urls = [u for u in urls if u not in blocked]
    if not urls:
        return await m.reply_text("🚫 الموقع ده مش مسموح بيه (مخالف لقواعد تليجرام).", reply_markup=back_kb())
    safe = await asyncio.gather(*[asyncio.to_thread(is_safe_url, u) for u in urls])
    urls = [u for u, ok in zip(urls, safe) if ok]
    if not urls:
        return await m.reply_text("❌ الرابط غير صالح.", reply_markup=back_kb())
    if sum(1 for x in batches.values() if x.user_id == uid and x.started) >= MAX_ACTIVE_BATCHES:
        return await m.reply_text("عندك تحميلات شغالة دلوقتي، استنى لما تخلص أو الغيها.")

    wait = await m.reply_text(f"⏳ بفحص {len(urls)} رابط...")
    async def _probe(u):
        async with PROBE_SEM:
            return await asyncio.to_thread(probe, u)
    results = await asyncio.gather(*[_probe(u) for u in urls], return_exceptions=True)
    items, fails, pl = [], [], None
    for u, r in zip(urls, results):
        if isinstance(r, Exception): fails.append(f"• {u[:40]}: {clean_err(r)[:80]}")
        else:
            items += r[0]; pl = pl or r[1]
    items = items[:MAX_ITEMS]
    if not items:
        return await wait.edit_text("❌ مقدرتش أحمّل الرابط:\n" + "\n".join(fails), reply_markup=back_kb())

    b = Batch(id=uuid.uuid4().hex[:8], user_id=uid, chat_id=m.chat.id, items=items, msg=wait)
    batches[b.id] = b

    pref = get_pref(uid)
    only_gallery = all(i.engine == "gallery" for i in items)
    if pref != "ask" or only_gallery:         # جودة افتراضية، أو صور بس (مفيش جودة تتختار)
        b.mode = "best" if only_gallery else pref
        await safe_edit(wait, render(b), cancel_kb(b))
        return await begin(b)

    txt = (f"📋 {pl}\n" if pl else "") + f"🎬 لقيت {len(items)} عنصر:\n"
    txt += "\n".join(f"• {i.title[:45]}" for i in items[:6])
    if len(items) > 6: txt += f"\n... و{len(items)-6} كمان"
    if fails: txt += "\n\n⚠️ فشل:\n" + "\n".join(fails[:3])
    txt += "\n\nاختار الجودة:"
    await wait.edit_text(txt, reply_markup=picker_kb(b.id))

@app.on_callback_query()
async def on_cb(_, cq):
    uid, d = cq.from_user.id, cq.data.split("|")
    act = d[0]
    if not is_allowed(uid):
        return await cq.answer("🔒 البوت خاص", show_alert=True)
    touch_user(uid)

    if act == "m":
        awaiting.pop(uid, None)
        return await show(cq, welcome_text(cq.from_user.first_name or ""), menu_kb(uid))
    if act == "dl":
        return await show(cq, "📥 ابعتلي لينك الفيديو أو الصور (أو أكتر من لينك في رسالة واحدة) 👇", back_kb())
    if act == "hp":
        return await show(cq, HELP, back_kb())
    if act == "sites":
        return await show(cq, sites_text(), back_kb())

    if act == "me":
        if len(d) > 1 and d[1] == "clr":
            db_exec("DELETE FROM history WHERE user_id=?", (uid,))
        total = db_exec("SELECT downloads FROM users WHERE id=?", (uid,))[0][0]
        rows = db_exec("SELECT title, mode FROM history WHERE user_id=? ORDER BY ts DESC LIMIT 8", (uid,))
        txt = f"📊 تحميلاتك\n\nالإجمالي: {total}\n"
        txt += ("\nآخر تحميلاتك:\n" + "\n".join(f"• {t[:40]} ({mode_label(q) if q != 'gallery' else 'صور'})" for t, q in rows)
                if rows else "\nلسه مفيش تحميلات.")
        kb = KB([B("🗑 مسح السجل", "me|clr", S.DANGER)], [B("🏠 القائمة الرئيسية", "m")]) if rows else back_kb()
        return await show(cq, txt, kb)

    if act == "st":
        if len(d) > 1 and d[1] in VALID_PREFS:
            set_pref(uid, d[1])
            await cq.answer("✅ اتحفظ")
        return await show(cq, f"⚙️ الإعدادات\n\nالجودة الافتراضية: {mode_label(get_pref(uid))}\n"
                              "(لو اخترت جودة، البوت هيبدأ التحميل فورًا من غير ما يسألك)",
                          settings_kb(uid))

    if act == "q":
        b = batches.get(d[1])
        if not b or b.user_id != uid or b.started:
            return await cq.answer("انتهت صلاحية الطلب، ابعت الرابط تاني.", show_alert=True)
        if d[2] not in VALID_PREFS or d[2] == "ask":
            return await cq.answer("اختيار غير صالح", show_alert=True)
        b.mode = d[2]
        b.started = True            # يمنع الضغط المزدوج على الزرار من تشغيل الدفعة مرتين
        await cq.answer("بدأنا 🚀")
        return await begin(b)

    if act == "x":
        b = batches.get(d[1])
        if b and b.user_id == uid and not b.started:
            batches.pop(b.id, None)
        await cq.answer("اتلغى")
        return await show(cq, welcome_text(cq.from_user.first_name or ""), menu_kb(uid))

    if act == "c":
        b = batches.get(d[1])
        if b and b.user_id == uid:
            b.cancelled = True
            return await cq.answer("جاري الإلغاء...")
        return await cq.answer()

    if act == "ad":
        if not is_admin(uid):
            return await cq.answer("للأدمن فقط", show_alert=True)
        sub = d[1] if len(d) > 1 else ""
        if sub == "":
            return await show(cq, "🛠 لوحة الأدمن", admin_kb())
        if sub == "st":
            u = db_exec("SELECT COUNT(*), COALESCE(SUM(downloads),0) FROM users")[0]
            c = db_exec("SELECT COUNT(*) FROM cache")[0][0]
            act = db_exec("SELECT COUNT(DISTINCT user_id) FROM history WHERE ts > ?", (int(time.time()) - 86400,))[0][0]
            bn = db_exec("SELECT COUNT(*) FROM banned")[0][0]
            return await show(cq, f"📊 إحصائيات\n\n👥 مستخدمين: {u[0]}\n📥 تحميلات: {u[1]}\n"
                                  f"⚡ في الكاش: {c}\n🔄 دفعات شغالة: {len(batches)}\n"
                                  f"👤 نشطين 24س: {act}  |  🚫 محظورين: {bn}" + chr(10) +
                                  f"🚦 حد يومي: {DAILY_LIMIT or '∞'}  |  مسموح لهم: {len(ALLOWED_IDS) or 'الكل'}\n\n"
                                  f"🧩 aria2c: {'✅' if HAS_ARIA2 else '❌'}  "
                                  f"تقليد المتصفح: {'✅' if IMPERSONATE else '❌'}  "
                                  f"gallery-dl: {'✅' if HAS_GALLERY else '❌'}\n"
                                  f"⚡ uvloop: {'✅' if HAS_UVLOOP else '❌'}  رفع متوازي: {UPLOAD_WORKERS if UPLOAD_TUNED else 4}",
                              KB([B("🔄 تحديث", "ad|st", S.SUCCESS)], [B("🔙 رجوع", "ad")]))
        if sub == "bc":
            if len(d) == 2:
                awaiting[uid] = "bc"
                return await show(cq, "📢 ابعت دلوقتي الرسالة اللي عايز تذيعها (نص أو صورة أو فيديو...)",
                                  KB([B("❌ إلغاء", "ad|bc|no", S.DANGER)]))
            if d[2] == "no":
                awaiting.pop(uid, None); bc_pending.pop(uid, None)
                return await show(cq, "🛠 لوحة الأدمن", admin_kb())
            if d[2] == "ok":
                p = bc_pending.pop(uid, None)
                if not p: return await cq.answer("مفيش رسالة معلّقة", show_alert=True)
                await safe_edit(cq.message, "📢 جاري الإرسال...")
                return asyncio.create_task(broadcast(cq, *p))
        if sub == "sp":
            await cq.answer("بدأ الاختبار ⚡")
            return asyncio.create_task(speed_test(cq))
        if sub == "cc":
            if len(d) == 2:
                return await show(cq, "🗑 متأكد إنك عايز تمسح الكاش كله؟ الفيديوهات هتتحمل من الأول.",
                                  KB([B("✅ أيوه امسح", "ad|cc|ok", S.DANGER)], [B("🔙 لأ، رجوع", "ad")]))
            db_exec("DELETE FROM cache")
            await cq.answer("✅ اتمسح")
            return await show(cq, "🛠 لوحة الأدمن", admin_kb())
    await cq.answer()

# ───────────── التشغيل ─────────────
async def main():
    clean_stale_tmp(max_age=0)      # أي بقايا من تشغيل سابق (البوت لسه مبدأش يحمّل حاجة)
    await app.start()
    try: await app.delete_bot_commands()
    except Exception as e: log.info("delete_bot_commands: %s", e)
    log.info("sites: %s | aria2c: %s | impersonate: %s | gallery-dl: %s | uvloop: %s | upload-workers: %s | colors: %s | db: %s",
             SITE_COUNT, HAS_ARIA2, bool(IMPERSONATE), HAS_GALLERY, HAS_UVLOOP,
             UPLOAD_WORKERS if UPLOAD_TUNED else 4, COLORS, DB_PATH)
    cleanup = asyncio.create_task(cleanup_loop())
    await idle()
    cleanup.cancel()
    await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
