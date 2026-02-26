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
      --subtext: rgba(31, 43, 42, 0.90);
      --muted: rgba(31, 43, 42, 0.66);
      --muted-strong: rgba(31, 43, 42, 0.74);
      --card-base: #8ebfb0;
      --card-splashes:
        radial-gradient(120% 120% at 14% 18%, rgba(233, 233, 235, 0.34) 0%, rgba(233, 233, 235, 0) 46%),
        radial-gradient(95% 120% at 86% 20%, rgba(122, 148, 169, 0.30) 0%, rgba(122, 148, 169, 0) 54%),
        radial-gradient(88% 94% at 62% 88%, rgba(174, 203, 195, 0.28) 0%, rgba(174, 203, 195, 0) 52%);
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
      align-items: flex-start;
      justify-content: flex-start;
      padding: 0;
      box-sizing: border-box;
    }
    .card {
      width: 780px;
      height: 238px;
      border-radius: 20px;
      overflow: hidden;
      position: relative;
      isolation: isolate;
      display: grid;
      grid-template-columns: 180px 1fr;
      background: var(--card-splashes), var(--card-base);
      font-family: var(--font);
      color: var(--ink);
      backdrop-filter: blur(2px);
    }
    .art-wrap {
      padding: 16px;
      box-sizing: border-box;
      position: relative;
      z-index: 1;
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
      position: relative;
      z-index: 1;
    }
    #title {
      font-size: 38px;
      font-weight: 780;
      line-height: 1.16;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      margin-top: 2px;
      padding-bottom: 0.14em;
    }
    #artist {
      font-size: 32px;
      line-height: 1.16;
      color: var(--subtext);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      padding-bottom: 0.14em;
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
      color: var(--subtext);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      min-width: 0;
      flex: 1 1 auto;
      padding-bottom: 0.14em;
    }
    #status {
      font-size: 24px;
      font-weight: 700;
      letter-spacing: 0.04em;
      color: var(--muted-strong);
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
      color: var(--muted-strong);
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
    const cardEl = document.querySelector(".card");
    const artEl = document.getElementById("art");
    const titleEl = document.getElementById("title");
    const artistEl = document.getElementById("artist");
    const albumEl = document.getElementById("album");
    const statusEl = document.getElementById("status");
    const elapsedEl = document.getElementById("elapsed");
    const totalEl = document.getElementById("total");
    const fillEl = document.getElementById("fill");

    const DEFAULT_PALETTE = [
      { r: 126, g: 198, b: 170 },
      { r: 142, g: 191, b: 176 },
      { r: 205, g: 210, b: 214 },
      { r: 233, g: 233, b: 235 },
    ];

    let lastArtUrl = "";

    function formatTimeFromUs(us) {
      const totalSeconds = Math.max(0, Math.floor((Number(us) || 0) / 1000000));
      const minutes = Math.floor(totalSeconds / 60);
      const seconds = totalSeconds % 60;
      return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
    }

    function clampByte(v) {
      return Math.max(0, Math.min(255, Math.round(v)));
    }

    function mixColor(a, b, t) {
      return {
        r: clampByte(a.r + (b.r - a.r) * t),
        g: clampByte(a.g + (b.g - a.g) * t),
        b: clampByte(a.b + (b.b - a.b) * t),
      };
    }

    function toRgb(color) {
      return `rgb(${color.r}, ${color.g}, ${color.b})`;
    }

    function toRgba(color, alpha) {
      return `rgba(${color.r}, ${color.g}, ${color.b}, ${alpha})`;
    }

    function colorDistance(a, b) {
      const dr = a.r - b.r;
      const dg = a.g - b.g;
      const db = a.b - b.b;
      return Math.sqrt(dr * dr + dg * dg + db * db);
    }

    function luminance(color) {
      const channels = [color.r, color.g, color.b].map((v) => {
        const c = v / 255;
        return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
      });
      return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
    }

    function contrastRatio(a, b) {
      const l1 = luminance(a);
      const l2 = luminance(b);
      const lighter = Math.max(l1, l2);
      const darker = Math.min(l1, l2);
      return (lighter + 0.05) / (darker + 0.05);
    }

    function blendOver(bottom, top, alpha) {
      return {
        r: clampByte(bottom.r * (1 - alpha) + top.r * alpha),
        g: clampByte(bottom.g * (1 - alpha) + top.g * alpha),
        b: clampByte(bottom.b * (1 - alpha) + top.b * alpha),
      };
    }

    function minContrastForForeground(fg, samples) {
      let min = Infinity;
      for (const bg of samples) {
        min = Math.min(min, contrastRatio(fg, bg));
      }
      return min;
    }

    function applyPaletteTheme(palette) {
      const colors = palette.length ? palette : DEFAULT_PALETTE;
      const c1 = colors[0];
      const c2 = colors[Math.min(1, colors.length - 1)];
      const c3 = colors[Math.min(2, colors.length - 1)];
      const c4 = colors[Math.min(3, colors.length - 1)];
      const primary = mixColor(c1, c2, 0.18);
      const splashA = mixColor(c2, { r: 255, g: 255, b: 255 }, 0.22);
      const splashB = mixColor(c3, { r: 8, g: 12, b: 18 }, 0.18);
      const splashC = mixColor(c4, c2, 0.52);
      const jitter = (base, range) => base + (Math.random() * 2 - 1) * range;

      const aW = jitter(120, 7);
      const aH = jitter(120, 7);
      const aX = jitter(14, 3);
      const aY = jitter(18, 3);
      const aFade = jitter(46, 3);

      const bW = jitter(95, 6);
      const bH = jitter(120, 8);
      const bX = jitter(86, 3);
      const bY = jitter(20, 3);
      const bFade = jitter(54, 3);

      const cW = jitter(88, 6);
      const cH = jitter(94, 6);
      const cX = jitter(62, 4);
      const cY = jitter(88, 4);
      const cFade = jitter(52, 3);

      cardEl.style.setProperty("--card-base", toRgb(primary));
      cardEl.style.setProperty(
        "--card-splashes",
        `radial-gradient(${aW}% ${aH}% at ${aX}% ${aY}%, ${toRgba(splashA, 0.34)} 0%, ${toRgba(splashA, 0)} ${aFade}%),
         radial-gradient(${bW}% ${bH}% at ${bX}% ${bY}%, ${toRgba(splashB, 0.30)} 0%, ${toRgba(splashB, 0)} ${bFade}%),
         radial-gradient(${cW}% ${cH}% at ${cX}% ${cY}%, ${toRgba(splashC, 0.28)} 0%, ${toRgba(splashC, 0)} ${cFade}%)`
      );

      const backgroundSamples = [
        primary,
        blendOver(primary, splashA, 0.26),
        blendOver(primary, splashB, 0.24),
        blendOver(primary, splashC, 0.22),
        blendOver(blendOver(primary, splashA, 0.18), splashB, 0.14),
        blendOver(blendOver(primary, splashB, 0.16), splashC, 0.16),
      ];
      const darkBase = { r: 18, g: 28, b: 34 };
      const lightBase = { r: 246, g: 250, b: 253 };
      const darkScore = minContrastForForeground(darkBase, backgroundSamples);
      const lightScore = minContrastForForeground(lightBase, backgroundSamples);

      if (darkScore >= lightScore) {
        cardEl.style.setProperty("--ink", "rgba(14, 24, 29, 0.98)");
        cardEl.style.setProperty("--subtext", "rgba(14, 24, 29, 0.94)");
        cardEl.style.setProperty("--muted", "rgba(14, 24, 29, 0.82)");
        cardEl.style.setProperty("--muted-strong", "rgba(14, 24, 29, 0.88)");
        cardEl.style.setProperty("--progress-bg", "rgba(14, 24, 29, 0.30)");
        cardEl.style.setProperty("--progress-fill", "rgba(14, 24, 29, 0.62)");
      } else {
        cardEl.style.setProperty("--ink", "rgba(248, 252, 255, 0.99)");
        cardEl.style.setProperty("--subtext", "rgba(248, 252, 255, 0.94)");
        cardEl.style.setProperty("--muted", "rgba(248, 252, 255, 0.85)");
        cardEl.style.setProperty("--muted-strong", "rgba(248, 252, 255, 0.90)");
        cardEl.style.setProperty("--progress-bg", "rgba(248, 252, 255, 0.34)");
        cardEl.style.setProperty("--progress-fill", "rgba(248, 252, 255, 0.70)");
      }
    }

    function extractPaletteFromArt() {
      try {
        const size = 56;
        const canvas = document.createElement("canvas");
        canvas.width = size;
        canvas.height = size;
        const ctx = canvas.getContext("2d", { willReadFrequently: true });
        if (!ctx) {
          return null;
        }
        ctx.drawImage(artEl, 0, 0, size, size);
        const data = ctx.getImageData(0, 0, size, size).data;
        const buckets = new Map();

        for (let i = 0; i < data.length; i += 4) {
          const alpha = data[i + 3];
          if (alpha < 160) {
            continue;
          }
          const r = data[i];
          const g = data[i + 1];
          const b = data[i + 2];
          const qr = Math.round(r / 32) * 32;
          const qg = Math.round(g / 32) * 32;
          const qb = Math.round(b / 32) * 32;
          const key = `${qr},${qg},${qb}`;
          const slot = buckets.get(key) || { r: 0, g: 0, b: 0, count: 0 };
          slot.r += r;
          slot.g += g;
          slot.b += b;
          slot.count += 1;
          buckets.set(key, slot);
        }

        const ranked = [...buckets.values()]
          .filter((slot) => slot.count > 6)
          .map((slot) => ({
            r: clampByte(slot.r / slot.count),
            g: clampByte(slot.g / slot.count),
            b: clampByte(slot.b / slot.count),
            count: slot.count,
          }))
          .sort((a, b) => b.count - a.count);

        if (!ranked.length) {
          return null;
        }

        const picked = [];
        for (const color of ranked) {
          if (picked.every((existing) => colorDistance(existing, color) >= 74)) {
            picked.push({ r: color.r, g: color.g, b: color.b });
          }
          if (picked.length >= 4) {
            break;
          }
        }

        while (picked.length < 4) {
          const tail = picked[picked.length - 1] || DEFAULT_PALETTE[picked.length];
          const next = mixColor(tail, { r: 255, g: 255, b: 255 }, picked.length * 0.12);
          picked.push(next);
        }
        return picked;
      } catch (_) {
        return null;
      }
    }

    function applyDefaultTheme() {
      applyPaletteTheme(DEFAULT_PALETTE);
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
        applyDefaultTheme();
        return;
      }
      if (artUrl !== lastArtUrl) {
        if (/^https?:\\/\\//i.test(artUrl)) {
          artEl.crossOrigin = "anonymous";
        } else {
          artEl.removeAttribute("crossorigin");
        }
        artEl.src = artUrl;
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

    artEl.addEventListener("load", () => {
      artEl.classList.remove("placeholder");
      const palette = extractPaletteFromArt();
      if (palette) {
        applyPaletteTheme(palette);
      } else {
        applyDefaultTheme();
      }
    });

    artEl.addEventListener("error", () => {
      artEl.classList.add("placeholder");
      applyDefaultTheme();
    });

    applyDefaultTheme();
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
