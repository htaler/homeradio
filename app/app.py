"""Home radio: one web page to put any station on any speaker in the house."""
import asyncio
import json
import logging
import os
import re
import secrets
import socket
import threading
import time
from pathlib import Path

from aiohttp import web

from catalog import BUILTIN, Catalog, Playable, resolve_url, ytdl_info
from players import Discovery
from plex import DIRECT, Plex

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
        plex = cfg.get("plex") or {}
        self.plex = Plex(plex.get("url", "http://127.0.0.1:32400"), plex["token"]) if plex.get("token") else None
        self.proxies = {}   # token -> (input_url, user_agent, created)
        self._yt_cache = {} # video id -> (googlevideo url, user agent, resolved_at)
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
        return f"{self.base_url(toward_host)}/stream/{self.make_proxy(p)}.mp3"

    @staticmethod
    def base_url(toward_host):
        """http://<our LAN IP>:PORT as seen from a given device."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((toward_host, 8009))   # UDP connect sends nothing; picks the right interface
        ip = s.getsockname()[0]
        s.close()
        return f"http://{ip}:{PORT}"

    def track_input(self, t):
        """Queue track -> (url ffmpeg reads, user agent). YouTube tracks are resolved on demand
        (googlevideo URLs expire after hours) and cached for a while."""
        inp = t["input"]
        if not inp.startswith("yt:"):
            return inp, None
        vid = inp[3:]
        hit = self._yt_cache.get(vid)
        if hit and time.time() - hit[2] < 3 * 3600:
            return hit[0], hit[1]
        info = ytdl_info(f"https://www.youtube.com/watch?v={vid}")
        ua = (info.get("http_headers") or {}).get("User-Agent")
        self._yt_cache[vid] = (info["url"], ua, time.time())
        return info["url"], ua

    def prefetch(self, tracks):
        """Resolve upcoming YouTube tracks in the background so track changes don't wait for yt-dlp."""
        todo = [t for t in tracks[:25] if t["input"].startswith("yt:")]
        def run():
            for t in todo:
                try:
                    self.track_input(t)
                except Exception:
                    pass
        if todo:
            threading.Thread(target=run, daemon=True).start()

    async def playable(self, src):
        """Source descriptor -> Playable. Plex albums become a queue of tracks."""
        if src.get("type") != "plex_album":
            return await asyncio.to_thread(self.catalog.playable, src)
        if not self.plex:
            raise ValueError("Plex is not configured")
        album = await self.plex.album(src.get("id", ""))
        tracks = Plex.order(album["tracks"], int(src.get("start") or 0), bool(src.get("shuffle")))
        items = [{"id": t["id"], "title": t["title"], "artist": t["artist"], "album": t["album"],
                  "duration": t["duration"], "path": f"/plex/track/{t['id']}",
                  "art": f"/plex/thumb/{t['album_id']}" if t["thumb"] else None,
                  "ctype": DIRECT.get(t["codec"], "audio/mpeg"), "plex": True,
                  "input": self.plex.part_url(t)}       # has the token: server-side only
                 for t in tracks]
        return Playable(f"{album['artist']} – {album['title']}", "queue", tracks=items)


PUBLIC_TRACK = ("id", "title", "artist", "album", "duration", "path", "art", "plex")


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
@routes.get("/plex")
async def index(_):
    # One document for both views, so "This device" keeps playing while you switch.
    return web.FileResponse(HERE / "static" / "index.html", headers={"Cache-Control": "no-cache"})


@routes.get("/api/catalog")
async def api_catalog(_):
    c = ctx.catalog
    nets, favs = await asyncio.gather(asyncio.to_thread(c.networks), asyncio.to_thread(c.favorites))
    return web.json_response({"networks": nets, "favorites": favs,
                              "builtin": [{"id": b["id"], "name": b["name"]} for b in BUILTIN],
                              "saved": [{k: s[k] for k in ("id", "name", "kind", "url")} for s in c.saved()],
                              "plex": ctx.plex is not None})


