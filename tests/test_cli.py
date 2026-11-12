import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pixel_eub import EubError
from pixel_eub.cli import main
from pixel_eub.transport import Fastboot, NoDevice, Port, SerialLink, find_port, ports
from tests.helpers import FakeLink, signed


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(list(args))
        return code, output.getvalue(), errors.getvalue()

    def test_models_and_actionable_failures_without_traceback(self):
        self.assertIn("Pixel 8 Pro", self.run_cli("models")[1])
        for args in (
            ("boot", "--dry-run"),
            ("boot", "--device", "Pixel 6", "--dry-run"),
            ("boot", "--device", "invalid"),
            ("boot", "--device", "husky", "--image", "a", "--build", "b"),
        ):
            code, _, errors = self.run_cli(*args)
            self.assertEqual(code, 1)
            self.assertNotIn("Traceback", errors)

    def test_dry_run_opens_no_usb_and_validates_sources(self):
        with tempfile.TemporaryDirectory() as temp:
            for name, tag in (
                ("bl1", "APBL"),
                ("pbl", "EPBL"),
                ("bl2", "BL2"),
                ("abl", "ABL"),
                ("bl31", "BL31"),
            ):
                (Path(temp) / f"{name}.img").write_bytes(signed(tag))
            with (
                patch("pixel_eub.cli.open_port") as opened,
                patch("pixel_eub.cli.Fastboot") as fb,
            ):
                code, output, errors = self.run_cli(
                    "boot",
                    "--device",
                    "Pixel 8 Pro",
                    "--dir",
                    temp,
                    "--pack",
                    "none",
                    "--dry-run",
                )
                self.assertEqual((code, errors), (0, ""))
                self.assertIn("USB was not opened", output)
                opened.assert_not_called()
                fb.assert_not_called()

    def test_missing_rom_is_reported_before_preparing_images_or_fastboot(self):
        with (
            patch(
                "pixel_eub.cli.find_port",
                side_effect=NoDevice("ROM USB device not found"),
            ),
            patch("pixel_eub.cli.prepare") as prepare,
            patch("pixel_eub.cli.open_port") as opened,
            patch("pixel_eub.cli.Fastboot") as fastboot,
        ):
            code, output, errors = self.run_cli("boot", "--device", "Pixel 7 Pro")
        self.assertEqual(code, 1)
        self.assertIn("[1/3]", output)
        self.assertIn("ROM USB device not found", errors)
        prepare.assert_not_called()
        opened.assert_not_called()
        fastboot.assert_not_called()

    def test_listen_after_failure_option_is_passed_and_validated(self):
        with patch("pixel_eub.cli.boot", return_value=0) as boot:
            self.run_cli("boot", "--device", "bluejay", "--listen-after-failure", "10")
        self.assertEqual(boot.call_args.args[0].listen_after_failure, 10)
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as exit:
            main(["boot", "--device", "bluejay", "--listen-after-failure", "-1"])
        self.assertEqual(exit.exception.code, 2)
        self.assertIn("0 or more", errors.getvalue())

    def test_capture_records_the_boot_session(self):
        import json

        from pixel_eub.images import Sources

        link = FakeLink([b"eub:req:09865001:BL3B\nC"])
        fb = Mock()
        fb.verify.return_value = "OK"
        with (
            tempfile.TemporaryDirectory() as temp,
            patch(
                "pixel_eub.cli.prepare",
                return_value=Sources([{"bl31": signed("BL31", b"last")}], "zuma"),
            ),
            patch("pixel_eub.cli.find_port", side_effect=[Port("p"), NoDevice("x")]),
            patch("pixel_eub.cli.open_port", return_value=link),
            patch("pixel_eub.cli.Fastboot", return_value=fb),
        ):
            code, output, _ = self.run_cli(
                "boot",
                "--device",
                "husky",
                "--reconnect-wait",
                "0.01",
                "--capture",
                temp,
            )
            (path,) = Path(temp).glob("capture-*.jsonl")
            records = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(code, 0)
        self.assertIn("Capturing raw serial traffic to", output)
        self.assertEqual(records[0]["event"], "open")
        self.assertTrue(any("rx" in r for r in records))
        self.assertTrue(any(r.get("tx") for r in records))

    def test_dump_usb_saves_each_connection_and_never_blocks_recovery(self):
        from pixel_eub.images import Sources
        from pixel_eub.usbinfo import UsbInfo

        first = FakeLink([b"eub:req:09865001:BL2B\nC"])
        second = FakeLink([b"eub:req:09865001:BL3B\nC"])
        infos = [
            UsbInfo("a", {"bcdDevice": "0100"}, b"\x12\x01"),
            UsbInfo("b", {"bcdDevice": "0200"}, b"\x12\x01"),
        ]
        fb = Mock()
        fb.verify.return_value = "OK"
        sources = Sources(
            [{"bl2": signed("BL2", b"two"), "bl31": signed("BL31", b"last")}], "zuma"
        )
        with (
            tempfile.TemporaryDirectory() as temp,
            patch("pixel_eub.cli.prepare", return_value=sources),
            patch("pixel_eub.cli.Fastboot", return_value=fb),
            patch(
                "pixel_eub.cli.find_port",
                side_effect=[Port("first"), Port("second"), NoDevice("gone")],
            ),
            patch("pixel_eub.cli.open_port", side_effect=[first, second]),
            patch("pixel_eub.cli.usbinfo.usb_device_dir", return_value=Path(temp)),
            patch("pixel_eub.cli.usbinfo.read_info", side_effect=infos),
        ):
            code, output, errors = self.run_cli(
                "boot",
                "--device",
                "husky",
                "--reconnect-wait",
                "0.05",
                "--dump-usb",
                temp,
            )
            saved = sorted(p.name for p in Path(temp).glob("usb-*-connection*.txt"))
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(len(saved), 2)
        self.assertIn("USB descriptors (connection 2)", output)
        self.assertIn(
            "changed since the previous connection: bcdDevice 0100 -> 0200", output
        )
        # A failed dump is reported, not fatal.
        link = FakeLink([b"eub:req:09865001:BL3B\nC"])
        with (
            patch("pixel_eub.cli.prepare", return_value=sources),
            patch("pixel_eub.cli.Fastboot", return_value=fb),
            patch("pixel_eub.cli.find_port", side_effect=[Port("p"), NoDevice("x")]),
            patch("pixel_eub.cli.open_port", return_value=link),
            patch(
                "pixel_eub.cli.usbinfo.usb_device_dir",
                side_effect=EubError("No sysfs USB entry"),
            ),
        ):
            code, output, _ = self.run_cli(
                "boot",
                "--device",
                "husky",
                "--reconnect-wait",
                "0.01",
                "--dump-usb",
                "/nonexistent",
            )
        self.assertEqual(code, 0)
        self.assertIn("USB descriptors not saved: No sysfs USB entry", output)

    def test_no_usb_check_sends_retail_bl1_unchanged(self):
        from pixel_eub.protocol import frame

        retail = signed("APBL", flags=0x400)
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "bl1.img").write_bytes(retail)
            for flags, sent in (([], False), (["--no-usb-check"], True)):
                link = FakeLink([b"eub:req:09845001:BL1\nC"])
                with (
                    self.subTest(flags=flags),
                    patch("pixel_eub.cli.find_port", return_value=Port("first")),
                    patch("pixel_eub.cli.open_port", return_value=link),
                    patch("pixel_eub.cli.Fastboot") as fastboot,
                ):
                    code, output, errors = self.run_cli(
                        "boot",
                        "--device",
                        "bluejay",
                        "--dir",
                        temp,
                        "--pack",
                        "none",
                        "--reconnect-wait",
                        "0.01",
                        *flags,
                    )
                    self.assertEqual(code, 1)
                    self.assertEqual(bytes(link.output) == frame(retail), sent)
                    if sent:
                        self.assertIn("WARNING: --no-usb-check", output)
                    else:
                        self.assertIn("USB-bootable flag", errors)
                        fastboot.assert_not_called()

    def test_inspect_command_reports_usb_flag(self):
        with tempfile.TemporaryDirectory() as temp:
            retail = Path(temp) / "bl1.bin"
            retail.write_bytes(signed("APBL", flags=0x400))
            usb = Path(temp) / "bl31.img"
            usb.write_bytes(signed("BL31", flags=0x401))
            code, output, _ = self.run_cli("inspect", str(retail), str(usb))
        self.assertEqual(code, 0)
        self.assertIn("bl1.bin | BL1 (APBL) | 4100 bytes | USB boot: no", output)
        self.assertIn("  ! retail BL1", output)
        self.assertIn("bl31.img | BL31 (BL31) | 4100 bytes | USB boot: yes", output)

    def test_battery_voltage_after_fastboot_is_best_effort(self):
        from pixel_eub.cli import report_battery

        for reply, expected in (
            ((0, "battery-voltage: 3756\nFinished."), "WARNING: battery 3756 mV"),
            ((0, "battery-voltage: 4302\n"), "Battery: 4302 mV."),
            ((1, "FAILED (remote: unknown variable)"), ""),
        ):
            fastboot = Mock()
            fastboot.run.return_value = reply
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                report_battery(fastboot, "SERIAL")
            self.assertEqual(
                output.getvalue().split("\n")[0][: len(expected)], expected
            )

    def test_dpm_first_policy_follows_model(self):
        from pixel_eub.cli import dpm_first_policy
        from pixel_eub.models import model_for

        for device, allowed, expected in (
            ("panther", False, "stop"),
            ("cheetah", False, "stop"),
            ("felix", False, "stop"),
            ("cheetah", True, "warn"),
            ("lynx", False, "warn"),
            ("tangorpro", False, "allow"),
            ("husky", False, "allow"),
        ):
            with self.subTest(device=device, allowed=allowed):
                self.assertEqual(dpm_first_policy(model_for(device), allowed), expected)

    def test_dpm_first_stops_cli_boot_without_fastboot_check(self):
        from pixel_eub.images import Sources

        link = FakeLink([b"eub:req:09855001:DPM\nC"])
        with (
            patch(
                "pixel_eub.cli.prepare",
                return_value=Sources([{"bl1": signed("APBL")}], "gs201"),
            ),
            patch("pixel_eub.cli.find_port", return_value=Port("first")),
            patch("pixel_eub.cli.open_port", return_value=link),
            patch("pixel_eub.cli.Fastboot") as fastboot,
        ):
            code, _, errors = self.run_cli("boot", "--device", "cheetah")
        self.assertEqual(code, 1)
        self.assertIn("DPM before BL1", errors)
        self.assertIn("--allow-dpm-first", errors)
        self.assertEqual(link.output, b"")
        fastboot.assert_not_called()

    def test_port_recovery_is_opt_in(self):
        from pixel_eub.images import Sources

        for flags, enabled in (
            ((), False),
            (("--no-port-recovery",), False),
            (("--port-recovery", "--no-port-recovery"), False),
            (("--port-recovery",), True),
        ):
            link = FakeLink([b"eub:req:09865001:BL3B\nC"])
            fb = Mock()
            fb.verify.return_value = "RECOVERED"
            with (
                self.subTest(flags=flags),
                patch("pixel_eub.cli.PortAccessRecovery") as repair,
                patch(
                    "pixel_eub.cli.prepare",
                    return_value=Sources([{"bl31": signed("BL31", b"last")}], "zuma"),
                ),
                patch(
                    "pixel_eub.cli.find_port",
                    side_effect=[Port("first"), NoDevice("not found")],
                ),
                patch("pixel_eub.cli.open_port", return_value=link) as opened,
                patch("pixel_eub.cli.Fastboot", return_value=fb),
            ):
                code, _, errors = self.run_cli(
                    "boot",
                    "--device",
                    "husky",
                    "--reconnect-wait",
                    "0.01",
                    *flags,
                )
                self.assertEqual((code, errors), (0, ""))
                self.assertEqual(repair.called, enabled)
                recovery = opened.call_args.kwargs["recovery"]
                if enabled:
                    self.assertIs(recovery, repair.return_value)
                else:
                    self.assertIsNone(recovery)

    def test_reconnects_then_confirms_fastboot(self):
        first = FakeLink([b"eub:req:09865001:BL2B\nC"])
        second = FakeLink([b"eub:req:09865001:BL3B\nC"])
        fb = Mock()
        fb.devices.return_value = set()
        fb.verify.side_effect = [None, "FASTBOOT_SERIAL"]
        from pixel_eub.images import Sources

        sources = Sources(
            [{"bl2": signed("BL2", b"two"), "bl31": signed("BL31", b"last")}], "zuma"
        )
        with (
            patch("pixel_eub.cli.prepare", return_value=sources),
            patch("pixel_eub.cli.Fastboot", return_value=fb),
            patch(
                "pixel_eub.cli.find_port",
                side_effect=[
                    Port("first", "USBID"),
                    Port("second", "USBID"),
                    NoDevice("not found"),
                ],
            ),
            patch("pixel_eub.cli.open_port", side_effect=[first, second]),
        ):
            code, output, errors = self.run_cli(
                "boot", "--device", "husky", "--reconnect-wait", "0.01"
            )
        self.assertEqual((code, errors), (0, ""))
        self.assertTrue(first.closed and second.closed)
        self.assertIn("fastboot confirmed", output)
        self.assertEqual(first.output[8:-2], b"two")
        self.assertEqual(second.output[8:-2], b"last")

    def test_missing_fastboot_still_uploads_both_rom_connections(self):
        from pixel_eub.images import Sources

        first = FakeLink([b"eub:req:09865001:BL2B\nC"])
        second = FakeLink([b"eub:req:09865001:BL3B\nC"])
        sources = Sources(
            [{"bl2": signed("BL2", b"two"), "bl31": signed("BL31", b"last")}], "zuma"
        )
        with (
            patch("pixel_eub.cli.prepare", return_value=sources),
            patch("pixel_eub.cli.Fastboot", side_effect=EubError("fastboot not found")),
            patch(
                "pixel_eub.cli.find_port",
                side_effect=[
                    Port("first", "USBID"),
                    Port("second", "USBID"),
                    NoDevice("not found"),
                ],
            ),
            patch("pixel_eub.cli.open_port", side_effect=[first, second]),
        ):
            code, output, errors = self.run_cli(
                "boot", "--device", "husky", "--reconnect-wait", "0.01"
            )
        self.assertEqual(code, 1)  # Cannot confirm the outcome without fastboot.
        self.assertEqual(first.output[8:-2], b"two")
        self.assertEqual(second.output[8:-2], b"last")
        self.assertIn("NOT confirmed", errors)
        self.assertNotIn("fastboot confirmed:", output)
        self.assertTrue(first.closed and second.closed)

    def test_fastboot_verification_waits_until_later_rom_stages_sent(self):
        from pixel_eub.images import Sources

        first = FakeLink([b"eub:req:09865001:BL2B\nC"])
        second = FakeLink([b"eub:req:09865001:BL3B\nC"])
        fb = Mock()

        def verify(_model):
            self.assertTrue(first.closed and second.closed)
            self.assertEqual(second.output[8:-2], b"last")
            return "RECOVERED"

        fb.verify.side_effect = verify

        def construct_fastboot(*_args):
            self.assertTrue(first.closed and second.closed)
            self.assertEqual(second.output[8:-2], b"last")
            return fb

        sources = Sources(
            [{"bl2": signed("BL2", b"two"), "bl31": signed("BL31", b"last")}], "zuma"
        )
        with (
            patch("pixel_eub.cli.prepare", return_value=sources),
            patch("pixel_eub.cli.Fastboot", side_effect=construct_fastboot),
            patch(
                "pixel_eub.cli.find_port",
                side_effect=[
                    Port("first", "USBID"),
                    Port("second", "USBID"),
                    NoDevice("not found"),
                ],
            ),
            patch("pixel_eub.cli.open_port", side_effect=[first, second]),
        ):
            code, _, errors = self.run_cli(
                "boot", "--device", "husky", "--reconnect-wait", "0.01"
            )
        self.assertEqual((code, errors), (0, ""))
        fb.verify.assert_called_once()
        fb.devices.assert_not_called()

    def test_final_fastboot_error_happens_after_upload(self):
        import subprocess

        from pixel_eub.images import Sources

        for failure in (
            EubError("fastboot devices failed"),
            subprocess.TimeoutExpired("fastboot", 3),
        ):
            with self.subTest(failure=failure):
                link = FakeLink([b"eub:req:09865001:BL3B\nC"])
                fb = Mock()
                fb.verify.side_effect = failure
                with (
                    patch(
                        "pixel_eub.cli.prepare",
                        return_value=Sources(
                            [{"bl31": signed("BL31", b"last")}], "zuma"
                        ),
                    ),
                    patch("pixel_eub.cli.Fastboot", return_value=fb),
                    patch(
                        "pixel_eub.cli.find_port",
                        side_effect=[Port("first"), NoDevice("not found")],
                    ),
                    patch("pixel_eub.cli.open_port", return_value=link),
                ):
                    code, _, errors = self.run_cli(
                        "boot", "--device", "husky", "--reconnect-wait", "0.01"
                    )
                self.assertEqual(code, 1)
                self.assertEqual(link.output[8:-2], b"last")
                self.assertIn("NOT confirmed", errors)
                fb.verify.assert_called_once()

    def test_disconnect_is_not_success_and_write_timeout_not_hidden(self):
        import serial

        from pixel_eub.images import Sources

        sources = Sources([{"bl1": signed("APBL")}], "zuma")
        fb = Mock()
        fb.devices.return_value = set()
        fb.verify.return_value = None
        link = FakeLink([b"eub:req:09865001:BL1\nC"])
        with (
            patch("pixel_eub.cli.prepare", return_value=sources),
            patch("pixel_eub.cli.Fastboot", return_value=fb),
            patch(
                "pixel_eub.cli.find_port",
                side_effect=[Port("first"), NoDevice("not found")],
            ),
            patch("pixel_eub.cli.open_port", return_value=link),
        ):
            code, output, _ = self.run_cli(
                "boot",
                "--device",
                "husky",
                "--reconnect-wait",
                "0.01",
                "--verify-timeout",
                "0.01",
            )
        self.assertEqual(code, 1)
        self.assertNotIn("fastboot confirmed", output)
        link = FakeLink([b"eub:req:09865001:BL1\nC"])
        link.write = Mock(side_effect=serial.SerialTimeoutException("write timed out"))
        with (
            patch("pixel_eub.cli.prepare", return_value=sources),
            patch("pixel_eub.cli.Fastboot", return_value=fb),
            patch("pixel_eub.cli.find_port", return_value=Port("first")),
            patch("pixel_eub.cli.open_port", return_value=link),
        ):
            code, _, errors = self.run_cli("boot", "--device", "husky")
        self.assertEqual(code, 1)
        self.assertIn("write timed out", errors)

    def test_only_override_enables_rom_panic_confirmation(self):
        from pixel_eub.cli import confirm_rom_panic
        from pixel_eub.images import Sources

        for override in (False, True):
            with self.subTest(override=override):

                def uploaded(
                    _link, _sources, session, _soc, override=override, **kwargs
                ):
                    self.assertIs(
                        kwargs["confirm_panic"], confirm_rom_panic if override else None
                    )
                    self.assertFalse(kwargs["allow_cached_identity"])
                    session.attempts["BL1"] = 1
                    session.sent.append("BL1")

                fb = Mock()
                fb.verify.return_value = "RECOVERED"
                sequence = [Port("first")]
                if override:
                    sequence.append(Port("first"))
                sequence.append(NoDevice("not found"))
                with (
                    patch("pixel_eub.cli.probe", return_value="09865001"),
                    patch("pixel_eub.cli.prepare", return_value=Sources([], "zuma")),
                    patch("pixel_eub.cli.serve", side_effect=uploaded),
                    patch("pixel_eub.cli.Fastboot", return_value=fb),
                    patch("pixel_eub.cli.find_port", side_effect=sequence),
                    patch(
                        "pixel_eub.cli.open_port",
                        side_effect=lambda *_a, **_kw: FakeLink([]),
                    ),
                    patch("builtins.input") as prompt,
                ):
                    args = ["boot", "--device", "husky", "--reconnect-wait", "0.01"]
                    if override:
                        args.append("--override")
                    code, _, errors = self.run_cli(*args)
                self.assertEqual((code, errors), (0, ""))
                prompt.assert_not_called()

    def test_override_panic_after_reconnect_without_fresh_identity_offers_choice(self):
        from pixel_eub.images import Sources
        from pixel_eub.protocol import frame

        for answer in ("yes", "NO"):
            with self.subTest(answer=answer):
                sources = Sources(
                    [
                        {
                            "bl1": signed("APBL"),
                            "pbl": signed("EPBL", b"pbl"),
                            "bl2": signed("BL2", b"bl2"),
                            "abl": signed("ABL", b"abl"),
                            "bl31": signed("BL31", b"bl31"),
                        }
                    ],
                    "gs101",
                )
                first = FakeLink([b"eub:req:09845001:bl1\nC"])
                second = FakeLink(
                    [b"00000001\ne0000080\na0031111\nirom_booting_failure::\n"]
                )
                fb = Mock()
                fb.verify.return_value = "RECOVERED"
                with (
                    patch("pixel_eub.cli.probe", return_value="09845001"),
                    patch("pixel_eub.cli.prepare", return_value=sources),
                    patch(
                        "pixel_eub.cli.find_port",
                        side_effect=[
                            Port("first", "USBID"),
                            Port("first", "USBID"),
                            Port("second", "USBID"),
                            NoDevice("not found"),
                        ],
                    ),
                    patch(
                        "pixel_eub.cli.open_port",
                        side_effect=[FakeLink([]), first, second],
                    ),
                    patch("pixel_eub.cli.Fastboot", return_value=fb) as fastboot,
                    patch("builtins.input") as prompt,
                ):

                    def choose(message, answer=answer, second=second):
                        self.assertIn(
                            "Continue uploading despite the ROM panic", message
                        )
                        self.assertFalse(second.output)
                        fastboot.assert_not_called()
                        return answer

                    prompt.side_effect = choose
                    code, output, errors = self.run_cli(
                        "boot",
                        "--device",
                        "Pixel 6a",
                        "--pack",
                        "gs201",
                        "--override",
                        "--verbose",
                        "--reconnect-wait",
                        "0.01",
                    )
                prompt.assert_called_once()
                self.assertIn("using cached session identity 09845001 (gs101)", output)
                self.assertEqual(first.output, frame(sources.payload("BL1")))
                if answer == "yes":
                    self.assertEqual((code, errors), (0, ""))
                    self.assertTrue(
                        second.output.startswith(frame(sources.payload("EPBL")))
                    )
                    self.assertIn("Uploaded BL3B", output)
                else:
                    self.assertEqual(code, 1)
                    self.assertIn("cancelled", errors)
                    self.assertFalse(second.output)
                    fastboot.assert_not_called()


