import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pixel_eub import EubError
from pixel_eub.cli import main, parser
from pixel_eub.transport import Port


class DoctorTests(unittest.TestCase):
    def run_doctor(self, cache):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["doctor", "--cache-dir", str(cache)])
        return code, output.getvalue()

    def test_offline_diagnostics_do_not_open_phone_or_create_cache(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            patch(
                "pixel_eub.doctor.serial_module", return_value=Mock(__version__="3.5")
            ),
            patch("pixel_eub.doctor.ports", return_value=[]),
            patch("pixel_eub.doctor.shutil.which", return_value=None),
            patch("pixel_eub.cli.open_port") as opened,
            patch("pixel_eub.cli.Fastboot") as fastboot,
            patch("pixel_eub.cli.PortAccessRecovery") as repair,
        ):
            cache = Path(temp) / "new-cache"
            code, output = self.run_doctor(cache)
            self.assertEqual(code, 0)
            self.assertIn("fastboot not found", output)
            self.assertIn("No Pixel ROM USB port", output)
            self.assertFalse(cache.exists())
            opened.assert_not_called()
            fastboot.assert_not_called()
            repair.assert_not_called()

    def test_missing_dependency_still_reports_cache_and_fastboot(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            patch(
                "pixel_eub.doctor.serial_module",
                side_effect=EubError("pyserial missing"),
            ),
            patch("pixel_eub.doctor.shutil.which", return_value=None),
        ):
            code, output = self.run_doctor(Path(temp))
        self.assertEqual(code, 1)
        self.assertIn("pyserial missing", output)
        self.assertIn("Cache path", output)
        self.assertIn("fastboot not found", output)

    @unittest.skipUnless(os.name == "posix", "POSIX serial permissions")
    def test_denied_port_is_reported_without_leaking_serial_number(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            patch("pixel_eub.doctor.serial_module"),
            patch(
                "pixel_eub.doctor.ports",
                return_value=[Port("/dev/ttyACM0", "PRIVATE-SERIAL")],
            ),
            patch(
                "pixel_eub.doctor.os.access",
                side_effect=lambda path, mode: str(path) != "/dev/ttyACM0",
            ),
        ):
            code, output = self.run_doctor(Path(temp))
        self.assertEqual(code, 1)
        self.assertIn("no read/write permission", output)
        self.assertNotIn("PRIVATE-SERIAL", output)

    def test_top_level_help_contains_command_options_and_explanations(self):
        help_text = parser().format_help()
        for text in (
            "--wait-timeout",
            "--checksum",
            "--override",
            "--verbose",
            "doctor",
            "seconds without ROM activity",
            "raw ROM messages",
        ):
            self.assertIn(text, help_text)

    def test_top_level_help_lists_shared_options_once_and_preserves_command_help(self):
        root = parser()
        before = root.command_parsers["builds"].format_help()
        help_text = root.format_help()
        self.assertEqual(help_text.count("usage:"), 1)
        self.assertEqual(help_text.count("\noptions:"), 1)
        self.assertEqual(help_text.count("--cache-dir CACHE_DIR"), 1)
        self.assertEqual(help_text.count("--offline"), 1)
        self.assertIn(
            "[commands: doctor, builds, fetch, boot]", " ".join(help_text.split())
        )
        self.assertIn("builds/fetch: latest by default", help_text)
        self.assertEqual(root.command_parsers["builds"].format_help(), before)
