import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pixel_eub import EubError
from pixel_eub.cli import main
from pixel_eub.gs101 import REQUIRED, validate_profile


class GS101Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.profile = Path(self.tmp.name)
        for name in REQUIRED:
            path = self.profile / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"stage")
        (self.profile / "metadata.json").write_text(json.dumps({"device": "bluejay"}))

    def test_profile_checks_model_and_required_stages(self):
        self.assertEqual(validate_profile("bluejay", self.profile), self.profile)
        with self.assertRaises(EubError):
            validate_profile("oriole", self.profile)
        (self.profile / REQUIRED[0]).unlink()
        with self.assertRaises(EubError):
            validate_profile("bluejay", self.profile)

    def test_cli_requires_risk_acknowledgement(self):
        with patch("pixel_eub.gs101.run") as run:
            self.assertEqual(main(["gs101", "--device", "bluejay", "--profile", str(self.profile)]), 1)
            run.assert_not_called()
            run.return_value = 0
            self.assertEqual(main(["gs101", "--device", "bluejay", "--profile", str(self.profile), "--acknowledge-risk"]), 0)
            run.assert_called_once()


class GUIArgumentTest(unittest.TestCase):
    def test_gs101_command_and_dry_run(self):
        from pixel_eub.gui.runner import Options, gs101_args

        options = Options(device="oriole", gs101_profile="/tmp/my profile")
        self.assertEqual(gs101_args(options), ["gs101", "--device", "oriole", "--profile", "/tmp/my profile", "--acknowledge-risk"])
        self.assertEqual(gs101_args(options, dry_run=True)[-1], "--check-profile")
        with self.assertRaises(ValueError):
            gs101_args(Options(device="oriole"))
