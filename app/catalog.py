"""Stations: AudioAddict networks (one listen key), built-in radios, pasted stations.

Everything here is blocking I/O; the web layer calls it through asyncio.to_thread.
The listen key never leaves this module except inside stream URLs handed to
speakers/ffmpeg - the browser only ever sees `network:channel` ids.
"""
import hashlib
import json
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

UA = {"User-Agent": "home-radio/1.0"}
CACHE_MAX_AGE = 7 * 24 * 3600

# AudioAddict: the same premium listen key works on all of them.
NETWORKS = {
    "di": ("DI.FM", "di.fm"),
    "radiotunes": ("RadioTunes", "radiotunes.com"),
    "rockradio": ("Rock Radio", "rockradio.com"),
    "classicalradio": ("Classical Radio", "classicalradio.com"),
    "zenradio": ("Zen Radio", "zenradio.com"),
}

_RP = [(0, "Main Mix", "aac-320"), (1, "Mellow Mix", "mellow-320"), (2, "Rock Mix", "rock-320"),
       (3, "Global Mix", "global-320"), (42, "Serenity", "serenity")]
BUILTIN = [{"id": f"rp{c}", "name": f"Radio Paradise · {n}",
            "url": f"http://stream.radioparadise.com/{p}", "meta": ["rp", c]} for c, n, p in _RP] + [
    {"id": "r357", "name": "Radio 357", "url": "https://stream.radio357.pl/", "meta": ["icy"]},
    {"id": "nowyswiat", "name": "Radio Nowy Świat",
     "url": "https://stream.rcs.revma.com/ypqt40u0x1zuv", "meta": ["icy"]},
]

YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")
AUDIO_TYPES = ("audio/", "application/ogg", "application/x-mpegurl", "application/vnd.apple.mpegurl")
EXT_TYPES = {".mp3": "audio/mpeg", ".aac": "audio/aac", ".m4a": "audio/mp4", ".ogg": "audio/ogg",
             ".opus": "audio/ogg", ".flac": "audio/flac", ".wav": "audio/wav",
             ".m3u8": "application/x-mpegURL"}


@dataclass
class Playable:
    """What a speaker needs to start playing."""
    name: str
    kind: str                                  # "radio" (direct stream) | "ytdl" (needs yt-dlp)
    urls: list = field(default_factory=list)   # radio: candidate stream URLs (server fallback)
    page: str = ""                             # ytdl: page URL, re-resolved on every play
    meta: tuple = ()                           # how to find the current track


# ---------- URL helpers ----------

