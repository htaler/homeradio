"""Speakers: Google Cast (pychromecast) and AirPlay (ffmpeg -> volume -> cliraop).

Threads, not asyncio: pychromecast and the PCM pump are blocking by nature, and a
hiccup on the web event loop must never underrun the audio.
"""
import array
import collections
import logging
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pychromecast
from zeroconf import IPVersion, ServiceBrowser, ServiceStateChange, Zeroconf

log = logging.getLogger("players")
RAOP = "_raop._tcp.local."


class Speaker:
    kind = ""

    def __init__(self, sid, name, ctx):
        self.id, self.name, self.ctx = sid, name, ctx
        self.online = True
        self.status, self.error = "idle", None   # idle | connecting | playing | retrying | error | other
        self.source = self.playable = None
        self.volume = 50

    def info(self):
        return {"id": self.id, "name": self.name, "kind": self.kind, "online": self.online,
                "status": self.status, "error": self.error, "volume": self.volume,
                "source": self.source, "title": self.playable.name if self.playable else None}

    def _set_idle(self):
        self.status, self.error, self.source, self.playable = "idle", None, None, None


# ---------------------------------------------------------------- Cast

class CastSpeaker(Speaker):
    kind = "cast"

    def __init__(self, sid, name, cast_info, zconf, ctx):
        super().__init__(sid, name, ctx)
        self.cast_info, self.zconf = cast_info, zconf
        self.cc = None
        self._gen = 0
        self._lock = threading.Lock()
        threading.Thread(target=self._conn, daemon=True).start()

    def _conn(self):
        # One persistent connection. NEVER cc.disconnect(): it sends CLOSE and the
        # Smart Clock stops the stream. tries=None -> pychromecast reconnects forever.
        with self._lock:
            if self.cc is None:
                cc = pychromecast.get_chromecast_from_cast_info(self.cast_info, self.zconf)
                cc.wait(timeout=10)
                self.cc = cc
            return self.cc

    def play(self, playable, source):
        self._gen += 1
        self.source, self.playable, self.status, self.error = source, playable, "connecting", None
        threading.Thread(target=self._play, args=(playable, self._gen), daemon=True).start()

    def _cast(self, cc, url, ctype, title, timeout=15):
        mc = cc.media_controller
        mc.play_media(url, ctype, title=title, stream_type="LIVE")
        mc.block_until_active(timeout=15)
        end = time.time() + timeout
        while time.time() < end:
            mc.update_status()
            time.sleep(1)
            if mc.status.player_state == "PLAYING":
                break
        return mc.status.player_state

    def _play(self, p, gen):
        try:
            cc = self._conn()
            problems = []
            if p.kind == "radio":   # hand the stream straight to the speaker; servers in order
                from catalog import resolve_url
                for u in p.urls:
                    try:
                        url, ctype, _, _ = resolve_url(u)
                    except Exception as e:
                        problems.append(str(e))
                        continue
                    if gen != self._gen:
                        return
                    st = self._cast(cc, url, ctype, p.name)
                    if st == "PLAYING":
                        return self._ok(gen)
                    problems.append(f"speaker state {st}")
            # YouTube & co., or a stream the speaker refused: transcode on the NAS to MP3.
            if gen != self._gen:
                return
            url = self.ctx.proxy_url(p, cc.cast_info.host)
            st = self._cast(cc, url, "audio/mpeg", p.name, timeout=30)
            if st == "PLAYING":
                return self._ok(gen)
            problems.append(f"speaker state {st}")
            raise RuntimeError("; ".join(problems))
        except Exception as e:
            log.exception("cast play failed")
            if gen == self._gen:
                self.status, self.error = "error", str(e)

    def _ok(self, gen):
        if gen == self._gen:
            self.status, self.error = "playing", None

    def stop(self):
        self._gen += 1
        self._set_idle()
        if self.cc:
            self.cc.quit_app()

    def set_volume(self, v):
        self.volume = v
        self._conn().set_volume(v / 100)

    def info(self):
        cc = self.cc
        st = cc.status if cc else None
        if st is not None:
            self.volume = round((st.volume_level or 0) * 100)
            if self.status in ("playing", "other"):
                if not st.app_id or cc.is_idle:
                    self._set_idle()
                elif st.app_id != pychromecast.APP_MEDIA_RECEIVER:
                    self.status, self.playable = "other", None
                    self.error = st.display_name
                else:
                    ms = cc.media_controller.status
                    if ms.player_state == "IDLE" and ms.idle_reason in ("FINISHED", "ERROR"):
                        self._set_idle()
            elif self.status == "idle" and st.app_id and not cc.is_idle:
                self.status, self.error = "other", st.display_name   # started from a phone / difm CLI
        return super().info()


