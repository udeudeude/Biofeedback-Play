from __future__ import annotations

import math
import statistics
from typing import Iterable


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = max(0.0, min(1.0, fraction)) * (len(ordered) - 1)
    lo = int(math.floor(position))
    hi = int(math.ceil(position))
    if lo == hi:
        return ordered[lo]
    weight = position - lo
    return ordered[lo] * (1.0 - weight) + ordered[hi] * weight


def _window(points: Iterable[tuple[float, float]], seconds: float) -> list[tuple[float, float]]:
    data = [(float(t), float(v)) for t, v in points]
    if not data:
        return []
    cutoff = data[-1][0] - seconds
    return [(t, v) for t, v in data if t >= cutoff]


def _moving_average(values: list[float], width: int) -> list[float]:
    width = max(1, int(width))
    if width == 1 or len(values) < 3:
        return values[:]
    out: list[float] = []
    total = 0.0
    queue: list[float] = []
    for value in values:
        queue.append(value)
        total += value
        if len(queue) > width:
            total -= queue.pop(0)
        out.append(total / len(queue))
    return out


def _candidate_peaks(
    points: list[tuple[float, float]],
    polarity: float,
    min_interval_s: float = 0.30,
) -> list[tuple[float, float]]:
    if len(points) < 5:
        return []

    times = [t for t, _ in points]
    values = [polarity * v for _, v in points]
    duration = max(0.001, times[-1] - times[0])
    nominal_rate = max(1.0, (len(times) - 1) / duration)
    smooth_width = max(1, min(15, int(round(nominal_rate * 0.035))))
    smooth = _moving_average(values, smooth_width)

    p20 = _percentile(smooth, 0.20)
    p50 = _percentile(smooth, 0.50)
    p90 = _percentile(smooth, 0.90)
    span = max(1e-9, p90 - p20)
    threshold = p50 + 0.18 * span
    prominence = 0.12 * span

    peaks: list[tuple[float, float]] = []
    search = max(2, int(round(nominal_rate * 0.12)))
    last_t = -1e12

    for i in range(search, len(smooth) - search):
        value = smooth[i]
        if value < threshold:
            continue
        if value < max(smooth[i - search : i]) or value <= max(smooth[i + 1 : i + search + 1]):
            continue
        local_floor = min(
            min(smooth[i - search : i]),
            min(smooth[i + 1 : i + search + 1]),
        )
        if value - local_floor < prominence:
            continue

        t = times[i]
        if t - last_t < min_interval_s:
            if peaks and value > peaks[-1][1]:
                peaks[-1] = (t, value)
                last_t = t
            continue

        peaks.append((t, value))
        last_t = t

    return peaks


def _peak_score(peaks: list[tuple[float, float]]) -> float:
    if len(peaks) < 3:
        return -1e9
    intervals = [b[0] - a[0] for a, b in zip(peaks, peaks[1:])]
    plausible = [x for x in intervals if 0.30 <= x <= 2.0]
    if not plausible:
        return -1e9
    plausibility = len(plausible) / len(intervals)
    median = statistics.median(plausible)
    deviations = [abs(x - median) for x in plausible]
    mad = statistics.median(deviations) if deviations else 0.0
    regularity = max(0.0, 1.0 - mad / max(0.08, median))
    return len(plausible) * plausibility * (0.5 + 0.5 * regularity)


def detect_pulse_peaks(points: Iterable[tuple[float, float]]) -> list[float]:
    data = _window(points, 90.0)
    positive = _candidate_peaks(data, 1.0)
    negative = _candidate_peaks(data, -1.0)
    chosen = positive if _peak_score(positive) >= _peak_score(negative) else negative
    return [t for t, _ in chosen]


def _interpolate(times: list[float], values: list[float], sample_rate: float = 4.0) -> tuple[list[float], list[float]]:
    if len(times) < 2:
        return [], []
    start = times[0]
    end = times[-1]
    if end <= start:
        return [], []
    step = 1.0 / sample_rate
    out_t: list[float] = []
    out_v: list[float] = []
    source_index = 0
    t = start
    while t <= end:
        while source_index + 1 < len(times) and times[source_index + 1] < t:
            source_index += 1
        if source_index + 1 >= len(times):
            break
        t0, t1 = times[source_index], times[source_index + 1]
        v0, v1 = values[source_index], values[source_index + 1]
        ratio = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
        out_t.append(t)
        out_v.append(v0 + ratio * (v1 - v0))
        t += step
    return out_t, out_v


