import unittest

from biofeedback_play import (
    EmWaveParser,
    HTML,
    SIGNAL_DEFINITIONS,
    capture_summary,
    emwave_device_id,
    hid_is_obviously_unrelated,
)


class DiagnosticsTests(unittest.TestCase):
    def test_emwave_report_parser(self):
        parser = EmWaveParser()
        first = parser.feed_report([0x01, 0x98, 0x4C, 0x4B, 0x49, 0x46, 0x45, 0x46])
        second = parser.feed_report([0x01, 0x99, 0x48, 0x49, 0x46, 0x44, 0x45, 0x46])
        self.assertEqual(first["counter"], 0x98)
        self.assertEqual(first["samples"], [0x4C, 0x4B, 0x49, 0x46, 0x45, 0x46])
        self.assertEqual(first["gap"], 0)
        self.assertEqual(second["gap"], 0)


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


    def test_known_biofeedback_device_is_never_hidden(self):
        hidden, reason = hid_is_obviously_unrelated(
            "QUANTUM INTECH", "emWave Pulse Sensor", "HeartMath emWave Pulse Sensor"
        )
        self.assertFalse(hidden)
        self.assertEqual(reason, "")

    def test_obvious_computer_input_is_hidden(self):
        hidden, reason = hid_is_obviously_unrelated(
            "Apple Inc.", "Apple Internal Keyboard / Trackpad"
        )
        self.assertTrue(hidden)
        self.assertIn("keyboard", reason.lower())

    def test_touchbar_user_device_is_hidden(self):
        hidden, reason = hid_is_obviously_unrelated("", "TouchBarUserDevice")
        self.assertTrue(hidden)
        self.assertIn("touch bar", reason.lower())

    def test_unknown_device_remains_visible(self):
        hidden, reason = hid_is_obviously_unrelated(
            "Unknown Maker", "Experimental Sensor Interface"
        )
        self.assertFalse(hidden)
        self.assertEqual(reason, "")

    def test_multiple_emwave_signal_definitions_exist(self):
        self.assertEqual(emwave_device_id(1), "emwave")
        self.assertEqual(emwave_device_id(2), "emwave2")
        self.assertIn("emwave2.pulse_raw", SIGNAL_DEFINITIONS)
        self.assertIn("emwave2.heart_rate", SIGNAL_DEFINITIONS)
        self.assertIn("comparison.emwave1_emwave2.beat_offset", SIGNAL_DEFINITIONS)
        self.assertIn("comparison.emwave1_emwave2.amplitude_ratio", SIGNAL_DEFINITIONS)

    def test_ui_marks_direct_calculated_and_sources(self):
        self.assertIn('data-signal-filter="direct"', HTML)
        self.assertIn('data-signal-filter="calculated"', HTML)
        self.assertIn('data-signal-filter="comparison"', HTML)
        self.assertIn("Data from", HTML)
        self.assertIn("kind-badge", HTML)
        self.assertIn("source-chip", HTML)

    def test_ui_supports_persistent_panel_reordering(self):
        self.assertIn('draggable="true"', HTML)
        self.assertIn("Drag to rearrange", HTML)
        self.assertIn("biofeedbackPlay.panelOrder.v1", HTML)
        self.assertIn("Reset panel order", HTML)

    def test_camera_signals_and_lab_exist(self):
        self.assertIn("camera.ppg_raw", SIGNAL_DEFINITIONS)
        self.assertIn("camera.motion_raw", SIGNAL_DEFINITIONS)
        self.assertIn("camera.heart_rate", SIGNAL_DEFINITIONS)
        self.assertIn("camera.pulse_amplitude", SIGNAL_DEFINITIONS)
        self.assertIn("camera.signal_quality", SIGNAL_DEFINITIONS)
        self.assertNotIn("camera.hrv_rmssd", SIGNAL_DEFINITIONS)
        self.assertNotIn("camera.coherence_ratio", SIGNAL_DEFINITIONS)
        self.assertNotIn("camera.respiration_estimate", SIGNAL_DEFINITIONS)
        self.assertIn("comparison.lightstone_camera.beat_offset", SIGNAL_DEFINITIONS)
        self.assertIn("cameraSourceCanvas", HTML)
        self.assertIn("cameraMagnifiedCanvas", HTML)
        self.assertIn("cameraMagnifyEnabled", HTML)
        self.assertIn("No video is uploaded", HTML)
        self.assertIn("cameraQualityValue", HTML)
        self.assertIn("Heartbeat-band color magnification", HTML)
        self.assertIn("POS-style RGB combination", HTML)
        self.assertIn("cameraPosPulse", HTML)
        self.assertIn('action: action', HTML)


if __name__ == "__main__":
    unittest.main()
