import unittest

from biofeedback_play import LightstoneParser


class LightstoneParserTests(unittest.TestCase):
    def test_reassembles_fragmented_raw_frame(self):
        parser = LightstoneParser()
        reports = [
            [7, 60, 82, 65, 87, 62, 48, 48],
            [7, 48, 48, 32, 48, 52, 50, 54],
            [7, 60, 92, 82, 65, 87, 62, 10],
        ]
        values = []
        for report in reports:
            values.extend(parser.feed_report(report))
        self.assertEqual(values, [(0x0000, 0x0426)])


if __name__ == "__main__":
    unittest.main()
