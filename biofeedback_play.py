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

from devices.muse2014 import (
    CHANNEL_NAMES as MUSE_CHANNEL_NAMES,
    MUSE_ACCEL_RATE,
    MUSE_EEG_RATE,
    Muse2014SerialClient,
    list_muse_serial_ports,
)
from physiology import eeg_metrics, motion_metrics, pair_metrics, pulse_metrics, skin_metrics


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
SETTINGS_PATH = ROOT / "settings.json"


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(settings: dict) -> None:
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")

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
    "muse": {
        "name": "InteraXon Muse 2014",
        "manufacturer": "InteraXon",
        "transport": "Bluetooth RFCOMM / serial",
        "usb_id": "not applicable",
        "summary": "MU-01 EEG headband with four EEG channels, accelerometer, and battery telemetry.",
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

    "muse.eeg.tp9": {
        "device_id": "muse",
        "name": "EEG TP9",
        "short_name": "TP9",
        "data_label": "Experimental EEG channel",
        "unit": "µV, experimental scaling",
        "description": "Left temporal Muse EEG channel. Microvolt scaling follows the clean-room MU-01 implementation and is not treated as calibrated.",
        "audio": "Pitch follows the recent EEG level. This is a sonification, not an audible representation of brain activity.",
        "osc": "/biofeedback/muse/eeg/tp9",
        "nominal_rate": MUSE_EEG_RATE,
        "value_key": "tp9",
    },
    "muse.eeg.fp1": {
        "device_id": "muse",
        "name": "EEG FP1",
        "short_name": "FP1",
        "data_label": "Experimental EEG channel",
        "unit": "µV, experimental scaling",
        "description": "Left frontal Muse EEG channel. Microvolt scaling follows the clean-room MU-01 implementation and is not treated as calibrated.",
        "audio": "Pitch follows the recent EEG level.",
        "osc": "/biofeedback/muse/eeg/fp1",
        "nominal_rate": MUSE_EEG_RATE,
        "value_key": "fp1",
    },
    "muse.eeg.fp2": {
        "device_id": "muse",
        "name": "EEG FP2",
        "short_name": "FP2",
        "data_label": "Experimental EEG channel",
        "unit": "µV, experimental scaling",
        "description": "Right frontal Muse EEG channel. Microvolt scaling follows the clean-room MU-01 implementation and is not treated as calibrated.",
        "audio": "Pitch follows the recent EEG level.",
        "osc": "/biofeedback/muse/eeg/fp2",
        "nominal_rate": MUSE_EEG_RATE,
        "value_key": "fp2",
    },
    "muse.eeg.tp10": {
        "device_id": "muse",
        "name": "EEG TP10",
        "short_name": "TP10",
        "data_label": "Experimental EEG channel",
        "unit": "µV, experimental scaling",
        "description": "Right temporal Muse EEG channel. Microvolt scaling follows the clean-room MU-01 implementation and is not treated as calibrated.",
        "audio": "Pitch follows the recent EEG level.",
        "osc": "/biofeedback/muse/eeg/tp10",
        "nominal_rate": MUSE_EEG_RATE,
        "value_key": "tp10",
    },
    "muse.accel.x": {
        "device_id": "muse",
        "name": "Head motion X",
        "short_name": "Accel X",
        "data_label": "Accelerometer axis",
        "unit": "raw signed 10-bit units",
        "description": "Muse headband accelerometer X axis. Raw signed counts are preserved because physical scaling is not yet independently verified.",
        "audio": "Pitch follows the recent X-axis motion.",
        "osc": "/biofeedback/muse/accel/x",
        "nominal_rate": MUSE_ACCEL_RATE,
        "value_key": "x",
    },
    "muse.accel.y": {
        "device_id": "muse",
        "name": "Head motion Y",
        "short_name": "Accel Y",
        "data_label": "Accelerometer axis",
        "unit": "raw signed 10-bit units",
        "description": "Muse headband accelerometer Y axis. Raw signed counts are preserved because physical scaling is not yet independently verified.",
        "audio": "Pitch follows the recent Y-axis motion.",
        "osc": "/biofeedback/muse/accel/y",
        "nominal_rate": MUSE_ACCEL_RATE,
        "value_key": "y",
    },
    "muse.accel.z": {
        "device_id": "muse",
        "name": "Head motion Z",
        "short_name": "Accel Z",
        "data_label": "Accelerometer axis",
        "unit": "raw signed 10-bit units",
        "description": "Muse headband accelerometer Z axis. Raw signed counts are preserved because physical scaling is not yet independently verified.",
        "audio": "Pitch follows the recent Z-axis motion.",
        "osc": "/biofeedback/muse/accel/z",
        "nominal_rate": MUSE_ACCEL_RATE,
        "value_key": "z",
    },
}

def _derived_signal(
    device_id: str,
    name: str,
    data_label: str,
    unit: str,
    description: str,
    osc: str,
    value_key: str,
    *,
    audio: str = "Pitch follows the derived value.",
    precision: int = 1,
    requires_devices: list[str] | None = None,
    source_name: str | None = None,
) -> dict:
    return {
        "device_id": device_id,
        "name": name,
        "short_name": name,
        "data_label": data_label,
        "unit": unit,
        "description": description,
        "audio": audio,
        "osc": osc,
        "nominal_rate": 1.0,
        "value_key": value_key,
        "derived": True,
        "precision": precision,
        "requires_devices": requires_devices or [device_id],
        "source_name": source_name,
    }


