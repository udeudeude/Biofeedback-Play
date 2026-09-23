#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import os
import re
import socket
import struct
import subprocess
import threading
import time
import urllib.parse
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import hid


VENDOR_ID = 0x14FA
PRODUCT_ID = 0x0001
HOST = "127.0.0.1"
PORT = 8765

ROOT = Path(__file__).resolve().parent
RECORDINGS = ROOT / "recordings"
RECORDINGS.mkdir(exist_ok=True)


class LightstoneParser:
    pattern = re.compile(br"<RAW>([0-9A-Fa-f]{4}) ([0-9A-Fa-f]{4})<\\RAW>")

    def __init__(self) -> None:
        self.buffer = bytearray()

    def feed_report(self, report: list[int]) -> list[tuple[int, int]]:
        if not report:
            return []

        valid = int(report[0])
        if valid < 0:
            return []
        valid = min(valid, max(0, len(report) - 1))
        self.buffer.extend(bytes(report[1 : 1 + valid]))

        out: list[tuple[int, int]] = []
        while True:
            match = self.pattern.search(self.buffer)
            if match is None:
                if len(self.buffer) > 512:
                    del self.buffer[:-128]
                break

            skin = int(match.group(1), 16)
            pulse = int(match.group(2), 16)
            out.append((skin, pulse))
            del self.buffer[: match.end()]

        return out


def osc_pad(raw: bytes) -> bytes:
    raw += b"\x00"
    while len(raw) % 4:
        raw += b"\x00"
    return raw


def osc_message(address: str, value: int | float) -> bytes:
    if isinstance(value, int):
        tags = ",i"
        payload = struct.pack(">i", value)
    else:
        tags = ",f"
        payload = struct.pack(">f", float(value))
    return osc_pad(address.encode("utf-8")) + osc_pad(tags.encode("ascii")) + payload


class BiofeedbackState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.running = True
        self.connected = False
        self.shutdown = False
        self.last_error = ""
        self.seq = 0
        self.started_monotonic = time.monotonic()
        self.samples = deque(maxlen=2400)

        self.recording = False
        self.recording_path = ""
        self.recording_file = None
        self.recording_writer = None

        self.osc_enabled = False
        self.osc_host = "127.0.0.1"
        self.osc_port = 57120
        self.osc_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        self.parser = LightstoneParser()
        self.thread = threading.Thread(target=self._reader_loop, daemon=True)
        self.thread.start()

    def set_running(self, value: bool) -> None:
        with self.lock:
            self.running = bool(value)
            if not self.running:
                self.connected = False

    def set_osc(self, enabled: bool, host: str | None = None, port: int | None = None) -> None:
        with self.lock:
            if host:
                self.osc_host = host
            if port:
                self.osc_port = int(port)
            self.osc_enabled = bool(enabled)

    def start_recording(self) -> str:
        with self.lock:
            if self.recording:
                return self.recording_path

            stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
            path = RECORDINGS / ("lightstone_" + stamp + ".csv")
            fp = path.open("w", newline="", encoding="utf-8")
            writer = csv.writer(fp)
            writer.writerow(["unix_time", "elapsed_s", "skin_raw", "pulse_raw"])
            fp.flush()

            self.recording_file = fp
            self.recording_writer = writer
            self.recording_path = str(path)
            self.recording = True
            return self.recording_path

    def stop_recording(self) -> None:
        with self.lock:
            self.recording = False
            if self.recording_file:
                self.recording_file.flush()
                self.recording_file.close()
            self.recording_file = None
            self.recording_writer = None

    def reveal_recordings(self) -> None:
        try:
            subprocess.Popen(["open", str(RECORDINGS)])
        except Exception:
            pass

    def status(self) -> dict:
        with self.lock:
            latest = self.samples[-1] if self.samples else None
            return {
                "running": self.running,
                "connected": self.connected,
                "last_error": self.last_error,
                "latest": latest,
                "recording": self.recording,
                "recording_path": self.recording_path,
                "osc_enabled": self.osc_enabled,
                "osc_host": self.osc_host,
                "osc_port": self.osc_port,
            }

    def samples_after(self, seq: int) -> list[dict]:
        with self.lock:
            return [sample for sample in self.samples if sample["seq"] > seq][-300:]

    def _store_sample(self, skin: int, pulse: int) -> None:
        now_unix = time.time()
        elapsed = time.monotonic() - self.started_monotonic

        with self.lock:
            self.seq += 1
            sample = {
                "seq": self.seq,
                "unix": now_unix,
                "t": elapsed,
                "skin": skin,
                "pulse": pulse,
            }
            self.samples.append(sample)

            if self.recording and self.recording_writer:
                self.recording_writer.writerow(
                    [f"{now_unix:.6f}", f"{elapsed:.6f}", skin, pulse]
                )
                if self.seq % 30 == 0 and self.recording_file:
                    self.recording_file.flush()

            if self.osc_enabled:
                target = (self.osc_host, self.osc_port)
                try:
                    self.osc_socket.sendto(
                        osc_message("/biofeedback/lightstone/skin_raw", skin), target
                    )
                    self.osc_socket.sendto(
                        osc_message("/biofeedback/lightstone/pulse_raw", pulse), target
                    )
                except OSError as exc:
                    self.last_error = "OSC: " + str(exc)

    def _reader_loop(self) -> None:
        device = None

        while not self.shutdown:
            with self.lock:
                should_run = self.running

            if not should_run:
                if device is not None:
                    try:
                        device.close()
                    except Exception:
                        pass
                    device = None
                time.sleep(0.1)
                continue

            try:
                if device is None:
                    device = hid.device()
                    device.open(VENDOR_ID, PRODUCT_ID)
                    with self.lock:
                        self.connected = True
                        self.last_error = ""
                        self.parser = LightstoneParser()

                report = device.read(8, 500)
                if not report:
                    continue

                for skin, pulse in self.parser.feed_report(report):
                    self._store_sample(skin, pulse)

            except Exception as exc:
                with self.lock:
                    self.connected = False
                    self.last_error = str(exc)
                if device is not None:
                    try:
                        device.close()
                    except Exception:
                        pass
                    device = None
                time.sleep(1.0)

        if device is not None:
            try:
                device.close()
            except Exception:
                pass


