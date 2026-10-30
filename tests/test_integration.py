"""Real pyserial + CLI subprocess, byte-checked against a Linux PTY ROM."""

import contextlib
import errno
import os
import select
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests.helpers import signed


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux PTY integration test")
class PtyIntegrationTests(unittest.TestCase):
    def test_two_real_serial_ports_across_bl2_handover(self):
        import io
        import pty

        from pixel_eub.cli import main
        from pixel_eub.transport import Port

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "bl2.img").write_bytes(signed("BL2", b"second-stage"))
            (root / "bl31.img").write_bytes(signed("BL31", b"final-stage"))
            first_master, first_slave = pty.openpty()
            second_master, second_slave = pty.openpty()
            first_path, second_path = os.ttyname(first_slave), os.ttyname(second_slave)
            os.close(first_slave)
            os.close(second_slave)
            handed_over, done = threading.Event(), threading.Event()
            errors = []

            def emulate(master, stage, body, completed):
                def exact(count):
                    data = bytearray()
                    deadline = time.monotonic() + 10
                    while len(data) < count and time.monotonic() < deadline:
                        if not select.select([master], [], [], 0.05)[0]:
                            continue
                        try:
                            data.extend(os.read(master, count - len(data)))
                        except OSError as exc:
                            if exc.errno != errno.EIO:
                                raise
                            time.sleep(0.01)
                    self.assertEqual(len(data), count)
                    return bytes(data)

                try:
                    self.assertEqual(exact(1), b"\n")
                    os.write(master, f"eub:req:09865001:{stage}\nC".encode())
                    wire = exact(len(body) + 10)
                    self.assertEqual(wire[:4], b"\x1bDNW")
                    self.assertEqual(struct.unpack_from("<I", wire, 4)[0], len(wire))
                    self.assertEqual(wire[8:-2], body)
                    self.assertEqual(wire[-2:], b"\xff\xff")
                except Exception as exc:  # noqa: BLE001 - propagate thread assertion
                    errors.append(exc)
                finally:
                    os.close(master)
                    completed.set()

            workers = [
                threading.Thread(
                    target=emulate,
                    args=(first_master, "BL2B", b"second-stage", handed_over),
                    daemon=True,
                ),
                threading.Thread(
                    target=emulate,
                    args=(second_master, "BL3B", b"final-stage", done),
                    daemon=True,
                ),
            ]

            def inventory():
                if done.is_set():
                    return []
                return [
                    Port(
                        second_path if handed_over.is_set() else first_path,
                        "SAME-USB-SERIAL",
                        "1-2",
                    )
                ]

            fb = Mock()
            fb.devices.return_value = set()
            fb.verify.side_effect = lambda *_: (
                "EMU-RECONNECTED" if done.is_set() else None
            )
            output, stderr = io.StringIO(), io.StringIO()
            for worker in workers:
                worker.start()
            try:
                with (
                    patch("pixel_eub.transport.ports", side_effect=inventory),
                    patch("pixel_eub.cli.Fastboot", return_value=fb),
                    contextlib.redirect_stdout(output),
                    contextlib.redirect_stderr(stderr),
                ):
                    code = main(
                        [
                            "boot",
                            "--device",
                            "husky",
                            "--dir",
                            str(root),
                            "--pack",
                            "none",
                            "--reconnect-wait",
                            "2",
                            "--verify-timeout",
                            "2",
                        ]
                    )
            finally:
                for worker in workers:
                    worker.join(timeout=11)
                for master in (first_master, second_master):
                    with contextlib.suppress(OSError):
                        os.close(master)
            self.assertEqual(errors, [])
            self.assertNotEqual(first_path, second_path)
            self.assertEqual(code, 0, output.getvalue() + stderr.getvalue())
            self.assertIn(
                "Bootloader fastboot confirmed: EMU-RECONNECTED", output.getvalue()
            )

    def test_full_rom_to_fastboot_with_real_serial_transport(self):
        import pty

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            image_dir = root / "stages"
            image_dir.mkdir()
            images = {}
            for index, tag in enumerate(
                (
                    "APBL",
                    "GCF",
                    "EPBL",
                    "BL2",
                    "GSA1",
                    "GSAF",
                    "ABL",
                    "TZSW",
                    "LDFW",
                    "BL31",
                )
            ):
                images[tag] = signed(tag, bytes([index + 1]) * (13 + index * 4000))
                (image_dir / f"image{index}.img").write_bytes(images[tag])
            # Independent expected wire slices, not the production stage map.
            uploads = [
                ("BL1", images["APBL"]),
                ("DPM", bytes(4096)),
                ("GCF", images["GCF"][:4096]),
                ("GCFB", images["GCF"][4096:]),
                ("EPBL", images["EPBL"][:4096]),
                ("EPBB", images["EPBL"][4096:]),
                ("BL2", images["BL2"][:4096]),
                ("BL2B", images["BL2"][4096:]),
                ("GSA1", images["GSA1"]),
                ("GSAF", images["GSAF"]),
            ]
            for tag, body_name in (
                ("ABL", "ABLB"),
                ("TZSW", "TZSB"),
                ("LDFW", "LDFB"),
                ("BL31", "BL3B"),
            ):
                uploads.extend(
                    [(tag, images[tag][:4096]), (body_name, images[tag][4096:])]
                )
            ready = root / "fastboot.ready"
            fake_fastboot = root / "fastboot"
            fake_fastboot.write_text(f"""#!{sys.executable}
from pathlib import Path
import sys
ready = Path({str(ready)!r}).exists()
if sys.argv[-1] == "devices":
    if ready: print("EMU123\\tfastboot")
elif sys.argv[-2:] == ["getvar", "product"]:
    print("(bootloader) product: husky", file=sys.stderr)
elif sys.argv[-2:] == ["getvar", "is-userspace"]:
    print("(bootloader) is-userspace: no", file=sys.stderr)
else: sys.exit(2)
""")
            fake_fastboot.chmod(0o700)
            master, slave = pty.openpty()
            slave_name = os.ttyname(slave)
            os.close(slave)
            errors, checked = [], []

            def exact(count):
                data, deadline = bytearray(), time.monotonic() + 10
                while len(data) < count and time.monotonic() < deadline:
                    if not select.select([master], [], [], 0.05)[0]:
                        continue
                    try:
                        chunk = os.read(master, count - len(data))
                    except OSError as exc:
                        if exc.errno == errno.EIO:
                            time.sleep(0.01)
                            continue
                        raise
                    data.extend(chunk)
                if len(data) != count:
                    raise AssertionError(f"ROM received {len(data)}/{count} bytes")
                return bytes(data)

            def emulate():
                try:
                    # A silent ROM waits for the tool's delayed newline nudge.
                    self.assertEqual(exact(1), b"\n")
                    for stage, expected in uploads:
                        request = f"eub:req:09865001:{stage}\r\n".encode()
                        # Split one textual message across USB reads; C has no LF.
                        os.write(master, request[:7])
                        time.sleep(0.003)
                        os.write(master, request[7:] + b"C")
                        header = exact(8)
                        self.assertEqual(header[:4], b"\x1bDNW")
                        length = struct.unpack("<I", header[4:])[0]
                        self.assertEqual(length, len(expected) + 10)
                        self.assertEqual(exact(length - 8), expected + b"\xff\xff")
                        checked.append(stage)
                        os.write(master, f"eub:ack:09865001:{stage}\n".encode())
                    ready.touch()
                except Exception as exc:  # noqa: BLE001 - return emulator assertions to the test thread
                    errors.append(exc)
                finally:
                    os.close(master)

            worker = threading.Thread(target=emulate, daemon=True)
            worker.start()
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "pixel_eub",
                        "boot",
                        "--device",
                        "Pixel 8 Pro",
                        "--port",
                        slave_name,
                        "--dir",
                        str(image_dir),
                        "--pack",
                        "none",
                        "--fastboot",
                        str(fake_fastboot),
                        "--verify-timeout",
                        "2",
                        "--reconnect-wait",
                        "2",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
            finally:
                worker.join(timeout=11)
                with contextlib.suppress(OSError):
                    os.close(master)
            self.assertFalse(worker.is_alive(), "ROM emulator did not terminate")
            self.assertEqual(errors, [])
            self.assertEqual(checked, [stage for stage, _ in uploads])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Bootloader fastboot confirmed: EMU123", result.stdout)
            log = result.stdout
            self.assertLess(log.index("[1/3]"), log.index("[2/3]"))
            self.assertLess(
                log.index("ROM identity verified:"), log.index("Sending BL1:")
            )
            self.assertLess(log.index("Sending BL1:"), log.index("Uploaded BL1:"))
            self.assertIn("ROM acknowledged BL1", log)
            self.assertLess(log.index("Uploaded BL3B:"), log.index("[3/3]"))
