"""ClipDown API: يخدم واجهة ClipDown (POST /api/info و GET /api/download)."""
import json, os, re, shutil, tempfile, threading, time, urllib.request
from collections import defaultdict, deque
from urllib.parse import urlencode, urlparse

import yt_dlp
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

ALLOWED_HOSTS = ("youtube.com", "youtu.be", "instagram.com", "tiktok.com",
                 "facebook.com", "fb.watch", "twitter.com", "x.com", "soundcloud.com")
MAX_MINUTES = int(os.getenv("MAX_MINUTES", "60"))      # أطول مدة مسموحة
MAX_MB = int(os.getenv("MAX_MB", "500"))               # أكبر حجم ملف
RATE = int(os.getenv("RATE_PER_MIN", "10"))            # طلبات لكل IP بالدقيقة
ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",")]
COOKIES = os.getenv("COOKIES_FILE")                    # اختياري (cookies.txt)
slots = threading.BoundedSemaphore(int(os.getenv("MAX_JOBS", "2")))

app = FastAPI(title="ClipDown API")
app.add_middleware(CORSMiddleware, allow_origins=ORIGINS, allow_methods=["GET", "POST"],
                   allow_headers=["*"], expose_headers=["Content-Disposition", "Content-Length"])
hits = defaultdict(deque)


def limit(request: Request):
    ip = request.headers.get("x-forwarded-for", request.client.host).split(",")[0].strip()
    q, now = hits[ip], time.time()
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RATE:
        raise HTTPException(429, "طلبات كثيرة، انتظر دقيقة")
    q.append(now)


def check(url: str):
    p = urlparse(url)
    h = (p.hostname or "").lower()
    if p.scheme not in ("http", "https") or not any(h == d or h.endswith("." + d) for d in ALLOWED_HOSTS):
        raise HTTPException(400, "منصة غير مدعومة")


def opts(**extra):
    o = {"quiet": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 20}
    if COOKIES:
        o["cookiefile"] = COOKIES
    o.update(extra)
    return o


def clock(sec):
    s = int(sec or 0)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


class InfoReq(BaseModel):
    url: str


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/api/info")
def info(req: InfoReq, request: Request):
    limit(request)
    check(req.url)
    try:
        with yt_dlp.YoutubeDL(opts(skip_download=True)) as y:
            d = y.extract_info(req.url, download=False)
    except Exception:
        raise HTTPException(422, "تعذّر قراءة الرابط")
    dur = d.get("duration") or 0
    if dur > MAX_MINUTES * 60:
        raise HTTPException(413, f"الفيديو أطول من {MAX_MINUTES} دقيقة")
    heights = {f["height"] for f in d.get("formats", [])
               if f.get("vcodec") not in (None, "none") and f.get("height")}
    mbps = {1080: 5, 720: 2.5, 480: 1.2, 360: .7}
    formats = [{"id": f"v{h}", "type": "video", "label": f"{h}p MP4",
                "size": f"~{dur * mbps[h] / 8:.0f} ميغا" if dur else ""}
               for h in (1080, 720, 480, 360) if any(x >= h for x in heights)]
    if heights and not formats:
        formats = [{"id": "v360", "type": "video", "label": "360p MP4", "size": ""}]
    for q, label in ((320, "MP3 عالي (320)"), (128, "MP3 عادي (128)")):
        formats.append({"id": f"a{q}", "type": "audio", "label": label,
                        "size": f"~{dur * q / 8 / 1000:.1f} ميغا" if dur else ""})
    return {"title": d.get("title") or "", "duration": clock(dur),
            "thumbnail": d.get("thumbnail") or "", "formats": formats}


