"""Plex music library: album list, album tracks, and the token-holding proxy.

The Plex token is admin-level for the whole server, so it never leaves this
process: speakers and browsers only ever get /plex/... URLs on this app, and
clients only ever send ratingKeys (never Plex paths).
"""
import asyncio
import random
import time

import aiohttp

# Codecs that Cast speakers and browsers play as-is; everything else -> ffmpeg -> MP3.
DIRECT = {"mp3": "audio/mpeg", "flac": "audio/flac", "aac": "audio/mp4"}
ALBUM_TTL = 600


class Plex:
    def __init__(self, url, token):
        self.url, self.token = url.rstrip("/"), token
        self._session = None
        self._albums = (0, None)          # (fetched_at, slim list)
        self._section = None
        self._tracks = {}                 # ratingKey -> track dict (filled by album lookups)
        self._lock = asyncio.Lock()

    async def session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Accept": "application/json", "X-Plex-Token": self.token,
                         "X-Plex-Client-Identifier": "home-radio", "X-Plex-Product": "Home Radio"},
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=60))
        return self._session

    async def get(self, path):
        s = await self.session()
        async with s.get(self.url + path) as r:
            r.raise_for_status()
            return (await r.json())["MediaContainer"]

    async def music_section(self):
        if self._section is None:
            secs = (await self.get("/library/sections"))["Directory"]
            self._section = next(d["key"] for d in secs if d["type"] == "artist")
        return self._section

    # ---------- albums ----------
    async def albums(self):
        """Slim list of all albums (sent once to the page, filtered there)."""
        async with self._lock:
            at, data = self._albums
            if data is not None and time.time() - at < ALBUM_TTL:
                return data
            sec = await self.music_section()
            raw = (await self.get(f"/library/sections/{sec}/all?type=9&includeCollections=0")).get("Metadata", [])
            data = [{"id": a["ratingKey"], "title": a.get("title", ""), "artist": a.get("parentTitle", ""),
                     "year": a.get("year"), "added": a.get("addedAt", 0), "thumb": bool(a.get("thumb"))}
                    for a in raw]
            self._albums = (time.time(), data)
            return data

    async def album(self, rid):
        """Album header + tracks; also remembers each track for the proxy."""
        rid = str(int(rid))     # ratingKeys are numeric; anything else is rejected here
        meta = (await self.get(f"/library/metadata/{rid}")).get("Metadata", [{}])[0]
        if meta.get("type") != "album":
            raise ValueError("not an album")
        tracks = []
        for t in (await self.get(f"/library/metadata/{rid}/children")).get("Metadata", []):
            media = (t.get("Media") or [{}])[0]
            part = (media.get("Part") or [{}])[0]
            if not part.get("key"):
                continue
            tr = {"id": t["ratingKey"], "title": t.get("title", ""),
                  "artist": t.get("originalTitle") or t.get("grandparentTitle", ""),
                  "album": meta.get("title", ""), "album_id": rid,
                  "index": t.get("index"), "disc": t.get("parentIndex"),
                  "duration": (t.get("duration") or 0) / 1000,
                  "codec": (media.get("audioCodec") or "").lower(), "part": part["key"],
                  "thumb": bool(t.get("parentThumb") or meta.get("thumb"))}
            self._tracks[tr["id"]] = tr
            tracks.append(tr)
        return {"id": rid, "title": meta.get("title", ""), "artist": meta.get("parentTitle", ""),
                "year": meta.get("year"), "thumb": bool(meta.get("thumb")), "tracks": tracks}

    async def track(self, tid):
        tid = str(int(tid))
        tr = self._tracks.get(tid)
        if tr is None:          # e.g. after a restart, a Cast speaker still has the URL queued
            t = (await self.get(f"/library/metadata/{tid}")).get("Metadata", [{}])[0]
            if t.get("type") != "track":
                raise ValueError("not a track")
            await self.album(t["parentRatingKey"])
            tr = self._tracks[tid]
        return tr

    @staticmethod
    def order(tracks, start=0, shuffle=False):
        tracks = list(tracks)
        if shuffle:
            random.shuffle(tracks)
            return tracks
        return tracks[start:]

    # ---------- play reporting ----------
    async def timeline(self, client, device, tid, state, pos, duration):
        """Tell Plex what a player is doing. Plex shows it under "Now Playing" and, on 'stopped'
        at >= 90 %, counts the play itself (play count, history, Plexamp stats)."""
        s = await self.session()
        params = {"ratingKey": tid, "key": f"/library/metadata/{tid}", "state": state,
                  "time": int(pos * 1000), "duration": int(duration * 1000),
                  "identifier": "com.plexapp.plugins.library"}
        hdr = {"X-Plex-Client-Identifier": client, "X-Plex-Device-Name": device,
               "X-Plex-Platform": "Home Radio", "X-Plex-Device": "Speaker"}
        async with s.get(self.url + "/:/timeline", params=params, headers=hdr) as r:
            return r.status

    # ---------- proxy ----------
    def part_url(self, tr):
        """Direct Plex file URL with token - only ever used inside this process (ffmpeg input)."""
        return f"{self.url}{tr['part']}?X-Plex-Token={self.token}"

    def thumb_url(self, rid, size):
        inner = f"/library/metadata/{int(rid)}/thumb"
        return (f"{self.url}/photo/:/transcode?width={size}&height={size}&minSize=1&upscale=1"
                f"&url={inner}&X-Plex-Token={self.token}")
