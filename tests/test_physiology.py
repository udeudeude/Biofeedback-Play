import math
import unittest

from physiology import (
    camera_pulse_metrics,
    eeg_metrics,
    motion_metrics,
    muse_contact_quality,
    pair_metrics,
    pulse_metrics,
    skin_metrics,
)


class PhysiologyTests(unittest.TestCase):
    def test_pulse_metrics_detects_regular_60_bpm_signal(self):
        rate = 100.0
        points = []
        for i in range(int(rate * 70)):
            t = i / rate
            phase = t % 1.0
            pulse = math.exp(-((phase - 0.15) / 0.045) ** 2) * 100.0
            points.append((t, pulse))
        metrics = pulse_metrics(points)
        self.assertIsNotNone(metrics["heart_rate_bpm"])
        self.assertAlmostEqual(metrics["heart_rate_bpm"], 60.0, delta=3.0)
        self.assertIsNotNone(metrics["rmssd_ms"])
        self.assertIsNotNone(metrics["coherence_peak_percent"])

    def test_low_amplitude_pulse_does_not_emit_advanced_hrv(self):
        rate = 100.0
        points = []
        for i in range(int(rate * 70)):
            t = i / rate
            phase = t % 1.0
            pulse = math.exp(-((phase - 0.15) / 0.045) ** 2) * 1.0
            points.append((t, pulse))
        metrics = pulse_metrics(points)
        self.assertIsNotNone(metrics["heart_rate_bpm"])
        self.assertLess(metrics["beat_confidence_percent"], 60.0)
        self.assertIsNone(metrics["rmssd_ms"])
        self.assertIsNone(metrics["sdnn_ms"])
        self.assertIsNone(metrics["pnn50_percent"])
        self.assertIsNone(metrics["coherence_ratio"])

    def test_bad_latest_interval_is_not_replaced_by_stale_good_ibi(self):
        rate = 100.0
        beat_times = list(range(1, 21)) + [23.5]
        points = []
        for i in range(int(rate * 24)):
            t = i / rate
            pulse = sum(
                math.exp(-((t - beat - 0.15) / 0.045) ** 2) * 100.0
                for beat in beat_times
                if abs(t - beat - 0.15) < 0.25
            )
            points.append((t, pulse))
        metrics = pulse_metrics(points)
        self.assertIsNone(metrics["ibi_ms"])
        self.assertIsNone(metrics["heart_rate_bpm"])
        self.assertIsNone(metrics["rmssd_ms"])

    def test_camera_pulse_metrics_estimates_clean_spectral_rate(self):
        rate = 30.0
        points = []
        motion = []
        for i in range(int(rate * 12)):
            t = i / rate
            value = 1.5 * math.sin(2 * math.pi * 1.2 * t)
            points.append((t, value))
            motion.append((t, 0.08))

        metrics = camera_pulse_metrics(points, motion)
        self.assertIsNotNone(metrics["heart_rate_bpm"])
        self.assertAlmostEqual(metrics["heart_rate_bpm"], 72.0, delta=2.0)
        self.assertGreater(metrics["signal_quality_percent"], 35.0)

    def test_camera_pulse_metrics_refuses_high_motion_signal(self):
        rate = 30.0
        points = []
        motion = []
        for i in range(int(rate * 12)):
            t = i / rate
            value = (
                math.sin(2 * math.pi * 1.2 * t)
                + 0.9 * math.sin(2 * math.pi * 1.9 * t)
                + 0.7 * math.sin(2 * math.pi * 2.6 * t)
            )
            points.append((t, value))
            motion.append((t, 2.0))

        metrics = camera_pulse_metrics(points, motion)
        self.assertLess(metrics["signal_quality_percent"], 35.0)
        self.assertIsNone(metrics["heart_rate_bpm"])

    def test_muse_contact_quality_uses_robust_eeg_spread(self):
        rate = 500.0
        samples = []
        for i in range(1000):
            phase = 2 * math.pi * 10 * i / rate
            samples.append(
                {
                    "tp9": 1000.0 + 10.0 * math.sin(phase),
                    "fp1": 1000.0 + 35.0 * math.sin(phase),
                    "fp2": 1000.0 + 80.0 * math.sin(phase),
                    "tp10": 1000.0 + 10.0 * math.sin(phase),
                }
            )
        quality = muse_contact_quality(samples, rate)
        self.assertEqual(quality["tp9"]["level"], "good")
        self.assertEqual(quality["fp1"]["level"], "fair")
        self.assertEqual(quality["fp2"]["level"], "poor")
        self.assertEqual(quality["tp10"]["level"], "good")

    def test_skin_metrics_exposes_tonic_and_slope(self):
        points = [(i * 0.1, 100.0 + i * 0.02) for i in range(700)]
        metrics = skin_metrics(points)
        self.assertIsNotNone(metrics["tonic_level"])
        self.assertGreater(metrics["slope_per_min"], 0)

    def test_eeg_band_power_finds_alpha(self):
        rate = 500.0
        samples = []
        for i in range(int(rate * 2.2)):
            t = i / rate
            value = 20.0 * math.sin(2 * math.pi * 10.0 * t)
            samples.append({"t": t, "tp9": value, "fp1": value, "fp2": value, "tp10": value})
        metrics = eeg_metrics(samples, rate)
        self.assertGreater(metrics["alpha_power"], metrics["theta_power"])
        self.assertGreater(metrics["alpha_power"], metrics["beta_power"])

    def test_motion_metric_increases_with_motion(self):
        still = [{"x": 10, "y": 10, "z": 10} for _ in range(20)]
        moving = [{"x": i * 4, "y": -i * 3, "z": i} for i in range(20)]
        self.assertEqual(motion_metrics(still)["motion_intensity"], 0.0)
        self.assertGreater(motion_metrics(moving)["motion_intensity"], 0.0)

    def test_pair_metrics_withhold_beat_comparisons_for_weak_signals(self):
        a = []
        b = []
        for i in range(6000):
            t = i / 100.0
            va = math.exp(-((((t % 1.0) - 0.15) / 0.05) ** 2)) * 1.0
            vb = math.exp(-(((((t - 0.02) % 1.0) - 0.15) / 0.05) ** 2)) * 1.0
            a.append((t, va))
            b.append((t, vb))
        metrics = pair_metrics(a, b)
        self.assertIsNotNone(metrics["amplitude_ratio"])
        self.assertIsNone(metrics["heart_rate_difference_bpm"])
        self.assertIsNone(metrics["beat_offset_ms"])
        self.assertIsNone(metrics["waveform_correlation"])

    def test_pair_metrics_compare_two_matching_pulses(self):
        a = []
        b = []
        for i in range(6000):
            t = i / 100.0
            va = math.exp(-((((t % 1.0) - 0.15) / 0.05) ** 2)) * 100
            tb = t
            vb = math.exp(-(((((tb - 0.02) % 1.0) - 0.15) / 0.05) ** 2)) * 100
            a.append((t, va))
            b.append((t, vb))
        metrics = pair_metrics(a, b)
        self.assertIsNotNone(metrics["heart_rate_difference_bpm"])
        self.assertLess(metrics["heart_rate_difference_bpm"], 2.0)
        self.assertIsNotNone(metrics["beat_offset_ms"])
        self.assertIsNotNone(metrics["amplitude_ratio"])
        self.assertGreater(metrics["amplitude_ratio"], 0.0)


if __name__ == "__main__":
    unittest.main()
