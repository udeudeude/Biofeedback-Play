#!/usr/bin/env python3
from __future__ import annotations

import base64
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
EMWAVE_VENDOR_ID = 0x0E30
EMWAVE_PRODUCT_ID = 0x0002
EMWAVE_NOMINAL_SAMPLE_RATE = 375.0
HOST = "127.0.0.1"
PORT = 8765

ROOT = Path(__file__).resolve().parent
RECORDINGS = ROOT / "recordings"
RECORDINGS.mkdir(exist_ok=True)
CAPTURES = ROOT / "captures"
CAPTURES.mkdir(exist_ok=True)

DEVICE_DEFINITIONS = {
    "lightstone": {
        "name": "Wild Divine Lightstone",
        "manufacturer": "Wild Divine",
        "transport": "USB HID",
        "usb_id": "14fa:0001",
        "summary": "Finger-sensor interface providing skin-conductance and pulse-waveform channels.",
    },
    "emwave": {
        "name": "HeartMath emWave Pulse Sensor",
        "manufacturer": "HeartMath / Quantum Intech",
        "transport": "USB HID",
        "usb_id": "0e30:0002",
        "summary": "Ear-clip optical pulse sensor with a directly readable raw waveform.",
    },
}

SIGNAL_DEFINITIONS = {
    "lightstone.skin_raw": {
        "device_id": "lightstone",
        "name": "Skin conductance",
        "short_name": "Skin",
        "data_label": "Raw skin-conductance channel",
        "unit": "raw device units",
        "description": (
            "Electrical skin-conductance signal from the two plain finger electrodes. "
            "Useful for slow changes in arousal; values are not calibrated to microsiemens."
        ),
        "audio": "Pitch follows the recent signal level. Higher recent values produce a higher tone.",
        "osc": "/biofeedback/lightstone/skin_raw",
        "nominal_rate": None,
        "value_key": "skin",
    },
    "lightstone.pulse_raw": {
        "device_id": "lightstone",
        "name": "Pulse waveform",
        "short_name": "Pulse",
        "data_label": "Raw blood-volume pulse waveform",
        "unit": "raw device units",
        "description": (
            "Optical pulse waveform from the gold-dot finger sensor. This is the waveform itself, "
            "not heart rate or beats per minute."
        ),
        "audio": "Pitch follows the recent pulse-wave shape, making each beat audible as a contour.",
        "osc": "/biofeedback/lightstone/pulse_raw",
        "nominal_rate": None,
        "value_key": "pulse",
    },
    "emwave.pulse_raw": {
        "device_id": "emwave",
        "name": "Pulse waveform",
        "short_name": "Pulse",
        "data_label": "Raw 8-bit optical pulse waveform",
        "unit": "0–255 raw units",
        "description": (
            "Direct USB waveform from the emWave ear clip. The current packet interpretation is "
            "capture-derived and experimental; it is not a heart-rate value."
        ),
        "audio": "Pitch follows the recent pulse-wave shape. Packet gaps are tracked separately.",
        "osc": "/biofeedback/emwave/pulse_raw",
        "nominal_rate": EMWAVE_NOMINAL_SAMPLE_RATE,
        "value_key": "pulse",
    },
}


class EmWaveParser:
    """Experimental parser based on captures from emWave USB 0x0E30:0x0002.

    Observed reports are 8 bytes:
      byte 0: constant report marker 0x01
      byte 1: packet counter, incrementing modulo 256
      bytes 2..7: six consecutive 8-bit pulse waveform samples
    """

    def __init__(self) -> None:
        self.last_counter: int | None = None

    def feed_report(self, report: list[int]) -> dict | None:
        if len(report) < 8 or int(report[0]) != 0x01:
            return None

        counter = int(report[1]) & 0xFF
        gap = 0
        if self.last_counter is not None:
            expected = (self.last_counter + 1) & 0xFF
            gap = (counter - expected) & 0xFF
        self.last_counter = counter

        return {
            "counter": counter,
            "gap": gap,
            "samples": [int(value) & 0xFF for value in report[2:8]],
        }


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


