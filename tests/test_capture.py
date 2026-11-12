import json
import tempfile
import unittest
from pathlib import Path

from pixel_eub.capture import Capture
from tests.helpers import FakeLink


class CaptureTests(unittest.TestCase):
    def test_records_rx_tx_and_errors_per_connection(self):
        with tempfile.TemporaryDirectory() as temp:
            capture = Capture(Path(temp))
            self.assertEqual(capture.path.parent, Path(temp))
            link = capture.wrap(
                FakeLink([b"eub:req:09845001:bl1\nC", b""]), "/dev/ttyACM0"
            )
            self.assertEqual(link.read(4096), b"eub:req:09845001:bl1\nC")
            link.read(4096)  # Empty reads are not recorded.
            self.assertEqual(link.write(b"\x1bDNW" + bytes(100)), 104)
            with self.assertRaises(OSError):
                link.read(4096)
            capture.wrap(FakeLink([]), "/dev/ttyACM0")
            records = [
                json.loads(line) for line in capture.path.read_text().splitlines()
            ]
        self.assertEqual(
            [r.get("event") for r in records],
            ["open", None, None, "read-error", "open"],
        )
        self.assertEqual(bytes.fromhex(records[1]["rx"]), b"eub:req:09845001:bl1\nC")
        self.assertEqual(records[2]["tx"], 104)
        self.assertEqual(records[2]["head"][:8], "1b444e57")
        self.assertEqual(len(records[2]["sha256"]), 64)
        self.assertEqual([r["conn"] for r in records], [1, 1, 1, 1, 2])