def resolve_url(url, depth=0):
    """-> (stream_url, content_type, icy_name, is_audio). Unwraps .pls/.m3u, detects HLS.
    Keeps the original URL for plain streams: tokenized redirect targets expire."""
    if depth > 3:
        raise ValueError("too many nested playlists")
    ext = Path(urllib.parse.urlparse(url).path).suffix.lower()
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=8) as r:
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        icy = (r.headers.get("icy-name") or "").strip()
        icy = icy if len(icy) > 1 else ""   # Radio 357 sends "-"
        is_list = ext in (".pls", ".m3u", ".m3u8") or "mpegurl" in ctype or "scpls" in ctype
        if not is_list:
            is_audio = ctype.startswith(AUDIO_TYPES) or (ext in EXT_TYPES and not ctype.startswith("text/"))
            if not ctype.startswith(AUDIO_TYPES):
                ctype = EXT_TYPES.get(ext, "audio/mpeg")
            return url, ctype, icy, is_audio
        text = r.read(65536).decode("utf-8", "replace")
    if "#EXT-X-" in text:
        return url, "application/x-mpegURL", icy, True
    entries = re.findall(r"^File\d+=(\S+)", text, re.M) or \
        [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    if not entries:
        raise ValueError("playlist is empty")
    return resolve_url(urllib.parse.urljoin(url, entries[0]), depth + 1)


def ytdl_info(url):
    """yt-dlp metadata for the best audio-only format (url, title, is_live, http_headers)."""
    p = subprocess.run(["yt-dlp", "-j", "-f", "bestaudio/best", "--no-playlist", "--no-warnings", url],
                       capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        msg = (p.stderr.strip().splitlines() or ["yt-dlp failed"])[-1]
        raise ValueError(msg.removeprefix("ERROR: "))
    return json.loads(p.stdout)


def _ytdl_title(url):
    info = ytdl_info(url)
    title = info.get("title") or url
    if info.get("is_live"):   # yt-dlp appends " 2026-11-11 16:14" to live titles
        title = re.sub(r" \d{4}-\d{2}-\d{2} \d{2}:\d{2}$", "", title)
    return title


def is_youtube(url):
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in YOUTUBE_HOSTS)


def icy_title(url):
    try:
        req = urllib.request.Request(url, headers={**UA, "Icy-MetaData": "1"})
        with urllib.request.urlopen(req, timeout=5) as r:
            metaint = int(r.headers.get("icy-metaint") or 0)
            if not metaint:
                return None
            r.read(metaint)
            meta = r.read(r.read(1)[0] * 16).decode("utf-8", "replace")
        m = re.search(r"StreamTitle='(.*?)';", meta)
        return (m.group(1).strip() or None) if m else None
    except Exception:
        return None


def _get_json(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=6) as r:
        return json.load(r)


# ---------- catalog ----------

class Catalog:
    def __init__(self, data: Path, listen_key: str):
        self.data, self.key = data, listen_key
        (data / "cache").mkdir(parents=True, exist_ok=True)
        self.saved_file = data / "stations.json"
        self._lock = threading.Lock()
        self._track_cache = {}

    # --- AudioAddict ---
    def channels(self, net):
        cache = self.data / "cache" / f"{net}.json"
        if cache.exists() and time.time() - cache.stat().st_mtime < CACHE_MAX_AGE:
            return json.loads(cache.read_text())
        try:
            data = [{"id": c["id"], "key": c["key"], "name": c["name"]}
                    for c in _get_json(f"http://listen.{NETWORKS[net][1]}/premium_high")]
            data.sort(key=lambda c: c["name"].lower())
            cache.write_text(json.dumps(data))
            return data
        except Exception:
            return json.loads(cache.read_text()) if cache.exists() else []

    def favorites(self):
        """DI.FM favorites from favorites.pls (exported from di.fm), matched by key or title."""
        f = self.data / "favorites.pls"
        if not f.exists():
            return []
        text = f.read_text()
        keys = set(re.findall(r"di\.fm:\d+/(\w+)\?", text))
        norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower().replace("&", "n"))
        titles = {norm(t) for t in re.findall(r"Title\d+=DI\.FM - (.+)", text)}
        return [{"net": "di", "key": c["key"], "name": c["name"]} for c in self.channels("di")
                if c["key"] in keys or norm(c["name"]) in titles]

    def networks(self):
        if not self.key:
            return []
        return [{"id": n, "name": name, "channels": [{"key": c["key"], "name": c["name"]} for c in self.channels(n)]}
                for n, (name, _) in NETWORKS.items()]

    # --- pasted stations ---
    def saved(self):
        try:
            return json.loads(self.saved_file.read_text())
        except Exception:
            return []

    def add_saved(self, url, name=None):
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("paste an http(s) link")
        with self._lock:
            for s in self.saved():
                if s["url"] == url:
                    return s
        entry = {"id": hashlib.sha1(url.encode()).hexdigest()[:10], "url": url}
        if is_youtube(url):
            entry.update(kind="ytdl", name=name or _ytdl_title(url))
        else:
            try:
                _, _, icy, is_audio = resolve_url(url)
            except Exception:
                is_audio, icy = False, ""
            if is_audio:
                host = urllib.parse.urlparse(url).hostname
                entry.update(kind="radio", name=name or icy or host)
            else:   # a web page: SoundCloud, Bandcamp, Mixcloud... let yt-dlp try
                entry.update(kind="ytdl", name=name or _ytdl_title(url))
        with self._lock:
            items = self.saved()
            items.append(entry)
            self.saved_file.write_text(json.dumps(items, indent=1, ensure_ascii=False))
        return entry

    def delete_saved(self, sid):
        with self._lock:
            items = [s for s in self.saved() if s["id"] != sid]
            self.saved_file.write_text(json.dumps(items, indent=1, ensure_ascii=False))

    # --- source descriptor -> Playable ---
    def playable(self, src):
        t = src.get("type")
        if t == "aa":
            net, key = src["net"], src["key"]
            chan = next((c for c in self.channels(net) if c["key"] == key), None)
            if not chan:
                raise ValueError(f"unknown channel {net}:{key}")
            pls = f"http://listen.{NETWORKS[net][1]}/premium_high/{key}.pls?listen_key={self.key}"
            with urllib.request.urlopen(urllib.request.Request(pls, headers=UA), timeout=8) as r:
                urls = re.findall(r"^File\d+=(\S+)", r.read().decode(), re.M)
            if not urls:
                raise ValueError("no stream servers in playlist (is premium active?)")
            return Playable(f"{NETWORKS[net][0]} · {chan['name']}", "radio", urls=urls,
                            meta=("aa", net, chan["id"]))
        if t == "builtin":
            b = next(b for b in BUILTIN if b["id"] == src["id"])
            return Playable(b["name"], "radio", urls=[b["url"]], meta=tuple(b["meta"]))
        if t == "saved":
            s = next((s for s in self.saved() if s["id"] == src["id"]), None)
            if not s:
                raise ValueError("station no longer saved")
            if s["kind"] == "ytdl":
                return Playable(s["name"], "ytdl", page=s["url"])
            return Playable(s["name"], "radio", urls=[s["url"]], meta=("icy",))
        raise ValueError(f"bad source {src!r}")

    # --- now playing ---
    def now_playing(self, p: Playable):
        if not p or not p.meta:
            return None
        ck = (p.meta, p.urls[0] if p.urls else "")
        hit = self._track_cache.get(ck)
        if hit and time.time() - hit[0] < 15:
            return hit[1]
        text = None
        try:
            if p.meta[0] == "aa":
                cur = _get_json(f"https://api.audioaddict.com/v1/{p.meta[1]}/track_history/channel/{p.meta[2]}")[0]
                text = cur.get("track") or f"{cur.get('display_artist')} - {cur.get('display_title')}"
                if cur.get("type") != "track":
                    text = "(ad / jingle)"
            elif p.meta[0] == "rp":
                cur = _get_json(f"https://api.radioparadise.com/api/now_playing?chan={p.meta[1]}")
                text = f"{cur['artist']} - {cur['title']}"
            elif p.meta[0] == "icy":
                text = icy_title(p.urls[0])
        except Exception:
            pass
        self._track_cache[ck] = (time.time(), text)
        return text