def _pulse_derived_definitions(device_id: str, label: str, osc_prefix: str) -> dict[str, dict]:
    return {
        f"{device_id}.heart_rate": _derived_signal(
            device_id, "Heart rate", "Beat-derived heart rate", "beats/min",
            f"Median recent beat rate derived from the {label} pulse waveform. This is computed from detected pulse peaks.",
            f"{osc_prefix}/heart_rate", "heart_rate_bpm", precision=1,
            audio="Pitch follows heart rate; higher beats per minute produce a higher tone.",
        ),
        f"{device_id}.ibi": _derived_signal(
            device_id, "Inter-beat interval", "Time between the latest detected beats", "ms",
            f"Time between successive detected beats from the {label} pulse waveform. This is the raw material for HRV.",
            f"{osc_prefix}/ibi_ms", "ibi_ms", precision=0,
        ),
        f"{device_id}.hrv_rmssd": _derived_signal(
            device_id, "HRV RMSSD", "Short-term heart-rate variability", "ms",
            "Root mean square of successive inter-beat-interval differences. It summarizes beat-to-beat variability; short windows are exploratory rather than a standardized clinical assessment.",
            f"{osc_prefix}/hrv_rmssd_ms", "rmssd_ms", precision=1,
        ),
        f"{device_id}.hrv_sdnn": _derived_signal(
            device_id, "HRV SDNN", "Inter-beat-interval standard deviation", "ms",
            "Standard deviation of recent detected inter-beat intervals. Values depend strongly on recording duration and conditions.",
            f"{osc_prefix}/hrv_sdnn_ms", "sdnn_ms", precision=1,
        ),
        f"{device_id}.pnn50": _derived_signal(
            device_id, "pNN50", "Large successive IBI changes", "%",
            "Percentage of successive detected inter-beat intervals differing by more than 50 ms in the current rolling window.",
            f"{osc_prefix}/pnn50_percent", "pnn50_percent", precision=1,
        ),
        f"{device_id}.coherence_ratio": _derived_signal(
            device_id, "Coherence ratio", "Open HRV spectral coherence measure", "ratio",
            "Open implementation inspired by published coherence definitions: power in a narrow peak within 0.04–0.26 Hz divided by remaining HRV spectral power. It is not HeartMath's proprietary emWave score.",
            f"{osc_prefix}/coherence_ratio", "coherence_ratio", precision=2,
        ),
        f"{device_id}.coherence_peak": _derived_signal(
            device_id, "Coherence peak share", "HRV power concentrated near dominant coherence peak", "%",
            "Percentage of analyzed HRV spectral power concentrated within ±0.015 Hz of the dominant peak in the 0.04–0.26 Hz coherence range.",
            f"{osc_prefix}/coherence_peak_percent", "coherence_peak_percent", precision=1,
        ),
        f"{device_id}.respiration_estimate": _derived_signal(
            device_id, "Breathing estimate", "Respiration inferred from HRV", "breaths/min",
            "Experimental respiration-rate estimate from the dominant respiratory modulation of beat intervals. It is indirect and only appears when enough data and a sufficiently concentrated rhythm are present.",
            f"{osc_prefix}/respiration_bpm", "respiration_bpm", precision=1,
        ),
        f"{device_id}.pulse_amplitude": _derived_signal(
            device_id, "Pulse amplitude", "Recent pulse-waveform range", "raw device units",
            f"Robust 5-second pulse-wave amplitude from the {label} waveform, using the 5th-to-95th percentile range.",
            f"{osc_prefix}/pulse_amplitude", "pulse_amplitude", precision=1,
        ),
        f"{device_id}.beat_confidence": _derived_signal(
            device_id, "Beat confidence", "Beat detector confidence", "%",
            "Heuristic confidence in automated beat detection, based on plausible intervals, regularity, and waveform amplitude. This is a software quality indicator, not physiology.",
            f"{osc_prefix}/beat_confidence_percent", "beat_confidence_percent", precision=0,
        ),
    }


SIGNAL_DEFINITIONS.update(_pulse_derived_definitions(
    "lightstone", "Lightstone gold-dot finger sensor", "/biofeedback/lightstone"
))
SIGNAL_DEFINITIONS.update(_pulse_derived_definitions(
    "emwave", "emWave ear clip", "/biofeedback/emwave"
))