def encode_hid_path(path) -> str:
    if isinstance(path, str):
        path = path.encode("utf-8")
    return base64.urlsafe_b64encode(bytes(path)).decode("ascii")


def decode_hid_path(token: str) -> bytes:
    return base64.urlsafe_b64decode(token.encode("ascii"))


def hid_is_obviously_unrelated(
    manufacturer: str, product_name: str, known: str = ""
) -> tuple[bool, str]:
    if known:
        return False, ""

    text = f"{manufacturer} {product_name}".strip().lower()
    compact_text = re.sub(r"[^a-z0-9]+", "", text)
    obvious_terms = {
        "keyboard": "keyboard",
        "trackpad": "trackpad",
        "mouse": "mouse",
        "touch bar": "Touch Bar",
        "touchbar": "Touch Bar",
        "facetime": "camera",
        "camera": "camera",
        "headset": "headset controls",
        "usb storage": "storage",
        "storage": "storage",
        "usb hub": "USB hub",
        "backlight": "display/backlight",
        "ambient light sensor": "computer ambient-light sensor",
        "apple t2 controller": "computer controller",
    }
    for term, reason in obvious_terms.items():
        if term in text or re.sub(r"[^a-z0-9]+", "", term) in compact_text:
            return True, reason

    return False, ""


def hid_device_list() -> list[dict]:
    devices: list[dict] = []
    for item in hid.enumerate():
        path = item.get("path")
        if path is None:
            continue

        vendor = int(item.get("vendor_id") or 0)
        product = int(item.get("product_id") or 0)
        manufacturer = str(item.get("manufacturer_string") or "").strip()
        product_name = str(item.get("product_string") or "").strip()

        known = ""
        if vendor == VENDOR_ID and product == PRODUCT_ID:
            known = "Wild Divine Lightstone"
        elif vendor == EMWAVE_VENDOR_ID and product == EMWAVE_PRODUCT_ID:
            known = "HeartMath emWave Pulse Sensor"

        obviously_unrelated, hidden_reason = hid_is_obviously_unrelated(
            manufacturer, product_name, known
        )

        if isinstance(path, str):
            path_bytes = path.encode("utf-8")
            path_display = path
        else:
            path_bytes = bytes(path)
            path_display = path_bytes.decode("utf-8", errors="replace")

        devices.append(
            {
                "path_token": encode_hid_path(path_bytes),
                "path": path_display,
                "vendor_id": vendor,
                "product_id": product,
                "vendor_hex": f"0x{vendor:04x}",
                "product_hex": f"0x{product:04x}",
                "manufacturer": manufacturer,
                "product": product_name,
                "serial_number": str(item.get("serial_number") or "").strip(),
                "release_number": int(item.get("release_number") or 0),
                "usage_page": int(item.get("usage_page") or 0),
                "usage": int(item.get("usage") or 0),
                "interface_number": int(item.get("interface_number") or 0),
                "bus_type": int(item.get("bus_type") or 0),
                "known": known,
                "obviously_unrelated": obviously_unrelated,
                "hidden_reason": hidden_reason,
            }
        )

    devices.sort(
        key=lambda d: (
            0 if d["known"] else 1,
            (d["manufacturer"] or "").lower(),
            (d["product"] or "").lower(),
            d["vendor_id"],
            d["product_id"],
        )
    )
    return devices


def hid_device_for_token(token: str) -> dict:
    for device in hid_device_list():
        if device["path_token"] == token:
            return device
    raise RuntimeError("That HID device is no longer connected. Scan again.")


def test_hid_device(token: str) -> dict:
    meta = hid_device_for_token(token)
    device = hid.device()
    try:
        device.open_path(decode_hid_path(token))
    finally:
        try:
            device.close()
        except Exception:
            pass
    return {"opened": True, "device": meta}


