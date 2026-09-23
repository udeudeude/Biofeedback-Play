import unittest

from biofeedback_play import capture_summary


class DiagnosticsTests(unittest.TestCase):
    def test_capture_summary_formats_reports(self):
        capture = {
            "device": {
                "known": "HeartMath emWave Pulse Sensor",
                "product": "emWave Pulse Sensor",
                "manufacturer": "QUANTUM INTECH",
                "vendor_hex": "0x0e30",
                "product_hex": "0x0002",
                "usage_page": 0xFF00,
                "usage": 1,
            },
            "duration_s": 1.25,
            "reports": [
                {"t": 0.1, "hex": "01 02 41", "ascii": "..A"},
            ],
        }

        text = capture_summary(capture)
        self.assertIn("HeartMath emWave Pulse Sensor", text)
        self.assertIn("0x0e30 / 0x0002", text)
        self.assertIn("01 02 41", text)


if __name__ == "__main__":
    unittest.main()
