#!/usr/bin/env python3
from __future__ import annotations

import base64
import csv
import json
import math
import os
import re
import socket
import statistics
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
from physiology import (
    camera_pulse_metrics,
    eeg_metrics,
    motion_metrics,
    pair_metrics,
    pulse_metrics,
    skin_metrics,
)


VENDOR_ID = 0x14FA
PRODUCT_ID = 0x0001
EMWAVE_VENDOR_ID = 0x0E30
EMWAVE_PRODUCT_ID = 0x0002
EMWAVE_NOMINAL_SAMPLE_RATE = 370.0
EMWAVE_SAMPLES_PER_REPORT = 6
EMWAVE_RATE_ESTIMATE_WINDOW = 512
EMWAVE_RATE_MIN = 330.0
EMWAVE_RATE_MAX = 410.0
HOST = "127.0.0.1"
PORT = 8765

ROOT = Path(__file__).resolve().parent
RECORDINGS = ROOT / "recordings"
RECORDINGS.mkdir(exist_ok=True)
CAPTURES = ROOT / "captures"
CAPTURES.mkdir(exist_ok=True)
SETTINGS_PATH = ROOT / "settings.json"

# hidapi's macOS backend uses native IOKit objects. Keep enumeration serialized:
# concurrent hid.enumerate() calls from multiple background threads can abort the
# entire Python process instead of raising a Python exception.
HID_ENUM_LOCK = threading.Lock()


def safe_hid_enumerate(vendor_id: int = 0, product_id: int = 0) -> list[dict]:
    with HID_ENUM_LOCK:
        return list(hid.enumerate(vendor_id, product_id))


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
        "name": "HeartMath emWave 1",
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
    "camera": {
        "name": "Camera",
        "manufacturer": "Browser camera",
        "transport": "Local browser MediaDevices",
        "usb_id": "browser-managed",
        "summary": "Local camera analysis for subtle facial color and motion changes. Video stays in the browser.",
        "browser_controlled": True,
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
    "camera.ppg_raw": {
        "device_id": "camera",
        "name": "Camera color pulse",
        "short_name": "Camera pulse",
        "data_label": "POS-style heartbeat-band facial color waveform",
        "unit": "relative RGB pulse units",
        "description": (
            "Locally extracted facial color waveform from guided forehead and lower-cheek regions. "
            "A POS-style combination of red, green, and blue changes suppresses common lighting variation, "
            "then a 0.7–3 Hz temporal band-pass emphasizes pulse-frequency changes. "
            "It is experimental remote photoplethysmography, not a calibrated optical sensor."
        ),
        "audio": "Pitch follows the camera-derived pulse waveform.",
        "osc": "/biofeedback/camera/ppg_raw",
        "nominal_rate": 30.0,
        "value_key": "ppg",
    },
    "camera.motion_raw": {
        "device_id": "camera",
        "name": "Camera motion",
        "short_name": "Motion",
        "data_label": "Frame-to-frame facial motion",
        "unit": "% mean pixel change",
        "description": (
            "Mean frame-to-frame luminance change in the guided face region. "
            "Useful both as a movement signal and as a warning that motion may contaminate camera pulse extraction."
        ),
        "audio": "Pitch follows recent camera motion intensity.",
        "osc": "/biofeedback/camera/motion_raw",
        "nominal_rate": 30.0,
        "value_key": "motion",
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
    hide_when_inactive: bool = False,
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
        "hide_when_inactive": hide_when_inactive,
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
SIGNAL_DEFINITIONS["camera.heart_rate"] = _derived_signal(
    "camera",
    "Heart rate",
    "Spectral camera heart-rate estimate",
    "beats/min",
    (
        "Conservative average heart-rate estimate from the dominant frequency of the recent "
        "camera pulse waveform. It is withheld when camera signal quality is too low."
    ),
    "/biofeedback/camera/heart_rate",
    "heart_rate_bpm",
    precision=1,
    audio="Pitch follows the accepted camera heart-rate estimate.",
)
SIGNAL_DEFINITIONS["camera.pulse_amplitude"] = _derived_signal(
    "camera",
    "Pulse amplitude",
    "Recent camera pulse-waveform range",
    "relative RGB pulse units",
    "Robust recent 5th-to-95th percentile range of the camera pulse waveform. Useful while tuning lighting and face position.",
    "/biofeedback/camera/pulse_amplitude",
    "pulse_amplitude",
    precision=2,
)

SIGNAL_DEFINITIONS["camera.signal_quality"] = _derived_signal(
    "camera",
    "Camera signal quality",
    "Periodic pulse-band strength after motion penalty",
    "%",
    (
        "Conservative camera-only quality estimate based on how strongly the recent "
        "waveform concentrates around one pulse-like frequency and how little facial "
        "motion is present. Low quality suppresses camera heart-rate output."
    ),
    "/biofeedback/camera/signal_quality",
    "signal_quality_percent",
    precision=0,
)

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
    "comparison.lightstone_emwave.amplitude_ratio": _derived_signal(
        "emwave", "Relative pulse amplitude", "Lightstone / emWave pulse amplitude ratio", "ratio",
        "Ratio of recent raw pulse-wave amplitudes. Useful for comparing contact quality and placement, but the sensors are uncalibrated so it is not a blood-flow ratio.",
        "/biofeedback/comparison/lightstone_emwave/amplitude_ratio",
        "amplitude_ratio", precision=3,
        requires_devices=["lightstone", "emwave"],
        source_name="Lightstone + emWave",
    ),
})


def emwave_device_id(index: int) -> str:
    return "emwave" if index == 1 else f"emwave{index}"


def emwave_display_name(index: int) -> str:
    return f"HeartMath emWave {index}"


def emwave_osc_prefix(index: int) -> str:
    return "/biofeedback/emwave" if index == 1 else f"/biofeedback/emwave/{index}"


for _emwave_index in range(2, 5):
    _device_id = emwave_device_id(_emwave_index)
    DEVICE_DEFINITIONS[_device_id] = {
        "name": emwave_display_name(_emwave_index),
        "manufacturer": "HeartMath / Quantum Intech",
        "transport": "USB HID",
        "usb_id": "0e30:0002",
        "summary": (
            "Additional emWave ear-clip optical pulse sensor. "
            "The app keeps each simultaneously connected unit in its own session slot."
        ),
        "optional": True,
    }
    SIGNAL_DEFINITIONS[f"{_device_id}.pulse_raw"] = {
        "device_id": _device_id,
        "name": "Pulse waveform",
        "short_name": "Pulse",
        "data_label": "Raw 8-bit optical pulse waveform",
        "unit": "0–255 raw units",
        "description": (
            f"Direct USB waveform from {emwave_display_name(_emwave_index)}. "
            "The packet interpretation remains capture-derived and experimental."
        ),
        "audio": "Pitch follows the recent pulse-wave shape.",
        "osc": f"{emwave_osc_prefix(_emwave_index)}/pulse_raw",
        "nominal_rate": EMWAVE_NOMINAL_SAMPLE_RATE,
        "value_key": "pulse",
    }
    SIGNAL_DEFINITIONS.update(
        _pulse_derived_definitions(
            _device_id,
            f"emWave {_emwave_index} ear clip",
            emwave_osc_prefix(_emwave_index),
        )
    )


def _add_pair_signal_definitions(
    first_id: str,
    second_id: str,
    first_name: str,
    second_name: str,
    pair_key: str,
) -> None:
    source_name = f"{first_name} + {second_name}"
    osc_base = f"/biofeedback/comparison/{pair_key}"
    common = {
        "requires_devices": [first_id, second_id],
        "source_name": source_name,
    }
    if "camera" in {first_id, second_id}:
        common["hide_when_inactive"] = True
    SIGNAL_DEFINITIONS[f"comparison.{pair_key}.hr_difference"] = _derived_signal(
        second_id,
        "Pulse-source HR difference",
        f"{first_name} vs {second_name} heart-rate difference",
        "beats/min",
        "Absolute difference between independently detected heart rates. Useful as a cross-check between sensors.",
        f"{osc_base}/hr_difference",
        "heart_rate_difference_bpm",
        precision=2,
        **common,
    )
    SIGNAL_DEFINITIONS[f"comparison.{pair_key}.beat_offset"] = _derived_signal(
        second_id,
        "Pulse-source beat offset",
        f"{first_name} vs {second_name} beat timing offset",
        "ms",
        "Median nearest-beat timing difference. Sensor placement, optical path, USB scheduling, and buffering all contribute, so this is not a medical pulse-transit-time measurement.",
        f"{osc_base}/beat_offset_ms",
        "beat_offset_ms",
        precision=1,
        **common,
    )
    SIGNAL_DEFINITIONS[f"comparison.{pair_key}.correlation"] = _derived_signal(
        second_id,
        "Pulse-source correlation",
        f"{first_name} vs {second_name} waveform correlation",
        "correlation −1…1",
        "Experimental recent waveform similarity after interpolation. Different sensor shapes, clipping, placement, and timing offsets can reduce it.",
        f"{osc_base}/correlation",
        "waveform_correlation",
        precision=3,
        **common,
    )
    SIGNAL_DEFINITIONS[f"comparison.{pair_key}.amplitude_ratio"] = _derived_signal(
        second_id,
        "Relative pulse amplitude",
        f"{first_name} / {second_name} pulse amplitude ratio",
        "ratio",
        "Ratio of recent raw pulse-wave amplitudes. This is useful for comparing contact quality and placement, but the sensors are uncalibrated so it should not be interpreted as a blood-flow ratio.",
        f"{osc_base}/amplitude_ratio",
        "amplitude_ratio",
        precision=3,
        **common,
    )


for _emwave_index in range(2, 5):
    _device_id = emwave_device_id(_emwave_index)
    _add_pair_signal_definitions(
        "lightstone",
        _device_id,
        "Lightstone",
        f"emWave {_emwave_index}",
        f"lightstone_emwave{_emwave_index}",
    )

for _first_index in range(1, 5):
    for _second_index in range(_first_index + 1, 5):
        _first_id = emwave_device_id(_first_index)
        _second_id = emwave_device_id(_second_index)
        _add_pair_signal_definitions(
            _first_id,
            _second_id,
            f"emWave {_first_index}",
            f"emWave {_second_index}",
            f"emwave{_first_index}_emwave{_second_index}",
        )


_add_pair_signal_definitions(
    "lightstone", "camera", "Lightstone", "Camera", "lightstone_camera"
)
for _emwave_index in range(1, 5):
    _emwave_id = emwave_device_id(_emwave_index)
    _add_pair_signal_definitions(
        _emwave_id,
        "camera",
        f"emWave {_emwave_index}",
        "Camera",
        f"emwave{_emwave_index}_camera",
    )


class EmWaveSampleClock:
    """Reconstruct emWave sample time without inheriting USB delivery jitter.

    The module sends six consecutive waveform samples per numbered report. macOS
    can deliver those reports in small bursts, so timestamping every report at
    its host-arrival time injects several milliseconds of artificial beat-time
    jitter. This clock follows packet-counter progression, reserves time for
    missing reports, and slowly estimates each connected module's actual sample
    rate from long-run packet delivery while starting from HeartMath's 370 Hz
    nominal rate.
    """

    def __init__(
        self,
        nominal_rate: float = EMWAVE_NOMINAL_SAMPLE_RATE,
        samples_per_report: int = EMWAVE_SAMPLES_PER_REPORT,
    ) -> None:
        self.nominal_rate = float(nominal_rate)
        self.samples_per_report = int(samples_per_report)
        self.sample_rate_hz = float(nominal_rate)
        self.last_end_t: float | None = None
        self.packet_index = 0
        self.clock_resets = 0
        self.rate_points = deque(maxlen=EMWAVE_RATE_ESTIMATE_WINDOW)
        self.last_rate_update_packet = 0

    def reset(self) -> None:
        self.sample_rate_hz = self.nominal_rate
        self.last_end_t = None
        self.packet_index = 0
        self.rate_points.clear()
        self.last_rate_update_packet = 0

    def _update_rate_estimate(self) -> None:
        if len(self.rate_points) < 128:
            return
        if self.packet_index - self.last_rate_update_packet < 64:
            return

        first_arrival = self.rate_points[0][1]
        last_arrival = self.rate_points[-1][1]
        if last_arrival - first_arrival < 2.0:
            return

        mean_x = statistics.fmean(point[0] for point in self.rate_points)
        mean_t = statistics.fmean(point[1] for point in self.rate_points)
        denominator = sum((x - mean_x) ** 2 for x, _ in self.rate_points)
        if denominator <= 0:
            return
        slope = sum(
            (x - mean_x) * (t - mean_t)
            for x, t in self.rate_points
        ) / denominator
        if slope <= 0:
            return

        observed_rate = self.samples_per_report / slope
        if EMWAVE_RATE_MIN <= observed_rate <= EMWAVE_RATE_MAX:
            # Long-window host timing is useful for correcting crystal/rate
            # differences, but short USB scheduling bursts should not jerk the
            # physiological clock around.
            self.sample_rate_hz = (
                0.85 * self.sample_rate_hz + 0.15 * observed_rate
            )
            self.last_rate_update_packet = self.packet_index

    def packet_times(self, arrival_t: float, gap: int = 0) -> list[float]:
        packet_steps = max(1, int(gap) + 1)
        self.packet_index += packet_steps
        arrival_t = float(arrival_t)
        self.rate_points.append((self.packet_index, arrival_t))
        self._update_rate_estimate()

        sample_period = 1.0 / max(1.0, self.sample_rate_hz)
        if self.last_end_t is None:
            packet_end_t = arrival_t
        else:
            predicted_end = (
                self.last_end_t
                + packet_steps * self.samples_per_report * sample_period
            )
            arrival_error = arrival_t - predicted_end
            if abs(arrival_error) > 0.35:
                # Host sleep, a long stall, or a reconnect makes the old phase
                # meaningless. Re-anchor rather than fabricating a long run of
                # uniformly timed samples.
                packet_end_t = arrival_t
                self.clock_resets += 1
                self.rate_points.clear()
                self.packet_index = 0
                self.rate_points.append((0, arrival_t))
                self.last_rate_update_packet = 0
            else:
                # Correct phase very gently so independent devices stay aligned
                # to the host clock without importing millisecond-scale USB
                # scheduling jitter into beat timing.
                correction = max(-0.0015, min(0.0015, arrival_error * 0.03))
                packet_end_t = predicted_end + correction

        self.last_end_t = packet_end_t
        first_t = packet_end_t - (self.samples_per_report - 1) * sample_period
        return [
            first_t + index * sample_period
            for index in range(self.samples_per_report)
        ]


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
            "samples": [
                int(value) & 0xFF
                for value in report[2 : 2 + EMWAVE_SAMPLES_PER_REPORT]
            ],
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
    for item in safe_hid_enumerate():
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


def emwave_hid_paths() -> list[bytes]:
    paths: list[bytes] = []
    for item in safe_hid_enumerate(EMWAVE_VENDOR_ID, EMWAVE_PRODUCT_ID):
        path = item.get("path")
        if path is None:
            continue
        if isinstance(path, str):
            path = path.encode("utf-8")
        paths.append(bytes(path))
    return sorted(paths)


def reconcile_emwave_slot_paths(
    assignments: dict[int, bytes],
    connected_paths: list[bytes],
    max_units: int = 4,
) -> tuple[dict[int, bytes], set[int]]:
    """Keep identical emWave modules in stable session slots by HID path.

    Removing emWave 1 must not silently rename emWave 2 as emWave 1. Existing
    connected paths therefore keep their slots. A newly appearing path first
    replaces a reservation whose old path is absent, then uses a never-assigned
    slot. Replaced slots are returned so their physiological history can be
    cleared instead of mixing two physical sensors under one label.
    """

    out = dict(assignments)
    current = list(dict.fromkeys(bytes(path) for path in connected_paths))
    current_set = set(current)
    assigned_present = {
        path for path in out.values()
        if path in current_set
    }
    newcomers = [path for path in current if path not in assigned_present]
    replaced: set[int] = set()

    for path in newcomers:
        missing_slots = [
            unit_number
            for unit_number in range(1, max_units + 1)
            if unit_number in out and out[unit_number] not in current_set
        ]
        empty_slots = [
            unit_number
            for unit_number in range(1, max_units + 1)
            if unit_number not in out
        ]
        candidates = missing_slots or empty_slots
        if not candidates:
            break
        unit_number = candidates[0]
        previous = out.get(unit_number)
        out[unit_number] = path
        if previous is not None and previous != path:
            replaced.add(unit_number)

    return out, replaced


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