class TransportTests(unittest.TestCase):
    @unittest.skipIf(__import__("os").name == "nt", "POSIX termios error")
    def test_termios_error_is_normalized(self):
        import termios

        raw = Mock()
        raw.flush.side_effect = termios.error(5, "device disappeared")
        with self.assertRaises(OSError) as failure:
            SerialLink(raw).flush()
        self.assertEqual(failure.exception.errno, 5)

    def test_discovery_filters_vid_pid_and_macos_twins(self):
        def port(name, vid, pid):
            return Mock(
                device=name,
                vid=vid,
                pid=pid,
                serial_number="S",
                location="L",
                description="CDC",
            )

        inventory = [
            port("/dev/cu.usbmodem1", 0x18D1, 0x4F00),
            port("/dev/tty.usbmodem1", 0x18D1, 0x4F00),
            port("other", 123, 456),
        ]
        with patch("serial.tools.list_ports.comports", return_value=inventory):
            self.assertEqual([p.device for p in ports()], ["/dev/cu.usbmodem1"])
            self.assertEqual(len(ports(True)), 2)

    def test_multiple_phones_and_reconnect_identity(self):
        devices = [Port("a", "A", "1"), Port("b", "B", "2")]
        with patch("pixel_eub.transport.ports", return_value=devices):
            with self.assertRaisesRegex(EubError, "Multiple"):
                find_port()
            self.assertEqual(find_port(original=Port("old", "B")).device, "b")
            with self.assertRaisesRegex(EubError, "not found"):
                find_port(original=Port("old", "C"))

    def test_final_fastboot_checks_selected_serial_and_product(self):
        with patch("pixel_eub.transport.shutil.which", return_value="fastboot"):
            fb = Fastboot(serial="NEW")
        fb.devices = Mock(return_value={"OLD", "NEW"})
        fb.run = Mock(
            side_effect=[
                (0, "(bootloader) product: husky\n"),
                (0, "(bootloader) is-userspace: no\n"),
            ]
        )
        from pixel_eub.models import model_for

        self.assertEqual(fb.verify(model_for("husky")), "NEW")
        fb.run = Mock(return_value=(0, "product: shiba\n"))
        with self.assertRaisesRegex(EubError, "does not confirm"):
            fb.verify(model_for("husky"))
        fb.run = Mock(side_effect=[(0, "product: husky\n"), (0, "is-userspace: yes\n")])
        with self.assertRaisesRegex(EubError, "fastbootd"):
            fb.verify(model_for("husky"))

    def test_absent_or_multiple_fastboot_not_success(self):
        from pixel_eub.models import model_for

        with patch("pixel_eub.transport.shutil.which", return_value="fastboot"):
            fb = Fastboot()
        fb.devices = Mock(return_value=set())
        self.assertIsNone(fb.verify(model_for("husky")))
        fb.devices = Mock(return_value={"NEW1", "NEW2"})
        with self.assertRaisesRegex(EubError, "Several"):
            fb.verify(model_for("husky"))
