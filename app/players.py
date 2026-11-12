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
        self.status, self.error = "idle", None   # idle|connecting|playing|paused|retrying|error|other
        self.source = self.playable = None
        self.volume = 50

    def info(self):
        d = {"id": self.id, "name": self.name, "kind": self.kind, "online": self.online,
             "status": self.status, "error": self.error, "volume": self.volume,
             "source": self.source, "title": self.playable.name if self.playable else None}
        if self.playable and self.playable.kind == "queue" and self.status in ("playing", "paused", "retrying"):
            d["controls"] = True
            d.update(self.queue_info())
        return d

    def queue_info(self):
        return {}

    def progress(self):
        """(track, position s, 'playing'|'paused') for a Plex track being played, else None."""
        return None

    def _plex_track(self, i):
        p = self.playable
        if not (p and p.kind == "queue" and self.status in ("playing", "paused")):
            return None
        t = p.tracks[min(i, len(p.tracks) - 1)]
        return t if t.get("plex") else None

    def control(self, action):
        raise ValueError("next/previous/pause work for albums only")

    def _set_idle(self):
        self.status, self.error, self.source, self.playable = "idle", None, None, None


def track_text(t):
    return f"{t['artist']} - {t['title']}" if t.get("artist") else t.get("title", "")


def queue_entry(tracks, i):
    t = tracks[min(i, len(tracks) - 1)]
    return {"track": track_text(t), "art": t.get("art"), "queue": {"index": i + 1, "length": len(tracks)}}


# ---------------------------------------------------------------- Cast

class CastSpeaker(Speaker):
    kind = "cast"

    def __init__(self, sid, name, cast_info, zconf, ctx):
        super().__init__(sid, name, ctx)
        self.cast_info, self.zconf = cast_info, zconf
        self.cc = None
        self._gen = 0
        self._idle_since = None
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
        self._idle_since = None
        threading.Thread(target=self._play, args=(playable, self._gen), daemon=True).start()

    def _cast(self, cc, url, ctype, title, timeout=15, stream_type="LIVE", metadata=None):
        mc = cc.media_controller
        mc.play_media(url, ctype, title=title, stream_type=stream_type, metadata=metadata)
        mc.block_until_active(timeout=15)
        end = time.time() + timeout
        while time.time() < end:
            mc.update_status()
            time.sleep(1)
            if mc.status.player_state == "PLAYING":
                break
        return mc.status.player_state

    @staticmethod
    def _music_meta(t, base):
        m = {"metadataType": 3, "title": t["title"], "artist": t["artist"], "albumName": t["album"]}
        if t.get("art"):
            m["images"] = [{"url": f"{base}{t['art']}?s=512"}]   # the Smart Clock shows the cover
        return m

    def _play(self, p, gen):
        try:
            cc = self._conn()
            if p.kind == "queue":
                return self._play_queue(cc, p, gen)
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

    def _play_queue(self, cc, p, gen):
        """Album: first track via LOAD, the rest via QUEUE_INSERT. The speaker then walks
        the queue by itself (it fetches each track from our /plex/track proxy)."""
        base = self.ctx.base_url(cc.cast_info.host)
        first, rest = p.tracks[0], p.tracks[1:]
        st = self._cast(cc, base + first["path"], first["ctype"], None, stream_type="BUFFERED",
                        metadata=self._music_meta(first, base))
        if st != "PLAYING":
            raise RuntimeError(f"speaker state {st}")
        self._ok(gen)
        mc = cc.media_controller
        for t in rest:
            if gen != self._gen:
                return
            mc.play_media(base + t["path"], t["ctype"], stream_type="BUFFERED",
                          metadata=self._music_meta(t, base), enqueue=True)

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

    def control(self, action):
        if not (self.playable and self.playable.kind == "queue"):
            return super().control(action)
        mc = self._conn().media_controller
        if action == "next":
            mc.queue_next()
        elif action == "prev":
            if (mc.status.adjusted_current_time or 0) > 3:
                mc.seek(0)
            else:
                mc.queue_prev()
        elif mc.status.player_state == "PAUSED":
            mc.play()
        else:
            mc.pause()

    def _queue_index(self):
        ms = self.cc.media_controller.status if self.cc else None
        cid = (ms.content_id or "") if ms else ""
        return next((i for i, t in enumerate(self.playable.tracks) if cid.endswith(t["path"])), 0), ms

    def queue_info(self):
        return queue_entry(self.playable.tracks, self._queue_index()[0])

    def progress(self):
        if not (self.playable and self.playable.kind == "queue"):
            return None
        i, ms = self._queue_index()
        t = self._plex_track(i)
        if not t or ms is None:
            return None
        return t, ms.adjusted_current_time or 0, "paused" if ms.player_state == "PAUSED" else "playing"

    def info(self):
        cc = self.cc
        st = cc.status if cc else None
        if st is not None:
            self.volume = round((st.volume_level or 0) * 100)
            if self.status in ("playing", "paused", "other"):
                if not st.app_id or cc.is_idle:
                    self._set_idle()
                elif st.app_id != pychromecast.APP_MEDIA_RECEIVER:
                    self.status, self.playable = "other", None
                    self.error = st.display_name
                else:
                    ms = cc.media_controller.status
                    queue = self.playable is not None and self.playable.kind == "queue"
                    if ms.player_state == "IDLE" and ms.idle_reason in ("FINISHED", "ERROR"):
                        # between album tracks the speaker may report FINISHED for a moment
                        self._idle_since = self._idle_since or time.time()
                        if not queue or time.time() - self._idle_since > 6:
                            self._set_idle()
                    else:
                        self._idle_since = None
                        if queue and self.status in ("playing", "paused"):
                            self.status = "paused" if ms.player_state == "PAUSED" else "playing"
            elif self.status == "idle" and st.app_id and not cc.is_idle:
                self.status, self.error = "other", st.display_name   # started from a phone / difm CLI
        return super().info()


