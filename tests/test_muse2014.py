import struct
import unittest

from devices.muse2014 import (
    Muse2014SerialClient,
    MusePacketParser,
    decode_accelerometer_packet,
    decode_battery_packet,
    decode_eeg_packet,
)


class Muse2014Tests(unittest.TestCase):
    def test_packet_parser_handles_sync_and_partial_packets(self):
        parser = MusePacketParser()
        eeg = bytes([0xE0]) + struct.pack(">HHHH", 100, 200, 300, 400)
        first = parser.feed(b"\xff\xff\xaa\x55" + eeg[:4])
        self.assertEqual(first, [])
        second = parser.feed(eeg[4:])
        self.assertEqual(second, [("eeg", eeg)])

    def test_eeg_packet_decodes_four_channels(self):
        packet = bytes([0xE0]) + struct.pack(">HHHH", 100, 200, 300, 400)
        decoded = decode_eeg_packet(packet)
        self.assertEqual(decoded["raw"], (100, 200, 300, 400))
        self.assertEqual(len(decoded["microvolts"]), 4)

    def test_accelerometer_packet_decodes_signed_axes(self):
        x, y, z = 12, -20, 30
        def enc(v):
            return v & 0x3FF
        word = enc(x) | (enc(y) << 10) | (enc(z) << 20)
        packet = bytes([0xA0]) + word.to_bytes(4, "little")
        self.assertEqual(decode_accelerometer_packet(packet), (x, y, z))

    def test_muse_name_from_macos_port(self):
        self.assertEqual(
            Muse2014SerialClient._muse_name_from_port("/dev/cu.Muse-A620"),
            "Muse-A620",
        )

    def test_mac_serial_candidates_prefer_tty_sibling(self):
        candidates = Muse2014SerialClient._mac_serial_candidates(
            "/dev/cu.Muse-A620"
        )
        self.assertEqual(candidates[0], "/dev/tty.Muse-A620")
        self.assertIn("/dev/cu.Muse-A620", candidates)

    def test_battery_packet(self):
        packet = bytes([0xB0]) + struct.pack(">HHHH", 8750, 3900, 3880, 27)
        decoded = decode_battery_packet(packet)
        self.assertEqual(decoded["percentage"], 87.5)
        self.assertEqual(decoded["fuel_gauge_mv"], 3900)


if __name__ == "__main__":
    unittest.main()
