"""Home radio: one web page to put any station on any speaker in the house."""
import asyncio
import json
import logging
import os
import secrets
import socket
import threading
import time
from pathlib import Path

from aiohttp import web

from catalog import BUILTIN, Catalog, resolve_url, ytdl_info
from players import Discovery

HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("DATA", HERE.parent / "data"))
PORT = int(os.environ.get("PORT", "8780"))
log = logging.getLogger("radio")


class Ctx:
    """Shared services handed to the speakers."""

    def __init__(self):
        try:
            cfg = json.loads((DATA / "config.json").read_text())
        except FileNotFoundError:
            cfg = {}
        # Without an AudioAddict premium key the five networks are hidden; everything else works.
        self.catalog = Catalog(DATA, cfg.get("listen_key", ""))
        self.cliraop = os.environ.get("CLIRAOP", "cliraop")
        self.hw_volume = int(cfg.get("airplay_max_volume", 80))   # cliraop -v, 0-100 -> -30..0 dB
        self.state_file = DATA / "state.json"
        self._state_lock = threading.Lock()
        try:
            self.state = json.loads(self.state_file.read_text())
        except Exception:
            self.state = {}
        self.proxies = {}   # token -> (input_url, user_agent, created)
        self.browser = {}   # key -> Playable, for "This device" playback

    # --- persisted per-speaker state ---
    def saved_volume(self, sid, default):
        return self.state.get("volume", {}).get(sid, default)

    def save_volume(self, sid, v):
        self._save("volume", sid, v)

    def _save(self, section, key, value):
        with self._state_lock:
            self.state.setdefault(section, {})[key] = value
            self.state_file.write_text(json.dumps(self.state, indent=1))

    # --- audio inputs ---
    def input_for(self, p, attempt=0):
        """-> (url ffmpeg should read, user agent, is_live). Blocking."""
        if p.kind == "ytdl":
            info = ytdl_info(p.page)   # re-resolved every time: googlevideo URLs expire
            ua = (info.get("http_headers") or {}).get("User-Agent")
            return info["url"], ua, bool(info.get("is_live"))
        url, _, _, _ = resolve_url(p.urls[attempt % len(p.urls)])
        return url, None, True

    def make_proxy(self, p):
        """Resolve now; -> token for /stream/<token>.mp3 (NAS transcodes to MP3)."""
        url, ua, _ = self.input_for(p)
        now = time.time()
        self.proxies = {k: v for k, v in self.proxies.items() if now - v[2] < 12 * 3600}
        token = secrets.token_urlsafe(8)
        self.proxies[token] = (url, ua, now)
        return token

    def proxy_url(self, p, toward_host):
        """Absolute NAS URL for a Cast speaker."""
        token = self.make_proxy(p)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((toward_host, 8009))   # UDP connect sends nothing; picks the right interface
        ip = s.getsockname()[0]
        s.close()
        return f"http://{ip}:{PORT}/stream/{token}.mp3"


ctx = Ctx()
discovery = None
routes = web.RouteTableDef()


def speaker(req):
    sp = discovery.speakers.get(req.match_info["sid"])
    if not sp:
        raise web.HTTPNotFound(text="unknown speaker")
    return sp


def bad(msg):
    return web.json_response({"error": msg}, status=400)


@routes.get("/")
async def index(_):
    return web.FileResponse(HERE / "static" / "index.html", headers={"Cache-Control": "no-cache"})


@routes.get("/api/catalog")
async def api_catalog(_):
    c = ctx.catalog
    nets, favs = await asyncio.gather(asyncio.to_thread(c.networks), asyncio.to_thread(c.favorites))
    return web.json_response({"networks": nets, "favorites": favs,
                              "builtin": [{"id": b["id"], "name": b["name"]} for b in BUILTIN],
                              "saved": [{k: s[k] for k in ("id", "name", "kind", "url")} for s in c.saved()]})


@routes.get("/api/state")
async def api_state(_):
    sps = sorted(discovery.speakers.values(), key=lambda s: (not s.online, s.name.lower()))
    infos = [s.info() for s in sps]

    async def track(sp, info):
        if sp.status == "playing" and sp.playable:
            info["track"] = await asyncio.to_thread(ctx.catalog.now_playing, sp.playable)
    await asyncio.gather(*(track(s, i) for s, i in zip(sps, infos)))
    return web.json_response({"speakers": infos})


