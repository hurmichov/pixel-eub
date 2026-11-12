import os
import struct
import tempfile
import unittest
from pathlib import Path

from pixel_eub import EubError, usbinfo

# CDC ACM device like the Pixel ROM: device, config, IAD, two interfaces.
DESCRIPTORS = (
    struct.pack(
        "<BBHBBBBHHHBBBB",
        18,
        1,
        0x0200,
        2,
        0,
        0,
        64,
        0x18D1,
        0x4F00,
        0x0100,
        1,
        2,
        3,
        1,
    )
    + struct.pack("<BBHBBBBB", 9, 2, 67, 2, 1, 0, 0xC0, 50)
    + bytes([8, 0x0B, 0, 2, 2, 2, 1, 0])
    + struct.pack("<BBBBBBBBB", 9, 4, 0, 0, 1, 2, 2, 1, 0)
    + bytes([5, 0x24, 0, 0x10, 0x01])
    + struct.pack("<BBBBHB", 7, 5, 0x81, 3, 16, 9)
    + struct.pack("<BBBBBBBBB", 9, 4, 1, 0, 2, 0x0A, 0, 0, 0)
    + struct.pack("<BBBBHB", 7, 5, 0x82, 2, 512, 0)
    + struct.pack("<BBBBHB", 7, 5, 0x02, 2, 512, 0)
)


def fake_sysfs(root: Path, descriptors=DESCRIPTORS, bcd="0100"):
    device = root / "devices" / "pci0" / "usb1" / "1-2"
    interface = device / "1-2:1.0"
    tty = interface / "tty" / "ttyACM0"
    tty.mkdir(parents=True)
    (device / "descriptors").write_bytes(descriptors)
    for name, value in (
        ("idVendor", "18d1"),
        ("idProduct", "4f00"),
        ("bcdDevice", bcd),
        ("speed", "480"),
        ("version", " 2.00"),
        ("product", "Pixel ROM Recovery"),
        ("serial", "0bd5ce1bc6f0"),
    ):
        (device / name).write_text(value + "\n")
    link = root / "class" / "tty" / "ttyACM0"
    link.mkdir(parents=True)
    (link / "device").symlink_to(interface)
    return device


class UsbInfoTests(unittest.TestCase):
    def test_decode_cdc_descriptors(self):
        lines = usbinfo.decode(DESCRIPTORS)
        self.assertIn("id 18d1:4f00 bcdDevice 0100", lines[0])
        self.assertTrue(lines[1].startswith("CONFIG 1 total 67 interfaces 2"))
        self.assertIn("max 100 mA", lines[1])
        self.assertIn("ASSOCIATION interfaces 0+2", lines[2])
        self.assertIn("ENDPOINT 81 IN interrupt max 16", "\n".join(lines))
        self.assertIn("ENDPOINT 02 OUT bulk max 512", "\n".join(lines))
        self.assertIn("malformed", usbinfo.decode(b"\x09\x02\x00")[0])
        self.assertIn("malformed", usbinfo.decode(b"\x01\x02")[0])

    @unittest.skipIf(os.name == "nt", "Linux sysfs paths cannot be created on Windows")
    def test_sysfs_lookup_save_and_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "sys"
            device = fake_sysfs(root)
            found = usbinfo.usb_device_dir("/dev/ttyACM0", sys_root=root)
            self.assertEqual(found, device.resolve())
            info = usbinfo.read_info(found)
            self.assertEqual(info.attributes["product"], "Pixel ROM Recovery")
            self.assertIn("bcdDevice 0100", usbinfo.summary(info))
            self.assertNotIn("0bd5ce1bc6f0", usbinfo.summary(info))
            text = usbinfo.save(info, Path(temp) / "out", "connection1")
            self.assertEqual(text.with_suffix(".bin").read_bytes(), DESCRIPTORS)
            self.assertIn("DEVICE usb 0200", text.read_text())
            (device / "bcdDevice").write_text("0200\n")
            later = usbinfo.read_info(found)
            self.assertEqual(usbinfo.changes(info, later), ["bcdDevice 0100 -> 0200"])
            self.assertEqual(usbinfo.changes(None, later), [])
            with self.assertRaisesRegex(EubError, "No sysfs"):
                usbinfo.usb_device_dir("/dev/ttyACM9", sys_root=root)
