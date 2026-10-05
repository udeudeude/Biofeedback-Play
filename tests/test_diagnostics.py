import inspect
import unittest

from biofeedback_play import (
    BiofeedbackState,
    EmWaveParser,
    EmWaveSampleClock,
    HTML,
    DEVICE_DEFINITIONS,
    SIGNAL_DEFINITIONS,
    capture_summary,
    emwave_device_id,
    hid_is_obviously_unrelated,
    reconcile_emwave_slot_paths,
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

    def test_emwave_counter_wrap_and_gap_detection(self):
        parser = EmWaveParser()
        self.assertEqual(
            parser.feed_report([0x01, 0xFF, 1, 2, 3, 4, 5, 6])["gap"],
            0,
        )
        self.assertEqual(
            parser.feed_report([0x01, 0x00, 7, 8, 9, 10, 11, 12])["gap"],
            0,
        )
        self.assertEqual(
            parser.feed_report([0x01, 0x02, 13, 14, 15, 16, 17, 18])["gap"],
            1,
        )

    def test_emwave_sample_clock_rejects_short_usb_delivery_jitter(self):
        clock = EmWaveSampleClock()
        first = clock.packet_times(0.016, gap=0)
        second = clock.packet_times(0.048, gap=0)
        self.assertEqual(len(first), 6)
        self.assertEqual(len(second), 6)
        self.assertLess(second[-1], 0.036)

        third = clock.packet_times(0.064, gap=1)
        self.assertAlmostEqual(
            third[-1] - second[-1],
            12.0 / clock.sample_rate_hz,
            delta=0.002,
        )

    def test_emwave_sample_clock_can_learn_long_run_rate(self):
        clock = EmWaveSampleClock()
        report_period = 6.0 / 375.0
        jitter = (0.0, 0.0015, -0.0007, 0.0004, -0.0010)
        for index in range(720):
            arrival = (index + 1) * report_period + jitter[index % len(jitter)]
            clock.packet_times(arrival, gap=0)
        self.assertGreater(clock.sample_rate_hz, 373.0)
        self.assertLess(clock.sample_rate_hz, 377.0)

    def test_emwave_slots_do_not_shift_when_identical_sensor_is_unplugged(self):
        first = b"path-A"
        second = b"path-B"
        third = b"path-C"
        assignments, replaced = reconcile_emwave_slot_paths({}, [first, second])
        self.assertEqual(assignments, {1: first, 2: second})
        self.assertEqual(replaced, set())

        assignments, replaced = reconcile_emwave_slot_paths(assignments, [second])
        self.assertEqual(assignments[2], second)
        self.assertEqual(assignments[1], first)
        self.assertEqual(replaced, set())

        assignments, replaced = reconcile_emwave_slot_paths(
            assignments, [second, third]
        )
        self.assertEqual(assignments[2], second)
        self.assertEqual(assignments[1], third)
        self.assertEqual(replaced, {1})

    def test_capture_summary_formats_reports(self):
        capture = {
            "device": {
                "known": "HeartMath emWave Pulse Sensor",
                "product": "emWave Pulse Sensor",
                "manufacturer": "QUANTUM INTECH",
                "vendor_id": 0x0E30,
                "product_id": 0x0002,
                "vendor_hex": "0x0e30",
                "product_hex": "0x0002",
                "usage_page": 0xFF00,
                "usage": 1,
            },
            "duration_s": 0.032,
            "reports": [
                {
                    "t": 0.016,
                    "bytes": [0x01, 0x02, 60, 61, 62, 63, 64, 65],
                    "hex": "01 02 3C 3D 3E 3F 40 41",
                    "ascii": "..<=>?@A",
                },
                {
                    "t": 0.032,
                    "bytes": [0x01, 0x03, 66, 67, 68, 69, 70, 71],
                    "hex": "01 03 42 43 44 45 46 47",
                    "ascii": "..BCDEFG",
                },
            ],
        }

        text = capture_summary(capture)
        self.assertIn("HeartMath emWave Pulse Sensor", text)
        self.assertIn("0x0e30 / 0x0002", text)
        self.assertIn("01 02 3C 3D 3E 3F 40 41", text)
        self.assertIn("6 consecutive waveform samples/report", text)
        self.assertIn("Counter gaps: 0", text)
        self.assertIn("Host report timing", text)


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

    def test_all_four_emwave_slots_are_defined(self):
        self.assertIn("emwave", DEVICE_DEFINITIONS)
        self.assertIn("emwave2", DEVICE_DEFINITIONS)
        self.assertIn("emwave3", DEVICE_DEFINITIONS)
        self.assertIn("emwave4", DEVICE_DEFINITIONS)

    def test_ui_has_persistent_light_and_dark_modes(self):
        self.assertIn('id="themeToggle"', HTML)
        self.assertIn('biofeedbackPlay.theme.v1', HTML)
        self.assertIn('data-theme="light"', HTML)
        self.assertIn('Light mode', HTML)
        self.assertIn('Dark mode', HTML)

    def test_audio_stops_when_signal_becomes_unavailable(self):
        self.assertIn("function stopUnavailableAudio()", HTML)
        self.assertIn("signal.connected", HTML)
        self.assertIn("signal.running", HTML)
        self.assertIn("signalViewEnabled(signal)", HTML)
        self.assertIn("stopAudio(signalId, false)", HTML)
        self.assertIn("stopUnavailableAudio();", HTML)

    def test_signal_panels_have_persistent_standard_mini_wide_views(self):
        self.assertIn("biofeedbackPlay.panelViews.v1", HTML)
        self.assertIn("view-standard", HTML)
        self.assertIn("view-mini", HTML)
        self.assertIn("view-wide", HTML)
        self.assertNotIn("view-name", HTML)
        self.assertNotIn("view-full", HTML)
        self.assertIn("panel-size-control", HTML)
        self.assertIn('data-panel-size="standard"', HTML)
        self.assertIn('data-panel-size="mini"', HTML)
        self.assertIn('data-panel-size="wide"', HTML)
        self.assertIn("setPanelView", HTML)
        self.assertIn("grid-column: span 4 !important", HTML)
        self.assertIn("grid-column: span 12 !important", HTML)
        self.assertIn("signal-collapse-device", HTML)

    def test_ui_has_persistent_device_view_switches(self):
        self.assertIn('id="deviceViewButtons"', HTML)
        self.assertIn('biofeedbackPlay.deviceViews.v1', HTML)
        self.assertIn('deviceViewEnabled', HTML)
        self.assertIn('signalViewEnabled', HTML)
        self.assertIn('role="switch"', HTML)
        self.assertIn("switch-track", HTML)
        self.assertIn("switch-knob", HTML)
        self.assertNotIn("Choose which devices may appear here, even while they are unplugged.", HTML)

    def test_audio_uses_speaker_icons_and_remains_in_mini_view(self):
        self.assertIn("🔊", HTML)
        self.assertIn("🔇", HTML)
        self.assertIn("audio-icon", HTML)
        self.assertIn("Mute audio", HTML)
        self.assertNotIn('[id^="audio_"]', HTML)

    def test_primary_buttons_keep_readable_text_in_light_mode(self):
        self.assertIn("button.primary { background: #4338ca; border-color: #635bdf; color: #fff; }", HTML)

    def test_ui_has_glanceable_signal_hierarchy(self):
        self.assertIn('data-signal-filter="direct"', HTML)
        self.assertIn('data-signal-filter="calculated"', HTML)
        self.assertIn('data-signal-filter="comparison"', HTML)
        self.assertIn("Direct sensor data", HTML)
        self.assertIn("Derived from this sensor", HTML)
        self.assertIn("Cross-device comparisons", HTML)
        self.assertIn("What the hardware itself is sending", HTML)
        self.assertIn("Calculations made from the direct signal above", HTML)
        self.assertIn("signalMeaning", HTML)
        self.assertIn("Details & technical information", HTML)
        self.assertIn("kind-badge", HTML)
        self.assertIn("source-chip", HTML)

    def test_live_signal_polling_is_batched(self):
        self.assertIn("/api/signal_samples_batch", HTML)
        self.assertNotIn('fetch("/api/signal_samples?id="', HTML)

    def test_live_data_tab_only_shows_enabled_live_signals(self):
        self.assertIn(">Live data</button>", HTML)
        self.assertNotIn("Only live signals from enabled device views appear below.", HTML)
        self.assertIn("signal.connected && signal.running && signalViewEnabled(signal)", HTML)
        self.assertIn("No live device data", HTML)

    def test_muse_setup_can_use_paired_bluetooth_and_copy_trace(self):
        self.assertIn("Scan Muse connections", HTML)
        self.assertIn("Paired Bluetooth devices", HTML)
        self.assertIn("Use selected connection", HTML)
        self.assertIn("Copy Muse trace", HTML)
        self.assertIn("muse-trace", HTML)
        self.assertIn("data.connections || data.ports || []", HTML)
        self.assertIn("paired classic-Bluetooth", HTML)
        self.assertIn("const directMuse = bluetooth.filter", HTML)
        self.assertIn(
            "selected it directly instead of its legacy serial endpoint",
            HTML,
        )

    def test_macos_muse_uses_main_thread_helper_process(self):
        source = inspect.getsource(BiofeedbackState._muse_worker_session)
        self.assertIn("devices.muse2014_worker", source)
        self.assertIn("subprocess.Popen", source)
        self.assertIn("select.select", source)

    def test_live_graphs_reduce_offscreen_rendering_load(self):
        self.assertIn("IntersectionObserver", HTML)
        self.assertIn("visibleSignalPanels", HTML)
        self.assertIn("signalPanelIsDrawable", HTML)
        self.assertIn("decimateGraphValues", HTML)
        self.assertIn("Math.min(1.5, window.devicePixelRatio || 1)", HTML)
        self.assertIn("setInterval(pollSignals, 200)", HTML)
        self.assertIn("signalPanelIsDrawable(signal) || state.audioOn", HTML)

    def test_muse_live_summary_shows_battery_contact_and_combined_bands(self):
        self.assertIn("muse-battery-shell", HTML)
        self.assertIn("Estimated electrode contact", HTML)
        self.assertIn("muse-contact-sensor", HTML)
        self.assertIn("EEG frequency bands", HTML)
        self.assertIn("museBandCanvas", HTML)
        self.assertIn("MUSE_BANDS", HTML)
        self.assertIn("drawMuseBandOverview", HTML)
        self.assertIn("10 * Math.log10", HTML)
        self.assertIn("Combined display in dB relative to 1 µV²", HTML)

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
        self.assertNotIn("cameraMagnifiedCanvas", HTML)
        self.assertNotIn("cameraMagnifyEnabled", HTML)
        self.assertIn("cameraMagnifyToggle", HTML)
        self.assertIn("cameraGuideScale", HTML)
        self.assertIn("cameraGuideX", HTML)
        self.assertIn("cameraGuideY", HTML)
        self.assertIn("Optional input · Camera", HTML)
        self.assertIn("camera-lab-details", HTML)
        self.assertIn('classList.add("active")', HTML)
        self.assertIn('classList.remove("active")', HTML)
        self.assertIn("cameraQualityValue", HTML)
        self.assertIn("Magnified pulse-band view", HTML)
        self.assertIn(
            "POS-style",
            SIGNAL_DEFINITIONS["camera.ppg_raw"]["description"],
        )
        self.assertIn("cameraPosPulse", HTML)
        self.assertIn('action: action', HTML)
        self.assertIn("Waveform rate:", HTML)
        self.assertIn("Estimated waveform rate:", HTML)
        self.assertIn("source_generation", HTML)
        self.assertIn("generation !== generation", HTML)


if __name__ == "__main__":
    unittest.main()
