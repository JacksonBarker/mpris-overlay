#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse


LOGGER = logging.getLogger("mpris-overlay")
TRACK_KEY = "org.mpris.MediaPlayer2.Player"
MPRIS_PATH = "/org/mpris/MediaPlayer2"


@dataclass
class TrackInfo:
    player: str = ""
    status: str = "Stopped"
    title: str = ""
    artist: str = ""
    album: str = ""
    art_url: str = ""
    track_id: str = ""
    length_us: int = 0
    position_us: int = 0
    last_update: float = 0.0


class TrackStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._track = TrackInfo(last_update=time.time())

    def set_track(self, track: TrackInfo) -> None:
        with self._lock:
            self._track = track

    def get_track(self) -> TrackInfo:
        with self._lock:
            return TrackInfo(**asdict(self._track))


class GDBusMPRISClient:
    def __init__(self, mode: str = "auto") -> None:
        gdbus = shutil.which("gdbus")
        if gdbus is None:
            raise RuntimeError("gdbus not found. Install glib2 tools and try again.")
        host_exec = shutil.which("distrobox-host-exec")
        if mode == "host" and host_exec is None:
            raise RuntimeError("distrobox-host-exec was requested, but it is not available.")
        self._gdbus_local = gdbus
        self._host_exec = host_exec
        self._mode = mode
        self._prefer_host = mode == "host"

    def get_active_track(self) -> TrackInfo:
        players = self._list_players()
        if not players:
            return TrackInfo(status="Stopped")

        tracks: list[TrackInfo] = []
        for player in players:
            track = self._read_track(player)
            if track is not None:
                tracks.append(track)

        if not tracks:
            return TrackInfo(status="Stopped")

        for track in tracks:
            if track.status == "Playing":
                return track
        return tracks[0]

    def _list_players(self) -> list[str]:
        output = self._run_gdbus(
            [
                "call",
                "--session",
                "--dest",
                "org.freedesktop.DBus",
                "--object-path",
                "/org/freedesktop/DBus",
                "--method",
                "org.freedesktop.DBus.ListNames",
            ]
        )
        names = re.findall(r"'(org\.mpris\.MediaPlayer2\.[^']+)'", output)
        return sorted(set(names))

    def _read_track(self, service: str) -> TrackInfo | None:
        try:
            status_blob = self._run_gdbus(
                [
                    "call",
                    "--session",
                    "--dest",
                    service,
                    "--object-path",
                    MPRIS_PATH,
                    "--method",
                    "org.freedesktop.DBus.Properties.Get",
                    TRACK_KEY,
                    "PlaybackStatus",
                ]
            )
            metadata_blob = self._run_gdbus(
                [
                    "call",
                    "--session",
                    "--dest",
                    service,
                    "--object-path",
                    MPRIS_PATH,
                    "--method",
                    "org.freedesktop.DBus.Properties.Get",
                    TRACK_KEY,
                    "Metadata",
                ]
            )
        except RuntimeError:
            return None

        position_us = 0
        try:
            position_blob = self._run_gdbus(
                [
                    "call",
                    "--session",
                    "--dest",
                    service,
                    "--object-path",
                    MPRIS_PATH,
                    "--method",
                    "org.freedesktop.DBus.Properties.Get",
                    TRACK_KEY,
                    "Position",
                ]
            )
            position_us = self._extract_int_variant(position_blob)
        except RuntimeError:
            position_us = 0

        return TrackInfo(
            player=service.replace("org.mpris.MediaPlayer2.", ""),
            status=self._extract_status(status_blob),
            title=self._extract_text_key(metadata_blob, "xesam:title"),
            artist=self._extract_artists(metadata_blob),
            album=self._extract_text_key(metadata_blob, "xesam:album"),
            art_url=self._extract_text_key(metadata_blob, "mpris:artUrl"),
            track_id=self._extract_track_id(metadata_blob),
            length_us=max(0, self._extract_int_metadata_key(metadata_blob, "mpris:length")),
            position_us=max(0, position_us),
        )

    def _run_gdbus(self, args: list[str]) -> str:
        if self._prefer_host:
            return self._run_cmd(args, use_host=True)

        if self._mode == "local":
            return self._run_cmd(args, use_host=False)

        proc = subprocess.run(
            [self._gdbus_local, *args],
            capture_output=True,
            text=True,
            timeout=4.0,
            check=False,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()

        message = proc.stderr.strip() or proc.stdout.strip() or "gdbus call failed"
        if self._mode == "auto" and self._host_exec and self._should_fallback_to_host(message):
            LOGGER.info("Local DBus access failed, retrying via distrobox-host-exec")
            self._prefer_host = True
            return self._run_cmd(args, use_host=True)
        raise RuntimeError(message)

    def _run_cmd(self, args: list[str], use_host: bool) -> str:
        if use_host:
            if self._host_exec is None:
                raise RuntimeError("distrobox-host-exec is not available")
            cmd = [self._host_exec, "gdbus", *args]
        else:
            cmd = [self._gdbus_local, *args]

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=4.0,
            check=False,
        )
        if proc.returncode != 0:
            message = proc.stderr.strip() or proc.stdout.strip() or "gdbus call failed"
            raise RuntimeError(message)
        return proc.stdout.strip()

    @staticmethod
    def _should_fallback_to_host(message: str) -> bool:
        lowered = message.lower()
        signals = [
            "operation not permitted",
            "failed to connect",
            "could not connect",
            "cannot autolaunch",
            "session bus",
        ]
        return any(token in lowered for token in signals)

    @staticmethod
    def _extract_status(blob: str) -> str:
        match = re.search(r"(Playing|Paused|Stopped)", blob)
        return match.group(1) if match else "Stopped"

    @staticmethod
    def _extract_text_key(blob: str, key: str) -> str:
        pattern = rf"'{re.escape(key)}': <(?:string )?'((?:[^'\\]|\\.)*)'>"
        match = re.search(pattern, blob)
        if not match:
            return ""
        return GDBusMPRISClient._unescape(match.group(1))

    @staticmethod
    def _extract_artists(blob: str) -> str:
        match = re.search(r"'xesam:artist': <\[(.*?)\]>", blob)
        if not match:
            return ""
        values_blob = match.group(1)
        values = re.findall(r"'((?:[^'\\]|\\.)*)'", values_blob)
        return ", ".join(GDBusMPRISClient._unescape(v) for v in values if v)

    @staticmethod
    def _extract_track_id(blob: str) -> str:
        match = re.search(r"'mpris:trackid': <objectpath '((?:[^'\\]|\\.)*)'>", blob)
        if not match:
            return ""
        return GDBusMPRISClient._unescape(match.group(1))

    @staticmethod
    def _extract_int_metadata_key(blob: str, key: str) -> int:
        pattern = rf"'{re.escape(key)}': <(?:int64|uint64|int32|uint32)\s+(-?\d+)>"
        match = re.search(pattern, blob)
        if not match:
            return 0
        try:
            return int(match.group(1))
        except ValueError:
            return 0

    @staticmethod
    def _extract_int_variant(blob: str) -> int:
        match = re.search(r"<(?:int64|uint64|int32|uint32)\s+(-?\d+)>", blob)
        if not match:
            match = re.search(r"(-?\d+)", blob)
        if not match:
            return 0
        try:
            return int(match.group(1))
        except ValueError:
            return 0

    @staticmethod
    def _unescape(value: str) -> str:
        return value.replace("\\\\", "\\").replace("\\'", "'")