def _spectrum(values: list[float], sample_rate: float, frequencies: list[float]) -> dict[float, float]:
    if len(values) < 8:
        return {}
    mean = statistics.fmean(values)
    centered = [v - mean for v in values]
    n = len(centered)
    windowed = [
        value * (0.5 - 0.5 * math.cos(2.0 * math.pi * i / max(1, n - 1)))
        for i, value in enumerate(centered)
    ]
    power: dict[float, float] = {}
    for frequency in frequencies:
        real = 0.0
        imag = 0.0
        omega = 2.0 * math.pi * frequency / sample_rate
        for i, value in enumerate(windowed):
            angle = omega * i
            real += value * math.cos(angle)
            imag -= value * math.sin(angle)
        power[frequency] = (real * real + imag * imag) / max(1, n)
    return power


def _hrv_spectrum(peak_times: list[float]) -> tuple[dict[float, float], float | None]:
    if len(peak_times) < 8:
        return {}, None
    intervals = [(b - a) * 1000.0 for a, b in zip(peak_times, peak_times[1:])]
    interval_times = peak_times[1:]
    cutoff = interval_times[-1] - 64.0
    trimmed = [(t, v) for t, v in zip(interval_times, intervals) if t >= cutoff]
    if len(trimmed) < 7 or trimmed[-1][0] - trimmed[0][0] < 20.0:
        return {}, None
    times = [x[0] for x in trimmed]
    values = [x[1] for x in trimmed]
    _, interpolated = _interpolate(times, values, 4.0)
    if len(interpolated) < 40:
        return {}, None
    frequencies = [round(0.01 + i * 0.005, 3) for i in range(99)]
    return _spectrum(interpolated, 4.0, frequencies), trimmed[-1][0] - trimmed[0][0]


def pulse_metrics(points: Iterable[tuple[float, float]]) -> dict[str, float | None]:
    data = _window(points, 90.0)
    result: dict[str, float | None] = {
        "heart_rate_bpm": None,
        "ibi_ms": None,
        "rmssd_ms": None,
        "sdnn_ms": None,
        "pnn50_percent": None,
        "coherence_ratio": None,
        "coherence_peak_percent": None,
        "respiration_bpm": None,
        "pulse_amplitude": None,
        "beat_confidence_percent": None,
    }
    if len(data) < 8:
        return result

    recent5 = _window(data, 5.0)
    recent_values = [v for _, v in recent5]
    if recent_values:
        result["pulse_amplitude"] = _percentile(recent_values, 0.95) - _percentile(recent_values, 0.05)

    peaks = detect_pulse_peaks(data)
    if len(peaks) < 2:
        return result

    intervals_s = [b - a for a, b in zip(peaks, peaks[1:])]
    plausible = [x for x in intervals_s if 0.30 <= x <= 2.0]
    if not plausible:
        return result

    recent_intervals = plausible[-7:]
    median_interval = statistics.median(recent_intervals)
    result["heart_rate_bpm"] = 60.0 / median_interval
    result["ibi_ms"] = plausible[-1] * 1000.0

    intervals_ms = [x * 1000.0 for x in plausible]
    if len(intervals_ms) >= 3:
        successive = [b - a for a, b in zip(intervals_ms, intervals_ms[1:])]
        result["rmssd_ms"] = math.sqrt(statistics.fmean([x * x for x in successive]))
        result["sdnn_ms"] = statistics.stdev(intervals_ms) if len(intervals_ms) >= 2 else 0.0
        result["pnn50_percent"] = 100.0 * sum(abs(x) > 50.0 for x in successive) / len(successive)

    plausibility = len(plausible) / max(1, len(intervals_s))
    recent = plausible[-10:]
    med = statistics.median(recent)
    mad = statistics.median([abs(x - med) for x in recent]) if recent else med
    regularity = max(0.0, min(1.0, 1.0 - mad / max(0.12, med)))
    amplitude = result["pulse_amplitude"] or 0.0
    amplitude_factor = 1.0 if amplitude > 4.0 else max(0.0, amplitude / 4.0)
    result["beat_confidence_percent"] = 100.0 * plausibility * (0.55 + 0.45 * regularity) * amplitude_factor

    spectrum, duration = _hrv_spectrum(peaks)
    if spectrum and duration is not None:
        coherence_band = {f: p for f, p in spectrum.items() if 0.04 <= f <= 0.26}
        if coherence_band:
            peak_frequency = max(coherence_band, key=coherence_band.get)
            peak_power = sum(
                p for f, p in spectrum.items()
                if abs(f - peak_frequency) <= 0.015
            )
            total_power = sum(p for f, p in spectrum.items() if 0.01 <= f <= 0.40)
            remainder = max(1e-12, total_power - peak_power)
            if total_power > 0 and duration >= 60.0:
                result["coherence_ratio"] = peak_power / remainder
                result["coherence_peak_percent"] = 100.0 * peak_power / total_power

        breathing_band = {f: p for f, p in spectrum.items() if 0.08 <= f <= 0.40}
        if breathing_band and duration >= 30.0:
            breathing_frequency = max(breathing_band, key=breathing_band.get)
            band_total = sum(breathing_band.values())
            if band_total > 0 and breathing_band[breathing_frequency] / band_total >= 0.08:
                result["respiration_bpm"] = breathing_frequency * 60.0

    return result