def emwave_capture_diagnostics(capture: dict) -> list[str]:
    device = capture.get("device") or {}
    if (
        int(device.get("vendor_id") or 0) != EMWAVE_VENDOR_ID
        or int(device.get("product_id") or 0) != EMWAVE_PRODUCT_ID
    ):
        return []

    reports = [
        report
        for report in capture.get("reports", [])
        if len(report.get("bytes") or []) >= 8
        and int(report["bytes"][0]) == 0x01
    ]
    if not reports:
        return ["emWave framing: no valid 8-byte 0x01 reports found"]

    parser = EmWaveParser()
    gaps = 0
    samples: list[int] = []
    for report in reports:
        parsed = parser.feed_report(report["bytes"])
        if parsed is None:
            continue
        gaps += int(parsed.get("gap") or 0)
        samples.extend(parsed["samples"])

    intervals = [
        float(second["t"]) - float(first["t"])
        for first, second in zip(reports, reports[1:])
        if float(second["t"]) > float(first["t"])
    ]
    duration = float(capture.get("duration_s") or 0.0)
    observed_delivery = (
        len(samples) / duration
        if duration > 0
        else 0.0
    )

    lines = [
        (
            "emWave framing: "
            f"{len(reports)} valid reports × {EMWAVE_SAMPLES_PER_REPORT} "
            "consecutive waveform samples/report"
        ),
        f"Counter gaps: {gaps}",
    ]
    if intervals:
        lines.append(
            "Host report timing: median "
            f"{statistics.median(intervals) * 1000.0:.3f} ms; "
            f"longest {max(intervals) * 1000.0:.3f} ms"
        )
    lines.append(
        "Host-observed waveform delivery: "
        f"{observed_delivery:.1f} samples/s "
        f"(nominal device rate {EMWAVE_NOMINAL_SAMPLE_RATE:.0f} Hz)"
    )
    if samples:
        near_top = 100.0 * sum(value >= 250 for value in samples) / len(samples)
        lines.append(
            f"Raw waveform range: {min(samples)}–{max(samples)}; "
            f"samples ≥250: {near_top:.1f}%"
        )
    lines.append(
        "Timing note: USB arrival can be bursty; live acquisition reconstructs "
        "sample time from packet order rather than treating host arrival as the sensor clock."
    )
    return lines


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
    ]
    diagnostics = emwave_capture_diagnostics(capture)
    if diagnostics:
        lines.extend([""] + diagnostics)
    lines.extend([
        "",
        "time_s    hex bytes                                              ASCII",
    ])

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
        self.started_unix = time.time()
        self.started_monotonic = time.monotonic()
        self.samples = deque(maxlen=5000)

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
        self.emwave_generation = 0
        self.emwave_samples = deque(maxlen=36000)
        self.emwave_parser = EmWaveParser()
        self.emwave_clock = EmWaveSampleClock()

        self.emwave_extra_units = {}
        for unit_number in range(2, 5):
            runtime = {
                "unit_number": unit_number,
                "running": True,
                "connected": False,
                "seen": False,
                "error": "",
                "seq": 0,
                "packet_count": 0,
                "gap_count": 0,
                "generation": 0,
                "samples": deque(maxlen=36000),
                "parser": EmWaveParser(),
                "clock": EmWaveSampleClock(),
            }
            self.emwave_extra_units[unit_number] = runtime

        # One native HID worker owns every emWave. This avoids several threads
        # simultaneously enumerating/opening HID devices through macOS IOKit.
        self.emwave_thread = threading.Thread(
            target=self._emwave_manager_loop, daemon=True
        )
        self.emwave_thread.start()

        settings = load_settings()
        self.muse_running = True
        self.muse_connected = False
        self.muse_last_error = ""
        self.muse_port = str(settings.get("muse_port") or "")
        self.muse_version = ""
        self.muse_status_text = ""
        self.muse_stage = "Waiting for serial port"
        self.muse_attempt_state = "idle"
        self.muse_stage_started_monotonic = time.monotonic()
        self.muse_retry_at_monotonic = 0.0
        self.muse_attempt_number = 0
        self.muse_transport = ""
        self.muse_rfcomm_services = ""
        self.muse_rfcomm_channel = None
        self.muse_passive_probe = ""
        self.muse_afe_gain = None
        self.muse_battery = None
        self.muse_last_data_monotonic = 0.0
        self.muse_last_eeg_monotonic = 0.0
        self.muse_last_accel_monotonic = 0.0
        self.muse_eeg_seq = 0
        self.muse_accel_seq = 0
        self.muse_eeg_samples = deque(maxlen=12000)
        self.muse_accel_samples = deque(maxlen=4000)
        self.muse_thread = threading.Thread(target=self._muse_reader_loop, daemon=True)
        self.muse_thread.start()

        self.camera_running = True
        self.camera_connected = False
        self.camera_last_data_monotonic = 0.0
        self.camera_last_error = ""
        self.camera_seq = 0
        self.camera_samples = deque(maxlen=5000)

        self.derived_samples = {
            signal_id: deque(maxlen=900)
            for signal_id, definition in SIGNAL_DEFINITIONS.items()
            if definition.get("derived")
        }
        self.derived_seq = {signal_id: 0 for signal_id in self.derived_samples}
        self.analysis_thread = threading.Thread(target=self._analysis_loop, daemon=True)
        self.analysis_thread.start()

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
            if self.muse_running:
                self.muse_attempt_state = "working"
                self.muse_last_error = ""
                self.muse_retry_at_monotonic = 0.0
                self.muse_attempt_number += 1
                self.muse_stage = "Starting Muse connection attempt"
                self.muse_stage_started_monotonic = time.monotonic()
            else:
                self.muse_connected = False
                self.muse_attempt_state = "stopped"
                self.muse_stage = "Acquisition stopped"
                self.muse_stage_started_monotonic = time.monotonic()

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
        elif device_id.startswith("emwave") and device_id[6:].isdigit():
            unit_number = int(device_id[6:])
            runtime = self.emwave_extra_units.get(unit_number)
            if runtime is None:
                raise ValueError(f"Unknown emWave unit: {device_id}")
            with self.lock:
                runtime["running"] = bool(value)
                if not runtime["running"]:
                    runtime["connected"] = False
        elif device_id == "muse":
            self.set_muse_running(value)
        elif device_id == "camera":
            with self.lock:
                self.camera_running = bool(value)
                if not self.camera_running:
                    self.camera_connected = False
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
                "emwave_sample_rate_hz": self.emwave_clock.sample_rate_hz,
                "emwave_clock_resets": self.emwave_clock.clock_resets,
                "muse_running": self.muse_running,
                "muse_connected": (
                    self.muse_connected
                    and time.monotonic() - self.muse_last_data_monotonic < 3.0
                ),
                "muse_last_error": self.muse_last_error,
                "muse_port": self.muse_port,
                "muse_version": self.muse_version,
                "muse_status_text": self.muse_status_text,
                "muse_stage": self.muse_stage,
                "muse_attempt_state": self.muse_attempt_state,
                "muse_stage_elapsed_s": max(
                    0.0, time.monotonic() - self.muse_stage_started_monotonic
                ),
                "muse_retry_seconds": max(
                    0,
                    int(self.muse_retry_at_monotonic - time.monotonic() + 0.999),
                ) if self.muse_retry_at_monotonic else 0,
                "muse_attempt_number": self.muse_attempt_number,
                "muse_transport": self.muse_transport,
                "muse_rfcomm_services": self.muse_rfcomm_services,
                "muse_rfcomm_channel": self.muse_rfcomm_channel,
                "muse_passive_probe": self.muse_passive_probe,
                "muse_afe_gain": self.muse_afe_gain,
                "muse_battery": self.muse_battery,
                "muse_eeg_sample_count": self.muse_eeg_seq,
                "muse_accel_sample_count": self.muse_accel_seq,
                "camera_running": self.camera_running,
                "camera_connected": (
                    self.camera_connected
                    and time.monotonic() - self.camera_last_data_monotonic < 2.5
                ),
                "camera_sample_count": self.camera_seq,
                "camera_last_error": self.camera_last_error,
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
                    "generation": self.emwave_generation,
                    "estimated_sample_rate_hz": round(
                        self.emwave_clock.sample_rate_hz, 2
                    ),
                    "clock_resets": self.emwave_clock.clock_resets,
                },
                "muse": {
                    "connected": (
                        self.muse_connected
                        and time.monotonic() - self.muse_last_data_monotonic < 3.0
                    ),
                    "running": self.muse_running,
                    "error": self.muse_last_error,
                    "sample_count": self.muse_eeg_seq,
                    "eeg_fresh": (
                        self.muse_last_eeg_monotonic > 0
                        and time.monotonic() - self.muse_last_eeg_monotonic < 3.0
                    ),
                    "accel_fresh": (
                        self.muse_last_accel_monotonic > 0
                        and time.monotonic() - self.muse_last_accel_monotonic < 3.0
                    ),
                    "eeg_sample_count": self.muse_eeg_seq,
                    "accel_sample_count": self.muse_accel_seq,
                    "port": self.muse_port,
                    "version": self.muse_version,
                    "status_text": self.muse_status_text,
                    "stage": self.muse_stage,
                    "attempt_state": self.muse_attempt_state,
                    "stage_elapsed_s": max(
                        0.0, time.monotonic() - self.muse_stage_started_monotonic
                    ),
                    "retry_seconds": max(
                        0,
                        int(self.muse_retry_at_monotonic - time.monotonic() + 0.999),
                    ) if self.muse_retry_at_monotonic else 0,
                    "attempt_number": self.muse_attempt_number,
                    "connection_transport": self.muse_transport,
                    "rfcomm_services": self.muse_rfcomm_services,
                    "rfcomm_channel": self.muse_rfcomm_channel,
                    "passive_probe": self.muse_passive_probe,
                    "afe_gain": self.muse_afe_gain,
                    "battery": self.muse_battery,
                },
                "camera": {
                    "connected": (
                        self.camera_connected
                        and time.monotonic() - self.camera_last_data_monotonic < 2.5
                    ),
                    "running": self.camera_running,
                    "error": self.camera_last_error,
                    "sample_count": self.camera_seq,
                },
            }
            for unit_number, runtime in self.emwave_extra_units.items():
                device_id = emwave_device_id(unit_number)
                status_by_device[device_id] = {
                    "connected": runtime["connected"],
                    "running": runtime["running"],
                    "error": runtime["error"],
                    "sample_count": runtime["seq"],
                    "packet_count": runtime["packet_count"],
                    "packet_gaps": runtime["gap_count"],
                    "generation": runtime["generation"],
                    "estimated_sample_rate_hz": round(
                        runtime["clock"].sample_rate_hz, 2
                    ),
                    "clock_resets": runtime["clock"].clock_resets,
                    "seen": runtime["seen"],
                }

            out = []
            for device_id, definition in DEVICE_DEFINITIONS.items():
                status = status_by_device.get(device_id)
                if status is None:
                    if definition.get("optional"):
                        continue
                    continue
                item = {"id": device_id, **definition, **status}
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
            device = devices.get(definition["device_id"])
            if device is None:
                continue
            sample_count = device["sample_count"]
            if signal_id.startswith("muse.accel."):
                sample_count = device.get("accel_sample_count", 0)
            elif signal_id.startswith("muse.eeg."):
                sample_count = device.get("eeg_sample_count", 0)
            if definition.get("derived"):
                sample_count = self.derived_seq.get(signal_id, 0)

            required = definition.get("requires_devices") or [definition["device_id"]]
            connected = all(devices.get(req, {}).get("connected", False) for req in required)
            running = all(devices.get(req, {}).get("running", False) for req in required)

            if definition.get("hide_when_inactive") and not (connected and running):
                continue

            if definition["device_id"] == "muse":
                if signal_id.startswith("muse.eeg.") or signal_id.startswith("muse.band.") or signal_id in {
                    "muse.alpha_asymmetry",
                    "muse.eeg_rms",
                }:
                    connected = connected and bool(device.get("eeg_fresh"))
                elif signal_id.startswith("muse.accel.") or signal_id == "muse.motion_intensity":
                    connected = connected and bool(device.get("accel_fresh"))

            data_sources = [
                devices[req]["name"]
                for req in required
                if req in devices
            ]
            source_generation = "|".join(
                f"{req}:{devices.get(req, {}).get('generation', 0)}"
                for req in required
            )

            signals.append(
                {
                    "id": signal_id,
                    **definition,
                    "device_name": definition.get("source_name") or device["name"],
                    "data_sources": data_sources,
                    "source_generation": source_generation,
                    "connected": connected,
                    "running": running,
                    "device_error": device["error"],
                    "sample_count": sample_count,
                    "packet_gaps": device.get("packet_gaps"),
                    "estimated_sample_rate_hz": device.get("estimated_sample_rate_hz"),
                    "clock_resets": device.get("clock_resets"),
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
            if definition.get("derived"):
                source = [
                    sample
                    for sample in self.derived_samples.get(signal_id, ())
                    if sample["seq"] > seq
                ][-600:]
                return [
                    {"seq": sample["seq"], "t": sample["t"], "value": sample["value"]}
                    for sample in source
                ]

            if definition["device_id"] == "lightstone":
                source = [sample for sample in self.samples if sample["seq"] > seq][-600:]
            elif definition["device_id"] == "emwave":
                source = [
                    sample for sample in self.emwave_samples if sample["seq"] > seq
                ][-1500:]
            elif definition["device_id"].startswith("emwave") and definition["device_id"][6:].isdigit():
                unit_number = int(definition["device_id"][6:])
                runtime = self.emwave_extra_units.get(unit_number)
                source = [] if runtime is None else [
                    sample for sample in runtime["samples"] if sample["seq"] > seq
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
            elif definition["device_id"] == "camera":
                source = [
                    sample for sample in self.camera_samples if sample["seq"] > seq
                ][-900:]
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

    def store_camera_samples(self, samples: list[dict]) -> None:
        if not self.camera_running:
            return
        if not isinstance(samples, list) or not samples:
            return

        cleaned: list[tuple[float, float, float]] = []
        for item in samples[-60:]:
            if not isinstance(item, dict):
                continue
            try:
                client_t = float(item.get("t"))
                ppg = float(item.get("ppg"))
                motion = float(item.get("motion"))
            except (TypeError, ValueError):
                continue
            if not (math.isfinite(client_t) and math.isfinite(ppg) and math.isfinite(motion)):
                continue
            cleaned.append((client_t, ppg, motion))

        if not cleaned:
            return

        cleaned.sort(key=lambda item: item[0])
        newest_client_t = cleaned[-1][0]
        now_mono = time.monotonic()
        newest_elapsed = now_mono - self.started_monotonic
        now_unix = time.time()

        with self.lock:
            self.camera_connected = True
            self.camera_last_data_monotonic = now_mono
            self.camera_last_error = ""

            for client_t, ppg, motion in cleaned:
                elapsed = newest_elapsed - max(0.0, newest_client_t - client_t)
                self.camera_seq += 1
                sample = {
                    "seq": self.camera_seq,
                    "t": elapsed,
                    "ppg": ppg,
                    "motion": motion,
                }
                self.camera_samples.append(sample)

                if self.recording and self.recording_writer:
                    sample_unix = now_unix - max(0.0, newest_elapsed - elapsed)
                    self.recording_writer.writerow(
                        [f"{sample_unix:.6f}", f"{elapsed:.6f}", "camera", "camera.ppg_raw", ppg]
                    )
                    self.recording_writer.writerow(
                        [f"{sample_unix:.6f}", f"{elapsed:.6f}", "camera", "camera.motion_raw", motion]
                    )

                if self.osc_enabled:
                    target = (self.osc_host, self.osc_port)
                    try:
                        self.osc_socket.sendto(
                            osc_message("/biofeedback/camera/ppg_raw", ppg), target
                        )
                        self.osc_socket.sendto(
                            osc_message("/biofeedback/camera/motion_raw", motion), target
                        )
                    except OSError as exc:
                        self.camera_last_error = "OSC: " + str(exc)

            if self.recording and self.recording_file:
                self.recording_file.flush()

    def set_camera_inactive(self) -> None:
        with self.lock:
            self.camera_connected = False

    def samples_after(self, seq: int) -> list[dict]:
        with self.lock:
            return [sample for sample in self.samples if sample["seq"] > seq][-300:]

    def emwave_samples_after(self, seq: int) -> list[dict]:
        with self.lock:
            return [
                sample for sample in self.emwave_samples if sample["seq"] > seq
            ][-1200:]


    def _store_derived(self, signal_id: str, value: float | int | None, elapsed: float) -> None:
        if value is None:
            return
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return
        if not math.isfinite(numeric):
            return

        definition = SIGNAL_DEFINITIONS.get(signal_id)
        if definition is None:
            return

        with self.lock:
            if signal_id not in self.derived_samples:
                self.derived_samples[signal_id] = deque(maxlen=900)
                self.derived_seq[signal_id] = 0
            self.derived_seq[signal_id] += 1
            sample = {"seq": self.derived_seq[signal_id], "t": elapsed, "value": numeric}
            self.derived_samples[signal_id].append(sample)

            if self.recording and self.recording_writer:
                self.recording_writer.writerow(
                    [
                        f"{time.time():.6f}",
                        f"{elapsed:.6f}",
                        definition["device_id"],
                        signal_id,
                        numeric,
                    ]
                )

            if self.osc_enabled:
                try:
                    self.osc_socket.sendto(
                        osc_message(definition["osc"], numeric),
                        (self.osc_host, self.osc_port),
                    )
                except OSError:
                    pass

    def _analysis_loop(self) -> None:
        def pulse_signal_map(device_id: str) -> dict[str, str]:
            return {
                "heart_rate_bpm": f"{device_id}.heart_rate",
                "ibi_ms": f"{device_id}.ibi",
                "rmssd_ms": f"{device_id}.hrv_rmssd",
                "sdnn_ms": f"{device_id}.hrv_sdnn",
                "pnn50_percent": f"{device_id}.pnn50",
                "coherence_ratio": f"{device_id}.coherence_ratio",
                "coherence_peak_percent": f"{device_id}.coherence_peak",
                "respiration_bpm": f"{device_id}.respiration_estimate",
                "pulse_amplitude": f"{device_id}.pulse_amplitude",
                "beat_confidence_percent": f"{device_id}.beat_confidence",
            }

        camera_signal_map = {
            "heart_rate_bpm": "camera.heart_rate",
            "pulse_amplitude": "camera.pulse_amplitude",
            "signal_quality_percent": "camera.signal_quality",
        }

        skin_map = {
            "tonic_level": "lightstone.skin_tonic",
            "phasic_level": "lightstone.skin_phasic",
            "slope_per_min": "lightstone.skin_slope",
            "response_rate_per_min": "lightstone.skin_responses",
            "variability": "lightstone.skin_variability",
        }
        eeg_map = {
            "delta_power": "muse.band.delta",
            "theta_power": "muse.band.theta",
            "alpha_power": "muse.band.alpha",
            "beta_power": "muse.band.beta",
            "gamma_power": "muse.band.gamma",
            "alpha_asymmetry": "muse.alpha_asymmetry",
            "broadband_rms": "muse.eeg_rms",
        }

        while not self.shutdown:
            started = time.monotonic()
            with self.lock:
                light_connected = self.connected and self.running
                primary_emwave_connected = self.emwave_connected and self.emwave_running
                muse_connected = (
                    self.muse_connected
                    and self.muse_running
                    and time.monotonic() - self.muse_last_data_monotonic < 3.0
                )
                camera_connected = (
                    self.camera_connected
                    and self.camera_running
                    and time.monotonic() - self.camera_last_data_monotonic < 2.5
                )
                light = list(self.samples)
                primary_emwave = list(self.emwave_samples)
                muse_eeg = list(self.muse_eeg_samples)
                muse_accel = list(self.muse_accel_samples)
                camera_samples = list(self.camera_samples)
                extra_snapshots = {
                    unit_number: {
                        "connected": runtime["connected"] and runtime["running"],
                        "samples": list(runtime["samples"]),
                    }
                    for unit_number, runtime in self.emwave_extra_units.items()
                    if runtime["seen"] or runtime["connected"]
                }

            elapsed = time.monotonic() - self.started_monotonic
            pulse_points: dict[str, list[tuple[float, float]]] = {}

            if light_connected:
                light_pulse_points = [(s["t"], s["pulse"]) for s in light]
                pulse_points["lightstone"] = light_pulse_points
                metrics = pulse_metrics(light_pulse_points)
                for key, signal_id in pulse_signal_map("lightstone").items():
                    self._store_derived(signal_id, metrics.get(key), elapsed)

                skin = skin_metrics([(s["t"], s["skin"]) for s in light])
                for key, signal_id in skin_map.items():
                    self._store_derived(signal_id, skin.get(key), elapsed)

            if primary_emwave_connected:
                primary_points = [(s["t"], s["pulse"]) for s in primary_emwave]
                pulse_points["emwave"] = primary_points
                metrics = pulse_metrics(primary_points)
                for key, signal_id in pulse_signal_map("emwave").items():
                    self._store_derived(signal_id, metrics.get(key), elapsed)

            for unit_number, snapshot in extra_snapshots.items():
                if not snapshot["connected"]:
                    continue
                device_id = emwave_device_id(unit_number)
                points = [(s["t"], s["pulse"]) for s in snapshot["samples"]]
                pulse_points[device_id] = points
                metrics = pulse_metrics(points)
                for key, signal_id in pulse_signal_map(device_id).items():
                    self._store_derived(signal_id, metrics.get(key), elapsed)

            if camera_connected:
                camera_points = [(s["t"], s["ppg"]) for s in camera_samples]
                camera_motion_points = [(s["t"], s["motion"]) for s in camera_samples]
                metrics = camera_pulse_metrics(camera_points, camera_motion_points)
                for key, signal_id in camera_signal_map.items():
                    self._store_derived(signal_id, metrics.get(key), elapsed)

                camera_quality = metrics.get("signal_quality_percent")
                if camera_quality is not None and camera_quality >= 35.0:
                    pulse_points["camera"] = camera_points

            def publish_pair(first_id: str, second_id: str, pair_key: str) -> None:
                if first_id not in pulse_points or second_id not in pulse_points:
                    return
                comparison = pair_metrics(pulse_points[first_id], pulse_points[second_id])
                for key, suffix in {
                    "heart_rate_difference_bpm": "hr_difference",
                    "beat_offset_ms": "beat_offset",
                    "waveform_correlation": "correlation",
                    "amplitude_ratio": "amplitude_ratio",
                }.items():
                    self._store_derived(
                        f"comparison.{pair_key}.{suffix}",
                        comparison.get(key),
                        elapsed,
                    )

            publish_pair("lightstone", "emwave", "lightstone_emwave")
            for unit_number in range(2, 5):
                publish_pair(
                    "lightstone",
                    emwave_device_id(unit_number),
                    f"lightstone_emwave{unit_number}",
                )

            publish_pair("lightstone", "camera", "lightstone_camera")
            for unit_number in range(1, 5):
                publish_pair(
                    emwave_device_id(unit_number),
                    "camera",
                    f"emwave{unit_number}_camera",
                )

            connected_emwaves = [
                device_id
                for device_id in [emwave_device_id(i) for i in range(1, 5)]
                if device_id in pulse_points
            ]
            for first_pos, first_id in enumerate(connected_emwaves):
                for second_id in connected_emwaves[first_pos + 1 :]:
                    first_index = 1 if first_id == "emwave" else int(first_id[6:])
                    second_index = 1 if second_id == "emwave" else int(second_id[6:])
                    publish_pair(
                        first_id,
                        second_id,
                        f"emwave{first_index}_emwave{second_index}",
                    )

            if muse_connected:
                eeg = eeg_metrics(muse_eeg, MUSE_EEG_RATE)
                for key, signal_id in eeg_map.items():
                    self._store_derived(signal_id, eeg.get(key), elapsed)
                motion = motion_metrics(muse_accel)
                self._store_derived(
                    "muse.motion_intensity", motion.get("motion_intensity"), elapsed
                )

            delay = max(0.15, 1.0 - (time.monotonic() - started))
            time.sleep(delay)

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
            self.muse_connected = True
            self.muse_last_data_monotonic = time.monotonic()
            self.muse_last_eeg_monotonic = self.muse_last_data_monotonic
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
            self.muse_connected = True
            self.muse_last_data_monotonic = time.monotonic()
            self.muse_last_accel_monotonic = self.muse_last_data_monotonic
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
            self.muse_connected = True
            self.muse_last_data_monotonic = time.monotonic()
            self.muse_battery = battery

    def _store_muse_status(self, status) -> None:
        with self.lock:
            if status.stage != self.muse_stage:
                self.muse_stage_started_monotonic = time.monotonic()
            self.muse_version = status.version
            self.muse_status_text = status.status_text
            self.muse_stage = status.stage
            self.muse_attempt_state = "working"
            self.muse_last_error = ""
            self.muse_retry_at_monotonic = 0.0
            self.muse_transport = status.transport
            self.muse_rfcomm_services = status.rfcomm_services
            self.muse_rfcomm_channel = status.rfcomm_channel
            self.muse_passive_probe = status.passive_probe
            self.muse_afe_gain = status.afe_gain

    def _reset_emwave_slot_history(self, unit_number: int) -> None:
        """Clear data when a session slot is reassigned to a different device path."""

        device_id = emwave_device_id(unit_number)
        with self.lock:
            if unit_number == 1:
                self.emwave_seq = 0
                self.emwave_packet_count = 0
                self.emwave_gap_count = 0
                self.emwave_generation += 1
                self.emwave_samples.clear()
                self.emwave_parser = EmWaveParser()
                self.emwave_clock.reset()
            else:
                runtime = self.emwave_extra_units.get(unit_number)
                if runtime is None:
                    return
                runtime["seq"] = 0
                runtime["packet_count"] = 0
                runtime["gap_count"] = 0
                runtime["generation"] += 1
                runtime["samples"].clear()
                runtime["parser"] = EmWaveParser()
                runtime["clock"].reset()

            # A slot name is a session identity, not a permanent physical
            # identity. If a different HID path takes the slot, old derived
            # physiology and pair comparisons must not bleed into the new unit.
            if hasattr(self, "derived_samples"):
                for signal_id, definition in SIGNAL_DEFINITIONS.items():
                    required = definition.get("requires_devices") or [
                        definition.get("device_id")
                    ]
                    if device_id not in required:
                        continue
                    history = self.derived_samples.get(signal_id)
                    if history is not None:
                        history.clear()
                        self.derived_seq[signal_id] = 0

    def _store_emwave_packet(self, parsed: dict, unit_number: int = 1) -> None:
        arrival_t = time.monotonic() - self.started_monotonic
        device_id = emwave_device_id(unit_number)
        signal_id = f"{device_id}.pulse_raw"
        osc_path = f"{emwave_osc_prefix(unit_number)}/pulse_raw"

        with self.lock:
            if unit_number == 1:
                self.emwave_packet_count += 1
                self.emwave_gap_count += int(parsed.get("gap") or 0)
                values = parsed["samples"]
                sample_times = self.emwave_clock.packet_times(
                    arrival_t, int(parsed.get("gap") or 0)
                )
                for sample_t, value in zip(sample_times, values):
                    self.emwave_seq += 1
                    sample = {
                        "seq": self.emwave_seq,
                        "t": sample_t,
                        "pulse": int(value),
                        "packet": int(parsed["counter"]),
                    }
                    self.emwave_samples.append(sample)

                    if self.recording and self.recording_writer:
                        self.recording_writer.writerow(
                            [
                                f"{self.started_unix + sample_t:.6f}",
                                f"{sample_t:.6f}",
                                device_id,
                                signal_id,
                                int(value),
                            ]
                        )

                    if self.osc_enabled:
                        try:
                            self.osc_socket.sendto(
                                osc_message(osc_path, int(value)),
                                (self.osc_host, self.osc_port),
                            )
                        except OSError as exc:
                            self.emwave_last_error = "OSC: " + str(exc)

                if (
                    self.recording
                    and self.recording_file
                    and self.emwave_packet_count % 10 == 0
                ):
                    self.recording_file.flush()
                return

            runtime = self.emwave_extra_units.get(unit_number)
            if runtime is None:
                return
            runtime["packet_count"] += 1
            runtime["gap_count"] += int(parsed.get("gap") or 0)
            values = parsed["samples"]
            sample_times = runtime["clock"].packet_times(
                arrival_t, int(parsed.get("gap") or 0)
            )
            for sample_t, value in zip(sample_times, values):
                runtime["seq"] += 1
                sample = {
                    "seq": runtime["seq"],
                    "t": sample_t,
                    "pulse": int(value),
                    "packet": int(parsed["counter"]),
                }
                runtime["samples"].append(sample)

                if self.recording and self.recording_writer:
                    self.recording_writer.writerow(
                        [
                            f"{self.started_unix + sample_t:.6f}",
                            f"{sample_t:.6f}",
                            device_id,
                            signal_id,
                            int(value),
                        ]
                    )

                if self.osc_enabled:
                    try:
                        self.osc_socket.sendto(
                            osc_message(osc_path, int(value)),
                            (self.osc_host, self.osc_port),
                        )
                    except OSError as exc:
                        runtime["error"] = "OSC: " + str(exc)

            if (
                self.recording
                and self.recording_file
                and runtime["packet_count"] % 10 == 0
            ):
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
                    if not safe_hid_enumerate(VENDOR_ID, PRODUCT_ID):
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
                    wanted_stage = (
                        "Acquisition stopped" if not should_run else "Waiting for serial port"
                    )
                    if wanted_stage != self.muse_stage:
                        self.muse_stage_started_monotonic = time.monotonic()
                    self.muse_stage = wanted_stage
                    self.muse_attempt_state = "stopped" if not should_run else "idle"
                    self.muse_retry_at_monotonic = 0.0
                time.sleep(0.5)
                continue

            with self.lock:
                self.muse_attempt_number += 1
                self.muse_attempt_state = "working"
                self.muse_last_error = ""
                self.muse_retry_at_monotonic = 0.0
                self.muse_stage = "Starting Muse connection attempt"
                self.muse_stage_started_monotonic = time.monotonic()

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
                    # Opening a persistent macOS Bluetooth serial port is not proof
                    # that the headband itself is present. "Connected" becomes true
                    # only when actual Muse packets arrive.
                    self.muse_connected = False
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
                    self.muse_attempt_state = "failed"
                    self.muse_stage = "Attempt finished: connection failed"
                    self.muse_stage_started_monotonic = time.monotonic()
                    self.muse_retry_at_monotonic = 0.0
                    # A failed diagnostic pass should stop here. Repeating the
                    # same Bluetooth probes indefinitely makes it impossible to
                    # tell whether useful work is still happening.
                    self.muse_running = False
            finally:
                client.close()
                with self.lock:
                    self.muse_connected = False

    def _emwave_manager_loop(self) -> None:
        """Own all emWave HID handles in one thread.

        hidapi on macOS crosses into IOKit. Keeping enumeration, open/close, and
        reads for identical emWave devices in one worker avoids native races that
        can terminate Python with SIGABRT rather than a catchable exception.
        """

        devices: dict[int, hid.device] = {}
        active_paths: dict[int, bytes] = {}
        slot_paths: dict[int, bytes] = {}
        next_scan = 0.0

        def runtime_for(unit_number: int):
            if unit_number == 1:
                return None
            return self.emwave_extra_units[unit_number]

        def should_run(unit_number: int) -> bool:
            with self.lock:
                if unit_number == 1:
                    return self.emwave_running
                return bool(self.emwave_extra_units[unit_number]["running"])

        def set_disconnected(unit_number: int, error: str = "") -> None:
            with self.lock:
                if unit_number == 1:
                    self.emwave_connected = False
                    self.emwave_last_error = error
                else:
                    runtime = self.emwave_extra_units[unit_number]
                    runtime["connected"] = False
                    runtime["error"] = error

        def set_connected(unit_number: int) -> None:
            with self.lock:
                if unit_number == 1:
                    self.emwave_connected = True
                    self.emwave_last_error = ""
                    self.emwave_parser = EmWaveParser()
                    self.emwave_clock.reset()
                else:
                    runtime = self.emwave_extra_units[unit_number]
                    runtime["connected"] = True
                    runtime["seen"] = True
                    runtime["error"] = ""
                    runtime["parser"] = EmWaveParser()
                    runtime["clock"].reset()

        def close_unit(unit_number: int) -> None:
            device = devices.pop(unit_number, None)
            active_paths.pop(unit_number, None)
            if device is not None:
                try:
                    device.close()
                except Exception:
                    pass
            set_disconnected(unit_number)

        while not self.shutdown:
            now = time.monotonic()

            if now >= next_scan:
                try:
                    paths = emwave_hid_paths()
                except Exception as exc:
                    for unit_number in range(1, 5):
                        set_disconnected(unit_number, str(exc))
                    time.sleep(1.0)
                    next_scan = time.monotonic() + 1.0
                    continue

                next_scan = now + 1.0
                slot_paths, replaced_slots = reconcile_emwave_slot_paths(
                    slot_paths, paths
                )
                for unit_number in sorted(replaced_slots):
                    if unit_number in devices:
                        close_unit(unit_number)
                    self._reset_emwave_slot_history(unit_number)

                connected_paths = set(paths)
                for unit_number in range(1, 5):
                    assigned_path = slot_paths.get(unit_number)
                    wanted_path = (
                        assigned_path
                        if (
                            should_run(unit_number)
                            and assigned_path in connected_paths
                        )
                        else None
                    )

                    if wanted_path is None:
                        if unit_number in devices:
                            close_unit(unit_number)
                        else:
                            set_disconnected(unit_number)
                        continue

                    if (
                        unit_number in devices
                        and active_paths.get(unit_number) == wanted_path
                    ):
                        continue

                    if unit_number in devices:
                        close_unit(unit_number)

                    try:
                        device = hid.device()
                        device.open_path(wanted_path)
                        # A single manager polls all handles, so no one read may
                        # block the others.
                        device.set_nonblocking(True)
                        devices[unit_number] = device
                        active_paths[unit_number] = wanted_path
                        set_connected(unit_number)
                    except Exception as exc:
                        try:
                            device.close()
                        except Exception:
                            pass
                        set_disconnected(unit_number, str(exc))

            for unit_number, device in list(devices.items()):
                if not should_run(unit_number):
                    close_unit(unit_number)
                    continue

                try:
                    report = device.read(8)
                    if not report:
                        continue

                    if unit_number == 1:
                        parser = self.emwave_parser
                    else:
                        parser = runtime_for(unit_number)["parser"]

                    parsed = parser.feed_report(report)
                    if parsed is not None:
                        self._store_emwave_packet(parsed, unit_number)
                except Exception as exc:
                    close_unit(unit_number)
                    set_disconnected(unit_number, str(exc))

            time.sleep(0.005)

        for unit_number in list(devices):
            close_unit(unit_number)


STATE: BiofeedbackState | None = None


HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Biofeedback Play</title>
<script>
try {
  document.documentElement.dataset.theme =
    localStorage.getItem("biofeedbackPlay.theme.v1") || "dark";
} catch (_) {
  document.documentElement.dataset.theme = "dark";
}
</script>
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
html[data-theme="light"] {
  color-scheme: light;
  --bg: #f2f4f8;
  --panel: #ffffff;
  --panel2: #e8ecf3;
  --text: #171b24;
  --muted: #606979;
  --line: #c7ced9;
  --good: #16834a;
  --bad: #c74444;
  --accent: #6254cf;
  --accent2: #7668d9;
  --soft: rgba(24,31,45,.045);
}
html[data-theme="light"] body {
  background: radial-gradient(circle at 20% 0%, #ffffff, var(--bg) 48%);
}
html[data-theme="light"] .tabs,
html[data-theme="light"] .card,
html[data-theme="light"] .signal-panel {
  background: rgba(255,255,255,.94);
  box-shadow: 0 9px 26px rgba(45,55,75,.08);
}
html[data-theme="light"] .signal-device-section {
  background: rgba(231,235,243,.72);
}
html[data-theme="light"] .device-section-header {
  background: rgba(255,255,255,.92);
}
html[data-theme="light"] .signal-panel.direct,
html[data-theme="light"] .signal-panel.calculated,
html[data-theme="light"] .signal-panel.comparison,
html[data-theme="light"] .device-card,
html[data-theme="light"] .metric {
  background: #ffffff;
}
html[data-theme="light"] .signal-meaning,
html[data-theme="light"] .info-line strong,
html[data-theme="light"] .device-section-counts {
  color: #333b49;
}
html[data-theme="light"] input[type=text],
html[data-theme="light"] input[type=number],
html[data-theme="light"] select,
html[data-theme="light"] #musePortScanStatus {
  background: #ffffff !important;
  color: var(--text);
}
html[data-theme="light"] canvas,
html[data-theme="light"] .camera-view,
html[data-theme="light"] .diag-output {
  background: #eef1f6;
}
html[data-theme="light"] .camera-lab,
html[data-theme="light"] .camera-lab.active,
html[data-theme="light"] .signal-toolbar,
html[data-theme="light"] .camera-note {
  background: rgba(255,255,255,.86);
}
html[data-theme="light"] .camera-view-label {
  background: rgba(255,255,255,.88);
  color: #303746;
}
html[data-theme="light"] .filter-button.active {
  color: var(--text);
  background: #dde2eb;
  border-color: #aeb7c6;
}
html[data-theme="light"] .kind-badge.direct { color: #176b3d; }
html[data-theme="light"] .kind-badge.calculated { color: #5444aa; }
html[data-theme="light"] .kind-badge.comparison { color: #805b12; }
html[data-theme="light"] .source-chip { color: #3d4655; }
html[data-theme="light"] .audio-note { color: #596273; }
html[data-theme="light"] #error { color: #a42f2f; }
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
button.primary { background: #4338ca; border-color: #635bdf; color: #fff; }
button.recording { background: #81233f; border-color: #b33c60; color: #fff; }
button.audio-on { background: #315b46; border-color: #4d8b6b; color: #fff; }
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
  display: block;
  grid-column: span 12;
}
.signal-device-section {
  margin-top: 18px;
  border: 1px solid var(--line);
  border-radius: 18px;
  overflow: hidden;
  background: rgba(9,11,16,.46);
  --device-accent: var(--accent);
}
.device-section-header {
  display: flex; justify-content: space-between; align-items: center;
  gap: 14px; flex-wrap: wrap;
  padding: 14px 16px;
  border-left: 7px solid var(--device-accent);
  background: rgba(29,34,48,.8);
}
.device-section-name { font-size: 21px; font-weight: 760; letter-spacing: -.015em; }
.device-section-summary { margin-top: 3px; color: var(--muted); font-size: 12px; max-width: 700px; }
.device-section-counts {
  color: #d7dce8; font-size: 12px; font-weight: 650;
}
.signal-subsection { padding: 14px 14px 4px; }
.signal-subsection + .signal-subsection { border-top: 1px solid rgba(52,59,78,.62); }
.signal-subsection-heading {
  display: flex; align-items: baseline; gap: 8px; margin: 0 2px 10px; flex-wrap: wrap;
}
.signal-subsection-title {
  font-size: 12px; font-weight: 760; letter-spacing: .09em;
  text-transform: uppercase; color: #dce1ed;
}
.signal-subsection-help { color: var(--muted); font-size: 11px; }
.signal-section-grid {
  display: grid; grid-template-columns: repeat(12, 1fr); gap: 11px;
  align-items: start;
}
.signal-panel {
  grid-column: span 4; overflow: hidden; position: relative; align-self: start;
  --device-accent: #8b7cf6;
  --panel-accent: var(--device-accent);
  box-shadow: none;
}
.signal-panel.direct {
  grid-column: span 12;
  border-width: 1px 1px 1px 5px;
  border-left-color: var(--device-accent);
  background: rgba(22,27,38,.98);
}
.signal-panel.calculated {
  background: rgba(16,19,27,.82);
}
.signal-panel.comparison {
  grid-column: span 6;
  background:
    linear-gradient(145deg, rgba(255,210,120,.055), transparent 40%),
    rgba(16,19,27,.9);
}
.signal-panel::before { content: none; }
.signal-panel-header {
  display: flex; justify-content: space-between; gap: 10px;
  align-items: flex-start; padding: 12px 13px 8px;
}
.signal-title-row { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
.signal-title { font-size: 17px; font-weight: 720; }
.signal-panel.direct .signal-title { font-size: 20px; }
.signal-device { margin-top: 5px; color: var(--muted); font-size: 11px; }
.signal-glance { padding: 0 13px 10px; }
.signal-primary-value {
  display: flex; align-items: baseline; gap: 7px; flex-wrap: wrap;
}
.signal-primary-number {
  font-size: 29px; font-weight: 720; font-variant-numeric: tabular-nums;
  letter-spacing: -.025em;
}
.signal-panel.direct .signal-primary-number { font-size: 34px; }
.signal-primary-unit { color: var(--muted); font-size: 12px; }
.signal-meaning {
  margin-top: 4px; color: #cbd2df; font-size: 12px; line-height: 1.42;
}
.signal-tech {
  margin: 0 13px 12px; border-top: 1px solid rgba(52,59,78,.7);
  padding-top: 8px;
}
.signal-tech summary { color: var(--muted); font-size: 11px; cursor: pointer; }
.signal-tech[open] summary { margin-bottom: 8px; }
.signal-panel canvas { height: 92px; margin: 0 13px 10px; width: calc(100% - 26px); }
.signal-panel.direct canvas { height: 165px; }
.signal-panel.comparison canvas { height: 110px; }
.signal-header-actions {
  display: flex; align-items: center; gap: 6px; flex: 0 0 auto;
}
.panel-view-dot {
  position: absolute; top: 9px; left: 9px; z-index: 3;
  width: 13px; height: 13px; min-width: 13px;
  padding: 0; border: 0; border-radius: 50%;
  display: grid; place-items: center;
  box-shadow: inset 0 0 0 1px rgba(0,0,0,.18);
}
.signal-panel.view-full .panel-view-dot { background: #f5bf4f; }
.signal-panel.view-mini .panel-view-dot { background: #61c554; }
.panel-view-dot::after {
  color: rgba(29,32,39,.78);
  font-size: 11px; font-weight: 800; line-height: 1;
}
.signal-panel.view-full .panel-view-dot::after { content: "−"; }
.signal-panel.view-mini .panel-view-dot::after { content: "+"; }
.signal-panel-header { padding-left: 31px; }

.audio-icon {
  width: 31px; height: 31px; min-width: 31px;
  padding: 0; border-radius: 50%;
  display: inline-grid; place-items: center;
  font-size: 16px; line-height: 1;
  color: var(--muted); background: transparent;
}
.audio-icon.audio-on {
  color: #fff; background: #315b46; border-color: #4d8b6b;
}
.signal-collapse-device { display: none; }

/* Mini retains the same footprint as before, but keeps source identity and audio. */
.signal-panel.view-mini {
  grid-column: span 4 !important;
  display: grid;
  grid-template-columns: minmax(125px,.8fr) minmax(145px,1.2fr);
  align-items: center;
  min-height: 72px;
}
.signal-panel.view-mini .signal-panel-header {
  grid-column: 1;
  padding: 9px 7px 9px 31px;
  align-items: center;
}
.signal-panel.view-mini .signal-title-row { gap: 0; }
.signal-panel.view-mini .signal-title {
  font-size: 14px !important;
  line-height: 1.18;
}
.signal-panel.view-mini .signal-collapse-device {
  display: block;
  margin-top: 3px;
  color: var(--muted);
  font-size: 10px;
  line-height: 1.2;
}
.signal-panel.view-mini .kind-badge,
.signal-panel.view-mini .source-chips,
.signal-panel.view-mini .signal-glance,
.signal-panel.view-mini .signal-tech {
  display: none;
}
.signal-panel.view-mini .signal-header-actions {
  gap: 3px;
}
.signal-panel.view-mini .audio-icon {
  width: 27px; height: 27px; min-width: 27px;
  font-size: 14px;
}
.signal-panel.view-mini canvas {
  grid-column: 2;
  height: 54px !important;
  width: calc(100% - 10px);
  margin: 8px 10px 8px 0;
  border-radius: 7px;
}
.signal-panel.view-mini.offline { display: none; }
.kind-badge {
  display: inline-flex; align-items: center; gap: 5px;
  border: 1px solid var(--line); border-radius: 999px;
  padding: 3px 7px; font-size: 10px; line-height: 1;
  letter-spacing: .05em; text-transform: uppercase; color: var(--muted);
  background: rgba(255,255,255,.025);
}
.kind-badge.direct {
  border-color: rgba(89,209,133,.55); color: #b5f1ca;
  background: rgba(89,209,133,.09);
}
.kind-badge.calculated {
  border-color: rgba(196,181,253,.42); color: #d9d0ff;
  background: rgba(139,124,246,.07);
}
.kind-badge.comparison {
  border-color: rgba(255,210,120,.45); color: #f3d596;
  background: rgba(255,210,120,.07);
}
.source-chips { display: flex; gap: 5px; flex-wrap: wrap; margin-top: 6px; }
.source-chip {
  display: inline-flex; align-items: center; gap: 6px;
  border: 1px solid var(--line); border-radius: 999px;
  padding: 3px 8px 3px 6px; font-size: 11px; color: #cfd5e2;
  background: rgba(255,255,255,.025);
}
.source-swatch {
  width: 8px; height: 8px; border-radius: 50%; flex: 0 0 auto;
  background: var(--source-color, var(--accent));
}
.signal-body { padding: 0; }
.signal-panel.offline { display: none; }
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
  grid-column: span 6; padding: 0 14px 14px; overflow: hidden; position: relative;
  border: 1px solid var(--line); border-radius: 12px; background: #10131b;
  --device-accent: #8b7cf6;
}
.device-card::before {
  content: ""; display: block; height: 3px; margin: 0 -14px 12px;
  background: var(--device-accent);
}
.device-name { font-size: 17px; font-weight: 650; }
.device-meta { margin-top: 8px; display: grid; gap: 4px; }
.device-signals { margin-top: 10px; }
.badge {
  display: inline-block; border: 1px solid var(--line); border-radius: 999px;
  padding: 4px 8px; margin: 3px 4px 0 0; color: var(--muted); font-size: 11px;
}
.camera-lab {
  grid-column: span 12;
  overflow: hidden;
  margin-top: 18px;
  padding: 12px 14px;
  background: rgba(16,19,27,.72);
}
.camera-lab .camera-lab-details { display: none; }
.camera-lab.active { padding: 16px; background: rgba(21,24,33,.9); }
.camera-lab.active .camera-lab-details { display: block; }
.camera-lab:not(.active) h2 { font-size: 16px; }
.camera-lab:not(.active) .camera-off-hint { display: inline; }
.camera-lab.active .camera-off-hint { display: none; }
.camera-lab-grid {
  display: block;
  margin-top: 10px;
}
.camera-view {
  position: relative;
  border: 1px solid var(--line);
  border-radius: 12px;
  overflow: hidden;
  background: #090c12;
}
.camera-view canvas {
  width: 100%;
  height: auto;
  aspect-ratio: 4 / 3;
  margin: 0;
  border-radius: 0;
  display: block;
}
.camera-view-label {
  position: absolute;
  left: 9px; top: 8px; z-index: 2;
  padding: 4px 7px;
  border: 1px solid rgba(255,255,255,.14);
  border-radius: 999px;
  background: rgba(7,9,13,.72);
  color: #d9dfeb;
  font-size: 11px;
  backdrop-filter: blur(5px);
}
.camera-controls {
  display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
  margin-top: 12px;
}
.camera-controls.before-video {
  margin: 12px 0 8px;
  padding: 10px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--soft);
}
.camera-controls input[type=range] { width: 150px; }
.camera-magnify-toggle.active {
  background: #4338ca; border-color: #635bdf; color: #fff;
}
.camera-note {
  margin-top: 10px;
  padding: 9px 10px;
  border: 1px solid var(--line);
  border-radius: 9px;
  background: rgba(255,255,255,.025);
}
.camera-live-value { font-variant-numeric: tabular-nums; }
#cameraVideo { display: none; }

.signal-toolbar {
  grid-column: span 12; display: block; padding: 10px 12px;
  border: 1px solid var(--line); border-radius: 12px;
  background: rgba(21,24,33,.68);
}
.signal-toolbar-row {
  display: flex; align-items: center; justify-content: space-between;
  gap: 12px; flex-wrap: wrap;
}
.device-view-controls {
  display: flex; align-items: center; justify-content: space-between;
  gap: 12px; flex-wrap: wrap;
  margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--line);
}
.device-view-buttons { display: flex; gap: 7px; flex-wrap: wrap; }
.device-view-switch {
  display: inline-flex; align-items: center; gap: 7px;
  padding: 6px 8px; font-size: 12px;
  color: var(--muted); background: transparent;
  border-color: transparent;
}
.device-view-switch.enabled {
  color: var(--text);
  background: var(--soft);
  border-color: var(--line);
}
.device-view-switch:not(.enabled) {
  opacity: .58;
}
.device-view-switch .source-swatch { width: 9px; height: 9px; }
.switch-track {
  width: 34px; height: 20px; padding: 2px;
  border-radius: 999px;
  background: #515866;
  box-shadow: inset 0 0 0 1px rgba(255,255,255,.08);
  transition: background .14s ease;
}
.switch-knob {
  display: block; width: 16px; height: 16px;
  border-radius: 50%; background: #fff;
  box-shadow: 0 1px 3px rgba(0,0,0,.28);
  transform: translateX(0);
  transition: transform .14s ease;
}
.device-view-switch.enabled .switch-track { background: #34c759; }
.device-view-switch.enabled .switch-knob { transform: translateX(14px); }
.camera-lab.view-hidden { display: none; }
.filter-buttons { display: flex; gap: 6px; flex-wrap: wrap; }
.filter-button { padding: 6px 10px; font-size: 12px; color: var(--muted); }
.layout-actions { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
.layout-hint { color: var(--muted); font-size: 11px; }
.drag-handle {
  display: inline-flex; align-items: center; justify-content: center;
  width: 27px; height: 27px; padding: 0;
  border: 1px solid transparent; border-radius: 8px;
  color: var(--muted); background: transparent; cursor: grab;
  user-select: none; font-size: 17px; line-height: 1;
}
.drag-handle:hover { border-color: var(--line); background: rgba(255,255,255,.035); color: var(--text); }
.drag-handle:active { cursor: grabbing; }
.signal-panel.dragging { opacity: .42; transform: scale(.995); }
.signal-panel.drop-before { box-shadow: 0 -3px 0 var(--accent2), 0 12px 35px rgba(0,0,0,.15); }
.signal-panel.drop-after { box-shadow: 0 3px 0 var(--accent2), 0 12px 35px rgba(0,0,0,.15); }
.filter-button.active { color: var(--text); background: #2b3040; border-color: #606980; }
.legend { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.legend-item { color: var(--muted); font-size: 11px; display: inline-flex; align-items: center; gap: 5px; }
.legend-mark { width: 12px; height: 3px; border-radius: 3px; background: var(--accent2); }
.legend-mark.calculated {
  height: 7px; border: 1px solid rgba(196,181,253,.55); background: rgba(196,181,253,.12);
}
.legend-mark.comparison {
  height: 7px; border: 1px solid rgba(255,210,120,.55); background: rgba(255,210,120,.12);
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
.muse-attempt {
  margin-top: 10px;
  padding: 11px 12px;
  border-radius: 10px;
  border: 1px solid var(--line);
  font-size: 13px;
  line-height: 1.45;
}
.muse-attempt.working {
  background: rgba(139,124,246,.10);
  border-color: rgba(139,124,246,.55);
}
.muse-attempt.failed {
  background: rgba(255,107,107,.09);
  border-color: rgba(255,107,107,.55);
}
.muse-attempt.live {
  background: rgba(89,209,133,.09);
  border-color: rgba(89,209,133,.55);
}
.muse-attempt-title { font-weight: 700; margin-bottom: 3px; }
.muse-attempt-detail { color: var(--muted); }

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
@media (max-width: 900px) {
  .signal-panel, .signal-panel.comparison, .signal-panel.view-mini,
  .half, .device-card { grid-column: span 12 !important; }
  .signal-panel.view-mini {
    grid-template-columns: minmax(130px,.8fr) minmax(150px,1.2fr);
  }
  .camera-lab-grid { grid-template-columns: 1fr; }
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
    <div class="row">
      <button id="themeToggle" aria-label="Switch color theme">Light mode</button>
      <div class="status-pill">
        <span id="globalDot" class="dot"></span>
        <span id="globalStatus">Checking devices...</span>
      </div>
    </div>
  </div>

  <nav class="tabs" aria-label="Biofeedback Play sections">
    <button class="tab-button active" data-tab="use">Live data</button>
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



      <section class="signal-toolbar">
        <div class="signal-toolbar-row">
          <div>
            <div class="filter-buttons" aria-label="Signal panel filter">
              <button class="filter-button active" data-signal-filter="all">Everything</button>
              <button class="filter-button" data-signal-filter="direct">Direct sensor data</button>
              <button class="filter-button" data-signal-filter="calculated">Derived</button>
              <button class="filter-button" data-signal-filter="comparison">Comparisons</button>
            </div>
          </div>
          <div class="legend" aria-label="Panel legend">
            <span class="legend-item"><span class="legend-mark"></span>Direct sensor data</span>
            <span class="legend-item"><span class="legend-mark calculated"></span>Derived</span>
            <span class="legend-item"><span class="legend-mark comparison"></span>Comparison</span>
          </div>
        </div>
        <div class="device-view-controls">
          <div>
            <div class="label">Device views</div>
          </div>
          <div id="deviceViewButtons" class="device-view-buttons"></div>
        </div>
      </section>

      <div id="signalGrid" class="signal-grid"></div>

      <section id="cameraLab" class="card camera-lab">
        <div class="row between">
          <div>
            <div class="label">Optional input · Camera</div>
            <h2 style="margin-top:4px">Camera pulse experiment</h2>
            <div class="small camera-off-hint" style="margin-top:4px">Camera off.</div>
          </div>
          <div class="row">
            <span class="status-pill"><span id="cameraDot" class="dot"></span><span id="cameraStatus">Camera off</span></span>
            <button id="cameraToggle" class="primary">Start camera</button>
          </div>
        </div>
        <div class="camera-lab-details">
          <div class="small" style="margin-top:10px">Align your face with the sampling guides. Processing stays local.</div>
          <video id="cameraVideo" playsinline muted></video>

          <div class="camera-controls before-video">
            <button id="cameraMagnifyToggle" class="camera-magnify-toggle" aria-pressed="false">Magnified view</button>
            <label class="small">Magnification
              <input id="cameraGain" type="range" min="0" max="40" step="1" value="12">
              <strong id="cameraGainValue">12×</strong>
            </label>
            <label class="small">Guide size
              <input id="cameraGuideScale" type="range" min="70" max="140" step="2" value="100">
              <strong id="cameraGuideScaleValue">100%</strong>
            </label>
            <label class="small">Guide left/right
              <input id="cameraGuideX" type="range" min="-20" max="20" step="1" value="0">
              <strong id="cameraGuideXValue">0%</strong>
            </label>
            <label class="small">Guide up/down
              <input id="cameraGuideY" type="range" min="-20" max="20" step="1" value="0">
              <strong id="cameraGuideYValue">0%</strong>
            </label>
          </div>

          <div class="camera-lab-grid">
            <div class="camera-view">
              <div id="cameraViewLabel" class="camera-view-label">Normal + sampling guides</div>
              <canvas id="cameraSourceCanvas" width="320" height="240"></canvas>
            </div>
          </div>

          <div class="camera-controls">
            <span class="small">Camera pulse: <strong id="cameraPpgValue" class="camera-live-value">—</strong></span>
            <span class="small">Motion: <strong id="cameraMotionValue" class="camera-live-value">—</strong></span>
            <span class="small">Signal quality: <strong id="cameraQualityValue" class="camera-live-value">—</strong></span>
          </div>
          <div class="camera-note small">Camera HRV and coherence remain hidden until timing is validated against a contact sensor.</div>
        </div>
      </section>
    </div>
  </section>

  <section id="tab-setup" class="tab-page">
    <div class="grid">
      <section class="card full">
        <div class="row between">
          <div>
            <div class="label">Configured devices</div>
            <div class="small" style="margin-top:6px">
              Start, stop, and configure connected devices here.
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
let signalFilter = "all";
let deviceViewState = loadDeviceViewState();
let deviceViewSignature = "";
let panelOrder = loadPanelOrder();
let panelViewState = loadPanelViewState();
let draggedSignalId = null;
let cameraStream = null;
let cameraAnimationFrame = null;
let cameraLastFrameAt = 0;
let cameraPpgFast = null;
let cameraPpgSlow = null;
let cameraPixelFast = null;
let cameraPixelSlow = null;
let cameraPreviousFrame = null;
let cameraPendingSamples = [];
let cameraLastPostAt = 0;
let cameraLastSampleTimestamp = null;
let cameraQualityHistory = [];
let cameraRgbHistory = [];
let cameraMagnificationOn = false;

function loadDeviceViewState() {
  try {
    const raw = localStorage.getItem("biofeedbackPlay.deviceViews.v1");
    const parsed = raw ? JSON.parse(raw) : {};
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
  } catch (_) {
    return {};
  }
}

function saveDeviceViewState() {
  try {
    localStorage.setItem("biofeedbackPlay.deviceViews.v1", JSON.stringify(deviceViewState));
  } catch (_) {}
}

function deviceViewEnabled(deviceId) {
  return deviceViewState[deviceId] !== false;
}

function signalViewEnabled(signal) {
  return signalSourceIds(signal).every(function(deviceId) {
    return deviceViewEnabled(deviceId);
  });
}

function setTheme(theme) {
  const next = theme === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try {
    localStorage.setItem("biofeedbackPlay.theme.v1", next);
  } catch (_) {}
  const button = document.getElementById("themeToggle");
  if (button) {
    button.textContent = next === "light" ? "Dark mode" : "Light mode";
    button.setAttribute("aria-label", next === "light" ? "Switch to dark mode" : "Switch to light mode");
  }
  requestAnimationFrame(drawAllSignals);
}

function loadPanelOrder() {
  try {
    const raw = localStorage.getItem("biofeedbackPlay.panelOrder.v1");
    const parsed = raw ? JSON.parse(raw) : null;
    return Array.isArray(parsed) ? parsed.filter(function(id) { return typeof id === "string"; }) : null;
  } catch (_) {
    return null;
  }
}

function savePanelOrder() {
  try {
    if (panelOrder) {
      localStorage.setItem("biofeedbackPlay.panelOrder.v1", JSON.stringify(panelOrder));
    } else {
      localStorage.removeItem("biofeedbackPlay.panelOrder.v1");
    }
  } catch (_) {}
}

function loadPanelViewState() {
  try {
    const raw = localStorage.getItem("biofeedbackPlay.panelViews.v1");
    const parsed = raw ? JSON.parse(raw) : {};
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
  } catch (_) {
    return {};
  }
}

function savePanelViewState() {
  try {
    localStorage.setItem("biofeedbackPlay.panelViews.v1", JSON.stringify(panelViewState));
  } catch (_) {}
}

function panelViewMode(signalId) {
  const mode = panelViewState[signalId];
  return mode === "mini" || mode === "name" ? "mini" : "full";
}

function nextPanelViewMode(mode) {
  return mode === "full" ? "mini" : "full";
}

function cyclePanelView(signalId) {
  const current = panelViewMode(signalId);
  const next = nextPanelViewMode(current);
  if (next === "full") delete panelViewState[signalId];
  else panelViewState[signalId] = next;
  savePanelViewState();

  const signal = catalog.signals.find(function(item) { return item.id === signalId; });
  if (!signal) return;
  const panel = document.getElementById("panel_" + domId(signalId));
  if (!panel) return;

  panel.classList.remove("view-full", "view-mini");
  panel.classList.add("view-" + next);
  const button = panel.querySelector("[data-panel-view-toggle]");
  if (button) {
    const action = next === "full" ? "Collapse panel" : "Expand panel";
    button.title = action;
    button.setAttribute("aria-label", action);
  }
  requestAnimationFrame(function() {
    drawSignal(signal);
  });
}

const DEVICE_COLORS = {
  lightstone: "#e6ad58",
  emwave: "#56c7b7",
  emwave2: "#55a7d8",
  emwave3: "#b28be0",
  emwave4: "#df8292",
  muse: "#7f9cf5",
  camera: "#f28b63"
};

function deviceColor(deviceId) {
  return DEVICE_COLORS[deviceId] || "#8b7cf6";
}

function signalSourceIds(signal) {
  const ids = signal.requires_devices && signal.requires_devices.length
    ? signal.requires_devices
    : [signal.device_id];
  return ids.filter(Boolean);
}

function signalKind(signal) {
  if (signalSourceIds(signal).length > 1) return "comparison";
  return signal.derived ? "calculated" : "direct";
}

function signalKindLabel(signal) {
  const kind = signalKind(signal);
  if (kind === "comparison") return "Comparison";
  if (kind === "calculated") return "Derived";
  return "Direct sensor";
}

function panelAccent(signal) {
  const colors = signalSourceIds(signal).map(deviceColor);
  if (colors.length <= 1) return colors[0] || "#8b7cf6";
  const stops = colors.map(function(color, index) {
    const start = Math.round(index * 100 / colors.length);
    const end = Math.round((index + 1) * 100 / colors.length);
    return color + " " + start + "%, " + color + " " + end + "%";
  });
  return "linear-gradient(90deg, " + stops.join(", ") + ")";
}

function defaultSortedSignals(signals) {
  return signals.slice().sort(function(a, b) {
    const aliveA = a.connected && a.running ? 0 : 1;
    const aliveB = b.connected && b.running ? 0 : 1;
    if (aliveA !== aliveB) return aliveA - aliveB;

    const sourceA = signalSourceIds(a)[0] || "";
    const sourceB = signalSourceIds(b)[0] || "";
    if (sourceA !== sourceB) return sourceA.localeCompare(sourceB);

    const order = {direct: 0, calculated: 1, comparison: 2};
    const kindA = order[signalKind(a)];
    const kindB = order[signalKind(b)];
    if (kindA !== kindB) return kindA - kindB;
    return String(a.name || a.id).localeCompare(String(b.name || b.id));
  });
}

function normalizedPanelOrder(signals) {
  const defaults = defaultSortedSignals(signals);
  if (!panelOrder) return defaults;

  const validIds = new Set(signals.map(function(signal) { return signal.id; }));
  const seen = new Set();
  const orderedIds = [];

  panelOrder.forEach(function(id) {
    if (validIds.has(id) && !seen.has(id)) {
      seen.add(id);
      orderedIds.push(id);
    }
  });

  defaults.forEach(function(signal) {
    if (!seen.has(signal.id)) {
      seen.add(signal.id);
      orderedIds.push(signal.id);
    }
  });

  panelOrder = orderedIds;
  savePanelOrder();
  const byId = new Map(signals.map(function(signal) { return [signal.id, signal]; }));
  return orderedIds.map(function(id) { return byId.get(id); }).filter(Boolean);
}

function sortedSignals(signals) {
  return normalizedPanelOrder(signals);
}

function storeVisiblePanelOrder(visibleIds) {
  const allSignals = normalizedPanelOrder(catalog.signals);
  const allIds = allSignals.map(function(signal) { return signal.id; });
  const visibleSet = new Set(visibleIds);
  let visibleIndex = 0;

  panelOrder = allIds.map(function(id) {
    if (!visibleSet.has(id)) return id;
    const replacement = visibleIds[visibleIndex];
    visibleIndex += 1;
    return replacement;
  });

  savePanelOrder();
}

function visibleSignals(signals) {
  const live = signals.filter(function(signal) {
    return Boolean(signal.connected && signal.running && signalViewEnabled(signal));
  });
  const ordered = defaultSortedSignals(live);
  if (signalFilter === "all") return ordered;
  return ordered.filter(function(signal) { return signalKind(signal) === signalFilter; });
}


function cameraRectangles(width, height) {
  const scaleControl = document.getElementById("cameraGuideScale");
  const xControl = document.getElementById("cameraGuideX");
  const yControl = document.getElementById("cameraGuideY");
  const scale = Math.max(.7, Math.min(1.4, Number(scaleControl ? scaleControl.value : 100) / 100));
  const xShift = Math.max(-.2, Math.min(.2, Number(xControl ? xControl.value : 0) / 100));
  const yShift = Math.max(-.2, Math.min(.2, Number(yControl ? yControl.value : 0) / 100));
  const centerX = .5 + xShift;
  const centerY = .43 + yShift;
  const base = [
    {x: .34, y: .17, w: .32, h: .15},
    {x: .27, y: .52, w: .16, h: .17},
    {x: .57, y: .52, w: .16, h: .17}
  ];
  return base.map(function(rect) {
    const rectCenterX = rect.x + rect.w / 2;
    const rectCenterY = rect.y + rect.h / 2;
    const scaledCenterX = centerX + (rectCenterX - .5) * scale;
    const scaledCenterY = centerY + (rectCenterY - .43) * scale;
    const w = rect.w * scale;
    const h = rect.h * scale;
    return {
      x: Math.round(width * (scaledCenterX - w / 2)),
      y: Math.round(height * (scaledCenterY - h / 2)),
      w: Math.round(width * w),
      h: Math.round(height * h)
    };
  }).map(function(rect) {
    rect.x = Math.max(0, Math.min(width - rect.w, rect.x));
    rect.y = Math.max(0, Math.min(height - rect.h, rect.y));
    return rect;
  });
}

function cameraFilterAlpha(cutoffHz, dtSeconds) {
  return 1 - Math.exp(-2 * Math.PI * cutoffHz * dtSeconds);
}

function cameraStdDev(values) {
  if (!values.length) return 0;
  const mean = values.reduce(function(a, b) { return a + b; }, 0) / values.length;
  const variance = values.reduce(function(total, value) {
    const delta = value - mean;
    return total + delta * delta;
  }, 0) / values.length;
  return Math.sqrt(Math.max(0, variance));
}

function cameraPosPulse(r, g, b) {
  cameraRgbHistory.push({r: r, g: g, b: b});
  if (cameraRgbHistory.length > 72) {
    cameraRgbHistory.splice(0, cameraRgbHistory.length - 72);
  }

  const windowLength = Math.min(48, cameraRgbHistory.length);
  if (windowLength < 24) return null;
  const window = cameraRgbHistory.slice(cameraRgbHistory.length - windowLength);

  const meanR = window.reduce(function(total, item) { return total + item.r; }, 0) / windowLength;
  const meanG = window.reduce(function(total, item) { return total + item.g; }, 0) / windowLength;
  const meanB = window.reduce(function(total, item) { return total + item.b; }, 0) / windowLength;
  if (meanR <= 1 || meanG <= 1 || meanB <= 1) return null;

  const xs = [];
  const ys = [];
  window.forEach(function(item) {
    const rn = item.r / meanR - 1;
    const gn = item.g / meanG - 1;
    const bn = item.b / meanB - 1;
    xs.push(gn - bn);
    ys.push(gn + bn - 2 * rn);
  });

  const yStd = cameraStdDev(ys);
  if (yStd < 1e-9) return null;
  const alpha = cameraStdDev(xs) / yStd;
  return xs[xs.length - 1] + alpha * ys[ys.length - 1];
}

function cameraInSamplingRegion(x, y, rects) {
  return rects.some(function(rect) {
    return x >= rect.x && x < rect.x + rect.w &&
      y >= rect.y && y < rect.y + rect.h;
  });
}

function cameraQualityScore(ppg, motion) {
  cameraQualityHistory.push({ppg: ppg, motion: motion});
  if (cameraQualityHistory.length > 180) {
    cameraQualityHistory.splice(0, cameraQualityHistory.length - 180);
  }
  if (cameraQualityHistory.length < 90) return null;

  const values = cameraQualityHistory.map(function(item) { return item.ppg; });
  const mean = values.reduce(function(a, b) { return a + b; }, 0) / values.length;
  const centered = values.map(function(value) { return value - mean; });
  const energy = centered.reduce(function(total, value) { return total + value * value; }, 0);
  if (energy < 1e-8) return 0;

  let bestCorrelation = 0;
  for (let lag = 10; lag <= 43; lag++) {
    let numerator = 0;
    let leftEnergy = 0;
    let rightEnergy = 0;
    for (let i = lag; i < centered.length; i++) {
      const left = centered[i];
      const right = centered[i - lag];
      numerator += left * right;
      leftEnergy += left * left;
      rightEnergy += right * right;
    }
    const denominator = Math.sqrt(leftEnergy * rightEnergy);
    if (denominator > 0) {
      bestCorrelation = Math.max(bestCorrelation, numerator / denominator);
    }
  }

  const motionValues = cameraQualityHistory.map(function(item) { return Math.max(0, item.motion); });
  const motionMean = motionValues.reduce(function(a, b) { return a + b; }, 0) / motionValues.length;
  const periodicityScore = Math.max(0, Math.min(1, (bestCorrelation - .12) / .68));
  const motionScore = 1 / (1 + Math.pow(motionMean / .65, 2));
  return Math.round(100 * periodicityScore * motionScore);
}

function cameraQualityLabel(score) {
  if (score == null) return "warming up";
  if (score >= 70) return score + "% good";
  if (score >= 40) return score + "% fair";
  return score + "% poor";
}

function drawCameraGuides(ctx, width, height) {
  ctx.save();
  ctx.strokeStyle = "rgba(255,255,255,.78)";
  ctx.lineWidth = 1.5;
  ctx.setLineDash([6, 5]);
  cameraRectangles(width, height).forEach(function(rect) {
    ctx.strokeRect(rect.x + .5, rect.y + .5, rect.w, rect.h);
  });
  ctx.setLineDash([]);
  ctx.restore();
}

function updateCameraStatus(text, live) {
  const dot = document.getElementById("cameraDot");
  const label = document.getElementById("cameraStatus");
  if (dot) dot.className = live ? "dot on" : "dot";
  if (label) label.textContent = text;
}

function cameraStopLocal() {
  if (cameraAnimationFrame) {
    cancelAnimationFrame(cameraAnimationFrame);
    cameraAnimationFrame = null;
  }
  if (cameraStream) {
    cameraStream.getTracks().forEach(function(track) { track.stop(); });
    cameraStream = null;
  }
  cameraPreviousFrame = null;
  cameraPpgFast = null;
  cameraPpgSlow = null;
  cameraPixelFast = null;
  cameraPixelSlow = null;
  cameraPendingSamples = [];
  cameraLastSampleTimestamp = null;
  cameraQualityHistory = [];
  cameraRgbHistory = [];
  document.getElementById("cameraToggle").textContent = "Start camera";
  document.getElementById("cameraToggle").className = "primary";
  document.getElementById("cameraLab").classList.remove("active");
  updateCameraStatus("Camera off", false);
  post("camera_stop").catch(function() {});
}

function cameraPostPending(force) {
  const now = performance.now();
  if (!cameraPendingSamples.length) return;
  if (!force && now - cameraLastPostAt < 250) return;
  const batch = cameraPendingSamples.splice(0, cameraPendingSamples.length);
  cameraLastPostAt = now;
  post("camera_samples", {samples: batch}).catch(function(err) {
    updateCameraStatus("Camera data error", false);
    document.getElementById("error").textContent = String(err);
  });
}

function cameraAnalyzeFrame(timestamp) {
  if (!cameraStream) return;
  cameraAnimationFrame = requestAnimationFrame(cameraAnalyzeFrame);
  if (timestamp - cameraLastFrameAt < 32) return;

  const dt = cameraLastSampleTimestamp == null
    ? 1 / 30
    : Math.max(1 / 120, Math.min(.12, (timestamp - cameraLastSampleTimestamp) / 1000));
  cameraLastSampleTimestamp = timestamp;
  cameraLastFrameAt = timestamp;

  const video = document.getElementById("cameraVideo");
  if (!video || video.readyState < 2) return;

  const source = document.getElementById("cameraSourceCanvas");
  const sourceCtx = source.getContext("2d", {willReadFrequently: true});
  const width = source.width;
  const height = source.height;

  sourceCtx.save();
  sourceCtx.translate(width, 0);
  sourceCtx.scale(-1, 1);
  sourceCtx.drawImage(video, 0, 0, width, height);
  sourceCtx.restore();

  const image = sourceCtx.getImageData(0, 0, width, height);
  const data = image.data;
  const rects = cameraRectangles(width, height);

  let sumR = 0;
  let sumG = 0;
  let sumB = 0;
  let count = 0;
  rects.forEach(function(rect) {
    for (let y = rect.y; y < rect.y + rect.h; y += 3) {
      for (let x = rect.x; x < rect.x + rect.w; x += 3) {
        const index = (y * width + x) * 4;
        const r = data[index];
        const g = data[index + 1];
        const b = data[index + 2];
        const luma = .2126 * r + .7152 * g + .0722 * b;

        // Clipped highlights and very dark pixels are dominated by exposure,
        // shadows, hair, or glare rather than useful tiny skin-color changes.
        if (luma < 35 || luma > 235) continue;
        if (Math.max(r, g, b) - Math.min(r, g, b) > 190) continue;

        sumR += r;
        sumG += g;
        sumB += b;
        count += 1;
      }
    }
  });

  let ppg = 0;
  if (count) {
    const r = sumR / count;
    const g = sumG / count;
    const b = sumB / count;
    const posValue = cameraPosPulse(r, g, b);

    if (posValue != null) {
      if (cameraPpgFast == null || cameraPpgSlow == null) {
        cameraPpgFast = posValue;
        cameraPpgSlow = posValue;
      }

      const fastAlpha = cameraFilterAlpha(3.0, dt);
      const slowAlpha = cameraFilterAlpha(0.7, dt);
      cameraPpgFast += fastAlpha * (posValue - cameraPpgFast);
      cameraPpgSlow += slowAlpha * (posValue - cameraPpgSlow);
      ppg = (cameraPpgFast - cameraPpgSlow) * 1000;
    }
  }

  let motionSum = 0;
  let motionCount = 0;
  if (cameraPreviousFrame && cameraPreviousFrame.length === data.length) {
    rects.forEach(function(rect) {
      for (let y = rect.y; y < rect.y + rect.h; y += 3) {
        for (let x = rect.x; x < rect.x + rect.w; x += 3) {
          const index = (y * width + x) * 4;
          const nowLum = .2126 * data[index] + .7152 * data[index + 1] + .0722 * data[index + 2];
          const oldLum = .2126 * cameraPreviousFrame[index] + .7152 * cameraPreviousFrame[index + 1] + .0722 * cameraPreviousFrame[index + 2];
          motionSum += Math.abs(nowLum - oldLum);
          motionCount += 1;
        }
      }
    });
  }
  const motion = motionCount ? 100 * motionSum / motionCount / 255 : 0;
  cameraPreviousFrame = new Uint8ClampedArray(data);

  const magnifyEnabled = Boolean(cameraMagnificationOn);
  const gain = magnifyEnabled
    ? Number(document.getElementById("cameraGain").value || 0)
    : 0;

  if (magnifyEnabled && gain > 0) {
    const output = new ImageData(new Uint8ClampedArray(data), width, height);
    const out = output.data;

    if (!cameraPixelFast || cameraPixelFast.length !== width * height) {
      cameraPixelFast = new Float32Array(width * height);
      cameraPixelSlow = new Float32Array(width * height);
      for (let pixel = 0; pixel < width * height; pixel++) {
        const currentG = data[pixel * 4 + 1];
        cameraPixelFast[pixel] = currentG;
        cameraPixelSlow[pixel] = currentG;
      }
    }

    const fastPixelAlpha = cameraFilterAlpha(3.0, dt);
    const slowPixelAlpha = cameraFilterAlpha(0.7, dt);
    rects.forEach(function(rect) {
      for (let y = rect.y; y < rect.y + rect.h; y++) {
        for (let x = rect.x; x < rect.x + rect.w; x++) {
          const pixel = y * width + x;
          const index = pixel * 4;
          const currentG = data[index + 1];

          let fast = cameraPixelFast[pixel];
          let slow = cameraPixelSlow[pixel];
          fast += fastPixelAlpha * (currentG - fast);
          slow += slowPixelAlpha * (currentG - slow);
          cameraPixelFast[pixel] = fast;
          cameraPixelSlow[pixel] = slow;

          const pulseBand = fast - slow;
          out[index] = Math.max(0, Math.min(255, data[index] + pulseBand * gain * .12));
          out[index + 1] = Math.max(0, Math.min(255, currentG + pulseBand * gain));
          out[index + 2] = Math.max(0, Math.min(255, data[index + 2] + pulseBand * gain * .12));
        }
      }
    });
    sourceCtx.putImageData(output, 0, 0);
  } else {
    cameraPixelFast = null;
    cameraPixelSlow = null;
  }
  drawCameraGuides(sourceCtx, width, height);

  const quality = cameraQualityScore(ppg, motion);
  document.getElementById("cameraPpgValue").textContent = ppg.toFixed(2);
  document.getElementById("cameraMotionValue").textContent = motion.toFixed(2) + "%";
  document.getElementById("cameraQualityValue").textContent = cameraQualityLabel(quality);

  cameraPendingSamples.push({
    t: performance.now() / 1000,
    ppg: ppg,
    motion: motion
  });
  if (cameraPendingSamples.length > 30) {
    cameraPendingSamples.splice(0, cameraPendingSamples.length - 30);
  }
  cameraPostPending(false);
}

async function cameraStartLocal() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    updateCameraStatus("Camera API unavailable", false);
    return;
  }

  updateCameraStatus("Requesting camera…", false);
  try {
    cameraStream = await navigator.mediaDevices.getUserMedia({
      video: {
        facingMode: "user",
        width: {ideal: 640},
        height: {ideal: 480},
        frameRate: {ideal: 30, max: 30}
      },
      audio: false
    });
    const video = document.getElementById("cameraVideo");
    video.srcObject = cameraStream;
    await video.play();
    cameraPreviousFrame = null;
    cameraPpgFast = null;
    cameraPpgSlow = null;
    cameraPixelFast = null;
    cameraPixelSlow = null;
    cameraPendingSamples = [];
    cameraLastSampleTimestamp = null;
    cameraQualityHistory = [];
    cameraRgbHistory = [];
    cameraLastFrameAt = 0;
    document.getElementById("cameraToggle").textContent = "Stop camera";
    document.getElementById("cameraToggle").className = "";
    document.getElementById("cameraLab").classList.add("active");
    updateCameraStatus("Camera live", true);
    cameraAnimationFrame = requestAnimationFrame(cameraAnalyzeFrame);
  } catch (err) {
    cameraStream = null;
    updateCameraStatus("Camera permission/device error", false);
    document.getElementById("error").textContent = "Camera: " + String(err);
  }
}

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

function renderDeviceViewControls(devices) {
  const host = document.getElementById("deviceViewButtons");
  if (!host) return;

  const signature = devices.map(function(device) {
    return [device.id, device.name, device.connected, device.running, deviceViewEnabled(device.id)].join(":");
  }).join("|");
  if (signature === deviceViewSignature) return;
  deviceViewSignature = signature;

  host.innerHTML = devices.map(function(device) {
    const enabled = deviceViewEnabled(device.id);
    const live = Boolean(device.connected && device.running);
    return '<button class="device-view-switch ' + (enabled ? 'enabled' : '') +
      '" data-device-view="' + escapeHtml(device.id) +
      '" role="switch" aria-checked="' + String(enabled) +
      '" title="' + escapeHtml(device.name + (live ? " · live" : "")) + '">' +
      '<span class="source-swatch" style="--source-color:' + escapeHtml(deviceColor(device.id)) + '"></span>' +
      '<span>' + escapeHtml(device.name) + '</span>' +
      '<span class="switch-track" aria-hidden="true"><span class="switch-knob"></span></span>' +
      '</button>';
  }).join("");

  host.querySelectorAll("[data-device-view]").forEach(function(button) {
    button.onclick = function() {
      const deviceId = button.dataset.deviceView;
      const enabled = !deviceViewEnabled(deviceId);
      deviceViewState[deviceId] = enabled;
      saveDeviceViewState();

      if (deviceId === "camera" && !enabled && cameraStream) {
        cameraStopLocal();
      }

      deviceViewSignature = "";
      signalSignature = "";
      renderDeviceViewControls(catalog.devices);
      syncCameraViewVisibility();

      const displayed = visibleSignals(catalog.signals);
      renderSignalPanels(displayed);
      updateSignalPanels(displayed);
      requestAnimationFrame(drawAllSignals);
    };
  });
}

function syncCameraViewVisibility() {
  const lab = document.getElementById("cameraLab");
  if (!lab) return;
  lab.classList.toggle("view-hidden", !deviceViewEnabled("camera"));
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

document.getElementById("themeToggle").onclick = function() {
  setTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
};
setTheme(document.documentElement.dataset.theme || "dark");

document.querySelectorAll("[data-signal-filter]").forEach(function(button) {
  button.onclick = function() {
    signalFilter = button.dataset.signalFilter;
    document.querySelectorAll("[data-signal-filter]").forEach(function(candidate) {
      candidate.classList.toggle("active", candidate === button);
    });
    renderSignalPanels(visibleSignals(catalog.signals));
    updateSignalPanels(visibleSignals(catalog.signals));
    requestAnimationFrame(drawAllSignals);
  };
});

document.getElementById("cameraToggle").onclick = function() {
  if (cameraStream) cameraStopLocal();
  else cameraStartLocal();
};
document.getElementById("cameraMagnifyToggle").onclick = function() {
  cameraMagnificationOn = !cameraMagnificationOn;
  const button = document.getElementById("cameraMagnifyToggle");
  button.classList.toggle("active", cameraMagnificationOn);
  button.setAttribute("aria-pressed", String(cameraMagnificationOn));
  document.getElementById("cameraViewLabel").textContent =
    cameraMagnificationOn ? "Magnified pulse-band view + sampling guides" : "Normal + sampling guides";
  if (!cameraMagnificationOn) {
    cameraPixelFast = null;
    cameraPixelSlow = null;
  }
};
document.getElementById("cameraGain").oninput = function() {
  document.getElementById("cameraGainValue").textContent =
    String(document.getElementById("cameraGain").value) + "×";
};
[
  ["cameraGuideScale", "cameraGuideScaleValue"],
  ["cameraGuideX", "cameraGuideXValue"],
  ["cameraGuideY", "cameraGuideYValue"]
].forEach(function(ids) {
  const input = document.getElementById(ids[0]);
  const output = document.getElementById(ids[1]);
  input.oninput = function() {
    output.textContent = String(input.value) + "%";
  };
});



function ensureSignalState(signal) {
  const generation = String(signal.source_generation || "");
  if (!signalState[signal.id]) {
    signalState[signal.id] = {
      seq: 0,
      values: [],
      times: [],
      audioOn: false,
      audioNode: null,
      generation: generation
    };
  } else if (signalState[signal.id].generation !== generation) {
    // A different physical sensor took this session slot. Reset the browser's
    // local graph cursor too, otherwise its old high sequence number would
    // suppress the new slot history after the server resets to sequence zero.
    signalState[signal.id].seq = 0;
    signalState[signal.id].values = [];
    signalState[signal.id].times = [];
    signalState[signal.id].generation = generation;
  }
  return signalState[signal.id];
}

function signalMeaning(signal) {
  const id = signal.id || "";
  const meanings = {
    "heart_rate": "How fast the detected pulse rhythm is beating.",
    "ibi": "Time from one detected beat to the next.",
    "hrv_rmssd": "Short-term beat-to-beat variability.",
    "hrv_sdnn": "Overall spread of recent beat intervals.",
    "pnn50": "How often adjacent beat intervals differ by more than 50 ms.",
    "coherence_ratio": "How strongly recent HRV is organized around one dominant rhythm.",
    "coherence_peak": "Share of HRV power concentrated in the dominant rhythm.",
    "respiration_estimate": "Breathing rhythm inferred indirectly from heart-rate variability.",
    "pulse_amplitude": "Recent strength or range of the pulse waveform.",
    "beat_confidence": "How trustworthy the automatic beat timing currently appears.",
    "signal_quality": "Whether the camera waveform looks periodic enough, with sufficiently little motion, to trust.",
    "skin_tonic": "Slow baseline level of skin conductance.",
    "skin_phasic": "Faster skin-conductance change above or below its recent baseline.",
    "skin_slope": "Whether skin conductance is generally rising or falling.",
    "skin_responses": "Rate of rapid relative skin-conductance responses.",
    "skin_variability": "How much skin conductance has varied recently.",
    "hr_difference": "How far apart the two sources' current heart-rate estimates are.",
    "beat_offset": "Typical timing offset between beats detected by the two sources.",
    "correlation": "How similarly the two recent pulse waveforms rise and fall.",
    "amplitude_ratio": "Relative recent pulse-wave amplitude between the two sources."
  };
  if (id.endsWith(".pulse_raw")) return "The sensor's direct optical pulse waveform before calculated metrics.";
  if (id.endsWith(".skin_raw")) return "The sensor's direct skin-conductance signal.";
  if (id === "camera.ppg_raw") return "The camera's directly extracted facial-color pulse waveform.";
  if (id === "camera.motion_raw") return "Facial movement that can contaminate camera pulse measurements.";
  for (const key in meanings) {
    if (id.endsWith("." + key) || id.indexOf("." + key + ".") >= 0) return meanings[key];
  }
  if (signalKind(signal) === "comparison") return "A cross-check between two live signal sources.";
  return signal.data_label || signal.description || "";
}

function renderSignalCard(signal) {
  ensureSignalState(signal);
  const id = domId(signal.id);
  const kind = signalKind(signal);
  const viewMode = panelViewMode(signal.id);
  const panelAction = viewMode === "full" ? "Collapse panel" : "Expand panel";
  const sourceIds = signalSourceIds(signal);
  const sourceNames = signal.data_sources || [signal.device_name];
  const sourceChips = kind === "comparison" ? sourceNames.map(function(name, index) {
    const sourceId = sourceIds[index] || signal.device_id;
    return '<span class="source-chip"><span class="source-swatch" style="--source-color:' +
      escapeHtml(deviceColor(sourceId)) + '"></span>' + escapeHtml(name) + '</span>';
  }).join("") : "";
  const accent = panelAccent(signal);

  return (
    '<article id="panel_' + id + '" data-signal-id="' + escapeHtml(signal.id) +
      '" class="signal-panel ' + kind + ' view-' + viewMode +
      '" style="--device-accent:' + escapeHtml(deviceColor(signal.device_id)) +
      ';--panel-accent:' + escapeHtml(accent) + '">' +
      '<button class="panel-view-dot" data-panel-view-toggle="' + escapeHtml(signal.id) +
        '" title="' + panelAction + '" aria-label="' + panelAction + '"></button>' +
      '<div class="signal-panel-header">' +
        '<div class="signal-title-block">' +
          '<div class="signal-title-row">' +
            '<span class="kind-badge ' + kind + '">' + escapeHtml(signalKindLabel(signal)) + '</span>' +
            '<div class="signal-title">' + escapeHtml(signal.name) + '</div>' +
          '</div>' +
          '<div class="signal-collapse-device">' + escapeHtml(signal.device_name || "") + '</div>' +
          (kind === "comparison" ? '<div class="source-chips">' + sourceChips + '</div>' : '') +
        '</div>' +
        '<div class="signal-header-actions">' +
          '<button id="audio_' + id + '" class="audio-icon" disabled aria-label="Turn audio on" title="Turn audio on">🔇</button>' +
        '</div>' +
      '</div>' +
      '<div class="signal-glance">' +
        '<div class="signal-primary-value">' +
          '<span id="current_' + id + '" class="signal-primary-number">—</span>' +
          '<span class="signal-primary-unit">' + escapeHtml(signal.unit) + '</span>' +
        '</div>' +
        '<div class="signal-meaning">' + escapeHtml(signalMeaning(signal)) + '</div>' +
      '</div>' +
      '<canvas id="canvas_' + id + '"></canvas>' +
      '<details class="signal-tech">' +
        '<summary>Details & technical information</summary>' +
        '<div class="metrics">' +
          '<div class="metric"><div class="label">Recent low</div><div id="min_' + id + '" class="metric-value">—</div></div>' +
          '<div class="metric"><div class="label">Recent high</div><div id="max_' + id + '" class="metric-value">—</div></div>' +
          '<div class="metric"><div class="label">Samples</div><div id="count_' + id + '" class="metric-value">0</div></div>' +
        '</div>' +
        '<div class="signal-info">' +
          '<div class="info-line"><strong>Definition:</strong> ' + escapeHtml(signal.description) + '</div>' +
          '<div class="info-line"><strong>OSC:</strong> <span class="mono">' + escapeHtml(signal.osc) + '</span></div>' +
          '<div class="info-line"><strong>Rate:</strong> ' +
            (signal.nominal_rate ? escapeHtml(signal.nominal_rate + " Hz nominal") : "device stream rate, not yet characterized") +
          '</div>' +
          '<div id="extra_' + id + '" class="info-line"></div>' +
        '</div>' +
        '<div class="audio-note"><strong>Audio:</strong> ' + escapeHtml(signal.audio) + '</div>' +
      '</details>' +
    '</article>'
  );
}

function renderSignalPanels(signals) {
  const grid = document.getElementById("signalGrid");
  if (!signals.length) {
    const anyConnected = catalog.devices.some(function(device) {
      return device.connected && device.running;
    });
    const anyEnabledConnected = catalog.devices.some(function(device) {
      return device.connected && device.running && deviceViewEnabled(device.id);
    });
    let hint = "Connect a sensor or start the camera.";
    if (anyConnected && !anyEnabledConnected) {
      hint = "A device is connected, but its view is turned off. Use Device views above to show it.";
    }
    grid.innerHTML =
      '<section class="signal-device-section" style="--device-accent:#68718a">' +
        '<div class="device-section-header">' +
          '<div><div class="device-section-name">No live device data</div>' +
          '<div class="device-section-summary">' + escapeHtml(hint) + '</div></div>' +
        '</div>' +
      '</section>';
    return;
  }

  const deviceGroups = new Map();
  const comparisons = [];

  signals.forEach(function(signal) {
    if (signalKind(signal) === "comparison") {
      comparisons.push(signal);
      return;
    }
    const key = signal.device_id;
    if (!deviceGroups.has(key)) deviceGroups.set(key, []);
    deviceGroups.get(key).push(signal);
  });

  const sections = [];
  deviceGroups.forEach(function(groupSignals, deviceId) {
    const device = catalog.devices.find(function(item) { return item.id === deviceId; });
    const direct = groupSignals.filter(function(signal) { return signalKind(signal) === "direct"; });
    const derived = groupSignals.filter(function(signal) { return signalKind(signal) === "calculated"; });
    const deviceName = device ? device.name : (groupSignals[0].device_name || deviceId);
    const summary = device && device.summary ? device.summary : "";
    const countText = direct.length + " direct" +
      (derived.length ? " · " + derived.length + " derived" : "");

    let html =
      '<section class="signal-device-section" style="--device-accent:' + escapeHtml(deviceColor(deviceId)) + '">' +
        '<div class="device-section-header">' +
          '<div>' +
            '<div class="device-section-name">' + escapeHtml(deviceName) + '</div>' +
            '<div class="device-section-summary">' + escapeHtml(summary) + '</div>' +
          '</div>' +
          '<div class="row">' +
            '<span class="status-pill"><span class="dot on"></span>Live</span>' +
            '<span class="device-section-counts">' + escapeHtml(countText) + '</span>' +
          '</div>' +
        '</div>';

    if (direct.length) {
      html +=
        '<div class="signal-subsection">' +
          '<div class="signal-subsection-heading">' +
            '<span class="signal-subsection-title">Direct sensor data</span>' +
            '<span class="signal-subsection-help">What the hardware itself is sending</span>' +
          '</div>' +
          '<div class="signal-section-grid">' + direct.map(renderSignalCard).join("") + '</div>' +
        '</div>';
    }

    if (derived.length) {
      html +=
        '<div class="signal-subsection">' +
          '<div class="signal-subsection-heading">' +
            '<span class="signal-subsection-title">Derived from this sensor</span>' +
            '<span class="signal-subsection-help">Calculations made from the direct signal above</span>' +
          '</div>' +
          '<div class="signal-section-grid">' + derived.map(renderSignalCard).join("") + '</div>' +
        '</div>';
    }

    html += '</section>';
    sections.push(html);
  });

  if (comparisons.length) {
    sections.push(
      '<section class="signal-device-section" style="--device-accent:#f3d596">' +
        '<div class="device-section-header">' +
          '<div><div class="device-section-name">Cross-device comparisons</div>' +
          '<div class="device-section-summary">Validation and agreement between two live sources</div></div>' +
          '<div class="device-section-counts">' + comparisons.length + ' comparison' +
            (comparisons.length === 1 ? '' : 's') + '</div>' +
        '</div>' +
        '<div class="signal-subsection"><div class="signal-section-grid">' +
          comparisons.map(renderSignalCard).join("") +
        '</div></div>' +
      '</section>'
    );
  }

  grid.innerHTML = sections.join("");

  signals.forEach(function(signal) {
    const button = document.getElementById("audio_" + domId(signal.id));
    if (button) button.onclick = function() { toggleAudio(signal.id); };
  });
  grid.querySelectorAll("[data-panel-view-toggle]").forEach(function(button) {
    button.onclick = function() {
      cyclePanelView(button.dataset.panelViewToggle);
    };
  });
}

function clearDropIndicators() {
  document.querySelectorAll(".signal-panel").forEach(function(panel) {
    panel.classList.remove("drop-before", "drop-after");
  });
}

function installPanelDragAndDrop() {
  const grid = document.getElementById("signalGrid");
  if (!grid) return;

  grid.querySelectorAll(".drag-handle").forEach(function(handle) {
    handle.addEventListener("dragstart", function(event) {
      const panel = handle.closest(".signal-panel");
      if (!panel) return;
      draggedSignalId = panel.dataset.signalId;
      panel.classList.add("dragging");
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", draggedSignalId || "");
    });

    handle.addEventListener("dragend", function() {
      const panel = handle.closest(".signal-panel");
      if (panel) panel.classList.remove("dragging");
      draggedSignalId = null;
      clearDropIndicators();
    });
  });

  grid.querySelectorAll(".signal-panel").forEach(function(panel) {
    panel.addEventListener("dragover", function(event) {
      if (!draggedSignalId || panel.dataset.signalId === draggedSignalId) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
      clearDropIndicators();

      const rect = panel.getBoundingClientRect();
      const centerX = rect.left + rect.width / 2;
      const centerY = rect.top + rect.height / 2;
      const nearSameRow = Math.abs(event.clientY - centerY) < rect.height * 0.28;
      const before = nearSameRow
        ? event.clientX < centerX
        : event.clientY < centerY;
      panel.classList.add(before ? "drop-before" : "drop-after");
    });

    panel.addEventListener("dragleave", function(event) {
      if (event.relatedTarget && panel.contains(event.relatedTarget)) return;
      panel.classList.remove("drop-before", "drop-after");
    });

    panel.addEventListener("drop", function(event) {
      if (!draggedSignalId || panel.dataset.signalId === draggedSignalId) return;
      event.preventDefault();

      const targetId = panel.dataset.signalId;
      const panels = Array.from(grid.querySelectorAll(".signal-panel"));
      let ids = panels.map(function(item) { return item.dataset.signalId; });
      ids = ids.filter(function(id) { return id !== draggedSignalId; });

      const targetIndex = ids.indexOf(targetId);
      if (targetIndex < 0) return;

      const rect = panel.getBoundingClientRect();
      const centerX = rect.left + rect.width / 2;
      const centerY = rect.top + rect.height / 2;
      const nearSameRow = Math.abs(event.clientY - centerY) < rect.height * 0.28;
      const before = nearSameRow
        ? event.clientX < centerX
        : event.clientY < centerY;

      ids.splice(targetIndex + (before ? 0 : 1), 0, draggedSignalId);
      storeVisiblePanelOrder(ids);
      draggedSignalId = null;
      signalSignature = "";

      const displayed = visibleSignals(catalog.signals);
      renderSignalPanels(displayed);
      updateSignalPanels(displayed);
      requestAnimationFrame(drawAllSignals);
    });
  });
}

function updateSignalPanels(signals) {
  signals.forEach(function(signal) {
    const id = domId(signal.id);
    const panel = document.getElementById("panel_" + id);
    if (!panel) return;

    const live = Boolean(signal.connected && signal.running);
    panel.classList.toggle("offline", !live);
    const dot = document.getElementById("dot_" + id);
    if (dot) dot.className = live ? "dot on" : "dot";
    const status = document.getElementById("status_" + id);
    if (status) {
      status.textContent =
        !signal.running ? "Acquisition stopped" :
        signal.connected ? "Live" : "Not connected";
    }

    const audioButton = document.getElementById("audio_" + id);
    if (!audioButton) return;
    audioButton.disabled = !live;
    const state = ensureSignalState(signal);
    if (!live && state.audioOn) stopAudio(signal.id);
    audioButton.textContent = state.audioOn ? "🔊" : "🔇";
    audioButton.className = state.audioOn ? "audio-icon audio-on" : "audio-icon";
    audioButton.setAttribute("aria-label", state.audioOn ? "Mute audio" : "Turn audio on");
    audioButton.title = state.audioOn ? "Mute audio" : "Turn audio on";

    document.getElementById("count_" + id).textContent =
      Number(signal.sample_count || 0).toLocaleString();

    const extra = document.getElementById("extra_" + id);
    if (!signal.derived && signal.packet_gaps != null) {
      const details = [
        '<strong>Packet gaps:</strong> ' + escapeHtml(signal.packet_gaps)
      ];
      if (signal.estimated_sample_rate_hz != null) {
        details.push(
          '<strong>Waveform rate:</strong> ' +
          escapeHtml(Number(signal.estimated_sample_rate_hz).toFixed(1)) +
          ' samples/s'
        );
      }
      if (Number(signal.clock_resets || 0) > 0) {
        details.push(
          '<strong>Timing re-anchors:</strong> ' +
          escapeHtml(signal.clock_resets)
        );
      }
      extra.innerHTML = details.join(' · ');
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
    const deviceSignalObjects = (device.signals || []).map(function(signalId) {
      return catalog.signals.find(function(s) { return s.id === signalId; });
    }).filter(Boolean);
    const rawSignals = deviceSignalObjects.filter(function(signal) { return !signal.derived; });
    const derivedCount = deviceSignalObjects.filter(function(signal) { return signal.derived; }).length;
    const signals = rawSignals.map(function(signal) {
      return '<span class="badge">' + escapeHtml(signal.name) + '</span>';
    }).join("") +
      (derivedCount ? '<span class="badge">+' + derivedCount + ' derived panels</span>' : "");

    let deviceSpecific = "";
    if (device.id === "camera") {
      deviceSpecific =
        '<div style="margin-top:12px;padding-top:12px;border-top:1px solid var(--line)">' +
          '<div class="label">Camera control</div>' +
          '<div class="small" style="margin-top:6px">Start or stop the camera from Live data. Video processing stays local.</div>' +
        '</div>';
    }
    if (device.id === "emwave" || /^emwave[2-4]$/.test(device.id)) {
      deviceSpecific =
        '<div style="margin-top:12px;padding-top:12px;border-top:1px solid var(--line)">' +
          '<div class="label">Multi-emWave experiments</div>' +
          '<div class="small" style="margin-top:6px">' +
            'When two or more emWave modules are connected, Biofeedback Play opens them simultaneously and adds pair-comparison panels for heart-rate agreement, beat timing offset, waveform correlation, and relative pulse amplitude. ' +
            'Useful placements include one sensor on each earlobe, or an ear sensor plus a compatible finger sensor. Timing offsets include USB and sensor delays, so they are experimental and are not presented as medical pulse-transit time.' +
          '</div>' +
        '</div>';
    }
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
      const museStage = device.stage ? escapeHtml(device.stage) : "—";
      const museStatusText = device.status_text ? escapeHtml(device.status_text) : "—";
      const museTransport = device.connection_transport ? escapeHtml(device.connection_transport) : "—";
      const museServices = device.rfcomm_services ? escapeHtml(device.rfcomm_services) : "—";
      const museChannel = device.rfcomm_channel != null ? String(device.rfcomm_channel) : "—";
      const musePassiveProbe = device.passive_probe ? escapeHtml(device.passive_probe) : "—";
      const museAttemptState = device.attempt_state || "idle";
      const museElapsed = Math.max(0, Math.round(Number(device.stage_elapsed_s || 0)));
      const museRetry = Math.max(0, Number(device.retry_seconds || 0));
      let museAttemptClass = "working";
      let museAttemptTitle = "Working…";
      let museAttemptDetail =
        "Biofeedback Play is still testing the Muse. You do not need to send a screenshot yet.";

      if (device.connected) {
        museAttemptClass = "live";
        museAttemptTitle = "Connected";
        museAttemptDetail = "Muse data is arriving. No troubleshooting screenshot is needed.";
      } else if (museAttemptState === "failed") {
        museAttemptClass = "failed";
        museAttemptTitle = "Attempt finished — send a screenshot now";
        museAttemptDetail =
          "This diagnostic pass is complete and has stopped. Biofeedback Play will not retry until you click Start acquisition.";
      } else if (museAttemptState === "stopped") {
        museAttemptClass = "";
        museAttemptTitle = "Acquisition stopped";
        museAttemptDetail = "Click Start acquisition when you want Biofeedback Play to try again.";
      } else if (museAttemptState === "idle") {
        museAttemptClass = "";
        museAttemptTitle = "Waiting";
        museAttemptDetail = "Biofeedback Play is waiting for the selected Muse serial port.";
      } else {
        museAttemptDetail =
          "Still working on this connection attempt · current step has been running for " +
          museElapsed + " second" + (museElapsed === 1 ? "" : "s") + ".";
      }

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
          '<div class="muse-attempt ' + museAttemptClass + '">' +
            '<div class="muse-attempt-title">' + escapeHtml(museAttemptTitle) + '</div>' +
            '<div class="muse-attempt-detail">' + escapeHtml(museAttemptDetail) + '</div>' +
          '</div>' +
          '<div id="musePortScanStatus" class="small" style="margin-top:10px;padding:9px 10px;border:1px solid var(--line);border-radius:9px;background:#0d1017">' +
            escapeHtml(musePortScanStatus) +
          '</div>' +
          '<div class="device-meta small" style="margin-top:8px">' +
            '<div><strong>Serial ports found:</strong> ' + musePorts.length + '</div>' +
            portSummary +
            '<div><strong>Selected port:</strong> <span class="mono">' + escapeHtml(currentPort || "none") + '</span></div>' +
            '<div><strong>Connection stage:</strong> ' + museStage + '</div>' +
            '<div><strong>Active transport:</strong> ' + museTransport + '</div>' +
            '<div><strong>Advertised RFCOMM services:</strong> <span class="mono">' + museServices + '</span></div>' +
            '<div><strong>Current RFCOMM channel:</strong> ' + museChannel + '</div>' +
            '<div><strong>Passive serial probe:</strong> <span class="mono">' + musePassiveProbe + '</span></div>' +
            '<div><strong>Battery:</strong> ' + battery + '</div>' +
            '<div><strong>AFE gain:</strong> ' + afe + '</div>' +
            '<div><strong>Version:</strong> <span class="mono">' + version + '</span></div>' +
            '<div><strong>Headband status:</strong> <span class="mono">' + museStatusText + '</span></div>' +
            '<div><strong>EEG samples:</strong> ' + Number(device.eeg_sample_count || 0).toLocaleString() +
              ' · <strong>Accelerometer samples:</strong> ' + Number(device.accel_sample_count || 0).toLocaleString() + '</div>' +
          '</div>' +
        '</div>';
    }

    return (
      '<div class="device-card" style="--device-accent:' + escapeHtml(deviceColor(device.id)) + '">' +
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
          (device.estimated_sample_rate_hz != null
            ? '<div><strong>Estimated waveform rate:</strong> ' +
              escapeHtml(Number(device.estimated_sample_rate_hz).toFixed(1)) +
              ' samples/s</div>'
            : '') +
          (Number(device.clock_resets || 0) > 0
            ? '<div><strong>Timing re-anchors:</strong> ' +
              escapeHtml(device.clock_resets) + '</div>'
            : '') +
          (device.error ? '<div style="color:#ff9b9b"><strong>Error:</strong> ' + escapeHtml(device.error) + '</div>' : '') +
        '</div>' +
        '<div class="device-signals">' + signals + '</div>' +
        deviceSpecific +
        (device.browser_controlled ? '' :
          '<div style="margin-top:12px">' +
            '<button data-device-toggle="' + escapeHtml(device.id) + '">' +
              (device.running ? "Stop acquisition" : "Start acquisition") +
            '</button>' +
          '</div>') +
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
  const shownSignals = visibleSignals(catalog.signals);
  const dot = document.getElementById("globalDot");
  dot.className = connected.length ? "dot on" : "dot";
  document.getElementById("globalStatus").textContent =
    connected.length + " device" + (connected.length === 1 ? "" : "s") +
    " connected · " + shownSignals.length + " shown";

  document.getElementById("sessionSummary").textContent = connected.length
    ? connected.map(function(d) { return d.name; }).join(" · ") +
      " · " + shownSignals.length + " shown of " + liveSignals.length + " live signal" +
      (liveSignals.length === 1 ? "" : "s")
    : "No configured device is currently connected.";
}

function stopUnavailableAudio() {
  Object.keys(signalState).forEach(function(signalId) {
    const state = signalState[signalId];
    if (!state || !state.audioOn) return;

    const signal = catalog.signals.find(function(item) {
      return item.id === signalId;
    });
    const stillAvailable = Boolean(
      signal &&
      signal.connected &&
      signal.running &&
      signalViewEnabled(signal)
    );

    if (!stillAvailable) {
      stopAudio(signalId, false);
    }
  });
}

function applyCatalog(data) {
  catalog = data;
  stopUnavailableAudio();
  renderDeviceViewControls(catalog.devices);
  syncCameraViewVisibility();
  const displayed = visibleSignals(catalog.signals);
  const signature = displayed.map(function(s) {
    return s.id + ":" + Boolean(s.connected && s.running);
  }).join("|") + "|filter:" + signalFilter;
  if (signature !== signalSignature) {
    signalSignature = signature;
    renderSignalPanels(displayed);
  }
  updateSignalPanels(displayed);
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

  ctx.strokeStyle = deviceColor(signal.device_id);
  ctx.lineWidth = 1.55 * size.ratio;
  ctx.lineJoin = "round";
  if (signalKind(signal) === "calculated") {
    ctx.setLineDash([5 * size.ratio, 3 * size.ratio]);
  } else if (signalKind(signal) === "comparison") {
    ctx.setLineDash([9 * size.ratio, 4 * size.ratio]);
  } else {
    ctx.setLineDash([]);
  }
  ctx.beginPath();
  state.values.forEach(function(value, index) {
    const x = index * size.w / Math.max(1, state.values.length - 1);
    const y = size.h - ((value - min) / (max - min)) * size.h;
    if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();
  ctx.setLineDash([]);
}

function drawAllSignals() {
  visibleSignals(catalog.signals).forEach(drawSignal);
}

function formatSignalValue(signal, value) {
  if (value == null || !Number.isFinite(Number(value))) return "—";
  const numeric = Number(value);
  let precision = signal.precision;
  if (precision == null) {
    precision = signal.derived ? 2 : (Number.isInteger(numeric) ? 0 : 2);
  }
  return numeric.toFixed(Math.max(0, Math.min(6, Number(precision))));
}

function updateSignalNumbers(signal) {
  const state = ensureSignalState(signal);
  if (!state.values.length) return;
  const id = domId(signal.id);
  const recent = state.values.slice(-400);
  const current = recent[recent.length - 1];
  const min = Math.min.apply(null, recent);
  const max = Math.max.apply(null, recent);
  document.getElementById("current_" + id).textContent = formatSignalValue(signal, current);
  document.getElementById("min_" + id).textContent = formatSignalValue(signal, min);
  document.getElementById("max_" + id).textContent = formatSignalValue(signal, max);
  updateAudio(signal);
}

function pollSignals() {
  const signals = visibleSignals(catalog.signals).filter(function(signal) {
    return signal.connected && signal.running;
  });
  if (!signals.length) return;

  const after = {};
  signals.forEach(function(signal) {
    after[signal.id] = ensureSignalState(signal).seq;
  });

  fetch("/api/signal_samples_batch", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({after: after})
  })
    .then(r => r.json())
    .then(function(data) {
      const samplesBySignal = data.samples || {};
      signals.forEach(function(signal) {
        const state = ensureSignalState(signal);
        const samples = samplesBySignal[signal.id] || [];
        samples.forEach(function(sample) {
          state.seq = Math.max(state.seq, sample.seq);
          state.values.push(sample.value);
          state.times.push(sample.t);
        });
        const maxPoints = signal.nominal_rate ? 1500 : 700;
        if (state.values.length > maxPoints) {
          state.values.splice(0, state.values.length - maxPoints);
          state.times.splice(0, state.times.length - maxPoints);
        }
        if (samples.length) {
          updateSignalNumbers(signal);
          drawSignal(signal);
        }
      });
    })
    .catch(function(err) {
      document.getElementById("error").textContent = String(err);
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

function stopAudio(signalId, refreshPanels) {
  if (refreshPanels === undefined) refreshPanels = true;
  const signal = catalog.signals.find(function(s) { return s.id === signalId; });
  const state = signalState[signalId];
  if (!state) return;
  state.audioOn = false;
  if (state.audioNode) {
    try { state.audioNode.gain.gain.setTargetAtTime(0, audioContext.currentTime, .02); } catch (_) {}
    try { state.audioNode.oscillator.stop(audioContext.currentTime + .08); } catch (_) {}
    state.audioNode = null;
  }
  if (refreshPanels && signal) updateSignalPanels(catalog.signals);
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
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")

            if self.path == "/api/signal_samples_batch":
                after = payload.get("after") or {}
                if not isinstance(after, dict):
                    self.send_json(
                        {"ok": False, "error": "after must be an object"},
                        400,
                    )
                    return
                samples = {}
                for signal_id, seq in list(after.items())[:128]:
                    try:
                        cursor = int(seq)
                    except (TypeError, ValueError):
                        cursor = 0
                    samples[str(signal_id)] = STATE.signal_samples_after(
                        str(signal_id), cursor
                    )
                self.send_json({"ok": True, "samples": samples})
                return

            if self.path != "/api/control":
                self.send_error(404)
                return

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
            elif action == "camera_samples":
                STATE.store_camera_samples(payload.get("samples") or [])
            elif action == "camera_stop":
                STATE.set_camera_inactive()
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
