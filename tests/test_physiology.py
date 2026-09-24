import math
import unittest

from physiology import eeg_metrics, motion_metrics, pair_metrics, pulse_metrics, skin_metrics


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


if __name__ == "__main__":
    unittest.main()
