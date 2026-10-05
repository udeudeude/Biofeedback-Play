import csv
import json
import socket
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import biofeedback_play as app
from physiology import muse_contact_quality


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