# ---------------------------------------------------------------- AirPlay

def scale(pcm: bytes, volume: int) -> bytes:
    """Software volume on s16le PCM. Squared curve ~ perceived loudness."""
    if volume >= 100:
        return pcm
    f = (volume / 100) ** 2
    a = array.array("h")
    a.frombytes(pcm)
    return array.array("h", [int(s * f) for s in a]).tobytes()


class AirPlaySpeaker(Speaker):
    """Plain AirPlay 1 via cliraop (the Belkin plays silence on AirPlay 2 from non-Apple senders).
    cliraop's own volume is fixed at `hw_volume`; the slider scales samples, so changes need no reconnect."""
    kind = "airplay"

    def __init__(self, sid, name, host, port, ctx):
        super().__init__(sid, name, ctx)
        self.host, self.port = host, port
        self.volume = ctx.saved_volume(sid, 40)
        self.log = collections.deque(maxlen=60)
        self._stop_ev = threading.Event()
        self._thread = None
        self._procs = []

    def play(self, playable, source):
        self.stop()
        self.source, self.playable, self.status, self.error = source, playable, "connecting", None
        self._stop_ev = ev = threading.Event()
        self._thread = threading.Thread(target=self._supervise, args=(playable, ev), daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_ev.set()
        self._kill()
        if self._thread:
            self._thread.join(timeout=8)
            self._thread = None
        self._set_idle()

    def set_volume(self, v):
        self.volume = v     # picked up by the pump on the next chunk (~0.1 s), heard after cliraop's buffer
        self.ctx.save_volume(self.id, v)

    def _kill(self):
        for p in self._procs:
            if p.poll() is None:
                p.kill()

    def _supervise(self, p, ev):
        attempt = 0
        while not ev.is_set():
            started = time.time()
            try:
                src, ua, live = self.ctx.input_for(p, attempt)
                reason = self._run_once(src, ua, live, ev)
            except Exception as e:
                reason = f"source: {e}"
            if ev.is_set():
                return
            if reason == "finished":
                self._set_idle()
                return
            attempt = 1 if time.time() - started > 60 else attempt + 1
            delay = min(5 * attempt, 30)
            self.status, self.error = "retrying", f"{reason} - retry in {delay}s"
            self.log.append(f"{time.strftime('%H:%M:%S')} {reason}")
            log.warning("%s: %s", self.name, reason)
            ev.wait(delay)

    def _run_once(self, src, ua, live, ev):
        ff_cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning"]
        if src.startswith("http"):
            ff_cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10"]
        if ua:
            ff_cmd += ["-user_agent", ua]
        # No -re: cliraop paces the pipe itself (reads only when the speaker takes more), and the
        # station's initial burst pre-fills the speaker buffer. -re made data trickle in at start.
        ff_cmd += ["-i", src, "-vn", "-f", "s16le", "-ar", "44100", "-ac", "2", "pipe:1"]
        cl_cmd = [self.ctx.cliraop, "-p", str(self.port), "-v", str(self.ctx.hw_volume), "-d", "3", self.host, "-"]
        ff = subprocess.Popen(ff_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        cl = subprocess.Popen(cl_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self._procs = [ff, cl]
        if ev.is_set():          # stop() raced with startup
            self._kill()
        st = {"connected": False, "played": 0.0, "start": time.time(), "why": None, "done": False}

        def read_ff():
            for line in ff.stderr:
                self.log.append("ffmpeg: " + line.decode(errors="replace").rstrip())

        def read_cl():
            for raw in cl.stdout:
                line = raw.decode(errors="replace").rstrip()
                if "connected to" in line:
                    st["connected"], st["played"] = True, time.time()
                    if not ev.is_set():
                        self.status, self.error = "playing", None
                    self.log.append("cliraop: " + line)
                elif ", played " in line:
                    st["played"] = time.time()
                else:
                    self.log.append("cliraop: " + line)

        def watchdog():
            while not st["done"]:
                time.sleep(1)
                now = time.time()
                if cl.poll() is not None:
                    st["why"] = st["why"] or f"cliraop exited ({cl.returncode})"
                elif not st["connected"] and now - st["start"] > 20:
                    st["why"] = "speaker did not accept the connection"
                elif st["connected"] and now - st["played"] > 15:
                    st["why"] = "playback stalled"
                else:
                    continue
                self._kill()
                return

        for fn in (read_ff, read_cl, watchdog):
            threading.Thread(target=fn, daemon=True).start()

        rem = b""
        try:
            while True:
                data = ff.stdout.read1(16384)
                if not data:
                    break
                data = rem + data
                n = len(data) & ~3      # whole stereo frames (2 ch x 16 bit)
                rem = data[n:]
                cl.stdin.write(scale(data[:n], self.volume))
        except (BrokenPipeError, OSError, ValueError):
            pass
        ff_rc = ff.wait()
        if st["why"] is None and ff_rc == 0 and not live and not ev.is_set():
            # patched cliraop plays out its buffer and exits on EOF
            try:
                cl.stdin.close()
                cl.wait(timeout=10)
            except Exception:
                pass
            st["done"] = True
            self._kill()
            return "finished"
        st["done"] = True
        self._kill()
        cl.wait()
        if st["why"]:
            return st["why"]
        last = next((l for l in reversed(self.log) if l.startswith("ffmpeg:")), "")
        return f"stream ended ({ff_rc}) {last}".strip()


# ---------------------------------------------------------------- discovery

class Discovery:
    def __init__(self, ctx):
        self.ctx = ctx
        self.speakers = {}
        self.zconf = Zeroconf(ip_version=IPVersion.V4Only)
        self._pool = ThreadPoolExecutor(4)
        self.cast_browser = pychromecast.CastBrowser(
            pychromecast.SimpleCastListener(self._cast_seen, self._cast_gone, self._cast_seen), self.zconf)
        self.cast_browser.start_discovery()
        self.raop_browser = ServiceBrowser(self.zconf, RAOP, handlers=[self._raop_change])

    def _cast_seen(self, uuid, _service):
        info = self.cast_browser.devices.get(uuid)
        if not info or info.cast_type == "group":
            return
        sid = f"cast:{uuid}"
        sp = self.speakers.get(sid)
        if sp:
            sp.online, sp.cast_info = True, info
        else:
            self.speakers[sid] = CastSpeaker(sid, info.friendly_name, info, self.zconf, self.ctx)
            log.info("cast speaker: %s (%s, %s)", info.friendly_name, info.model_name, info.host)

    def _cast_gone(self, uuid, _service, _info):
        sp = self.speakers.get(f"cast:{uuid}")
        if sp:
            sp.online = False

    def _raop_change(self, zeroconf, service_type, name, state_change):
        sid = "airplay:" + name.split("@", 1)[0]
        if state_change is ServiceStateChange.Removed:
            if sid in self.speakers:
                self.speakers[sid].online = False
            return
        self._pool.submit(self._raop_resolve, name, sid)

    def _raop_resolve(self, name, sid):
        info = self.zconf.get_service_info(RAOP, name, timeout=3000)
        if not info or not info.parsed_addresses():
            return
        host, port = info.parsed_addresses()[0], info.port
        pretty = name.split("@", 1)[-1].removesuffix("." + RAOP)
        sp = self.speakers.get(sid)
        if sp:
            sp.online, sp.host, sp.port = True, host, port
        else:
            self.speakers[sid] = AirPlaySpeaker(sid, pretty, host, port, self.ctx)
            log.info("airplay speaker: %s (%s:%s)", pretty, host, port)
