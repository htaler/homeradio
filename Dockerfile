# ---- cliraop (philippe44/libraop), built from a pinned commit + our patch:
#      no 100% CPU busy-wait, whole-frame pipe reads, clean exit on EOF.
FROM debian:bookworm-slim AS cliraop
ARG LIBRAOP_COMMIT=c14088da44df37f58ef7a45b8ce8a3cbc051c5e4
RUN apt-get update && apt-get install -y --no-install-recommends gcc g++ make git ca-certificates
WORKDIR /src
RUN git init -q . && git remote add origin https://github.com/philippe44/libraop \
 && git fetch -q --depth 1 origin "${LIBRAOP_COMMIT}" && git checkout -q FETCH_HEAD \
 && git submodule update -q --init --depth 1 crosstools dmap-parser libcodecs libmdns libopenssl
COPY cliraop-home-radio.patch /tmp/
RUN git apply /tmp/cliraop-home-radio.patch \
 && make CC=x86_64-linux-gnu-gcc STATIC=1 -j4 \
 && cp bin/cliraop-linux-x86_64 /cliraop

# ---- app. Debian (glibc) on purpose: cliraop needs glibc + libatomic1.
FROM python:3.12.16-slim-bookworm

ARG DENO_VERSION=2.9.8
ARG DENO_SHA256=e890975bc2549807bbcc0b584ffafda72cc7c93349dd99c80483dd784f6c4609

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg libatomic1 ca-certificates curl unzip \
 && curl -fsSL -o /tmp/deno.zip "https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-x86_64-unknown-linux-gnu.zip" \
 && echo "${DENO_SHA256}  /tmp/deno.zip" | sha256sum -c - \
 && unzip -q /tmp/deno.zip -d /usr/local/bin && rm /tmp/deno.zip \
 && apt-get purge -y curl unzip && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

# cliraop: plain AirPlay 1 sender - the only thing the Belkin plays from.
COPY --from=cliraop /cliraop /usr/local/bin/cliraop

# yt-dlp is pinned too; when YouTube breaks, bump it in requirements.txt and rebuild.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY app /app
ENV DATA=/data PORT=8780 PYTHONUNBUFFERED=1
CMD ["python", "/app/app.py"]