class MPRISPoller:
    def __init__(self, store: TrackStore, interval: float, dbus_mode: str) -> None:
        self._store = store
        self._interval = interval
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name="mpris-poller", daemon=True)
        self._client = GDBusMPRISClient(mode=dbus_mode)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=max(1.0, self._interval + 0.5))

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                track = self._client.get_active_track()
                track.last_update = time.time()
                self._store.set_track(track)
            except Exception as exc:
                LOGGER.warning("MPRIS poll failed: %s", exc)
                self._store.set_track(TrackInfo(status="Stopped", last_update=time.time()))
            self._stop_event.wait(self._interval)


OVERLAY_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>MPRIS Overlay</title>
  <style>
    :root {
      --ink: #1f2b2a;
      --muted: rgba(31, 43, 42, 0.66);
      --card-left: #7ec6aa;
      --card-right: #e9e9eb;
      --progress-bg: rgba(31, 43, 42, 0.16);
      --progress-fill: #6d9388;
      --font: "Avenir Next", "Segoe UI", "Helvetica Neue", Arial, sans-serif;
    }
    html, body {
      width: 100%;
      height: 100%;
      margin: 0;
      background: transparent;
      overflow: hidden;
    }
    body {
      display: flex;
      align-items: flex-end;
      justify-content: flex-start;
      padding: 20px;
      box-sizing: border-box;
    }
    .card {
      width: min(780px, calc(100vw - 40px));
      min-height: 180px;
      border-radius: 20px;
      overflow: hidden;
      display: grid;
      grid-template-columns: 180px 1fr;
      background: linear-gradient(90deg, var(--card-left) 0%, var(--card-left) 84%, var(--card-right) 84%, var(--card-right) 100%);
      box-shadow: 0 10px 30px rgba(0, 0, 0, 0.28);
      font-family: var(--font);
      color: var(--ink);
      backdrop-filter: blur(2px);
    }
    .art-wrap {
      padding: 16px;
      box-sizing: border-box;
    }
    #art {
      width: 148px;
      height: 148px;
      border-radius: 14px;
      object-fit: cover;
      display: block;
      background: #b4b4b7;
    }
    #art.placeholder {
      background:
        radial-gradient(circle at 35% 35%, #f8f8f8 0 18%, #dbdbdf 18% 28%, transparent 29%),
        linear-gradient(140deg, #d9d9dc, #b9b9bf);
    }
    .meta {
      padding: 18px 24px 16px 10px;
      box-sizing: border-box;
      display: flex;
      flex-direction: column;
      gap: 6px;
      min-width: 0;
    }
    #title {
      font-size: 38px;
      font-weight: 780;
      line-height: 1.16;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      margin-top: 2px;
      padding-bottom: 0.08em;
    }
    #artist {
      font-size: 32px;
      line-height: 1.16;
      color: var(--muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      padding-bottom: 0.08em;
    }
    .row {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 12px;
      min-width: 0;
    }
    #album {
      font-size: 30px;
      line-height: 1.16;
      color: var(--muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      min-width: 0;
      flex: 1 1 auto;
      padding-bottom: 0.08em;
    }
    #status {
      font-size: 24px;
      font-weight: 700;
      letter-spacing: 0.04em;
      color: rgba(31, 43, 42, 0.7);
      flex: 0 0 auto;
    }
    .progress {
      margin-top: auto;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .times {
      display: flex;
      justify-content: space-between;
      font-size: 24px;
      color: rgba(31, 43, 42, 0.62);
      font-variant-numeric: tabular-nums;
    }
    .bar {
      height: 14px;
      border-radius: 999px;
      background: var(--progress-bg);
      overflow: hidden;
    }
    #fill {
      width: 0%;
      height: 100%;
      border-radius: inherit;
      background: var(--progress-fill);
      transition: width 220ms linear;
    }
    @media (max-width: 860px) {
      .card {
        grid-template-columns: 130px 1fr;
        min-height: 130px;
      }
      .art-wrap {
        padding: 12px;
      }
      #art {
        width: 106px;
        height: 106px;
      }
      .meta {
        padding: 14px 16px 12px 4px;
      }
      #title {
        font-size: 28px;
        line-height: 1.18;
      }
      #artist, #album {
        font-size: 22px;
        line-height: 1.18;
      }
      #status, .times {
        font-size: 17px;
      }
      .bar {
        height: 10px;
      }
    }
  </style>