def camera_pulse_metrics(
    points: Iterable[tuple[float, float]],
    motion_points: Iterable[tuple[float, float]] | None = None,
) -> dict[str, float | None]:
    """Conservative camera pulse summary for tuning remote PPG.

    Camera pulse timing is currently treated as a frequency-estimation problem,
    not a beat-to-beat interval source.  That avoids manufacturing HRV from
    unstable camera peaks before the camera pipeline has been validated against
    a contact sensor.
    """
    data = _window(points, 12.0)
    result: dict[str, float | None] = {
        "heart_rate_bpm": None,
        "pulse_amplitude": None,
        "signal_quality_percent": None,
    }
    if len(data) < 8:
        return result

    recent_values = [v for _, v in data]
    result["pulse_amplitude"] = (
        _percentile(recent_values, 0.95) - _percentile(recent_values, 0.05)
    )

    duration = data[-1][0] - data[0][0]
    if duration < 6.0:
        return result

    times = [t for t, _ in data]
    values = [v for _, v in data]
    _, signal = _interpolate(times, values, 30.0)
    if len(signal) < 150:
        return result
    signal = signal[-360:]

    frequencies = [round(0.70 + i * 0.02, 3) for i in range(116)]
    power = _spectrum(signal, 30.0, frequencies)
    if not power:
        return result

    peak_frequency = max(power, key=power.get)
    peak_bin_power = power[peak_frequency]

    # Camera waveforms can emphasize the second harmonic. Prefer a plausible
    # half-frequency when it carries substantial power of its own.
    if peak_frequency >= 1.50:
        half = peak_frequency / 2.0
        half_frequency = min(frequencies, key=lambda f: abs(f - half))
        if power.get(half_frequency, 0.0) >= 0.45 * peak_bin_power:
            peak_frequency = half_frequency

    total_power = sum(power.values())
    peak_power = sum(
        p for frequency, p in power.items()
        if abs(frequency - peak_frequency) <= 0.12
    )
    if total_power <= 1e-12:
        return result

    concentration = max(0.0, min(1.0, peak_power / total_power))
    spectral_score = max(0.0, min(1.0, (concentration - 0.10) / 0.55))

    motion_score = 1.0
    if motion_points is not None:
        motion = _window(motion_points, 8.0)
        if motion:
            recent_motion = [max(0.0, value) for _, value in motion]
            motion_level = statistics.median(recent_motion)
            motion_score = 1.0 / (1.0 + (motion_level / 0.65) ** 2)

    duration_score = max(0.0, min(1.0, (duration - 5.0) / 4.0))
    quality = 100.0 * spectral_score * motion_score * duration_score
    result["signal_quality_percent"] = quality

    # Refuse to emit a physiological-looking number until the camera waveform
    # has a reasonably concentrated rhythm and sufficiently little motion.
    if quality >= 35.0:
        result["heart_rate_bpm"] = peak_frequency * 60.0

    return result