SIGNAL_DEFINITIONS.update({
    "lightstone.skin_tonic": _derived_signal(
        "lightstone", "Skin tonic level", "Slow skin-conductance baseline", "raw device units",
        "Ten-second moving mean of the Lightstone skin-conductance channel. It approximates the slow tonic component but cannot be expressed in microsiemens without calibration.",
        "/biofeedback/lightstone/skin_tonic", "tonic_level", precision=1,
    ),
    "lightstone.skin_phasic": _derived_signal(
        "lightstone", "Skin phasic activity", "Fast skin-conductance component", "raw device units",
        "Current skin-conductance value minus the recent tonic baseline. It highlights faster changes without claiming a calibrated SCR amplitude.",
        "/biofeedback/lightstone/skin_phasic", "phasic_level", precision=1,
    ),
    "lightstone.skin_slope": _derived_signal(
        "lightstone", "Skin trend", "Ten-second skin-conductance slope", "raw units/min",
        "Linear trend of the recent skin-conductance signal. Positive values indicate rising conductance; negative values indicate falling conductance.",
        "/biofeedback/lightstone/skin_slope", "slope_per_min", precision=1,
    ),
    "lightstone.skin_responses": _derived_signal(
        "lightstone", "Skin responses", "Relative phasic response rate", "responses/min",
        "Experimental count of rapid relative skin-conductance peaks per minute. Because Lightstone units are uncalibrated, the threshold adapts to the recent signal rather than using a clinical microsiemens threshold.",
        "/biofeedback/lightstone/skin_response_rate", "response_rate_per_min", precision=1,
    ),
    "lightstone.skin_variability": _derived_signal(
        "lightstone", "Skin variability", "Recent skin-conductance spread", "raw device units",
        "Thirty-second standard deviation of the raw skin-conductance channel.",
        "/biofeedback/lightstone/skin_variability", "variability", precision=2,
    ),
    "muse.band.delta": _derived_signal(
        "muse", "EEG delta power", "1–4 Hz EEG band power", "µV², experimental",
        "Average spectral power across the four Muse EEG channels in the delta band. Scaling remains experimental.",
        "/biofeedback/muse/band/delta", "delta_power", precision=2,
    ),
    "muse.band.theta": _derived_signal(
        "muse", "EEG theta power", "4–8 Hz EEG band power", "µV², experimental",
        "Average spectral power across the four Muse EEG channels in the theta band.",
        "/biofeedback/muse/band/theta", "theta_power", precision=2,
    ),
    "muse.band.alpha": _derived_signal(
        "muse", "EEG alpha power", "8–13 Hz EEG band power", "µV², experimental",
        "Average spectral power across the four Muse EEG channels in the alpha band.",
        "/biofeedback/muse/band/alpha", "alpha_power", precision=2,
    ),
    "muse.band.beta": _derived_signal(
        "muse", "EEG beta power", "13–30 Hz EEG band power", "µV², experimental",
        "Average spectral power across the four Muse EEG channels in the beta band.",
        "/biofeedback/muse/band/beta", "beta_power", precision=2,
    ),
    "muse.band.gamma": _derived_signal(
        "muse", "EEG gamma power", "30–45 Hz EEG band power", "µV², experimental",
        "Average spectral power across the four Muse EEG channels in the gamma band. Muscle activity can strongly contaminate this range.",
        "/biofeedback/muse/band/gamma", "gamma_power", precision=2,
    ),
    "muse.alpha_asymmetry": _derived_signal(
        "muse", "Frontal alpha asymmetry", "FP2 minus FP1 log alpha power", "log-power difference",
        "Experimental right-minus-left frontal alpha log-power index. It is shown as a signal feature, not as a mood or personality diagnosis.",
        "/biofeedback/muse/alpha_asymmetry", "alpha_asymmetry", precision=3,
    ),
    "muse.eeg_rms": _derived_signal(
        "muse", "EEG broadband RMS", "Broadband EEG variation", "µV, experimental",
        "Root-mean-square variation across the four recent EEG channels after mean removal. Large muscle or motion artifacts can dominate it.",
        "/biofeedback/muse/eeg_rms", "broadband_rms", precision=2,
    ),
    "muse.motion_intensity": _derived_signal(
        "muse", "Head motion intensity", "Accelerometer variation", "raw counts RMS",
        "Combined recent variation across the Muse accelerometer axes. It suppresses constant gravity and emphasizes movement.",
        "/biofeedback/muse/motion_intensity", "motion_intensity", precision=1,
    ),
    "comparison.lightstone_emwave.hr_difference": _derived_signal(
        "emwave", "Pulse-source HR difference", "Lightstone vs emWave heart-rate difference", "beats/min",
        "Absolute difference between independently detected heart rates from Lightstone and emWave. Useful for validating the two pulse pipelines.",
        "/biofeedback/comparison/lightstone_emwave/hr_difference",
        "heart_rate_difference_bpm", precision=2,
        requires_devices=["lightstone", "emwave"],
        source_name="Lightstone + emWave",
    ),
    "comparison.lightstone_emwave.beat_offset": _derived_signal(
        "emwave", "Pulse-source beat offset", "Median Lightstone vs emWave beat timing offset", "ms",
        "Median nearest-beat timing difference between the two pulse sensors. Sensor placement and USB buffering contribute to this value, so it is not a medical pulse-transit-time measurement.",
        "/biofeedback/comparison/lightstone_emwave/beat_offset_ms",
        "beat_offset_ms", precision=1,
        requires_devices=["lightstone", "emwave"],
        source_name="Lightstone + emWave",
    ),
    "comparison.lightstone_emwave.correlation": _derived_signal(
        "emwave", "Pulse-source correlation", "Recent waveform correlation", "correlation −1…1",
        "Experimental correlation between recent Lightstone and emWave pulse waveforms after time interpolation. Different sensor shapes and delays can lower it.",
        "/biofeedback/comparison/lightstone_emwave/correlation",
        "waveform_correlation", precision=3,
        requires_devices=["lightstone", "emwave"],
        source_name="Lightstone + emWave",
    ),
})


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

        settings = load_settings()
        self.muse_running = True
        self.muse_connected = False
        self.muse_last_error = ""
        self.muse_port = str(settings.get("muse_port") or "")
        self.muse_version = ""
        self.muse_afe_gain = None
        self.muse_battery = None
        self.muse_eeg_seq = 0
        self.muse_accel_seq = 0
        self.muse_eeg_samples = deque(maxlen=12000)
        self.muse_accel_samples = deque(maxlen=4000)
        self.muse_thread = threading.Thread(target=self._muse_reader_loop, daemon=True)
        self.muse_thread.start()

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

    def set_muse_running(self, value: bool) -> None:
        with self.lock:
            self.muse_running = bool(value)
            if not self.muse_running:
                self.muse_connected = False

    def set_muse_port(self, port: str) -> None:
        with self.lock:
            self.muse_port = str(port or "")
            self.muse_connected = False
            self.muse_last_error = ""
        settings = load_settings()
        settings["muse_port"] = self.muse_port
        save_settings(settings)

    def set_device_running(self, device_id: str, value: bool) -> None:
        if device_id == "lightstone":
            self.set_running(value)
        elif device_id == "emwave":
            self.set_emwave_running(value)
        elif device_id == "muse":
            self.set_muse_running(value)
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

    def open_bluetooth_settings(self) -> None:
        try:
            subprocess.Popen(
                ["open", "x-apple.systempreferences:com.apple.BluetoothSettings"]
            )
        except Exception:
            try:
                subprocess.Popen(
                    ["open", "/System/Library/PreferencePanes/Bluetooth.prefPane"]
                )
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
                "muse_running": self.muse_running,
                "muse_connected": self.muse_connected,
                "muse_last_error": self.muse_last_error,
                "muse_port": self.muse_port,
                "muse_version": self.muse_version,
                "muse_afe_gain": self.muse_afe_gain,
                "muse_battery": self.muse_battery,
                "muse_eeg_sample_count": self.muse_eeg_seq,
                "muse_accel_sample_count": self.muse_accel_seq,
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
                "muse": {
                    "connected": self.muse_connected,
                    "running": self.muse_running,
                    "error": self.muse_last_error,
                    "sample_count": self.muse_eeg_seq,
                    "eeg_sample_count": self.muse_eeg_seq,
                    "accel_sample_count": self.muse_accel_seq,
                    "port": self.muse_port,
                    "version": self.muse_version,
                    "afe_gain": self.muse_afe_gain,
                    "battery": self.muse_battery,
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
            sample_count = device["sample_count"]
            if signal_id.startswith("muse.accel."):
                sample_count = device.get("accel_sample_count", 0)
            elif signal_id.startswith("muse.eeg."):
                sample_count = device.get("eeg_sample_count", 0)

            signals.append(
                {
                    "id": signal_id,
                    **definition,
                    "device_name": device["name"],
                    "connected": device["connected"],
                    "running": device["running"],
                    "device_error": device["error"],
                    "sample_count": sample_count,
                    "packet_gaps": device.get("packet_gaps"),
                    "battery": device.get("battery"),
                    "afe_gain": device.get("afe_gain"),
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
            elif definition["device_id"] == "muse":
                if signal_id.startswith("muse.eeg."):
                    source = [
                        sample for sample in self.muse_eeg_samples if sample["seq"] > seq
                    ][-1500:]
                else:
                    source = [
                        sample for sample in self.muse_accel_samples if sample["seq"] > seq
                    ][-800:]
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


    def _store_muse_eeg(self, decoded: dict) -> None:
        values = decoded["microvolts"]
        now_unix = time.time()
        elapsed = time.monotonic() - self.started_monotonic
        keys = ("tp9", "fp1", "fp2", "tp10")
        osc_paths = (
            "/biofeedback/muse/eeg/tp9",
            "/biofeedback/muse/eeg/fp1",
            "/biofeedback/muse/eeg/fp2",
            "/biofeedback/muse/eeg/tp10",
        )

        with self.lock:
            self.muse_eeg_seq += 1
            sample = {"seq": self.muse_eeg_seq, "t": elapsed}
            for key, value in zip(keys, values):
                sample[key] = float(value)
            self.muse_eeg_samples.append(sample)

            if self.recording and self.recording_writer:
                for key, value in zip(keys, values):
                    self.recording_writer.writerow(
                        [
                            f"{now_unix:.6f}",
                            f"{elapsed:.6f}",
                            "muse",
                            f"muse.eeg.{key}",
                            float(value),
                        ]
                    )
                if self.muse_eeg_seq % 100 == 0 and self.recording_file:
                    self.recording_file.flush()

            if self.osc_enabled:
                target = (self.osc_host, self.osc_port)
                try:
                    for path, value in zip(osc_paths, values):
                        self.osc_socket.sendto(osc_message(path, float(value)), target)
                except OSError as exc:
                    self.muse_last_error = "OSC: " + str(exc)

    def _store_muse_accel(self, values: tuple[int, int, int]) -> None:
        now_unix = time.time()
        elapsed = time.monotonic() - self.started_monotonic
        keys = ("x", "y", "z")
        osc_paths = (
            "/biofeedback/muse/accel/x",
            "/biofeedback/muse/accel/y",
            "/biofeedback/muse/accel/z",
        )

        with self.lock:
            self.muse_accel_seq += 1
            sample = {"seq": self.muse_accel_seq, "t": elapsed}
            for key, value in zip(keys, values):
                sample[key] = int(value)
            self.muse_accel_samples.append(sample)

            if self.recording and self.recording_writer:
                for key, value in zip(keys, values):
                    self.recording_writer.writerow(
                        [
                            f"{now_unix:.6f}",
                            f"{elapsed:.6f}",
                            "muse",
                            f"muse.accel.{key}",
                            int(value),
                        ]
                    )

            if self.osc_enabled:
                target = (self.osc_host, self.osc_port)
                try:
                    for path, value in zip(osc_paths, values):
                        self.osc_socket.sendto(osc_message(path, int(value)), target)
                except OSError as exc:
                    self.muse_last_error = "OSC: " + str(exc)

    def _store_muse_battery(self, battery: dict) -> None:
        with self.lock:
            self.muse_battery = battery

    def _store_muse_status(self, status) -> None:
        with self.lock:
            self.muse_version = status.version
            self.muse_afe_gain = status.afe_gain

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


    def _muse_reader_loop(self) -> None:
        while not self.shutdown:
            with self.lock:
                should_run = self.muse_running
                port = self.muse_port

            if not should_run or not port:
                with self.lock:
                    self.muse_connected = False
                time.sleep(0.5)
                continue

            client = Muse2014SerialClient(
                port=port,
                on_eeg=self._store_muse_eeg,
                on_accelerometer=self._store_muse_accel,
                on_battery=self._store_muse_battery,
                on_status=self._store_muse_status,
            )

            try:
                client.open_and_configure()
                with self.lock:
                    if port != self.muse_port:
                        client.close()
                        continue
                    self.muse_connected = True
                    self.muse_last_error = ""

                client.run(
                    lambda: self.shutdown
                    or (not self.muse_running)
                    or (self.muse_port != port)
                )
            except Exception as exc:
                with self.lock:
                    self.muse_connected = False
                    self.muse_last_error = str(exc)
                time.sleep(1.0)
            finally:
                client.close()
                with self.lock:
                    self.muse_connected = False

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
  --accent: #8b7cf6;
  --accent2: #c4b5fd;
  --soft: rgba(255,255,255,.035);
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: radial-gradient(circle at 20% 0%, #171b27, var(--bg) 44%);
  color: var(--text);
}
main { max-width: 1180px; margin: 0 auto; padding: 26px 22px 52px; }
h1 { margin: 0; font-size: 30px; font-weight: 720; letter-spacing: -.03em; }
h2 { margin: 0; font-size: 18px; }
.subtitle { margin-top: 5px; color: var(--muted); }
.topbar {
  display: flex; justify-content: space-between; align-items: center;
  gap: 14px; flex-wrap: wrap; margin-bottom: 18px;
}
.status-pill {
  display: inline-flex; align-items: center; gap: 8px;
  border: 1px solid var(--line); border-radius: 999px;
  padding: 8px 12px; background: var(--soft); font-size: 13px;
}
.dot { width: 9px; height: 9px; border-radius: 50%; background: var(--bad); flex: 0 0 auto; }
.dot.on { background: var(--good); box-shadow: 0 0 12px rgba(89,209,133,.45); }
.tabs {
  display: flex; gap: 8px; padding: 5px; margin-bottom: 16px;
  border: 1px solid var(--line); border-radius: 13px;
  background: rgba(21,24,33,.72); width: fit-content;
}
.tab-button {
  border: 0; border-radius: 9px; padding: 9px 15px;
  color: var(--muted); background: transparent; cursor: pointer; font: inherit;
}
.tab-button.active { color: var(--text); background: var(--panel2); }
.tab-page { display: none; }
.tab-page.active { display: block; }
.grid { display: grid; grid-template-columns: repeat(12, 1fr); gap: 14px; }
.card, .signal-panel {
  border: 1px solid var(--line);
  border-radius: 15px;
  background: rgba(21,24,33,.9);
  box-shadow: 0 12px 35px rgba(0,0,0,.15);
}
.card { padding: 16px; }
.full { grid-column: span 12; }
.half { grid-column: span 6; }
.third { grid-column: span 4; }
.row { display: flex; gap: 9px; flex-wrap: wrap; align-items: center; }
.between { justify-content: space-between; }
.label {
  color: var(--muted); font-size: 12px; text-transform: uppercase;
  letter-spacing: .08em;
}
.small { color: var(--muted); font-size: 12px; line-height: 1.48; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
button, input, select { font: inherit; }
button {
  border: 1px solid var(--line); color: var(--text); background: var(--panel2);
  border-radius: 10px; padding: 9px 13px; cursor: pointer;
}
button:hover { border-color: #68718a; }
button.primary { background: #4338ca; border-color: #635bdf; }
button.recording { background: #81233f; border-color: #b33c60; }
button.audio-on { background: #315b46; border-color: #4d8b6b; }
button:disabled { opacity: .45; cursor: default; }
input[type=text], input[type=number], select {
  background: #0d1017; border: 1px solid var(--line); color: var(--text);
  border-radius: 9px; padding: 8px 9px;
}
input.host { width: 150px; }
input.port { width: 90px; }
.session-summary {
  grid-column: span 12;
  display: flex; justify-content: space-between; align-items: center;
  gap: 12px; flex-wrap: wrap;
}
.signal-grid {
  display: grid; grid-template-columns: repeat(12, 1fr); gap: 14px;
  grid-column: span 12;
}
.signal-panel { grid-column: span 6; overflow: hidden; }
.signal-panel-header {
  display: flex; justify-content: space-between; gap: 12px;
  align-items: flex-start; padding: 15px 16px;
}
.signal-title { margin-top: 4px; font-size: 19px; font-weight: 680; }
.signal-device { margin-top: 4px; color: var(--muted); font-size: 12px; }
.signal-body { padding: 0 16px 16px; }
.signal-panel.offline .signal-body { display: none; }
.signal-panel.offline { opacity: .72; }
.signal-panel.offline .signal-panel-header { align-items: center; }
.signal-status { white-space: nowrap; }
.metrics {
  display: grid; grid-template-columns: repeat(4, minmax(0,1fr));
  gap: 9px; margin: 6px 0 10px;
}
.metric {
  background: #10131b; border: 1px solid rgba(52,59,78,.75);
  border-radius: 11px; padding: 10px;
}
.metric-value {
  font-size: 23px; font-variant-numeric: tabular-nums;
  margin-top: 5px; overflow: hidden; text-overflow: ellipsis;
}
.signal-info {
  display: grid; grid-template-columns: 1fr 1fr; gap: 8px 18px;
  margin: 10px 0;
}
.info-line { font-size: 12px; line-height: 1.45; }
.info-line strong { color: #dce1ed; font-weight: 600; }
canvas {
  width: 100%; height: 190px; display: block;
  background: #0a0d13; border-radius: 10px; margin-top: 10px;
}
.audio-note {
  margin-top: 9px; padding: 9px 10px; border-radius: 9px;
  background: rgba(139,124,246,.07); color: var(--muted);
  font-size: 12px; line-height: 1.45;
}
.device-grid {
  display: grid; grid-template-columns: repeat(12, 1fr); gap: 12px;
  margin-top: 12px;
}
.device-card {
  grid-column: span 6; padding: 14px;
  border: 1px solid var(--line); border-radius: 12px; background: #10131b;
}
.device-name { font-size: 17px; font-weight: 650; }
.device-meta { margin-top: 8px; display: grid; gap: 4px; }
.device-signals { margin-top: 10px; }
.badge {
  display: inline-block; border: 1px solid var(--line); border-radius: 999px;
  padding: 4px 8px; margin: 3px 4px 0 0; color: var(--muted); font-size: 11px;
}
.device-table { width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 13px; }
.device-table th {
  color: var(--muted); font-weight: 600; text-align: left;
  border-bottom: 1px solid var(--line); padding: 8px 7px;
}
.device-table td {
  border-bottom: 1px solid rgba(52,59,78,.6); padding: 8px 7px; vertical-align: top;
}
.device-table tr.selected { background: rgba(196,181,253,.09); }
.diag-output {
  white-space: pre-wrap; word-break: break-word; margin: 12px 0 0;
  max-height: 340px; overflow: auto; padding: 12px;
  background: #090c12; border: 1px solid var(--line); border-radius: 10px;
  font: 12px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace;
}
#error { color: #ff9b9b; font-size: 12px; min-height: 18px; margin-top: 8px; }
.empty {
  grid-column: span 12; padding: 24px; border: 1px dashed var(--line);
  border-radius: 14px; color: var(--muted); text-align: center;
}
@media (max-width: 820px) {
  .signal-panel, .half, .device-card { grid-column: span 12; }
  .third { grid-column: span 12; }
  .metrics { grid-template-columns: repeat(2, minmax(0,1fr)); }
  .signal-info { grid-template-columns: 1fr; }
}
</style>
</head>
<body>
<main>
  <div class="topbar">
    <div>
      <h1>Biofeedback Play</h1>
      <div class="subtitle">Live biosignal visual, audio, recording, and creative-control workspace</div>
    </div>
    <div class="status-pill">
      <span id="globalDot" class="dot"></span>
      <span id="globalStatus">Checking devices...</span>
    </div>
  </div>

  <nav class="tabs" aria-label="Biofeedback Play sections">
    <button class="tab-button active" data-tab="use">Use devices</button>
    <button class="tab-button" data-tab="setup">Device setup</button>
  </nav>

  <section id="tab-use" class="tab-page active">
    <div class="grid">
      <section class="card session-summary">
        <div>
          <div class="label">Session</div>
          <div id="sessionSummary" style="margin-top:5px">Waiting for connected devices.</div>
        </div>
        <div class="row">
          <button id="recordBtn">Start recording</button>
          <button id="folderBtn">Show recordings</button>
        </div>
      </section>

      <div id="signalGrid" class="signal-grid"></div>
    </div>
  </section>

  <section id="tab-setup" class="tab-page">
    <div class="grid">
      <section class="card full">
        <div class="row between">
          <div>
            <div class="label">Configured devices</div>
            <div class="small" style="margin-top:6px">
              Connection and acquisition controls live here. Signal panels stay on the Use devices tab.
            </div>
          </div>
        </div>
        <div id="deviceSetupGrid" class="device-grid"></div>
        <div id="error"></div>
      </section>

      <section class="card half">
        <div class="label">OSC output</div>
        <div class="small" style="margin-top:6px">
          Sends each live signal on its panel's OSC address for SuperCollider and other software.
        </div>
        <div class="row" style="margin-top:12px">
          <label><input id="oscEnabled" type="checkbox"> Enabled</label>
          <input id="oscHost" class="host" type="text" value="127.0.0.1" aria-label="OSC host">
          <input id="oscPort" class="port" type="number" value="57120" aria-label="OSC port">
          <button id="oscApply">Apply</button>
        </div>
      </section>

      <section class="card half">
        <div class="label">About audio feedback</div>
        <div class="small" style="margin-top:6px">
          Each signal panel has its own Audio button. Audio is generated locally in the browser.
          It is a sonification of the incoming signal, not a reconstructed heartbeat or diagnostic sound.
        </div>
      </section>

      <section class="card full">
        <div class="row between">
          <div>
            <div class="label">Devices & diagnostics</div>
            <div class="small" style="margin-top:6px">
              Scan HID hardware, inspect unknown devices, test access, and make raw captures without Terminal.
            </div>
          </div>
          <div class="row">
            <label class="small">
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
    </div>
  </section>
</main>

<script>
let catalog = {devices: [], signals: []};
let runtimeStatus = {};
let signalSignature = "";
const signalState = {};
let diagnosticDevice = null;
let diagnosticText = "";
let diagnosticDevices = [];
let musePorts = [];
let musePortScanStatus = "Not scanned yet.";
let audioContext = null;

function post(action, extra) {
  const body = Object.assign({action: action}, extra || {});
  return fetch("/api/control", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body)
  }).then(r => r.json());
}

function domId(value) {
  return String(value).replace(/[^A-Za-z0-9_-]/g, "_");
}

function escapeHtml(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function switchTab(name) {
  document.querySelectorAll(".tab-button").forEach(function(button) {
    button.classList.toggle("active", button.dataset.tab === name);
  });
  document.querySelectorAll(".tab-page").forEach(function(page) {
    page.classList.toggle("active", page.id === "tab-" + name);
  });
  if (name === "use") {
    requestAnimationFrame(drawAllSignals);
  }
}

document.querySelectorAll(".tab-button").forEach(function(button) {
  button.onclick = function() { switchTab(button.dataset.tab); };
});

function ensureSignalState(signal) {
  if (!signalState[signal.id]) {
    signalState[signal.id] = {
      seq: 0,
      values: [],
      times: [],
      audioOn: false,
      audioNode: null
    };
  }
  return signalState[signal.id];
}

function renderSignalPanels(signals) {
  const grid = document.getElementById("signalGrid");
  if (!signals.length) {
    grid.innerHTML = '<div class="empty">No signal definitions are configured yet.</div>';
    return;
  }

  grid.innerHTML = signals.map(function(signal) {
    ensureSignalState(signal);
    const id = domId(signal.id);
    return (
      '<article id="panel_' + id + '" class="signal-panel offline">' +
        '<div class="signal-panel-header">' +
          '<div>' +
            '<div class="label">' + escapeHtml(signal.data_label) + '</div>' +
            '<div class="signal-title">' + escapeHtml(signal.name) + '</div>' +
            '<div class="signal-device">Device: ' + escapeHtml(signal.device_name) + '</div>' +
          '</div>' +
          '<div class="row">' +
            '<span class="status-pill signal-status"><span id="dot_' + id + '" class="dot"></span>' +
              '<span id="status_' + id + '">Not connected</span></span>' +
            '<button id="audio_' + id + '" disabled>Audio off</button>' +
          '</div>' +
        '</div>' +
        '<div class="signal-body">' +
          '<div class="metrics">' +
            '<div class="metric"><div class="label">Current</div><div id="current_' + id + '" class="metric-value">-</div></div>' +
            '<div class="metric"><div class="label">Recent low</div><div id="min_' + id + '" class="metric-value">-</div></div>' +
            '<div class="metric"><div class="label">Recent high</div><div id="max_' + id + '" class="metric-value">-</div></div>' +
            '<div class="metric"><div class="label">Samples</div><div id="count_' + id + '" class="metric-value">0</div></div>' +
          '</div>' +
          '<div class="signal-info">' +
            '<div class="info-line"><strong>Displayed:</strong> ' + escapeHtml(signal.description) + '</div>' +
            '<div class="info-line"><strong>Units:</strong> ' + escapeHtml(signal.unit) + '</div>' +
            '<div class="info-line"><strong>OSC:</strong> <span class="mono">' + escapeHtml(signal.osc) + '</span></div>' +
            '<div class="info-line"><strong>Rate:</strong> ' +
              (signal.nominal_rate ? escapeHtml(signal.nominal_rate + " Hz nominal") : "device stream rate, not yet characterized") +
            '</div>' +
            '<div id="extra_' + id + '" class="info-line"></div>' +
          '</div>' +
          '<canvas id="canvas_' + id + '"></canvas>' +
          '<div class="audio-note"><strong>Audio mapping:</strong> ' + escapeHtml(signal.audio) +
            ' Audio is intentionally quiet and can be enabled independently for this panel.</div>' +
        '</div>' +
      '</article>'
    );
  }).join("");

  signals.forEach(function(signal) {
    const id = domId(signal.id);
    document.getElementById("audio_" + id).onclick = function() {
      toggleAudio(signal.id);
    };
  });
}

function updateSignalPanels(signals) {
  signals.forEach(function(signal) {
    const id = domId(signal.id);
    const panel = document.getElementById("panel_" + id);
    if (!panel) return;

    const live = Boolean(signal.connected && signal.running);
    panel.classList.toggle("offline", !live);
    document.getElementById("dot_" + id).className = live ? "dot on" : "dot";
    document.getElementById("status_" + id).textContent =
      !signal.running ? "Acquisition stopped" :
      signal.connected ? "Live" : "Not connected";

    const audioButton = document.getElementById("audio_" + id);
    audioButton.disabled = !live;
    const state = ensureSignalState(signal);
    if (!live && state.audioOn) stopAudio(signal.id);
    audioButton.textContent = state.audioOn ? "Audio on" : "Audio off";
    audioButton.className = state.audioOn ? "audio-on" : "";

    document.getElementById("count_" + id).textContent =
      Number(signal.sample_count || 0).toLocaleString();

    const extra = document.getElementById("extra_" + id);
    if (signal.packet_gaps != null) {
      extra.innerHTML = '<strong>Packet gaps:</strong> ' + escapeHtml(signal.packet_gaps);
    } else {
      extra.textContent = "";
    }
  });
}

function renderDeviceSetup(devices) {
  const host = document.getElementById("deviceSetupGrid");
  host.innerHTML = devices.map(function(device) {
    const statusText = !device.running ? "Acquisition stopped" :
      device.connected ? "Connected and live" : "Waiting for device";
    const statusClass = device.connected && device.running ? "dot on" : "dot";
    const signals = (device.signals || []).map(function(signalId) {
      const signal = catalog.signals.find(function(s) { return s.id === signalId; });
      return '<span class="badge">' + escapeHtml(signal ? signal.name : signalId) + '</span>';
    }).join("");

    let deviceSpecific = "";
    if (device.id === "muse") {
      const currentPort = device.port || "";
      let options = '<option value="">Select Muse serial port</option>';
      musePorts.forEach(function(port) {
        const selected = port.device === currentPort ? " selected" : "";
        const label = port.device + (port.description ? " · " + port.description : "") +
          (port.likely_muse ? " · likely Muse" : "");
        options += '<option value="' + escapeHtml(port.device) + '"' + selected + '>' +
          escapeHtml(label) + '</option>';
      });

      const battery = device.battery && device.battery.percentage != null
        ? Number(device.battery.percentage).toFixed(1) + "%"
        : "—";
      const afe = device.afe_gain != null ? String(device.afe_gain) : "—";
      const version = device.version ? escapeHtml(device.version) : "—";

      const portSummary = musePorts.length
        ? musePorts.map(function(port) {
            return '<div class="mono">' + escapeHtml(port.device) +
              (port.likely_muse ? ' <span class="badge">likely Muse</span>' : '') +
              '</div>';
          }).join("")
        : '<div class="small">No serial ports found in the last scan.</div>';

      deviceSpecific =
        '<div style="margin-top:12px;padding-top:12px;border-top:1px solid var(--line)">' +
          '<div class="label">Muse Bluetooth setup</div>' +
          '<div class="small" style="margin-top:6px">' +
            'MU-01 uses classic Bluetooth serial. Pair a device named Muse-… in macOS Bluetooth settings, then scan for its serial port.' +
          '</div>' +
          '<div class="row" style="margin-top:10px">' +
            '<select id="musePortSelect">' + options + '</select>' +
            '<button id="museSavePort">Use selected port</button>' +
            '<button id="museScanPorts">Scan serial ports</button>' +
            '<button id="museBluetoothSettings">Open Bluetooth settings</button>' +
          '</div>' +
          '<div id="musePortScanStatus" class="small" style="margin-top:10px;padding:9px 10px;border:1px solid var(--line);border-radius:9px;background:#0d1017">' +
            escapeHtml(musePortScanStatus) +
          '</div>' +
          '<div class="device-meta small" style="margin-top:8px">' +
            '<div><strong>Serial ports found:</strong> ' + musePorts.length + '</div>' +
            portSummary +
            '<div><strong>Selected port:</strong> <span class="mono">' + escapeHtml(currentPort || "none") + '</span></div>' +
            '<div><strong>Battery:</strong> ' + battery + '</div>' +
            '<div><strong>AFE gain:</strong> ' + afe + '</div>' +
            '<div><strong>Version/status:</strong> <span class="mono">' + version + '</span></div>' +
            '<div><strong>EEG samples:</strong> ' + Number(device.eeg_sample_count || 0).toLocaleString() +
              ' · <strong>Accelerometer samples:</strong> ' + Number(device.accel_sample_count || 0).toLocaleString() + '</div>' +
          '</div>' +
        '</div>';
    }

    return (
      '<div class="device-card">' +
        '<div class="row between">' +
          '<div>' +
            '<div class="device-name">' + escapeHtml(device.name) + '</div>' +
            '<div class="small">' + escapeHtml(device.summary) + '</div>' +
          '</div>' +
          '<span class="status-pill"><span class="' + statusClass + '"></span>' + escapeHtml(statusText) + '</span>' +
        '</div>' +
        '<div class="device-meta small">' +
          '<div><strong>Manufacturer:</strong> ' + escapeHtml(device.manufacturer) + '</div>' +
          '<div><strong>Transport:</strong> ' + escapeHtml(device.transport) + '</div>' +
          '<div><strong>Hardware ID:</strong> <span class="mono">' + escapeHtml(device.usb_id) + '</span></div>' +
          '<div><strong>Samples received:</strong> ' + Number(device.sample_count || 0).toLocaleString() + '</div>' +
          (device.packet_gaps != null ? '<div><strong>Packet gaps:</strong> ' + escapeHtml(device.packet_gaps) + '</div>' : '') +
          (device.error ? '<div style="color:#ff9b9b"><strong>Error:</strong> ' + escapeHtml(device.error) + '</div>' : '') +
        '</div>' +
        '<div class="device-signals">' + signals + '</div>' +
        deviceSpecific +
        '<div style="margin-top:12px">' +
          '<button data-device-toggle="' + escapeHtml(device.id) + '">' +
            (device.running ? "Stop acquisition" : "Start acquisition") +
          '</button>' +
        '</div>' +
      '</div>'
    );
  }).join("");

  host.querySelectorAll("[data-device-toggle]").forEach(function(button) {
    button.onclick = function() {
      const device = catalog.devices.find(function(d) {
        return d.id === button.dataset.deviceToggle;
      });
      if (!device) return;
      post(device.running ? "device_stop" : "device_start", {device_id: device.id})
        .then(refreshAll);
    };
  });

  const museScan = document.getElementById("museScanPorts");
  if (museScan) museScan.onclick = refreshMusePorts;

  const museSettings = document.getElementById("museBluetoothSettings");
  if (museSettings) museSettings.onclick = function() {
    post("open_bluetooth_settings");
  };

  const museSave = document.getElementById("museSavePort");
  if (museSave) museSave.onclick = function() {
    const select = document.getElementById("musePortSelect");
    post("muse_set_port", {port: select ? select.value : ""}).then(refreshAll);
  };
}


function refreshMusePorts() {
  musePortScanStatus = "Scanning macOS serial ports...";
  renderDeviceSetup(catalog.devices);

  return fetch("/api/muse_ports")
    .then(function(r) {
      if (!r.ok) throw new Error("Serial-port scan failed: HTTP " + r.status);
      return r.json();
    })
    .then(function(data) {
      musePorts = data.ports || [];
      const likely = musePorts.filter(function(port) { return port.likely_muse; });

      if (!data.current && likely.length === 1) {
        musePortScanStatus =
          "Found " + musePorts.length + " serial port(s). One looks like a Muse, so Biofeedback Play selected it automatically.";
        return post("muse_set_port", {port: likely[0].device})
          .then(function() {
            return refreshAll().then(function() {
              renderDeviceSetup(catalog.devices);
            });
          });
      }

      if (!musePorts.length) {
        musePortScanStatus =
          "Scan complete: no macOS serial ports were found. If the Muse is paired, this likely means macOS did not create an RFCOMM serial port for it.";
      } else if (likely.length) {
        musePortScanStatus =
          "Scan complete: found " + musePorts.length + " serial port(s), including " +
          likely.length + " likely Muse port(s).";
      } else {
        musePortScanStatus =
          "Scan complete: found " + musePorts.length +
          " serial port(s), but none are named like a Muse. You can still select one manually if you recognize it.";
      }

      renderDeviceSetup(catalog.devices);
    })
    .catch(function(err) {
      musePortScanStatus = "Serial-port scan failed: " + String(err);
      renderDeviceSetup(catalog.devices);
      document.getElementById("error").textContent = String(err);
    });
}

function updateGlobalStatus() {
  const connected = catalog.devices.filter(function(d) { return d.connected && d.running; });
  const liveSignals = catalog.signals.filter(function(s) { return s.connected && s.running; });
  const dot = document.getElementById("globalDot");
  dot.className = connected.length ? "dot on" : "dot";
  document.getElementById("globalStatus").textContent =
    connected.length + " device" + (connected.length === 1 ? "" : "s") +
    " connected · " + liveSignals.length + " live signal" + (liveSignals.length === 1 ? "" : "s");

  document.getElementById("sessionSummary").textContent = liveSignals.length
    ? liveSignals.map(function(s) { return s.device_name + ": " + s.name; }).join(" · ")
    : "No configured device is currently connected.";
}

function applyCatalog(data) {
  catalog = data;
  const signature = catalog.signals.map(function(s) { return s.id; }).join("|");
  if (signature !== signalSignature) {
    signalSignature = signature;
    renderSignalPanels(catalog.signals);
  }
  updateSignalPanels(catalog.signals);
  if (!document.activeElement || document.activeElement.id !== "musePortSelect") {
    renderDeviceSetup(catalog.devices);
  }
  updateGlobalStatus();
}

function refreshAll() {
  return Promise.all([
    fetch("/api/catalog").then(r => r.json()),
    fetch("/api/status").then(r => r.json())
  ]).then(function(results) {
    applyCatalog(results[0]);
    runtimeStatus = results[1];

    const record = document.getElementById("recordBtn");
    record.textContent = runtimeStatus.recording ? "Stop recording" : "Start recording";
    record.className = runtimeStatus.recording ? "recording" : "";

    document.getElementById("oscEnabled").checked = Boolean(runtimeStatus.osc_enabled);
    document.getElementById("oscHost").value = runtimeStatus.osc_host || "127.0.0.1";
    document.getElementById("oscPort").value = runtimeStatus.osc_port || 57120;

    const errors = catalog.devices.map(function(d) { return d.error; }).filter(Boolean);
    document.getElementById("error").textContent = errors.join(" · ");
  }).catch(function(err) {
    document.getElementById("error").textContent = String(err);
  });
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

function drawSignal(signal) {
  const state = ensureSignalState(signal);
  const canvas = document.getElementById("canvas_" + domId(signal.id));
  if (!canvas) return;
  const size = fitCanvas(canvas);
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, size.w, size.h);

  ctx.strokeStyle = "#202635";
  ctx.lineWidth = size.ratio;
  for (let i = 1; i < 4; i++) {
    const y = size.h * i / 4;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(size.w, y); ctx.stroke();
  }

  if (state.values.length < 2) return;
  let min = Math.min.apply(null, state.values);
  let max = Math.max.apply(null, state.values);
  if (max === min) { min -= 1; max += 1; }
  const padding = (max - min) * .08;
  min -= padding; max += padding;

  ctx.strokeStyle = "#c4b5fd";
  ctx.lineWidth = 1.55 * size.ratio;
  ctx.lineJoin = "round";
  ctx.beginPath();
  state.values.forEach(function(value, index) {
    const x = index * size.w / Math.max(1, state.values.length - 1);
    const y = size.h - ((value - min) / (max - min)) * size.h;
    if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

function drawAllSignals() {
  catalog.signals.forEach(drawSignal);
}

function updateSignalNumbers(signal) {
  const state = ensureSignalState(signal);
  if (!state.values.length) return;
  const id = domId(signal.id);
  const recent = state.values.slice(-400);
  const current = recent[recent.length - 1];
  const min = Math.min.apply(null, recent);
  const max = Math.max.apply(null, recent);
  document.getElementById("current_" + id).textContent = current;
  document.getElementById("min_" + id).textContent = min;
  document.getElementById("max_" + id).textContent = max;
  updateAudio(signal);
}

function pollSignals() {
  catalog.signals.forEach(function(signal) {
    if (!(signal.connected && signal.running)) return;
    const state = ensureSignalState(signal);
    fetch("/api/signal_samples?id=" + encodeURIComponent(signal.id) + "&after=" + state.seq)
      .then(r => r.json())
      .then(function(data) {
        (data.samples || []).forEach(function(sample) {
          state.seq = Math.max(state.seq, sample.seq);
          state.values.push(sample.value);
          state.times.push(sample.t);
        });
        const maxPoints = signal.nominal_rate ? 1500 : 700;
        if (state.values.length > maxPoints) {
          state.values.splice(0, state.values.length - maxPoints);
          state.times.splice(0, state.times.length - maxPoints);
        }
        if ((data.samples || []).length) {
          updateSignalNumbers(signal);
          drawSignal(signal);
        }
      })
      .catch(function(err) {
        document.getElementById("error").textContent = String(err);
      });
  });
}

function getAudioContext() {
  if (!audioContext) {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    audioContext = new AudioContextClass();
  }
  return audioContext;
}

function toggleAudio(signalId) {
  const signal = catalog.signals.find(function(s) { return s.id === signalId; });
  if (!signal || !(signal.connected && signal.running)) return;
  const state = ensureSignalState(signal);
  if (state.audioOn) {
    stopAudio(signalId);
    return;
  }

  const ctx = getAudioContext();
  ctx.resume();
  const oscillator = ctx.createOscillator();
  const gain = ctx.createGain();
  oscillator.type = signalId.indexOf("skin") >= 0 ? "sine" : "triangle";
  gain.gain.value = 0.018;
  oscillator.frequency.value = 220;
  oscillator.connect(gain).connect(ctx.destination);
  oscillator.start();

  state.audioOn = true;
  state.audioNode = {oscillator: oscillator, gain: gain};
  updateSignalPanels(catalog.signals);
  updateAudio(signal);
}

function stopAudio(signalId) {
  const signal = catalog.signals.find(function(s) { return s.id === signalId; });
  const state = signalState[signalId];
  if (!state) return;
  state.audioOn = false;
  if (state.audioNode) {
    try { state.audioNode.gain.gain.setTargetAtTime(0, audioContext.currentTime, .02); } catch (_) {}
    try { state.audioNode.oscillator.stop(audioContext.currentTime + .08); } catch (_) {}
    state.audioNode = null;
  }
  if (signal) updateSignalPanels(catalog.signals);
}

function updateAudio(signal) {
  const state = ensureSignalState(signal);
  if (!state.audioOn || !state.audioNode || !state.values.length || !audioContext) return;
  const recent = state.values.slice(-250);
  let min = Math.min.apply(null, recent);
  let max = Math.max.apply(null, recent);
  const current = recent[recent.length - 1];
  if (max === min) max = min + 1;
  let normalized = (current - min) / (max - min);
  normalized = Math.max(0, Math.min(1, normalized));

  const low = signal.id.indexOf("skin") >= 0 ? 150 : 180;
  const high = signal.id.indexOf("skin") >= 0 ? 720 : 900;
  const frequency = low * Math.pow(high / low, normalized);
  state.audioNode.oscillator.frequency.setTargetAtTime(
    frequency, audioContext.currentTime, .035
  );
}

document.getElementById("recordBtn").onclick = function() {
  post(runtimeStatus.recording ? "record_stop" : "record_start").then(refreshAll);
};
document.getElementById("folderBtn").onclick = function() { post("reveal_recordings"); };
document.getElementById("oscApply").onclick = function() {
  post("osc", {
    enabled: document.getElementById("oscEnabled").checked,
    host: document.getElementById("oscHost").value,
    port: Number(document.getElementById("oscPort").value)
  }).then(refreshAll);
};

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

function renderDiagnosticDevices(devices) {
  diagnosticDevices = devices || [];
  const host = document.getElementById("deviceList");
  const showAll = document.getElementById("showAllDevices").checked;
  const visible = showAll
    ? diagnosticDevices
    : diagnosticDevices.filter(function(d) { return !d.obviously_unrelated; });
  const hidden = diagnosticDevices.length - visible.length;

  if (!diagnosticDevices.length) {
    host.innerHTML = '<div class="small" style="margin-top:12px">No HID devices found.</div>';
    return;
  }

  let html = hidden
    ? '<div class="small" style="margin-top:12px">' + hidden + ' obviously unrelated HID device(s) hidden.</div>'
    : '';

  html += '<table class="device-table"><thead><tr>' +
    '<th>Device</th><th>Manufacturer</th><th>USB ID</th><th>Usage</th><th></th>' +
    '</tr></thead><tbody>';

  visible.forEach(function(d, index) {
    html += '<tr data-device-index="' + index + '">' +
      '<td><strong>' + escapeHtml(deviceName(d)) + '</strong>' +
        (d.known ? '<div class="small">' + escapeHtml(d.product) + '</div>' : '') + '</td>' +
      '<td>' + escapeHtml(d.manufacturer || "-") + '</td>' +
      '<td class="mono">' + escapeHtml(d.vendor_hex + ":" + d.product_hex) + '</td>' +
      '<td class="mono">0x' + Number(d.usage_page).toString(16).padStart(4, "0") +
        ' / ' + escapeHtml(d.usage) + '</td>' +
      '<td><button class="select-device">Select</button></td>' +
    '</tr>';
  });
  html += '</tbody></table>';
  host.innerHTML = html;

  host.querySelectorAll("tr[data-device-index]").forEach(function(row) {
    const index = Number(row.dataset.deviceIndex);
    row.querySelector(".select-device").onclick = function() {
      selectDiagnosticDevice(visible[index], row);
    };
  });

  const preferred = visible.findIndex(function(d) {
    return d.vendor_id === 0x0e30 && d.product_id === 0x0002;
  });
  if (preferred >= 0) {
    const row = host.querySelector('tr[data-device-index="' + preferred + '"]');
    selectDiagnosticDevice(visible[preferred], row);
  }
}

function scanDevices() {
  const output = document.getElementById("diagOutput");
  output.textContent = "Scanning HID devices...";
  fetch("/api/devices")
    .then(r => r.json())
    .then(function(data) {
      diagnosticDevices = data.devices || [];
      renderDiagnosticDevices(diagnosticDevices);
      const hidden = diagnosticDevices.filter(function(d) { return d.obviously_unrelated; }).length;
      output.textContent =
        "Found " + diagnosticDevices.length + " HID device(s)." +
        (hidden ? " " + hidden + " obviously unrelated device(s) hidden by default." : "") +
        " Select one to test or capture.";
    })
    .catch(function(err) { output.textContent = String(err); });
}

document.getElementById("scanBtn").onclick = scanDevices;
document.getElementById("showAllDevices").onchange = function() {
  renderDiagnosticDevices(diagnosticDevices);
};
document.getElementById("testDeviceBtn").onclick = function() {
  const output = document.getElementById("diagOutput");
  if (!diagnosticDevice) { output.textContent = "Select a device first."; return; }
  output.textContent = "Testing access to " + deviceName(diagnosticDevice) + "...";
  post("diag_test", {path: diagnosticDevice.path_token}).then(function(result) {
    if (!result.ok) throw new Error(result.error || "Device test failed.");
    diagnosticText =
      "Opened successfully.\n" + deviceName(diagnosticDevice) + "\n" +
      diagnosticDevice.vendor_hex + ":" + diagnosticDevice.product_hex;
    output.textContent = diagnosticText;
  }).catch(function(err) { output.textContent = String(err); });
};
document.getElementById("captureDeviceBtn").onclick = function() {
  const output = document.getElementById("diagOutput");
  if (!diagnosticDevice) { output.textContent = "Select a device first."; return; }
  const seconds = Number(document.getElementById("captureSeconds").value || 5);
  output.textContent = "Capturing " + seconds + " seconds from " + deviceName(diagnosticDevice) + "...";
  post("diag_capture", {path: diagnosticDevice.path_token, seconds: seconds})
    .then(function(result) {
      if (!result.ok) throw new Error(result.error || "Capture failed.");
      diagnosticText = result.result.summary || "";
      output.textContent = diagnosticText + "\n\nSaved locally: " + result.result.saved_path;
    })
    .catch(function(err) { output.textContent = String(err); });
};
document.getElementById("copyDiagBtn").onclick = function() {
  const output = document.getElementById("diagOutput");
  const text = diagnosticText || output.textContent;
  if (!text) return;
  navigator.clipboard.writeText(text).then(function() {
    output.textContent += "\n\n[Copied to clipboard]";
  }).catch(function() {
    output.textContent += "\n\nClipboard access failed. Select the report and copy it manually.";
  });
};
document.getElementById("captureFolderBtn").onclick = function() { post("reveal_captures"); };

window.addEventListener("resize", function() {
  if (document.getElementById("tab-use").classList.contains("active")) drawAllSignals();
});

refreshAll();
refreshMusePorts();
scanDevices();
setInterval(refreshAll, 1000);
setInterval(pollSignals, 100);
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

        if parsed.path == "/api/muse_ports":
            ports = list_muse_serial_ports()
            current = STATE.status().get("muse_port") or ""
            self.send_json({"ports": ports, "current": current})
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
            elif action == "muse_set_port":
                STATE.set_muse_port(str(payload.get("port") or ""))
            elif action == "open_bluetooth_settings":
                STATE.open_bluetooth_settings()
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
