import contextlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from pixel_eub import EubError
from pixel_eub.cli import main, parser, prepare
from pixel_eub.compatibility import CompatibilityOverride
from pixel_eub.images import Sources, load_bootloader
from pixel_eub.models import model_for
from pixel_eub.protocol import Session, frame, serve
from pixel_eub.transport import NoDevice, Port
from tests.helpers import FakeLink, fbpk, signed


class OverridePolicyTests(unittest.TestCase):
    def test_default_denies_without_prompt(self):
        with (
            patch("builtins.input") as ask,
            self.assertRaisesRegex(EubError, "--override"),
        ):
            CompatibilityOverride().check("gs101 vs gs201")
        ask.assert_not_called()

    def test_only_full_yes_approves_and_confirmation_is_cached(self):
        for answer in ("YES", "yes", " Yes "):
            with self.subTest(answer=answer), contextlib.redirect_stdout(io.StringIO()):
                policy = CompatibilityOverride(True)
                with patch("builtins.input", return_value=answer) as ask:
                    policy.check("gs101 vs gs201")
                    policy.check("gs101 vs gs201")
                ask.assert_called_once()
                self.assertEqual(policy.approved, {"gs101 vs gs201"})

    def test_no_empty_invalid_and_eof_cancel(self):
        for answer in ("NO", "no", "", "y", "anything", EOFError()):
            with self.subTest(answer=answer), contextlib.redirect_stdout(io.StringIO()):
                policy = CompatibilityOverride(True)
                options = (
                    {"side_effect": answer}
                    if isinstance(answer, Exception)
                    else {"return_value": answer}
                )
                with (
                    patch("builtins.input", **options),
                    self.assertRaisesRegex(EubError, "cancelled"),
                ):
                    policy.check("gs101 vs gs201")
                self.assertEqual(policy.approved, set())

    def test_override_never_allows_changed_phone_or_unknown_chip(self):
        policy = CompatibilityOverride(True)
        session = Session("09845001", "gs101", compatibility=policy)
        with (
            patch("builtins.input") as ask,
            self.assertRaisesRegex(EubError, "different phone"),
        ):
            session.note_identity("09855001", "gs201")
        ask.assert_not_called()
        with (
            patch("builtins.input") as ask,
            self.assertRaisesRegex(EubError, "Unknown"),
        ):
            Session(compatibility=policy).note_identity("01234001", "gs201")
        ask.assert_not_called()

    def test_protocol_uses_actual_rom_soc_for_slicing_after_confirmation(self):
        policy = CompatibilityOverride(True)
        session = Session(compatibility=policy)
        with (
            patch("builtins.input", return_value="YES"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            session.note_identity("09845001", "zuma")
        image = signed("EPBL", b"full-body")
        sources = Sources([{"pbl": image}], "zuma")
        link = FakeLink([b"eub:req:09845001:EPBL\nC"])
        with patch("builtins.input") as ask, self.assertRaises(OSError):
            serve(link, sources, session, "zuma", report=lambda _: None)
        self.assertEqual(link.output, frame(image))
        ask.assert_not_called()

    def test_override_does_not_ignore_rom_signature_rejection(self):
        policy = CompatibilityOverride(True)
        session = Session(compatibility=policy)
        with (
            patch("builtins.input", return_value="YES"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            session.note_identity("09845001", "gs201")
        link = FakeLink([b"eub:req:09845001:BL1\nC", b"bl1 header fail\n"])
        with self.assertRaisesRegex(EubError, "ROM rejected"):
            serve(
                link,
                Sources([{"bl1": signed("APBL")}], "gs201"),
                session,
                "gs201",
                report=lambda _: None,
            )


class OverrideImageTests(unittest.TestCase):
    def test_fbpk_mismatches_require_one_confirmation(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bootloader-panther-test.img"
            path.write_bytes(fbpk([("bl1", signed("APBL"))], "gs201", "panther"))
            with self.assertRaises(EubError):
                load_bootloader(path, model_for("Pixel 6"))
            with (
                patch("builtins.input", return_value="YES") as ask,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                pack = load_bootloader(
                    path, model_for("Pixel 6"), CompatibilityOverride(True)
                )
            self.assertEqual(pack.platform, "gs201")
            ask.assert_called_once()

    def test_corruption_and_retail_usb_flag_cannot_be_overridden(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "wrong.img"
            data = bytearray(fbpk([("bl1", signed("APBL"))], "gs201", "panther"))
            data[-1] ^= 1
            path.write_bytes(data)
            with (
                patch("builtins.input") as ask,
                self.assertRaisesRegex(EubError, "CRC32"),
            ):
                load_bootloader(path, model_for("oriole"), CompatibilityOverride(True))
            ask.assert_not_called()
        with self.assertRaisesRegex(EubError, "USB-bootable"):
            Sources([{"bl1": signed("APBL", flags=0)}], "gs101").payload("BL1")

    def test_named_foreign_pack_is_rejected_before_download_without_override(self):
        args = parser().parse_args(["boot", "--device", "Pixel 6", "--pack", "gs201"])
        cache = Mock()
        with (
            patch("builtins.input") as ask,
            self.assertRaisesRegex(EubError, "gs201.*gs101"),
        ):
            prepare(args, model_for(args.device), cache)
        cache.download.assert_not_called()
        ask.assert_not_called()

    def test_named_foreign_pack_uses_requested_archive_subdirectory_after_yes(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "recovery.zip"
            image = signed("APBL", b"gs201-image")
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("sources/gs201/bl1.img", image)
                archive.writestr("sources/zuma/husky/bl1.img", signed("APBL", b"wrong"))
            args = parser().parse_args(
                ["boot", "--device", "Pixel 6", "--pack", "gs201", "--override"]
            )
            cache = Mock()
            cache.download.return_value = path
            with (
                patch("builtins.input", return_value="YES") as ask,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                sources = prepare(
                    args, model_for(args.device), cache, CompatibilityOverride(True)
                )
            self.assertEqual(sources.payload("BL1"), image)
            ask.assert_called_once()

    def test_known_directory_and_same_soc_wrong_model_require_confirmation(self):
        for arguments, message in (
            (
                [
                    "--device",
                    "Pixel 6",
                    "--dir",
                    "sources/gs201",
                    "--pack",
                    "none",
                ],
                "gs201",
            ),
            (["--device", "Pixel 8 Pro", "--pack", "zuma/shiba"], "normally uses"),
        ):
            args = parser().parse_args(["boot"] + arguments)
            with (
                self.subTest(arguments=arguments),
                self.assertRaisesRegex(EubError, message),
            ):
                prepare(args, model_for(args.device), Mock())


class OverrideCliTests(unittest.TestCase):
    def run_cli(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(list(args))
        return code, output.getvalue(), errors.getvalue()

    def test_yes_is_requested_with_probe_closed_before_preparing_and_uploading(self):
        initial = FakeLink([b"exynos_usb_booting:eub:09845001\n"])
        upload = FakeLink([b"eub:req:09845001:BL1\nC"])
        image = signed("APBL")
        fb = Mock()
        fb.verify.return_value = "EMU"

        def answer(_prompt):
            self.assertTrue(initial.closed)
            self.assertEqual(initial.output, b"")
            self.assertEqual(upload.output, b"")
            return "YES"

        with (
            patch("builtins.input", side_effect=answer) as ask,
            patch(
                "pixel_eub.cli.find_port",
                side_effect=[
                    Port("initial", "USB"),
                    Port("reopened", "USB"),
                    NoDevice("gone"),
                ],
            ),
            patch("pixel_eub.cli.open_port", side_effect=[initial, upload]),
            patch(
                "pixel_eub.cli.prepare", return_value=Sources([{"bl1": image}], "gs201")
            ),
            patch("pixel_eub.cli.Fastboot", return_value=fb),
        ):
            code, output, errors = self.run_cli(
                "boot",
                "--device",
                "Pixel 7 Pro",
                "--pack",
                "auto",
                "--override",
                "--reconnect-wait",
                "0.01",
            )
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("Connected ROM is gs101", output)
        self.assertIn("requires gs201", output)
        self.assertEqual(upload.output, frame(image))
        ask.assert_called_once()

    def test_no_or_eof_cancels_before_image_preparation_or_payload(self):
        for answer in ("NO", "", EOFError()):
            with self.subTest(answer=answer):
                initial = FakeLink([b"exynos_usb_booting:eub:09845001\n"])
                options = (
                    {"side_effect": answer}
                    if isinstance(answer, Exception)
                    else {"return_value": answer}
                )
                with (
                    patch("builtins.input", **options),
                    patch(
                        "pixel_eub.cli.find_port", return_value=Port("initial", "USB")
                    ),
                    patch("pixel_eub.cli.open_port", return_value=initial) as opened,
                    patch("pixel_eub.cli.prepare") as prepare_images,
                    patch("pixel_eub.cli.Fastboot") as fb,
                ):
                    code, _, errors = self.run_cli(
                        "boot", "--device", "Pixel 7 Pro", "--override"
                    )
                self.assertEqual(code, 1)
                self.assertIn("cancelled", errors)
                self.assertEqual(initial.output, b"")
                opened.assert_called_once()
                prepare_images.assert_not_called()
                fb.assert_not_called()

    def test_without_override_mismatched_rom_gets_no_payload(self):
        link = FakeLink([b"eub:req:09845001:BL1\nC"])
        with (
            patch("builtins.input") as ask,
            patch("pixel_eub.cli.find_port", return_value=Port("port")),
            patch("pixel_eub.cli.open_port", return_value=link),
            patch(
                "pixel_eub.cli.prepare",
                return_value=Sources([{"bl1": signed("APBL")}], "gs201"),
            ),
            patch("pixel_eub.cli.Fastboot") as fb,
        ):
            code, _, errors = self.run_cli("boot", "--device", "Pixel 7 Pro")
        self.assertEqual(code, 1)
        self.assertIn("--override", errors)
        self.assertEqual(link.output, b"")
        ask.assert_not_called()
        fb.assert_not_called()

    def test_matching_device_with_override_does_not_prompt(self):
        initial = FakeLink([b"exynos_usb_booting:eub:09855001\n"])
        upload = FakeLink([b"eub:req:09855001:BL3B\nC"])
        fb = Mock()
        fb.verify.return_value = "EMU"
        with (
            patch("builtins.input") as ask,
            patch(
                "pixel_eub.cli.find_port",
                side_effect=[
                    Port("initial", "USB"),
                    Port("reopened", "USB"),
                    NoDevice("gone"),
                ],
            ),
            patch("pixel_eub.cli.open_port", side_effect=[initial, upload]),
            patch(
                "pixel_eub.cli.prepare",
                return_value=Sources([{"bl31": signed("BL31")}], "gs201"),
            ),
            patch("pixel_eub.cli.Fastboot", return_value=fb),
        ):
            code, _, errors = self.run_cli(
                "boot",
                "--device",
                "Pixel 7 Pro",
                "--override",
                "--reconnect-wait",
                "0.01",
            )
        self.assertEqual((code, errors), (0, ""))
        ask.assert_not_called()