STATE: BiofeedbackState | None = None


HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Biofeedback Play</title>
<style>
:root {
  color-scheme: dark;
  --bg: #0b0c10;
  --panel: #151821;
  --panel2: #1d2230;
  --text: #eef1f7;
  --muted: #98a2b3;
  --line: #343b4e;
  --good: #59d185;
  --bad: #ff6b6b;
  --skin: #7dd3fc;
  --pulse: #f9a8d4;
  --accent: #c4b5fd;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: radial-gradient(circle at 20% 0%, #161a25, var(--bg) 42%);
  color: var(--text);
}
main { max-width: 1180px; margin: 0 auto; padding: 28px 22px 50px; }
h1 { margin: 0; font-size: 30px; font-weight: 700; letter-spacing: -0.03em; }
.subtitle { margin-top: 6px; color: var(--muted); }
.topbar {
  display: flex; gap: 14px; justify-content: space-between; align-items: center;
  margin-bottom: 22px; flex-wrap: wrap;
}
.status {
  display: inline-flex; align-items: center; gap: 8px;
  padding: 8px 12px; border: 1px solid var(--line); border-radius: 999px;
  background: rgba(255,255,255,0.03);
}
.dot { width: 9px; height: 9px; border-radius: 50%; background: var(--bad); }
.dot.on { background: var(--good); box-shadow: 0 0 12px rgba(89,209,133,.55); }
.grid { display: grid; grid-template-columns: repeat(12, 1fr); gap: 14px; }
.card {
  background: rgba(21,24,33,.88);
  border: 1px solid var(--line);
  border-radius: 15px;
  padding: 16px;
  box-shadow: 0 12px 35px rgba(0,0,0,.18);
}
.metric { grid-column: span 3; min-height: 112px; }
.controls { grid-column: span 6; }
.osc { grid-column: span 6; }
.chart { grid-column: span 12; }
.label { color: var(--muted); font-size: 13px; text-transform: uppercase; letter-spacing: .08em; }
.value { font-size: 36px; font-variant-numeric: tabular-nums; margin-top: 7px; }
.unit { color: var(--muted); font-size: 13px; margin-top: 4px; }
button, input {
  font: inherit;
}
button {
  border: 1px solid var(--line);
  color: var(--text);
  background: var(--panel2);
  border-radius: 10px;
  padding: 9px 13px;
  cursor: pointer;
}
button:hover { border-color: #626b84; }
button.primary { background: #4338ca; border-color: #635bdf; }
button.recording { background: #8b1e3f; border-color: #c33d65; }
.row { display: flex; flex-wrap: wrap; gap: 9px; align-items: center; }
input[type=text], input[type=number] {
  background: #0d1017;
  border: 1px solid var(--line);
  color: var(--text);
  border-radius: 9px;
  padding: 8px 9px;
}
input.host { width: 150px; }
input.port { width: 90px; }
canvas {
  width: 100%;
  height: 210px;
  display: block;
  margin-top: 10px;
  border-radius: 10px;
  background: #0b0e15;
}
.charthead { display: flex; justify-content: space-between; gap: 12px; align-items: baseline; }
.range { color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }
#error { color: #ff9b9b; font-size: 13px; margin-top: 9px; min-height: 18px; }
.small { color: var(--muted); font-size: 12px; line-height: 1.45; margin-top: 10px; }
@media (max-width: 760px) {
  .metric { grid-column: span 6; }
  .controls, .osc { grid-column: span 12; }
}
</style>
</head>
<body>
<main>
  <div class="topbar">
    <div>
      <h1>Biofeedback Play</h1>
      <div class="subtitle">Wild Divine Lightstone live laboratory</div>
    </div>
    <div class="status"><span id="dot" class="dot"></span><span id="statusText">Connecting…</span></div>
  </div>

  <div class="grid">
    <section class="card metric">
      <div class="label">Skin raw</div>
      <div id="skinValue" class="value">—</div>
      <div class="unit">conductance channel</div>
    </section>
    <section class="card metric">
      <div class="label">Pulse raw</div>
      <div id="pulseValue" class="value">—</div>
      <div class="unit">blood-volume waveform</div>
    </section>
    <section class="card metric">
      <div class="label">Samples</div>
      <div id="sampleValue" class="value">0</div>
      <div class="unit">this session</div>
    </section>
    <section class="card metric">
      <div class="label">Recording</div>
      <div id="recordValue" class="value">OFF</div>
      <div class="unit">CSV</div>
    </section>

    <section class="card controls">
      <div class="label">Acquisition & recording</div>
      <div class="row" style="margin-top:12px">
        <button id="runBtn" class="primary">Stop acquisition</button>
        <button id="recordBtn">Start recording</button>
        <button id="folderBtn">Show recordings</button>
      </div>
      <div id="error"></div>
    </section>

    <section class="card osc">
      <div class="label">OSC output</div>
      <div class="row" style="margin-top:12px">
        <label><input id="oscEnabled" type="checkbox"> Enabled</label>
        <input id="oscHost" class="host" type="text" value="127.0.0.1" aria-label="OSC host">
        <input id="oscPort" class="port" type="number" value="57120" aria-label="OSC port">
        <button id="oscApply">Apply</button>
      </div>
      <div class="small">
        Sends /biofeedback/lightstone/skin_raw and /biofeedback/lightstone/pulse_raw.
        Default SuperCollider language port is usually 57120.
      </div>
    </section>

    <section class="card chart">
      <div class="charthead">
        <div>
          <div class="label">Skin conductance</div>
          <div class="small">Raw Lightstone channel. Disconnecting either skin electrode drives this toward zero.</div>
        </div>
        <div id="skinRange" class="range"></div>
      </div>
      <canvas id="skinChart"></canvas>
    </section>

    <section class="card chart">
      <div class="charthead">
        <div>
          <div class="label">Pulse waveform</div>
          <div class="small">Raw optical blood-volume waveform. Heart-rate derivation will be layered on top later.</div>
        </div>
        <div id="pulseRange" class="range"></div>
      </div>
      <canvas id="pulseChart"></canvas>
    </section>
  </div>
</main>

<script>
let lastSeq = 0;
let totalSamples = 0;
const skin = [];
const pulse = [];
const maxPoints = 620;

function trim(a) {
  if (a.length > maxPoints) a.splice(0, a.length - maxPoints);
}

function post(action, extra) {
  const body = Object.assign({action: action}, extra || {});
  return fetch("/api/control", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body)
  }).then(r => r.json());
}

function fitCanvas(canvas) {
  const ratio = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const w = Math.max(1, Math.floor(rect.width * ratio));
  const h = Math.max(1, Math.floor(rect.height * ratio));
  if (canvas.width !== w || canvas.height !== h) {
    canvas.width = w;
    canvas.height = h;
  }
  return {w: w, h: h, ratio: ratio};
}

function draw(canvas, values, stroke, rangeEl) {
  const size = fitCanvas(canvas);
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, size.w, size.h);

  ctx.strokeStyle = "#1f2634";
  ctx.lineWidth = 1 * size.ratio;
  for (let i = 1; i < 4; i++) {
    const y = size.h * i / 4;
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(size.w, y);
    ctx.stroke();
  }

  if (values.length < 2) return;

  let min = Math.min.apply(null, values);
  let max = Math.max.apply(null, values);
  if (max === min) {
    max += 1;
    min -= 1;
  }
  const pad = (max - min) * 0.08;
  min -= pad;
  max += pad;

  rangeEl.textContent = Math.round(min) + " – " + Math.round(max);

  ctx.strokeStyle = stroke;
  ctx.lineWidth = 1.6 * size.ratio;
  ctx.lineJoin = "round";
  ctx.beginPath();

  values.forEach(function(v, i) {
    const x = i * size.w / Math.max(1, values.length - 1);
    const y = size.h - ((v - min) / (max - min)) * size.h;
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

function refreshStatus() {
  fetch("/api/status")
    .then(r => r.json())
    .then(function(s) {
      const dot = document.getElementById("dot");
      const text = document.getElementById("statusText");
      dot.className = s.connected ? "dot on" : "dot";
      if (!s.running) text.textContent = "Acquisition stopped";
      else if (s.connected) text.textContent = "Lightstone connected";
      else text.textContent = "Waiting for Lightstone";

      document.getElementById("runBtn").textContent = s.running ? "Stop acquisition" : "Start acquisition";
      document.getElementById("runBtn").className = s.running ? "primary" : "";

      const rb = document.getElementById("recordBtn");
      rb.textContent = s.recording ? "Stop recording" : "Start recording";
      rb.className = s.recording ? "recording" : "";
      document.getElementById("recordValue").textContent = s.recording ? "ON" : "OFF";
      document.getElementById("oscEnabled").checked = s.osc_enabled;
      document.getElementById("oscHost").value = s.osc_host;
      document.getElementById("oscPort").value = s.osc_port;
      document.getElementById("error").textContent = s.last_error || "";
    })
    .catch(function(err) {
      document.getElementById("error").textContent = String(err);
    });
}

function pollSamples() {
  fetch("/api/samples?after=" + lastSeq)
    .then(r => r.json())
    .then(function(data) {
      data.samples.forEach(function(s) {
        lastSeq = Math.max(lastSeq, s.seq);
        totalSamples++;
        skin.push(s.skin);
        pulse.push(s.pulse);
      });
      trim(skin);
      trim(pulse);

      if (data.samples.length) {
        const latest = data.samples[data.samples.length - 1];
        document.getElementById("skinValue").textContent = latest.skin;
        document.getElementById("pulseValue").textContent = latest.pulse;
        document.getElementById("sampleValue").textContent = totalSamples;
      }

      draw(document.getElementById("skinChart"), skin, "#7dd3fc", document.getElementById("skinRange"));
      draw(document.getElementById("pulseChart"), pulse, "#f9a8d4", document.getElementById("pulseRange"));
    })
    .catch(function(err) {
      document.getElementById("error").textContent = String(err);
    });
}

document.getElementById("runBtn").onclick = function() {
  fetch("/api/status").then(r => r.json()).then(function(s) {
    return post(s.running ? "stop" : "start");
  }).then(refreshStatus);
};

document.getElementById("recordBtn").onclick = function() {
  fetch("/api/status").then(r => r.json()).then(function(s) {
    return post(s.recording ? "record_stop" : "record_start");
  }).then(refreshStatus);
};

document.getElementById("folderBtn").onclick = function() {
  post("reveal_recordings");
};

document.getElementById("oscApply").onclick = function() {
  post("osc", {
    enabled: document.getElementById("oscEnabled").checked,
    host: document.getElementById("oscHost").value,
    port: Number(document.getElementById("oscPort").value)
  }).then(refreshStatus);
};

window.addEventListener("resize", function() {
  draw(document.getElementById("skinChart"), skin, "#7dd3fc", document.getElementById("skinRange"));
  draw(document.getElementById("pulseChart"), pulse, "#f9a8d4", document.getElementById("pulseRange"));
});

refreshStatus();
setInterval(refreshStatus, 1000);
setInterval(pollSamples, 100);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "BiofeedbackPlay/0.1"

    def log_message(self, format: str, *args) -> None:
        return

    def send_json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/":
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/status":
            self.send_json(STATE.status())
            return

        if parsed.path == "/api/samples":
            query = urllib.parse.parse_qs(parsed.query)
            try:
                after = int(query.get("after", ["0"])[0])
            except ValueError:
                after = 0
            self.send_json({"samples": STATE.samples_after(after)})
            return

        self.send_error(404)

    def do_POST(self) -> None:
        if self.path != "/api/control":
            self.send_error(404)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            action = payload.get("action")

            if action == "start":
                STATE.set_running(True)
            elif action == "stop":
                STATE.set_running(False)
            elif action == "record_start":
                STATE.start_recording()
            elif action == "record_stop":
                STATE.stop_recording()
            elif action == "reveal_recordings":
                STATE.reveal_recordings()
            elif action == "osc":
                STATE.set_osc(
                    bool(payload.get("enabled")),
                    str(payload.get("host") or "127.0.0.1"),
                    int(payload.get("port") or 57120),
                )
            else:
                self.send_json({"ok": False, "error": "Unknown action"}, 400)
                return

            self.send_json({"ok": True, "status": STATE.status()})
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, 500)


def main() -> None:
    global STATE
    STATE = BiofeedbackState()

    url = f"http://{HOST}:{PORT}"
    server = ThreadingHTTPServer((HOST, PORT), Handler)

    print("Biofeedback Play")
    print("Open:", url)
    print("Lightstone USB: 14FA:0001")
    print("Press Control-C here to quit.")

    threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        STATE.shutdown = True
        STATE.stop_recording()
        server.server_close()


if __name__ == "__main__":
    main()