@routes.get("/api/state")
async def api_state(_):
    sps = sorted(discovery.speakers.values(), key=lambda s: (not s.online, s.name.lower()))
    infos = [s.info() for s in sps]

    async def track(sp, info):
        if sp.status == "playing" and sp.playable and sp.playable.kind != "queue":
            info["track"] = await asyncio.to_thread(ctx.catalog.now_playing, sp.playable)
    await asyncio.gather(*(track(s, i) for s, i in zip(sps, infos)))
    return web.json_response({"speakers": infos})


@routes.post("/api/speakers/{sid}/play")
async def api_play(req):
    sp = speaker(req)
    src = (await req.json()).get("source") or {}
    try:
        p = await ctx.playable(src)
    except Exception as e:
        return bad(str(e))
    if p.kind == "queue":
        ctx.prefetch(p.tracks)
    await asyncio.to_thread(sp.play, p, src)
    ctx._save("last", sp.id, src)
    return web.json_response(sp.info())


@routes.post("/api/speakers/{sid}/stop")
async def api_stop(req):
    sp = speaker(req)
    await asyncio.to_thread(sp.stop)
    return web.json_response(sp.info())


@routes.post("/api/speakers/{sid}/control")
async def api_control(req):
    """Queue controls: next / prev / toggle (pause-resume)."""
    sp = speaker(req)
    action = (await req.json()).get("action")
    if action not in ("next", "prev", "toggle"):
        return bad("unknown action")
    try:
        await asyncio.to_thread(sp.control, action)
    except Exception as e:
        return bad(str(e))
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
        p = await ctx.playable(src)
    except Exception as e:
        return bad(str(e))
    if p.kind == "queue":
        ctx.prefetch(p.tracks)
        return web.json_response({"name": p.name, "direct": [],
                                  "queue": [{k: t[k] for k in PUBLIC_TRACK} for t in p.tracks]})
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
    return await mp3_response(req, url, ua)