def capture_summary(capture: dict, max_lines: int = 250) -> str:
    device = capture["device"]
    lines = [
        "Biofeedback Play HID capture",
        f"Device: {device.get('known') or device.get('product') or 'Unknown HID device'}",
        f"Manufacturer: {device.get('manufacturer') or 'Unknown'}",
        f"Vendor/Product: {device['vendor_hex']} / {device['product_hex']}",
        f"Usage page / usage: 0x{device['usage_page']:04x} / {device['usage']}",
        f"Duration: {capture['duration_s']:.3f} s",
        f"Reports received: {len(capture['reports'])}",
        "",
        "time_s    hex bytes                                              ASCII",
    ]

    for report in capture["reports"][:max_lines]:
        lines.append(
            f"{report['t']:8.4f}  {report['hex']:<54}  {report['ascii']}"
        )

    hidden = len(capture["reports"]) - max_lines
    if hidden > 0:
        lines.extend(["", f"... {hidden} additional reports are in the saved JSON capture."])

    return "\n".join(lines)


def capture_hid_device(token: str, seconds: float = 5.0) -> dict:
    seconds = max(1.0, min(float(seconds), 10.0))
    meta = hid_device_for_token(token)

    if (
        meta["vendor_id"] == VENDOR_ID
        and meta["product_id"] == PRODUCT_ID
        and STATE is not None
    ):
        lightstone = STATE.status()
        if lightstone["running"] and lightstone["connected"]:
            raise RuntimeError(
                "The Lightstone is already open for live acquisition. "
                "Stop acquisition before making a raw diagnostic capture of it."
            )

    if (
        meta["vendor_id"] == EMWAVE_VENDOR_ID
        and meta["product_id"] == EMWAVE_PRODUCT_ID
        and STATE is not None
    ):
        emwave = STATE.status()
        if emwave["emwave_running"] and emwave["emwave_connected"]:
            raise RuntimeError(
                "The emWave is already open for live acquisition. "
                "Stop emWave acquisition before making a raw diagnostic capture of it."
            )

    device = hid.device()
    reports: list[dict] = []
    started_wall = time.time()
    started_mono = time.monotonic()

    try:
        device.open_path(decode_hid_path(token))

        while time.monotonic() - started_mono < seconds and len(reports) < 5000:
            data = device.read(64, 100)
            if not data:
                continue

            values = [int(value) for value in data]
            reports.append(
                {
                    "t": round(time.monotonic() - started_mono, 6),
                    "bytes": values,
                    "hex": " ".join(f"{value:02X}" for value in values),
                    "ascii": "".join(
                        chr(value) if 32 <= value <= 126 else "."
                        for value in values
                    ),
                }
            )
    finally:
        try:
            device.close()
        except Exception:
            pass

    finished = time.monotonic()
    capture = {
        "format": "biofeedback-play-hid-capture-v1",
        "created_unix": started_wall,
        "created_local": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "duration_s": round(finished - started_mono, 6),
        "device": meta,
        "reports": reports,
    }

    label = meta.get("known") or meta.get("product") or "hid-device"
    safe_label = re.sub(r"[^A-Za-z0-9._-]+", "_", label).strip("_")[:64] or "hid-device"
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    path = CAPTURES / f"{stamp}_{safe_label}.json"
    path.write_text(json.dumps(capture, indent=2), encoding="utf-8")

    return {
        "device": meta,
        "duration_s": capture["duration_s"],
        "report_count": len(reports),
        "saved_path": str(path),
        "summary": capture_summary(capture),
    }


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

        self.emwave_running = True
        self.emwave_connected = False
        self.emwave_last_error = ""
        self.emwave_seq = 0
        self.emwave_packet_count = 0
        self.emwave_gap_count = 0
        self.emwave_samples = deque(maxlen=12000)
        self.emwave_parser = EmWaveParser()
        self.emwave_thread = threading.Thread(target=self._emwave_reader_loop, daemon=True)
        self.emwave_thread.start()

    def set_running(self, value: bool) -> None:
        with self.lock:
            self.running = bool(value)
            if not self.running:
                self.connected = False

    def set_emwave_running(self, value: bool) -> None:
        with self.lock:
            self.emwave_running = bool(value)
            if not self.emwave_running:
                self.emwave_connected = False

    def set_device_running(self, device_id: str, value: bool) -> None:
        if device_id == "lightstone":
            self.set_running(value)
        elif device_id == "emwave":
            self.set_emwave_running(value)
        else:
            raise ValueError(f"Unknown device: {device_id}")

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
            path = RECORDINGS / ("biofeedback_" + stamp + ".csv")
            fp = path.open("w", newline="", encoding="utf-8")
            writer = csv.writer(fp)
            writer.writerow(
                ["unix_time", "elapsed_s", "device_id", "signal_id", "value"]
            )
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

    def reveal_captures(self) -> None:
        try:
            subprocess.Popen(["open", str(CAPTURES)])
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
                "emwave_running": self.emwave_running,
                "emwave_connected": self.emwave_connected,
                "emwave_last_error": self.emwave_last_error,
                "emwave_latest": self.emwave_samples[-1] if self.emwave_samples else None,
                "emwave_sample_count": self.emwave_seq,
                "emwave_packet_count": self.emwave_packet_count,
                "emwave_gap_count": self.emwave_gap_count,
            }

    def device_catalog(self) -> list[dict]:
        with self.lock:
            status_by_device = {
                "lightstone": {
                    "connected": self.connected,
                    "running": self.running,
                    "error": self.last_error,
                    "sample_count": self.seq,
                },
                "emwave": {
                    "connected": self.emwave_connected,
                    "running": self.emwave_running,
                    "error": self.emwave_last_error,
                    "sample_count": self.emwave_seq,
                    "packet_count": self.emwave_packet_count,
                    "packet_gaps": self.emwave_gap_count,
                },
            }
            out = []
            for device_id, definition in DEVICE_DEFINITIONS.items():
                item = {"id": device_id, **definition, **status_by_device[device_id]}
                item["signals"] = [
                    signal_id
                    for signal_id, signal in SIGNAL_DEFINITIONS.items()
                    if signal["device_id"] == device_id
                ]
                out.append(item)
            return out

    def signal_catalog(self) -> list[dict]:
        devices = {item["id"]: item for item in self.device_catalog()}
        signals = []
        for signal_id, definition in SIGNAL_DEFINITIONS.items():
            device = devices[definition["device_id"]]
            signals.append(
                {
                    "id": signal_id,
                    **definition,
                    "device_name": device["name"],
                    "connected": device["connected"],
                    "running": device["running"],
                    "device_error": device["error"],
                    "sample_count": device["sample_count"],
                    "packet_gaps": device.get("packet_gaps"),
                }
            )
        return signals

    def signal_samples_after(self, signal_id: str, seq: int) -> list[dict]:
        definition = SIGNAL_DEFINITIONS.get(signal_id)
        if definition is None:
            raise ValueError(f"Unknown signal: {signal_id}")

        key = definition["value_key"]
        with self.lock:
            if definition["device_id"] == "lightstone":
                source = [sample for sample in self.samples if sample["seq"] > seq][-600:]
            elif definition["device_id"] == "emwave":
                source = [
                    sample for sample in self.emwave_samples if sample["seq"] > seq
                ][-1500:]
            else:
                source = []

            return [
                {
                    "seq": sample["seq"],
                    "t": sample["t"],
                    "value": sample[key],
                }
                for sample in source
            ]

    def samples_after(self, seq: int) -> list[dict]:
        with self.lock:
            return [sample for sample in self.samples if sample["seq"] > seq][-300:]

    def emwave_samples_after(self, seq: int) -> list[dict]:
        with self.lock:
            return [
                sample for sample in self.emwave_samples if sample["seq"] > seq
            ][-1200:]

    def _store_emwave_packet(self, parsed: dict) -> None:
        packet_time = time.monotonic() - self.started_monotonic
        sample_period = 1.0 / EMWAVE_NOMINAL_SAMPLE_RATE

        with self.lock:
            self.emwave_packet_count += 1
            self.emwave_gap_count += int(parsed.get("gap") or 0)

            values = parsed["samples"]
            for index, value in enumerate(values):
                self.emwave_seq += 1
                sample = {
                    "seq": self.emwave_seq,
                    "t": packet_time - (len(values) - 1 - index) * sample_period,
                    "pulse": int(value),
                    "packet": int(parsed["counter"]),
                }
                self.emwave_samples.append(sample)

                if self.recording and self.recording_writer:
                    self.recording_writer.writerow(
                        [
                            f"{time.time():.6f}",
                            f"{sample['t']:.6f}",
                            "emwave",
                            "emwave.pulse_raw",
                            int(value),
                        ]
                    )

                if self.osc_enabled:
                    target = (self.osc_host, self.osc_port)
                    try:
                        self.osc_socket.sendto(
                            osc_message("/biofeedback/emwave/pulse_raw", int(value)),
                            target,
                        )
                    except OSError as exc:
                        self.emwave_last_error = "OSC: " + str(exc)

            if self.recording and self.recording_file and self.emwave_packet_count % 10 == 0:
                self.recording_file.flush()

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
                    [
                        f"{now_unix:.6f}",
                        f"{elapsed:.6f}",
                        "lightstone",
                        "lightstone.skin_raw",
                        skin,
                    ]
                )
                self.recording_writer.writerow(
                    [
                        f"{now_unix:.6f}",
                        f"{elapsed:.6f}",
                        "lightstone",
                        "lightstone.pulse_raw",
                        pulse,
                    ]
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
                    if not hid.enumerate(VENDOR_ID, PRODUCT_ID):
                        with self.lock:
                            self.connected = False
                            self.last_error = ""
                        time.sleep(1.0)
                        continue

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

    def _emwave_reader_loop(self) -> None:
        device = None

        while not self.shutdown:
            with self.lock:
                should_run = self.emwave_running

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
                    if not hid.enumerate(EMWAVE_VENDOR_ID, EMWAVE_PRODUCT_ID):
                        with self.lock:
                            self.emwave_connected = False
                            self.emwave_last_error = ""
                        time.sleep(1.0)
                        continue

                    device = hid.device()
                    device.open(EMWAVE_VENDOR_ID, EMWAVE_PRODUCT_ID)
                    with self.lock:
                        self.emwave_connected = True
                        self.emwave_last_error = ""
                        self.emwave_parser = EmWaveParser()

                report = device.read(8, 500)
                if not report:
                    continue

                parsed = self.emwave_parser.feed_report(report)
                if parsed is not None:
                    self._store_emwave_packet(parsed)

            except Exception as exc:
                with self.lock:
                    self.emwave_connected = False
                    self.emwave_last_error = str(exc)
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
.diagnostics { grid-column: span 12; }
.device-table { width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 13px; }
.device-table th { color: var(--muted); font-weight: 600; text-align: left; border-bottom: 1px solid var(--line); padding: 8px 7px; }
.device-table td { border-bottom: 1px solid rgba(52,59,78,.6); padding: 8px 7px; vertical-align: top; }
.device-table tr.selected { background: rgba(196,181,253,.09); }
.device-table code { color: #d8dce7; }
.diag-output {
  white-space: pre-wrap; word-break: break-word; margin: 12px 0 0;
  max-height: 330px; overflow: auto; padding: 12px;
  background: #090c12; border: 1px solid var(--line); border-radius: 10px;
  font: 12px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace;
}
select {
  font: inherit; color: var(--text); background: #0d1017;
  border: 1px solid var(--line); border-radius: 9px; padding: 8px 9px;
}
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
        Sends /biofeedback/lightstone/skin_raw, /biofeedback/lightstone/pulse_raw,
        and /biofeedback/emwave/pulse_raw. Default SuperCollider language port is usually 57120.
      </div>
    </section>



    <section class="card chart">
      <div class="charthead">
        <div>
          <div class="label">HeartMath emWave pulse waveform</div>
          <div class="small">Experimental direct USB decoding. Each observed HID report carries six 8-bit waveform samples.</div>
        </div>
        <div class="row">
          <span class="status"><span id="emwaveDot" class="dot"></span><span id="emwaveStatus">Waiting for emWave</span></span>
          <button id="emwaveRunBtn">Stop emWave</button>
        </div>
      </div>
      <div class="row" style="margin-top:12px">
        <div><span class="label">Pulse raw</span> <strong id="emwaveValue">—</strong></div>
        <div><span class="label">Samples</span> <strong id="emwaveSampleValue">0</strong></div>
        <div><span class="label">Packet gaps</span> <strong id="emwaveGapValue">0</strong></div>
      </div>
      <canvas id="emwaveChart"></canvas>
    </section>

    <section class="card diagnostics">
      <div class="charthead">
        <div>
          <div class="label">Devices & diagnostics</div>
          <div class="small">Scan USB HID hardware, test access, and make short raw captures without Terminal.</div>
        </div>
        <div class="row">
          <label class="small" style="margin-top:0">
            <input id="showAllDevices" type="checkbox">
            Show obviously unrelated HID devices
          </label>
          <button id="scanBtn">Scan devices</button>
        </div>
      </div>

      <div id="deviceList"></div>

      <div class="row" style="margin-top:12px">
        <strong id="selectedDevice">No device selected</strong>
        <select id="captureSeconds" aria-label="Capture duration">
          <option value="2">2 second capture</option>
          <option value="5" selected>5 second capture</option>
          <option value="10">10 second capture</option>
        </select>
        <button id="testDeviceBtn">Test open</button>
        <button id="captureDeviceBtn" class="primary">Capture raw reports</button>
        <button id="copyDiagBtn">Copy report</button>
        <button id="captureFolderBtn">Show captures</button>
      </div>

      <pre id="diagOutput" class="diag-output">Scan, select a device, then test or capture it.</pre>
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
let emwaveLastSeq = 0;
const emwavePulse = [];
const maxPoints = 620;
const emwaveMaxPoints = 1500;

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


let diagnosticDevice = null;
let diagnosticText = "";
let diagnosticDevices = [];

function escapeHtml(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function deviceName(d) {
  return d.known || d.product || "Unnamed HID device";
}

function selectDiagnosticDevice(d, row) {
  diagnosticDevice = d;
  document.querySelectorAll(".device-table tr").forEach(function(r) {
    r.classList.remove("selected");
  });
  if (row) row.classList.add("selected");
  document.getElementById("selectedDevice").textContent =
    deviceName(d) + "  " + d.vendor_hex + ":" + d.product_hex;
}

function renderDevices(devices) {
  diagnosticDevices = devices || [];
  const host = document.getElementById("deviceList");
  const showAll = document.getElementById("showAllDevices").checked;
  const visibleDevices = showAll
    ? diagnosticDevices
    : diagnosticDevices.filter(function(d) { return !d.obviously_unrelated; });
  const hiddenCount = diagnosticDevices.length - visibleDevices.length;

  if (!diagnosticDevices.length) {
    host.innerHTML = '<div class="small">No HID devices were found.</div>';
    return;
  }

  if (!visibleDevices.length) {
    host.innerHTML =
      '<div class="small">No likely or unclassified devices are visible. ' +
      hiddenCount + ' obviously unrelated device(s) are hidden. ' +
      'Turn on “Show obviously unrelated HID devices” to inspect them.</div>';
    return;
  }

  let html = '';
  if (hiddenCount > 0) {
    html += '<div class="small">' + hiddenCount +
      ' obviously unrelated HID device(s) hidden.</div>';
  }

  html += '<table class="device-table"><thead><tr>' +
    '<th>Device</th><th>Manufacturer</th><th>USB ID</th>' +
    '<th>Usage</th><th></th></tr></thead><tbody>';

  visibleDevices.forEach(function(d, index) {
    const hiddenNote = d.obviously_unrelated && d.hidden_reason
      ? '<div class="small">Normally hidden: ' + escapeHtml(d.hidden_reason) + '</div>'
      : '';
    html += '<tr data-device-index="' + index + '">' +
      '<td><strong>' + escapeHtml(deviceName(d)) + '</strong>' +
      (d.known ? '<div class="small">' + escapeHtml(d.product) + '</div>' : '') +
      hiddenNote +
      '</td>' +
      '<td>' + escapeHtml(d.manufacturer || "—") + '</td>' +
      '<td><code>' + escapeHtml(d.vendor_hex + ":" + d.product_hex) + '</code></td>' +
      '<td><code>0x' + Number(d.usage_page).toString(16).padStart(4, "0") +
      ' / ' + escapeHtml(d.usage) + '</code></td>' +
      '<td><button class="select-device">Select</button></td>' +
      '</tr>';
  });

  html += '</tbody></table>';
  host.innerHTML = html;

  host.querySelectorAll("tr[data-device-index]").forEach(function(row) {
    const index = Number(row.dataset.deviceIndex);
    row.querySelector(".select-device").onclick = function() {
      selectDiagnosticDevice(visibleDevices[index], row);
    };
  });

  const emwaveIndex = visibleDevices.findIndex(function(d) {
    return d.vendor_id === 0x0e30 && d.product_id === 0x0002;
  });
  if (emwaveIndex >= 0) {
    const row = host.querySelector('tr[data-device-index="' + emwaveIndex + '"]');
    selectDiagnosticDevice(visibleDevices[emwaveIndex], row);
  }
}

function scanDevices() {
  const output = document.getElementById("diagOutput");
  output.textContent = "Scanning HID devices…";
  fetch("/api/devices")
    .then(r => r.json())
    .then(function(data) {
      diagnosticDevices = data.devices || [];
      renderDevices(diagnosticDevices);
      const hidden = diagnosticDevices.filter(function(d) {
        return d.obviously_unrelated;
      }).length;
      output.textContent =
        "Found " + diagnosticDevices.length + " HID device(s)." +
        (hidden ? " " + hidden + " obviously unrelated device(s) hidden by default." : "") +
        " Select one to test or capture.";
    })
    .catch(function(err) {
      output.textContent = String(err);
    });
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

      const emwaveDot = document.getElementById("emwaveDot");
      emwaveDot.className = s.emwave_connected ? "dot on" : "dot";
      const emwaveStatus = document.getElementById("emwaveStatus");
      if (!s.emwave_running) emwaveStatus.textContent = "emWave stopped";
      else if (s.emwave_connected) emwaveStatus.textContent = "emWave connected";
      else emwaveStatus.textContent = "Waiting for emWave";

      const emwaveBtn = document.getElementById("emwaveRunBtn");
      emwaveBtn.textContent = s.emwave_running ? "Stop emWave" : "Start emWave";
      emwaveBtn.className = s.emwave_running ? "primary" : "";
      document.getElementById("emwaveSampleValue").textContent = s.emwave_sample_count || 0;
      document.getElementById("emwaveGapValue").textContent = s.emwave_gap_count || 0;
      if (s.emwave_latest) {
        document.getElementById("emwaveValue").textContent = s.emwave_latest.pulse;
      }
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



function pollEmWaveSamples() {
  fetch("/api/emwave_samples?after=" + emwaveLastSeq)
    .then(r => r.json())
    .then(function(data) {
      data.samples.forEach(function(s) {
        emwaveLastSeq = Math.max(emwaveLastSeq, s.seq);
        emwavePulse.push(s.pulse);
      });
      if (emwavePulse.length > emwaveMaxPoints) {
        emwavePulse.splice(0, emwavePulse.length - emwaveMaxPoints);
      }
      if (data.samples.length) {
        const latest = data.samples[data.samples.length - 1];
        document.getElementById("emwaveValue").textContent = latest.pulse;
      }
      draw(
        document.getElementById("emwaveChart"),
        emwavePulse,
        "#c4b5fd",
        { textContent: "" }
      );
    })
    .catch(function(err) {
      document.getElementById("error").textContent = String(err);
    });
}

document.getElementById("scanBtn").onclick = scanDevices;
document.getElementById("showAllDevices").onchange = function() {
  renderDevices(diagnosticDevices);
};

document.getElementById("testDeviceBtn").onclick = function() {
  const output = document.getElementById("diagOutput");
  if (!diagnosticDevice) {
    output.textContent = "Select a device first.";
    return;
  }

  output.textContent = "Testing access to " + deviceName(diagnosticDevice) + "…";
  post("diag_test", {path: diagnosticDevice.path_token}).then(function(result) {
    if (!result.ok) throw new Error(result.error || "Device test failed.");
    diagnosticText =
      "Opened successfully.\n" +
      deviceName(diagnosticDevice) + "\n" +
      diagnosticDevice.vendor_hex + ":" + diagnosticDevice.product_hex;
    output.textContent = diagnosticText;
  }).catch(function(err) {
    output.textContent = String(err);
  });
};

document.getElementById("captureDeviceBtn").onclick = function() {
  const output = document.getElementById("diagOutput");
  if (!diagnosticDevice) {
    output.textContent = "Select a device first.";
    return;
  }

  const seconds = Number(document.getElementById("captureSeconds").value || 5);
  output.textContent =
    "Capturing " + seconds + " seconds from " + deviceName(diagnosticDevice) + "…";

  post("diag_capture", {
    path: diagnosticDevice.path_token,
    seconds: seconds
  }).then(function(result) {
    if (!result.ok) throw new Error(result.error || "Capture failed.");
    diagnosticText = result.result.summary || "";
    output.textContent = diagnosticText +
      "\n\nSaved locally: " + result.result.saved_path;
  }).catch(function(err) {
    output.textContent = String(err);
  });
};

document.getElementById("copyDiagBtn").onclick = function() {
  const output = document.getElementById("diagOutput");
  const text = diagnosticText || output.textContent;
  if (!text) return;

  navigator.clipboard.writeText(text).then(function() {
    const old = output.textContent;
    output.textContent = old + "\n\n[Copied to clipboard]";
  }).catch(function() {
    output.textContent += "\n\nClipboard access failed. Select the text above and copy it manually.";
  });
};

document.getElementById("captureFolderBtn").onclick = function() {
  post("reveal_captures");
};

document.getElementById("emwaveRunBtn").onclick = function() {
  fetch("/api/status").then(r => r.json()).then(function(s) {
    return post(s.emwave_running ? "emwave_stop" : "emwave_start");
  }).then(refreshStatus);
};

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
  draw(document.getElementById("emwaveChart"), emwavePulse, "#c4b5fd", { textContent: "" });
});

refreshStatus();
scanDevices();
setInterval(refreshStatus, 1000);
setInterval(pollSamples, 100);
setInterval(pollEmWaveSamples, 100);
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

        if parsed.path == "/api/catalog":
            self.send_json(
                {
                    "devices": STATE.device_catalog(),
                    "signals": STATE.signal_catalog(),
                }
            )
            return

        if parsed.path == "/api/signal_samples":
            query = urllib.parse.parse_qs(parsed.query)
            signal_id = query.get("id", [""])[0]
            try:
                after = int(query.get("after", ["0"])[0])
            except ValueError:
                after = 0
            self.send_json(
                {"samples": STATE.signal_samples_after(signal_id, after)}
            )
            return

        if parsed.path == "/api/devices":
            self.send_json({"devices": hid_device_list()})
            return

        if parsed.path == "/api/samples":
            query = urllib.parse.parse_qs(parsed.query)
            try:
                after = int(query.get("after", ["0"])[0])
            except ValueError:
                after = 0
            self.send_json({"samples": STATE.samples_after(after)})
            return

        if parsed.path == "/api/emwave_samples":
            query = urllib.parse.parse_qs(parsed.query)
            try:
                after = int(query.get("after", ["0"])[0])
            except ValueError:
                after = 0
            self.send_json({"samples": STATE.emwave_samples_after(after)})
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

            if action == "device_start":
                STATE.set_device_running(str(payload.get("device_id") or ""), True)
            elif action == "device_stop":
                STATE.set_device_running(str(payload.get("device_id") or ""), False)
            elif action == "start":
                STATE.set_running(True)
            elif action == "emwave_start":
                STATE.set_emwave_running(True)
            elif action == "emwave_stop":
                STATE.set_emwave_running(False)
            elif action == "stop":
                STATE.set_running(False)
            elif action == "record_start":
                STATE.start_recording()
            elif action == "record_stop":
                STATE.stop_recording()
            elif action == "reveal_recordings":
                STATE.reveal_recordings()
            elif action == "reveal_captures":
                STATE.reveal_captures()
            elif action == "diag_test":
                result = test_hid_device(str(payload.get("path") or ""))
                self.send_json({"ok": True, "result": result})
                return
            elif action == "diag_capture":
                result = capture_hid_device(
                    str(payload.get("path") or ""),
                    float(payload.get("seconds") or 5),
                )
                self.send_json({"ok": True, "result": result})
                return
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
    print("HeartMath emWave USB: 0E30:0002")
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