# ---------------------------------------------------------------- AirPlay

BYTES_PER_SEC = 44100 * 4     # s16le stereo
BUFFERED_S = 1.6              # cliraop latency (1.25 s) + pipe: what is written but not yet heard


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
    cliraop's own volume is fixed at `hw_volume`; the slider scales samples, so changes need no reconnect.
    An album is one cliraop session fed by one ffmpeg per track: gapless, no reconnects."""
    kind = "airplay"

    def __init__(self, sid, name, host, port, ctx):
        super().__init__(sid, name, ctx)
        self.host, self.port = host, port
        self.volume = ctx.saved_volume(sid, 40)
        self.log = collections.deque(maxlen=60)
        self._stop_ev = threading.Event()
        self._thread = None
        self._procs = []
        self._ff = None
        self._written = 0          # bytes of the current track sent to cliraop
        self._jump = None          # queue index requested by next/prev
        self.q_index, self.q_offset = 0, 0.0

    # --- lifecycle ---
    def play(self, playable, source):
        self.stop()
        self.source, self.playable = source, playable
        self.q_index, self.q_offset = 0, 0.0
        self._start()

    def _start(self):
        self.status, self.error = "connecting", None
        self._stop_ev = ev = threading.Event()
        self._thread = threading.Thread(target=self._supervise, args=(self.playable, ev), daemon=True)
        self._thread.start()

    def _halt(self):
        self._stop_ev.set()
        self._kill()
        if self._thread:
            self._thread.join(timeout=8)
            self._thread = None

    def stop(self):
        self._halt()
        self._set_idle()

    def set_volume(self, v):
        self.volume = v     # picked up by the pump on the next chunk (~0.1 s), heard after cliraop's buffer
        self.ctx.save_volume(self.id, v)

    def _kill(self):
        for p in self._procs:
            if p.poll() is None:
                p.kill()

    # --- album controls ---
    def _position(self):
        """Seconds into the current track as heard."""
        return max(0.0, self.q_offset + self._written / BYTES_PER_SEC - BUFFERED_S)

    def control(self, action):
        p = self.playable
        if not (p and p.kind == "queue"):
            return super().control(action)
        if action == "toggle":
            if self.status == "paused":
                return self._start()                 # resumes at q_index / q_offset
            pos = self._position()
            self._halt()
            self.q_offset, self.status, self.error = pos, "paused", None
            return
        target = self.q_index + 1 if action == "next" else \
            (self.q_index if self._position() > 3 else max(0, self.q_index - 1))
        if target >= len(p.tracks):
            return self.stop()
        if self.status == "paused":
            self.q_index, self.q_offset = target, 0.0
            return self._start()
        self._jump = target
        ff = self._ff
        if ff and ff.poll() is None:
            ff.kill()                                # the feeder sees EOF and moves to the target

    def queue_info(self):
        return queue_entry(self.playable.tracks, self.q_index)

    def progress(self):
        t = self._plex_track(self.q_index)
        if not t:
            return None
        pos = self.q_offset if self.status == "paused" else self._position()
        return t, pos, self.status

    # --- supervision ---
    def _supervise(self, p, ev):
        attempt = 0
        while not ev.is_set():
            started = time.time()
            try:
                reason = self._session(p, ev, attempt)
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

    def _session(self, p, ev, attempt):
        """One cliraop connection. Returns 'finished' or a reason to retry."""
        if p.kind != "queue":
            src, ua, live = self.ctx.input_for(p, attempt)   # resolve first (yt-dlp can take seconds)
        cl_cmd = [self.ctx.cliraop, "-p", str(self.port), "-v", str(self.ctx.hw_volume), "-d", "3", self.host, "-"]
        cl = subprocess.Popen(cl_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self._procs = [cl]
        st = {"connected": False, "played": 0.0, "start": time.time(), "why": None, "done": False}
        if ev.is_set():          # stop() raced with startup
            self._kill()

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

        threading.Thread(target=read_cl, daemon=True).start()
        threading.Thread(target=watchdog, daemon=True).start()
        try:
            if p.kind == "queue":
                outcome = self._feed_queue(p, cl, st, ev)
            else:
                rc, broken = self._feed(src, ua, cl)
                outcome = "eof" if rc == 0 and not live and not broken else \
                    f"stream ended ({rc}) " + next((l for l in reversed(self.log) if l.startswith("ffmpeg:")), "")
            if outcome == "eof" and st["why"] is None and not ev.is_set():
                try:                 # patched cliraop plays out its buffer, disconnects and exits on EOF
                    cl.stdin.close()
                    cl.wait(timeout=10)
                except Exception:
                    pass
                return "finished"
            return st["why"] or outcome.strip()
        finally:
            st["done"] = True
            self._kill()
            cl.wait()

    def _feed(self, src, ua, cl, seek=0.0):
        """Decode one input into cliraop's stdin. -> (ffmpeg exit code, pipe broke)."""
        ff_cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning"]
        if src.startswith("http"):
            ff_cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10"]
        if ua:
            ff_cmd += ["-user_agent", ua]
        if seek > 0.5:
            ff_cmd += ["-ss", f"{seek:.2f}"]
        # No -re: cliraop paces the pipe itself (reads only when the speaker takes more), and the
        # station's initial burst pre-fills the speaker buffer. -re made data trickle in at start.
        ff_cmd += ["-i", src, "-vn", "-f", "s16le", "-ar", "44100", "-ac", "2", "pipe:1"]
        ff = subprocess.Popen(ff_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._ff = ff
        self._procs = [cl, ff]

        def read_ff():
            for line in ff.stderr:
                self.log.append("ffmpeg: " + line.decode(errors="replace").rstrip())
        threading.Thread(target=read_ff, daemon=True).start()

        self._written, rem, broken = 0, b"", False   # rem is per input: never carry a partial frame over
        try:
            while True:
                data = ff.stdout.read1(16384)
                if not data:
                    break
                data = rem + data
                n = len(data) & ~3      # whole stereo frames (2 ch x 16 bit)
                rem = data[n:]
                cl.stdin.write(scale(data[:n], self.volume))
                self._written += n
        except (BrokenPipeError, OSError, ValueError):
            broken = True
        return ff.wait(), broken

    def _feed_queue(self, p, cl, st, ev):
        tracks = p.tracks
        while self.q_index < len(tracks) and not ev.is_set():
            t = tracks[self.q_index]
            self._jump = None
            self.ctx.prefetch(tracks[self.q_index + 1:self.q_index + 3])
            try:
                src, ua = self.ctx.track_input(t)
            except Exception as e:                   # e.g. a YouTube video that went private
                self.log.append(f"track skipped ({e}): {t['title']}")
                self.q_index, self.q_offset = self.q_index + 1, 0.0
                continue
            rc, broken = self._feed(src, ua, cl, seek=self.q_offset)
            if ev.is_set():
                return "stopped"
            if self._jump is not None:               # next / prev
                self.q_index, self.q_offset = self._jump, 0.0
                continue
            if broken or st["why"]:
                self.q_offset = self._position()     # reconnect resumes here
                return st["why"] or "speaker connection lost"
            if rc != 0:
                self.log.append(f"track failed ({rc}): {t['title']}")
            self.q_index, self.q_offset = self.q_index + 1, 0.0
        return "eof"


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