@app.get("/api/download")
def download(request: Request, url: str = Query(...), format: str = Query(...)):
    limit(request)
    check(url)
    m = re.fullmatch(r"v(\d{3,4})|a(320|128)", format)
    if not m:
        raise HTTPException(400, "صيغة غير صالحة")
    if not slots.acquire(blocking=False):
        raise HTTPException(503, "الخادم مشغول، جرّب بعد قليل")
    tmp = tempfile.mkdtemp(prefix="clipdown_")

    def cleanup():
        shutil.rmtree(tmp, ignore_errors=True)
        slots.release()

    try:
        base = dict(outtmpl=os.path.join(tmp, "%(title).80s.%(ext)s"),
                    max_filesize=MAX_MB * 1024 * 1024,
                    match_filter=yt_dlp.utils.match_filter_func(f"duration<={MAX_MINUTES * 60}"))
        if m.group(1):
            h = int(m.group(1))
            o = opts(format=f"bv*[height<={h}]+ba/b[height<={h}]/b", merge_output_format="mp4", **base)
        else:
            o = opts(format="bestaudio/best", postprocessors=[
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": m.group(2)}], **base)
        with yt_dlp.YoutubeDL(o) as y:
            y.extract_info(url, download=True)
        files = [os.path.join(tmp, f) for f in os.listdir(tmp)]
        if not files:
            raise HTTPException(422, "تعذّر التحميل (الملف كبير أو طويل أو غير متاح)")
        path = max(files, key=os.path.getsize)
        ext = path.rsplit(".", 1)[-1].lower()
        mime = {"mp4": "video/mp4", "mp3": "audio/mpeg"}.get(ext, "application/octet-stream")
        # FileResponse يضيف Content-Length و Content-Disposition: attachment
        return FileResponse(path, media_type=mime, filename=os.path.basename(path),
                            background=BackgroundTask(cleanup))
    except HTTPException:
        cleanup()
        raise
    except Exception:
        cleanup()
        raise HTTPException(422, "تعذّر التحميل")


# ---------- يوتيوب داخل التطبيق (YouTube Data API v3) ----------
YT_KEY = os.getenv("YOUTUBE_API_KEY")
YT_REGION = os.getenv("YT_REGION", "IQ")
_cache = {}


def cached(key, fn, ttl=600):
    """تخزين مؤقت 10 دقائق لتوفير حصة الـ API."""
    now, v = time.time(), _cache.get(key)
    if v and now - v[0] < ttl:
        return v[1]
    r = fn()
    _cache[key] = (now, r)
    if len(_cache) > 500:
        _cache.pop(next(iter(_cache)))
    return r


def yt_call(path, params):
    if not YT_KEY:
        raise HTTPException(503, "مفتاح YOUTUBE_API_KEY غير مضبوط")
    url = "https://www.googleapis.com/youtube/v3/" + path + "?" + urlencode({**params, "key": YT_KEY})
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.load(r)
    except Exception:
        raise HTTPException(502, "تعذّر الاتصال بيوتيوب")


def yt_items(data, search):
    out = []
    for it in data.get("items", []):
        sn = it.get("snippet", {})
        th = sn.get("thumbnails", {})
        vid = it["id"].get("videoId") if search else it["id"]
        if vid:
            out.append({"id": vid, "title": sn.get("title", ""), "channel": sn.get("channelTitle", ""),
                        "thumbnail": (th.get("medium") or th.get("default") or {}).get("url", "")})
    return out


@app.get("/api/youtube/search")
def yt_search(request: Request, q: str = Query(..., min_length=1, max_length=100)):
    limit(request)
    d = cached("s:" + q.lower(), lambda: yt_call("search", {
        "part": "snippet", "type": "video", "maxResults": 20, "q": q, "safeSearch": "moderate"}))
    return {"items": yt_items(d, True)}


@app.get("/api/youtube/trending")
def yt_trending(request: Request):
    limit(request)
    d = cached("trend", lambda: yt_call("videos", {
        "part": "snippet", "chart": "mostPopular", "maxResults": 20, "regionCode": YT_REGION}))
    return {"items": yt_items(d, False)}
