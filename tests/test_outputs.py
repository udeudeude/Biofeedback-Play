import csv
import json
import socket
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import biofeedback_play as app
from physiology import muse_contact_quality, eeg_metrics


class OutputTests(unittest.TestCase):
    def test_linear_band_power_and_telemetry_reach_csv_and_udp(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(app, 'RECORDINGS', Path(folder)), patch('threading.Thread.start'):
            state = app.BiofeedbackState()
            receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            receiver.bind(('127.0.0.1', 0))
            receiver.settimeout(1)
            try:
                state.set_osc(True, '127.0.0.1', receiver.getsockname()[1])
                path = Path(state.start_recording())
                expected = {f'muse.band.{band}': 1254.74 + i for i, band in enumerate(('delta', 'theta', 'alpha', 'beta', 'gamma'))}
                expected['muse.contact.tp9'] = 120.0
                for signal, value in expected.items():
                    state._store_derived(signal, value, 2.0)
                state._store_muse_battery({'percentage': 3.0})
                expected['muse.battery_percent'] = 3.0
                received = {}
                for _ in expected:
                    packet = receiver.recv(4096)
                    address = packet.split(b'\0')[0].decode()
                    received[address] = struct.unpack('>f', packet[-4:])[0]
                state.stop_recording()
                with path.open() as fp:
                    rows = list(csv.DictReader(fp))
                self.assertEqual(len(rows), len(expected))
                for row in rows:
                    signal = row['signal_id']
                    self.assertAlmostEqual(float(row['value']), expected[signal])
                    self.assertAlmostEqual(received[app.SIGNAL_DEFINITIONS[signal]['osc']], expected[signal], places=3)
                metadata = json.loads(path.with_suffix('.json').read_text())
                self.assertEqual(metadata['muse_eeg_contributors'], ['TP9', 'FP1', 'FP2', 'TP10'])
                self.assertIn('muse.band.gamma', metadata['signals'])
                self.assertGreaterEqual(next(d for d in state.device_catalog() if d['id'] == 'muse')['battery_age_s'], 0)
                second = state.start_recording()
                self.assertNotEqual(str(path), second)
            finally:
                state.stop_recording()
                state.osc_socket.close()
                receiver.close()

    def test_invalid_osc_port_is_rejected(self):
        with patch('threading.Thread.start'):
            state = app.BiofeedbackState()
            try:
                for port in (0, -1, 65536):
                    with self.assertRaises(ValueError):
                        state.set_osc(True, port=port)
            finally:
                state.osc_socket.close()

    def test_flatline_does_not_establish_good_contact(self):
        samples = [dict(tp9=1000, fp1=1000, fp2=1000, tp10=1000) for _ in range(1000)]
        quality = muse_contact_quality(samples, 500)
        self.assertTrue(all(item['level'] == 'unknown' for item in quality.values()))

    def test_stale_derived_results_are_explicitly_marked(self):
        with patch('threading.Thread.start'):
            state = app.BiofeedbackState()
            try:
                state._store_muse_eeg({'microvolts': (1, 2, 3, 4)})
                state._store_derived('muse.band.alpha', 100, -10)
                signal = next(s for s in state.signal_catalog() if s['id'] == 'muse.band.alpha')
                self.assertTrue(signal['connected'])
                self.assertFalse(signal['value_fresh'])
                state._store_derived('muse.band.alpha', 200, 0)
                signal = next(s for s in state.signal_catalog() if s['id'] == 'muse.band.alpha')
                self.assertTrue(signal['value_fresh'])
            finally:
                state.osc_socket.close()

    def test_diagnostics_guard_additional_live_emwave_slots(self):
        with patch('threading.Thread.start'):
            state = app.BiofeedbackState()
            try:
                state.emwave_extra_units[2]['connected'] = True
                meta = {'vendor_id': app.EMWAVE_VENDOR_ID, 'product_id': app.EMWAVE_PRODUCT_ID}
                with patch.object(app, 'STATE', state):
                    with self.assertRaisesRegex(RuntimeError, 'Stop emWave acquisition'):
                        app.ensure_hid_available_for_diagnostics(meta)
                    state.set_device_running('emwave2', False)
                    app.ensure_hid_available_for_diagnostics(meta)
            finally:
                state.osc_socket.close()

    def test_eeg_rms_does_not_measure_different_electrode_offsets(self):
        samples = [dict(tp9=1000, fp1=1100, fp2=1200, tp10=1300) for _ in range(200)]
        self.assertEqual(eeg_metrics(samples, 100)['broadband_rms'], 0.0)

    def test_second_launch_does_not_open_sensor_workers(self):
        from unittest.mock import MagicMock
        response = MagicMock()
        response.__enter__.return_value.headers = {'Server': 'BiofeedbackPlay/0.1'}
        with patch.object(app, 'ThreadingHTTPServer', side_effect=OSError), patch.object(app, 'BiofeedbackState') as ctor, patch.object(app.urllib.request, 'urlopen', return_value=response), patch.object(app.webbrowser, 'open') as open_browser, patch('builtins.print'):
            app.main()
            ctor.assert_not_called()
            open_browser.assert_called_once()