@routes.post("/api/speakers/{sid}/play")
async def api_play(req):
    sp = speaker(req)
    src = (await req.json()).get("source") or {}
    try:
        p = await asyncio.to_thread(ctx.catalog.playable, src)
    except Exception as e:
        return bad(str(e))
    await asyncio.to_thread(sp.play, p, src)
    ctx._save("last", sp.id, src)
    return web.json_response(sp.info())


@routes.post("/api/speakers/{sid}/stop")
async def api_stop(req):
    sp = speaker(req)
    await asyncio.to_thread(sp.stop)
    return web.json_response(sp.info())


@routes.post("/api/speakers/{sid}/volume")
async def api_volume(req):
    sp = speaker(req)
    v = max(0, min(100, int((await req.json())["volume"])))
    await asyncio.to_thread(sp.set_volume, v)
    return web.json_response(sp.info())


@routes.get("/api/speakers/{sid}/log")
async def api_log(req):
    return web.Response(text="\n".join(getattr(speaker(req), "log", [])))


@routes.post("/api/saved")
async def api_add_saved(req):
    body = await req.json()
    try:
        entry = await asyncio.to_thread(ctx.catalog.add_saved, body.get("url", ""), (body.get("name") or "").strip() or None)
    except Exception as e:
        return bad(f"Can't play that link: {e}")
    return web.json_response(entry)


@routes.delete("/api/saved/{id}")
async def api_del_saved(req):
    await asyncio.to_thread(ctx.catalog.delete_saved, req.match_info["id"])
    return web.json_response({"ok": True})


# ---------- "This device": the browser plays the stream itself ----------

@routes.post("/api/browser/play")
async def api_browser_play(req):
    """-> direct stream URLs for the browser (like a Cast speaker gets) + a key for fallback/track."""
    src = (await req.json()).get("source") or {}
    try:
        p = await asyncio.to_thread(ctx.catalog.playable, src)
    except Exception as e:
        return bad(str(e))
    key = secrets.token_urlsafe(6)
    ctx.browser[key] = p
    while len(ctx.browser) > 50:
        ctx.browser.pop(next(iter(ctx.browser)))

    def direct():
        if p.kind != "radio":
            return []                       # YouTube & co.: only via the NAS
        if p.meta and p.meta[0] == "aa":    # .pls entries are already the streams
            return [{"url": u, "type": "audio/aac"} for u in p.urls]
        out = []
        for u in p.urls:
            try:
                url, ctype, _, _ = resolve_url(u)
                out.append({"url": url, "type": ctype})
            except Exception:
                pass
        return out
    return web.json_response({"key": key, "name": p.name, "direct": await asyncio.to_thread(direct)})


@routes.post("/api/browser/{key}/proxy")
async def api_browser_proxy(req):
    p = ctx.browser.get(req.match_info["key"])
    if not p:
        raise web.HTTPNotFound()
    try:
        token = await asyncio.to_thread(ctx.make_proxy, p)
    except Exception as e:
        return bad(f"Can't play: {e}")
    return web.json_response({"url": f"/stream/{token}.mp3"})


@routes.get("/api/browser/{key}/track")
async def api_browser_track(req):
    p = ctx.browser.get(req.match_info["key"])
    track = await asyncio.to_thread(ctx.catalog.now_playing, p) if p else None
    return web.json_response({"track": track})


@routes.get("/stream/{token}")
async def stream(req):
    """MP3 transcode for Cast speakers (YouTube, or formats the speaker refuses)."""
    entry = ctx.proxies.get(req.match_info["token"].removesuffix(".mp3"))
    if not entry:
        raise web.HTTPNotFound()
    url, ua, _ = entry
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
    if url.startswith("http"):
        cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10"]
    if ua:
        cmd += ["-user_agent", ua]
    cmd += ["-i", url, "-vn", "-c:a", "libmp3lame", "-b:a", "256k", "-f", "mp3", "pipe:1"]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL)
    resp = web.StreamResponse(headers={"Content-Type": "audio/mpeg", "Cache-Control": "no-cache"})
    try:
        await resp.prepare(req)
        while chunk := await proc.stdout.read(16384):
            await resp.write(chunk)
    except ConnectionError:
        pass
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    return resp


def main():
    global discovery
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    discovery = Discovery(ctx)   # before the event loop exists: zeroconf gets its own thread
    app = web.Application()
    app.add_routes(routes)
    web.run_app(app, port=PORT, access_log=None, print=lambda *_: log.info("listening on :%s", PORT))


if __name__ == "__main__":
    main()