</head>
<body>
  <article class="card">
    <div class="art-wrap">
      <img id="art" class="placeholder" alt="Album art" />
    </div>
    <section class="meta">
      <div id="title">Nothing Playing</div>
      <div id="artist">Start playback in any MPRIS player</div>
      <div class="row">
        <div id="album"></div>
        <div id="status"></div>
      </div>
      <div class="progress">
        <div class="times">
          <span id="elapsed">00:00</span>
          <span id="total">00:00</span>
        </div>
        <div class="bar"><div id="fill"></div></div>
      </div>
    </section>
  </article>
  <script>
    const artEl = document.getElementById("art");
    const titleEl = document.getElementById("title");
    const artistEl = document.getElementById("artist");
    const albumEl = document.getElementById("album");
    const statusEl = document.getElementById("status");
    const elapsedEl = document.getElementById("elapsed");
    const totalEl = document.getElementById("total");
    const fillEl = document.getElementById("fill");

    let lastArtUrl = "";

    function formatTimeFromUs(us) {
      const totalSeconds = Math.max(0, Math.floor((Number(us) || 0) / 1000000));
      const minutes = Math.floor(totalSeconds / 60);
      const seconds = totalSeconds % 60;
      return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
    }

    function render(track) {
      const title = (track.title || "").trim();
      const artist = (track.artist || "").trim();
      const album = (track.album || "").trim();
      const status = (track.status || "").trim();
      const artUrl = (track.art_url || "").trim();
      const hasTrack = Boolean(title || artist);

      titleEl.textContent = hasTrack ? (title || "Unknown Title") : "Nothing Playing";
      artistEl.textContent = hasTrack ? (artist || "Unknown Artist") : "Start playback in any MPRIS player";
      albumEl.textContent = hasTrack ? album : "";
      statusEl.textContent = status && status !== "Playing" ? status.toUpperCase() : "";

      const lengthUs = Math.max(0, Number(track.length_us) || 0);
      const positionUs = Math.max(0, Number(track.position_us) || 0);
      const ratio = lengthUs > 0 ? Math.min(1, positionUs / lengthUs) : 0;
      fillEl.style.width = `${Math.round(ratio * 100)}%`;
      elapsedEl.textContent = formatTimeFromUs(positionUs);
      totalEl.textContent = formatTimeFromUs(lengthUs);

      if (!artUrl) {
        lastArtUrl = "";
        artEl.removeAttribute("src");
        artEl.classList.add("placeholder");
        return;
      }
      if (artUrl !== lastArtUrl) {
        artEl.src = artUrl;
        artEl.classList.remove("placeholder");
        lastArtUrl = artUrl;
      }
    }

    async function refreshTrack() {
      try {
        const response = await fetch("/now", { cache: "no-store" });
        if (!response.ok) {
          return;
        }
        const track = await response.json();
        render(track);
      } catch (_) {}
    }

    artEl.addEventListener("error", () => {
      artEl.classList.add("placeholder");
    });

    refreshTrack();
    setInterval(refreshTrack, 1000);
  </script>
</body>
</html>
"""


class OverlayHandler(BaseHTTPRequestHandler):
    store: TrackStore

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/" or path == "/overlay":
            self._send_html(OVERLAY_HTML)
            return
        if path == "/now":
            self._send_json(asdict(self.store.get_track()))
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        LOGGER.info("%s - %s", self.address_string(), format % args)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MPRIS now-playing overlay web server")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind")
    parser.add_argument("--port", type=int, default=8765, help="HTTP port")
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help="Seconds between MPRIS polls",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level",
    )
    parser.add_argument(
        "--dbus-mode",
        default="auto",
        choices=["auto", "local", "host"],
        help="DBus execution mode: local gdbus, host via distrobox-host-exec, or auto-fallback",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    store = TrackStore()
    OverlayHandler.store = store
    poller = MPRISPoller(store, interval=max(0.2, args.poll_interval), dbus_mode=args.dbus_mode)
    poller.start()

    server = ThreadingHTTPServer((args.host, args.port), OverlayHandler)
    LOGGER.info("Serving overlay on http://%s:%d", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("Stopping server...")
    finally:
        server.shutdown()
        server.server_close()
        poller.stop()


if __name__ == "__main__":
    main()