def skin_metrics(points: Iterable[tuple[float, float]]) -> dict[str, float | None]:
    data = _window(points, 90.0)
    result: dict[str, float | None] = {
        "tonic_level": None,
        "phasic_level": None,
        "slope_per_min": None,
        "response_rate_per_min": None,
        "variability": None,
    }
    if len(data) < 4:
        return result

    now = data[-1][0]
    last10 = [(t, v) for t, v in data if t >= now - 10.0]
    last30 = [(t, v) for t, v in data if t >= now - 30.0]
    if not last10:
        return result

    tonic = statistics.fmean([v for _, v in last10])
    current = data[-1][1]
    result["tonic_level"] = tonic
    result["phasic_level"] = current - tonic

    if len(last30) >= 3:
        values30 = [v for _, v in last30]
        result["variability"] = statistics.pstdev(values30)

    if len(last10) >= 3:
        times = [t - last10[0][0] for t, _ in last10]
        values = [v for _, v in last10]
        t_mean = statistics.fmean(times)
        v_mean = statistics.fmean(values)
        denominator = sum((t - t_mean) ** 2 for t in times)
        if denominator > 0:
            slope_per_s = sum((t - t_mean) * (v - v_mean) for t, v in zip(times, values)) / denominator
            result["slope_per_min"] = slope_per_s * 60.0

    last60 = [(t, v) for t, v in data if t >= now - 60.0]
    if len(last60) >= 8:
        values = [v for _, v in last60]
        baseline_width = max(3, len(values) // 20)
        baseline = _moving_average(values, baseline_width)
        phasic = [v - b for v, b in zip(values, baseline)]
        span = max(values) - min(values)
        threshold = max(1e-9, 0.06 * span)
        count = 0
        last_peak_t = -1e12
        for i in range(1, len(phasic) - 1):
            if phasic[i] > threshold and phasic[i] >= phasic[i - 1] and phasic[i] > phasic[i + 1]:
                t = last60[i][0]
                if t - last_peak_t >= 1.0:
                    count += 1
                    last_peak_t = t
        duration_min = max(1e-6, (last60[-1][0] - last60[0][0]) / 60.0)
        result["response_rate_per_min"] = count / duration_min

    return result


def _downsample(values: list[float], factor: int) -> list[float]:
    factor = max(1, factor)
    if factor == 1:
        return values[:]
    return [
        statistics.fmean(values[i : i + factor])
        for i in range(0, len(values) - factor + 1, factor)
    ]


def _band_power(values: list[float], sample_rate: float, low: float, high: float) -> float | None:
    if len(values) < 32:
        return None
    factor = max(1, int(sample_rate // 125.0))
    values = _downsample(values, factor)
    sample_rate = sample_rate / factor
    duration = len(values) / sample_rate
    if duration < 1.0:
        return None
    resolution = 1.0 / duration
    start_bin = max(1, int(math.ceil(low / resolution)))
    end_bin = max(start_bin, int(math.floor(high / resolution)))
    frequencies = [k * resolution for k in range(start_bin, end_bin + 1)]
    powers = _spectrum(values, sample_rate, frequencies)
    if not powers:
        return None
    return sum(powers.values()) / len(powers)


def eeg_metrics(
    samples: Iterable[dict],
    sample_rate: float,
    channel_names: tuple[str, ...] = ("tp9", "fp1", "fp2", "tp10"),
) -> dict[str, float | None]:
    data = list(samples)
    result: dict[str, float | None] = {
        "delta_power": None,
        "theta_power": None,
        "alpha_power": None,
        "beta_power": None,
        "gamma_power": None,
        "alpha_asymmetry": None,
        "broadband_rms": None,
    }
    if len(data) < max(32, int(sample_rate)):
        return result
    data = data[-int(sample_rate * 2.5) :]

    channels: dict[str, list[float]] = {
        name: [float(sample[name]) for sample in data if name in sample]
        for name in channel_names
    }
    if not all(channels.values()):
        return result

    bands = {
        "delta_power": (1.0, 4.0),
        "theta_power": (4.0, 8.0),
        "alpha_power": (8.0, 13.0),
        "beta_power": (13.0, 30.0),
        "gamma_power": (30.0, 45.0),
    }

    per_channel_band: dict[str, dict[str, float]] = {}
    for channel, values in channels.items():
        mean = statistics.fmean(values)
        centered = [v - mean for v in values]
        per_channel_band[channel] = {}
        for name, (low, high) in bands.items():
            power = _band_power(centered, sample_rate, low, high)
            if power is not None:
                per_channel_band[channel][name] = power

    for name in bands:
        values = [powers[name] for powers in per_channel_band.values() if name in powers]
        if values:
            result[name] = statistics.fmean(values)

    all_values = [v for values in channels.values() for v in values]
    mean = statistics.fmean(all_values)
    result["broadband_rms"] = math.sqrt(statistics.fmean([(v - mean) ** 2 for v in all_values]))

    left = per_channel_band.get("fp1", {}).get("alpha_power")
    right = per_channel_band.get("fp2", {}).get("alpha_power")
    if left is not None and right is not None:
        result["alpha_asymmetry"] = math.log(right + 1e-12) - math.log(left + 1e-12)

    return result


def motion_metrics(samples: Iterable[dict]) -> dict[str, float | None]:
    data = list(samples)
    result = {"motion_intensity": None}
    if len(data) < 3:
        return result
    data = data[-100:]
    xs = [float(s["x"]) for s in data]
    ys = [float(s["y"]) for s in data]
    zs = [float(s["z"]) for s in data]
    result["motion_intensity"] = math.sqrt(
        statistics.pvariance(xs) + statistics.pvariance(ys) + statistics.pvariance(zs)
    )
    return result


def pair_metrics(
    first_points: Iterable[tuple[float, float]],
    second_points: Iterable[tuple[float, float]],
) -> dict[str, float | None]:
    a = _window(first_points, 30.0)
    b = _window(second_points, 30.0)
    result: dict[str, float | None] = {
        "heart_rate_difference_bpm": None,
        "beat_offset_ms": None,
        "waveform_correlation": None,
        "amplitude_ratio": None,
    }
    if len(a) < 8 or len(b) < 8:
        return result

    ma = pulse_metrics(a)
    mb = pulse_metrics(b)
    if ma["heart_rate_bpm"] is not None and mb["heart_rate_bpm"] is not None:
        result["heart_rate_difference_bpm"] = abs(float(ma["heart_rate_bpm"]) - float(mb["heart_rate_bpm"]))

    amp_a = ma.get("pulse_amplitude")
    amp_b = mb.get("pulse_amplitude")
    if amp_a is not None and amp_b is not None and float(amp_b) > 1e-9:
        result["amplitude_ratio"] = float(amp_a) / float(amp_b)

    peaks_a = detect_pulse_peaks(a)
    peaks_b = detect_pulse_peaks(b)
    offsets: list[float] = []
    for ta in peaks_a[-12:]:
        candidates = [tb - ta for tb in peaks_b if abs(tb - ta) <= 0.35]
        if candidates:
            offsets.append(min(candidates, key=abs) * 1000.0)
    if offsets:
        result["beat_offset_ms"] = statistics.median(offsets)

    start = max(a[0][0], b[0][0], max(a[-1][0], b[-1][0]) - 10.0)
    end = min(a[-1][0], b[-1][0])
    if end > start + 2.0:
        grid_rate = 50.0
        at = [t for t, _ in a if t >= start]
        av = [v for t, v in a if t >= start]
        bt = [t for t, _ in b if t >= start]
        bv = [v for t, v in b if t >= start]
        _, ai = _interpolate(at, av, grid_rate)
        _, bi = _interpolate(bt, bv, grid_rate)
        n = min(len(ai), len(bi))
        if n >= 50:
            ai = ai[:n]
            bi = bi[:n]
            am = statistics.fmean(ai)
            bm = statistics.fmean(bi)
            numerator = sum((x - am) * (y - bm) for x, y in zip(ai, bi))
            aden = math.sqrt(sum((x - am) ** 2 for x in ai))
            bden = math.sqrt(sum((y - bm) ** 2 for y in bi))
            if aden > 0 and bden > 0:
                result["waveform_correlation"] = numerator / (aden * bden)

    return result
