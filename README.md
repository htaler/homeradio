# Home Radio

A small self-hosted web app that puts internet radio, your **Plex music library** and YouTube audio on the speakers in your house: **Google Cast** devices, **AirPlay** receivers, or the browser you're holding. One Docker container on a NAS or home server, one web page, no accounts, no cloud. Playback keeps going when your phone is off: the server is the player.

- **Speakers found automatically** over mDNS: Google Cast (Nest/Google speakers, smart displays, TVs) and AirPlay (RAOP) receivers.
- **AudioAddict premium**: one listen key unlocks all five networks (DI.FM, RadioTunes, Rock Radio, Classical Radio, Zen Radio) as a filterable channel dropdown, with your DI.FM favorites pinned on top (optional).
- **Built-in stations**: Radio Paradise (Main, Mellow, Rock, Global, Serenity), Radio 357, Radio Nowy Świat.
- **Plex albums** (optional): a separate Albums view (`/plex`) with your whole music library as a cover wall: search (diacritics-insensitive), sort, Play / Shuffle / play from a track, ⏮ ⏯ ⏭ on every speaker type. Built for libraries with thousands of albums.
- **Plex play history**: every player reports to Plex like a Plexamp client would. Tracks show under "Now Playing" in Plex, and play counts and history stay correct.
- **Paste any link**: a stream URL, `.pls`/`.m3u`, a YouTube video or live stream, a **YouTube / YouTube Music playlist or album** (plays as a queue), or anything else [yt-dlp](https://github.com/yt-dlp/yt-dlp) understands. Audio only, saved for one-tap replay.
- **Now playing**: track titles from the AudioAddict and Radio Paradise APIs, or ICY stream metadata.
- **"This device"**: play in the browser itself. Radio streams go straight from the station to the browser, and the server only steps in for YouTube and formats the browser can't play. Lock-screen controls via the Media Session API.
- Per-speaker volume and stop; several speakers can play different things at once.
- **Nothing is downloaded or stored**: audio is streamed and decoded in memory, and the server's data folder holds only settings and links (~100 KB).

## Why AirPlay goes through cliraop

Many AirPlay 2 receivers that aren't Apple devices (in my case a Belkin SOUNDFORM Connect adapter) accept a connection from open-source AirPlay 2 senders (pyatv, OwnTone) and then play **silence**. The same receivers play fine over plain **AirPlay 1 (RAOP)**. So the AirPlay path here is:

```
ffmpeg (any source → 44.1 kHz s16le PCM) → Python (software volume) → cliraop → receiver
```

[cliraop](https://github.com/philippe44/libraop) is built from source in the Dockerfile, from a pinned commit plus [`cliraop-home-radio.patch`](cliraop-home-radio.patch), which fixes three problems in its demo main loop:

1. **Partial pipe reads corrupt audio.** `read()` on a pipe can return a byte count that isn't a multiple of a frame. The remainder was dropped, which misaligned the 16-bit samples and produced loud noise, mostly in the first seconds of a stream.
2. **Busy-wait.** The loop polled the player with no sleep, so it used 100% of a CPU core for the whole stream. With the patch the container uses about 1–2% CPU while streaming.
3. **Never exits on EOF.** With the patch it plays out its buffer, disconnects and exits.
4. **`raopcl_disconnect` hangs** (the RTP threads are joined before they're told to stop). This is upstream [#61](https://github.com/philippe44/libraop/issues/61).

1 and 2 are reported upstream with a patch: [philippe44/libraop#63](https://github.com/philippe44/libraop/issues/63).

An album on AirPlay is **one** cliraop session: the server runs one ffmpeg per track into the same pipe, so tracks play **gapless** with no reconnects. Pause remembers the position and resumes with `ffmpeg -ss`.

Volume for AirPlay is applied in software, scaling the PCM samples, so moving the slider never reconnects the stream. You hear the change after the receiver's ~1.25 s buffer. cliraop's own volume is fixed at `airplay_max_volume`.

Google Cast speakers get the station's stream URL directly, as a phone would send it. YouTube, and anything a speaker refuses, is converted to MP3 by the server and served at `/stream/<token>.mp3`. Albums use the speaker's native queue, so it moves to the next track by itself.

## Plex

The server is the middle man between Plex and the speakers. **The Plex token never leaves the server:**
- speakers and browsers only get `/plex/track/<id>` and `/plex/thumb/<id>` URLs on this app;
- clients only send numeric `ratingKey`s, never Plex paths.

MP3, FLAC and AAC pass through untouched, with HTTP Range support for seeking. Other formats (Musepack, MP2…) are converted to MP3 on the fly. Covers Plex can't convert show a placeholder.

Play history: the server reports each player's Plex track to `/:/timeline` every 10 s ("Now Playing" in Plex), then sends `stopped` on a track change. Plex counts the play itself at ≥90%; a track left within 15 s of its end counts as played, so short tracks aren't missed between samples. Each speaker shows up in Plex as its own device. "This device" reports through the page.

## Running it

Requirements: Docker with Compose, on a Linux **x86_64** host on the same LAN as the speakers (the Dockerfile builds cliraop and fetches deno for x86_64). Host networking is required for mDNS discovery.

```sh
git clone https://github.com/htaler/homeradio.git
cd homeradio
mkdir data
cp config.example.json data/config.json   # optional, see below
chmod 600 data/config.json
docker compose up -d --build
```

Then open `http://<server-ip>:8780`; the albums are at `/plex`.

Building on a NAS with spinning disks is slow and loud. You can build the image on another machine and load it on the server instead:

```sh
docker buildx build --platform linux/amd64 -t home-radio:1 --load .
docker save home-radio:1 | gzip | ssh <server> 'gunzip | docker load'
# on the server, in the repo folder:
docker compose up -d --no-build
```

### Configuration: `data/config.json`

| key | meaning |
|---|---|
| `listen_key` | AudioAddict **premium** listen key (find it in your account settings on di.fm). Leave it out to hide the five networks; everything else works. |
| `airplay_max_volume` | cliraop volume, 0–100 (maps to −30…0 dB on the receiver). The page's slider scales below this. Default 80. |
| `plex.url` | Plex Media Server address as seen from the container. With host networking on the same machine: `http://127.0.0.1:32400`. |
| `plex.token` | Your Plex token. Leave the `plex` block out to hide the Albums view. |

**Getting the Plex token:** in Plex Web, open any item → ⋯ → *Get Info* → *View XML*. The token is the `X-Plex-Token=` value in the address bar. On the Plex server machine it's also `PlexOnlineToken` in `Preferences.xml`.

⚠️ **The Plex token is admin-level access to your whole Plex server**, not just music. Keep `data/config.json` at `chmod 600`, and never put the server on the internet.

Optional: put a DI.FM playlist export as `data/favorites.pls` to pin those channels at the top of the DI.FM list.

`data/` also holds `stations.json` (your pasted links), `state.json` (per-speaker volume and last station) and a weekly channel-list cache.

### Local development (macOS/Linux)

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# needs ffmpeg and yt-dlp's JS runtime (deno) on PATH, and a cliraop binary for AirPlay
CLIRAOP=/path/to/cliraop PATH="$PWD/.venv/bin:$PATH" .venv/bin/python app/app.py
```

Environment variables: `DATA` (default `./data`), `PORT` (default `8780`), `CLIRAOP` (default `cliraop`).

## Notes and limitations

- **No authentication.** Anyone on your LAN can use it. Don't expose it to the internet. With "This device", the browser receives AudioAddict stream URLs, which contain your listen key.
- **YouTube breaks from time to time.** The fix is almost always a newer yt-dlp: bump it in `requirements.txt` and rebuild. Public albums and playlists (including YouTube Music) work without logging in. Private playlists would need account cookies, which this app doesn't use. Note that YouTube's terms only allow playback through its own player.
- **AirPlay receivers that require pairing or a password** (Apple TV, a Mac's AirPlay Receiver set to "Current User") are listed but won't play through cliraop.
- **Playback state is in memory.** A container restart stops playback; nothing resumes automatically.
- **Google Cast:** the server never calls `disconnect()` on a Cast device. Some (e.g. the Lenovo Smart Clock) stop the stream when a sender closes its connection.

## How it's built

One Python process ([aiohttp](https://docs.aiohttp.org/)), no workers or queues:

- `app/app.py`: HTTP API, the MP3 transcoding endpoints, browser playback endpoints, the Plex play reporter.
- `app/catalog.py`: AudioAddict channel lists, built-in and saved stations, URL resolution (`.pls`/`.m3u`/HLS/yt-dlp, YouTube playlists), now-playing lookups.
- `app/plex.py`: Plex library (album list, tracks), the token-holding proxy, play reporting.
- `app/players.py`: mDNS discovery (zeroconf), Cast speakers ([pychromecast](https://github.com/home-assistant-libs/pychromecast), one persistent connection each, native queue for albums), AirPlay speakers (ffmpeg → volume → cliraop, gapless album queue, with a watchdog that restarts on stalls and backs off).
- `app/static/index.html`: the whole UI (Radio and Albums views in one document, so browser playback survives switching), vanilla JS, no build step.

## Credits

[libraop/cliraop](https://github.com/philippe44/libraop) by philippe44 (MIT) · [pychromecast](https://github.com/home-assistant-libs/pychromecast) · [python-zeroconf](https://github.com/python-zeroconf/python-zeroconf) · [yt-dlp](https://github.com/yt-dlp/yt-dlp) · [FFmpeg](https://ffmpeg.org/) · [aiohttp](https://github.com/aio-libs/aiohttp)

Not affiliated with AudioAddict, Plex, Radio Paradise or any station. You need your own premium subscription for the AudioAddict networks.

## License

MIT