@routes.get("/yt/track/{vid}")
async def yt_track(req):
    """One YouTube video as MP3 (YouTube playlists on Cast speakers and browsers)."""
    vid = req.match_info["vid"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
        raise web.HTTPNotFound()
    try:
        url, ua = await asyncio.to_thread(ctx.track_input, {"input": f"yt:{vid}"})
    except Exception as e:
        raise web.HTTPBadGateway(text=str(e))
    return await mp3_response(req, url, ua)


# ---------- Plex: this app is the middle man; the token never leaves the server ----------

def need_plex():
    if not ctx.plex:
        raise web.HTTPNotFound(text="Plex is not configured")
    return ctx.plex


@routes.get("/api/plex/albums")
async def api_plex_albums(_):
    try:
        return web.json_response(await need_plex().albums())
    except web.HTTPException:
        raise
    except Exception as e:
        return bad(f"Plex: {e}")


@routes.get("/api/plex/albums/{id}")
async def api_plex_album(req):
    try:
        a = await need_plex().album(req.match_info["id"])
    except web.HTTPException:
        raise
    except Exception as e:
        return bad(f"Plex: {e}")
    a["tracks"] = [{k: t[k] for k in ("id", "title", "artist", "index", "disc", "duration")} for t in a["tracks"]]
    return web.json_response(a)


@routes.get("/plex/thumb/{id}")
async def plex_thumb(req):
    plex = need_plex()
    try:
        size = max(64, min(1024, int(req.query.get("s", "300"))))
        url = plex.thumb_url(req.match_info["id"], size)
    except ValueError:
        raise web.HTTPNotFound()
    s = await plex.session()
    async with s.get(url) as up:
        if up.status != 200:
            raise web.HTTPNotFound()
        return web.Response(body=await up.read(), content_type=up.content_type,
                            headers={"Cache-Control": "public, max-age=604800"})


@routes.get("/plex/track/{id}")
async def plex_track(req):
    """Track audio for speakers and browsers. Direct codecs pass through with Range
    (seeking, Cast's partial requests); others (Musepack, MP2...) are transcoded to MP3."""
    plex = need_plex()
    try:
        tr = await plex.track(req.match_info["id"])
    except Exception:
        raise web.HTTPNotFound()
    if tr["codec"] not in DIRECT:
        return await mp3_response(req, plex.part_url(tr), None)
    s = await plex.session()
    fwd = {"Range": req.headers["Range"]} if "Range" in req.headers else {}
    async with s.get(plex.url + tr["part"], headers=fwd) as up:
        if up.status not in (200, 206):
            raise web.HTTPBadGateway(text=f"Plex answered {up.status}")
        resp = web.StreamResponse(status=up.status)
        for h in ("Content-Length", "Content-Range"):
            if h in up.headers:
                resp.headers[h] = up.headers[h]
        resp.headers["Accept-Ranges"] = "bytes"
        resp.content_type = DIRECT[tr["codec"]]
        await resp.prepare(req)
        if req.method != "HEAD":
            try:
                async for chunk in up.content.iter_chunked(65536):
                    await resp.write(chunk)
            except ConnectionError:
                pass
        return resp


async def mp3_response(req, url, ua):
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


# ---------- play history to Plex ----------

class PlexReporter:
    """Every 10 s: report the Plex track each player is on. A track change or stop sends
    'stopped' with the last position; within 15 s of the end counts as fully played
    (sampling every 10 s would otherwise miss Plex's 90 % mark on short tracks)."""
    EVERY, NEAR_END, BROWSER_STALE = 10, 15, 25

    def __init__(self):
        self.last = {}       # player id -> {"t": track, "pos": s, "client", "device"}
        self.browser = {}    # browser client id -> {"t", "pos", "state", "seen"}

    def players(self):
        for sp in list(discovery.speakers.values()):
            pr = sp.progress()
            yield sp.id, f"home-radio-{sp.id.replace(':', '-')}", sp.name, pr
        now = time.time()
        for cid, b in list(self.browser.items()):
            if now - b["seen"] > self.BROWSER_STALE:
                del self.browser[cid]
                pr = None
            else:
                pr = (b["t"], b["pos"], b["state"])
            yield f"browser:{cid}", f"home-radio-browser-{cid}", "Home Radio (browser)", pr

    async def tick(self):
        seen = set()
        for pid, client, device, pr in self.players():
            seen.add(pid)
            prev = self.last.get(pid)
            cur_t = pr[0] if pr else None
            if prev and (cur_t is None or cur_t["id"] != prev["t"]["id"]):
                await self._stopped(prev)
                self.last.pop(pid, None)
            if pr:
                t, pos, state = pr
                await ctx.plex.timeline(client, device, t["id"], state, pos, t["duration"])
                self.last[pid] = {"t": t, "pos": pos, "client": client, "device": device}
        for pid in [p for p in self.last if p not in seen]:     # speaker vanished
            await self._stopped(self.last.pop(pid))

    async def _stopped(self, prev):
        t, pos = prev["t"], prev["pos"]
        if t["duration"] and t["duration"] - pos <= self.NEAR_END:
            pos = t["duration"]
        await ctx.plex.timeline(prev["client"], prev["device"], t["id"], "stopped", pos, t["duration"])

    async def run(self, _app):
        async def loop():
            while True:
                await asyncio.sleep(self.EVERY)
                try:
                    await self.tick()
                except Exception as e:
                    log.warning("plex report: %s", e)
        task = asyncio.create_task(loop())
        yield
        task.cancel()


reporter = PlexReporter()


@routes.post("/api/plex/progress")
async def api_plex_progress(req):
    """'This device' reports its Plex track position (the server can't see the browser)."""
    b = await req.json()
    cid = re.sub(r"[^A-Za-z0-9]", "", str(b.get("client", "")))[:32]
    if not cid or not ctx.plex:
        return bad("bad report")
    try:
        t = await ctx.plex.track(b.get("track_id", ""))
    except Exception:
        return bad("unknown track")
    reporter.browser[cid] = {"t": {"id": t["id"], "duration": t["duration"]},
                             "pos": float(b.get("time") or 0),
                             "state": "paused" if b.get("state") == "paused" else "playing",
                             "seen": time.time()}
    return web.json_response({"ok": True})


def main():
    global discovery
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    discovery = Discovery(ctx)   # before the event loop exists: zeroconf gets its own thread
    app = web.Application()
    app.add_routes(routes)
    if ctx.plex:
        app.cleanup_ctx.append(reporter.run)
    web.run_app(app, port=PORT, access_log=None, print=lambda *_: log.info("listening on :%s", PORT))


if __name__ == "__main__":
    main()
