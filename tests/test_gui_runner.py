import queue
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pixel_eub.cli import main
from pixel_eub.gui import runner


def collect(job_factory):
    events = queue.Queue()
    job = job_factory(events.put)
    return job, events


def drain(events, until=runner.Exit, timeout=30):
    seen = []
    while True:
        event = events.get(timeout=timeout)
        seen.append(event)
        if isinstance(event, until):
            return seen


class OutputParserTests(unittest.TestCase):
    def test_progress_events_and_split_reads(self):
        parser = runner.OutputParser()
        text = (
            "[1/3] Checking Pixel ROM Recovery USB connection (18d1:4f00)…\n"
            "ROM identity verified: 0985500000000000 (gs201)\n"
            "ROM requests BL1\nSending BL1: 12288 bytes\n"
            "Uploaded BL1: 12288 bytes\nROM acknowledged BL1\n"
            "Sending DPM: 4096 bytes (empty placeholder: no signed DPM image available)\n"
            "Bootloader fastboot confirmed: ABC (cheetah). You can now use normal "
            "flashing tools.\n"
        )
        events = []
        for index in range(0, len(text), 7):  # Lines arrive in arbitrary chunks.
            events += parser.feed(text[index : index + 7])
        kinds = [type(e).__name__ for e in events if not isinstance(e, runner.Line)]
        self.assertEqual(
            kinds,
            [
                "Phase",
                "Identity",
                "Stage",
                "Stage",
                "Stage",
                "Stage",
                "Stage",
                "Outcome",
            ],
        )
        stages = [
            (e.name, e.state, e.size) for e in events if isinstance(e, runner.Stage)
        ]
        self.assertEqual(
            stages,
            [
                ("BL1", "requested", None),
                ("BL1", "sending", 12288),
                ("BL1", "uploaded", 12288),
                ("BL1", "acknowledged", None),
                ("DPM", "sending", 4096),
            ],
        )
        identity = next(e for e in events if isinstance(e, runner.Identity))
        self.assertEqual(identity.soc, "gs201")
        self.assertTrue(events[-1].success)

    def test_prompt_without_newline_carries_warning_context(self):
        parser = runner.OutputParser()
        parser.feed("ROM requests BL2\nWARNING: ROM PANIC: irom_booting_failure::\n")
        parser.feed("Experimental remaining chain: EPBL -> BL2\n")
        parser.feed("WARNING: experimental forced boot chain. Remaining images…\n")
        events = parser.feed(
            "Continue uploading despite the ROM panic? [YES/NO] (NO): "
        )
        prompt = events[-1]
        self.assertIsInstance(prompt, runner.Prompt)
        self.assertEqual(prompt.kind, "panic")
        self.assertEqual(
            prompt.context[0], "WARNING: ROM PANIC: irom_booting_failure::"
        )
        self.assertIn("Experimental remaining chain: EPBL -> BL2", prompt.context)
        self.assertEqual(parser.partial, "")

    def test_waiting_status_line(self):
        events = runner.OutputParser().feed(
            "Waiting for the ROM after GSA1: 95 s. Verification ...\n"
        )
        self.assertIsInstance(events[-1], runner.Waiting)

    def test_release_buttons_hint(self):
        events = runner.OutputParser().feed(
            "ROM booted USB BL1 and requests DPM: you can release the buttons now.\n"
        )
        self.assertIsInstance(events[-1], runner.Hint)

    def test_override_prompt_and_error_outcome(self):
        parser = runner.OutputParser()
        events = parser.feed(
            "WARNING: INCOMPATIBLE DEVICE / IMAGE\nmismatch\n"
            "Continue with these incompatible images? [YES/NO] (NO): "
        )
        self.assertEqual(events[-1].kind, "override")
        events = parser.feed("pixel-eub: Override cancelled.\n")
        self.assertFalse(events[-1].success)
        self.assertEqual(events[-1].text, "Override cancelled.")


class ArgumentTests(unittest.TestCase):
    def test_boot_arguments_follow_options(self):
        options = runner.Options(device="cheetah", port="/dev/ttyACM0")
        self.assertEqual(
            runner.boot_args(options),
            [
                "boot",
                "--device",
                "cheetah",
                "--pack",
                "auto",
                "--port",
                "/dev/ttyACM0",
                "--wait",
                "--wait-timeout",
                "60",
                "--reconnect-wait",
                "15",
                "--idle-timeout",
                "15",
                "--verify-timeout",
                "20",
                "--ack-timeout",
                "600",
                "--no-port-recovery",
            ],
        )
        options = runner.Options(
            device="husky",
            pack="path",
            pack_path="/packs/r.zip",
            image="/f.zip",
            directory="/stages",
            build="latest",
            epbl="split",
            checksum="sum16",
            override=True,
            allow_experimental=True,
            port_recovery=True,
            cache_dir="/cache",
            offline=True,
            refresh=True,
        )
        dry = runner.boot_args(options, dry_run=True)
        self.assertEqual(
            dry[:5], ["boot", "--device", "husky", "--pack", "/packs/r.zip"]
        )
        for expected in (
            ["--image", "/f.zip"],
            ["--dir", "/stages"],
            ["--build", "latest"],
            ["--epbl", "split"],
            ["--checksum", "sum16"],
            ["--cache-dir", "/cache"],
        ):
            index = dry.index(expected[0])
            self.assertEqual(dry[index : index + 2], expected)
        for flag in ("--override", "--allow-experimental", "--refresh", "--dry-run"):
            self.assertIn(flag, dry)
        options.dump_usb = options.capture = True
        self.assertNotIn("--dump-usb", runner.boot_args(options))  # No folder yet.
        options.research_dir = "/research"
        real = runner.boot_args(options)
        self.assertEqual(real[real.index("--dump-usb") + 1], "/research")
        self.assertEqual(real[real.index("--capture") + 1], "/research")
        dry = runner.boot_args(options, dry_run=True)
        self.assertNotIn("--dump-usb", dry)
        self.assertNotIn("--capture", dry)
        options.listen_after_failure = 10
        real = runner.boot_args(options)
        self.assertEqual(real[real.index("--listen-after-failure") + 1], "10")
        self.assertNotIn(
            "--listen-after-failure", runner.boot_args(options, dry_run=True)
        )
        options.no_usb_check = True
        self.assertIn("--no-usb-check", runner.boot_args(options, dry_run=True))
        options.allow_dpm_first = True
        self.assertIn("--allow-dpm-first", runner.boot_args(options))
        self.assertNotIn("--allow-dpm-first", runner.boot_args(options, dry_run=True))
        # USB-only options are not passed to a dry run.
        self.assertNotIn("--port-recovery", dry)
        self.assertNotIn("--wait", dry)
        self.assertIn("--port-recovery", runner.boot_args(options))

    def test_boot_requires_model_and_local_pack(self):
        with self.assertRaisesRegex(ValueError, "model"):
            runner.boot_args(runner.Options())
        with self.assertRaisesRegex(ValueError, "pack"):
            runner.boot_args(runner.Options(device="cheetah", pack="path"))

    def test_settings_round_trip_never_persists_risky_switches(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "gui.json"
            options = runner.Options(
                device="shiba",
                idle_timeout=30,
                override=True,
                port_recovery=True,
                allow_experimental=True,
                allow_dpm_first=True,
                no_usb_check=True,
            )
            runner.save_options(options, path)
            loaded = runner.load_options(path)
            self.assertEqual((loaded.device, loaded.idle_timeout), ("shiba", 30))
            self.assertFalse(loaded.override or loaded.port_recovery)
            self.assertFalse(loaded.allow_experimental or loaded.allow_dpm_first)
            self.assertFalse(loaded.no_usb_check)
            path.write_text('{"override": true, "idle_timeout": -1, "wait": "yes"}')
            loaded = runner.load_options(path)
            self.assertEqual((loaded.override, loaded.idle_timeout), (False, 15))
            self.assertTrue(loaded.wait)
            path.write_text("not json")
            self.assertEqual(runner.load_options(path), runner.Options())

    def test_output_parsers(self):
        self.assertEqual(
            runner.parse_doctor(["pixel-eub doctor", "[OK] pyserial 3.5", "[FAIL] x"]),
            [runner.DoctorCheck("OK", "pyserial 3.5"), runner.DoctorCheck("FAIL", "x")],
        )
        builds = runner.parse_builds(
            [
                "bp1a.250505.001\t15.0.0 (BP1A.250505.001, May 2025)",
                "  SHA-256 " + "a" * 64,
                "  https://dl.google.com/x.zip",
            ]
        )
        self.assertEqual(builds[0].build, "bp1a.250505.001")
        self.assertEqual(builds[0].sha256, "a" * 64)
        stages = runner.parse_inspect(
            [
                "bl1.bin | BL1 (APBL) | 12288 bytes | USB boot: no | family: unknown "
                "| flags 0x0400",
                "  ! retail BL1: no signed USB-boot flag",
                "sources/gs201/bl2.img | BL2 (BL2) | 536576 bytes | USB boot: yes "
                "| family: gs201 | flags 0x0401",
                "x.bin:ufs | not a stage image | 100 bytes",
                "Read-only check. ...",
            ]
        )
        self.assertEqual([s.size for s in stages], [12288, 536576, 100])
        self.assertEqual((stages[0].usb, stages[0].family), (False, ""))
        self.assertEqual(stages[0].notes, ["retail BL1: no signed USB-boot flag"])
        self.assertEqual((stages[1].usb, stages[1].family), (True, "gs201"))
        self.assertFalse(stages[2].is_stage)
        ports = runner.parse_detect(["/dev/ttyACM0\t18d1:4f00\t-\tPixel ROM"])
        self.assertEqual(ports[0].device, "/dev/ttyACM0")
        self.assertEqual(ports[0].usb, "18d1:4f00")


class JobTests(unittest.TestCase):
    """Run the real CLI as a child, exactly as the GUI does."""

    def test_models_command_streams_lines_and_exit_code(self):
        job, events = collect(lambda put: runner.Job(["models"], put))
        seen = drain(events)
        lines = [e.text for e in seen if isinstance(e, runner.Line)]
        self.assertTrue(any(line.startswith("Pixel 7 Pro") for line in lines))
        self.assertEqual(seen[-1].code, 0)

    def run_override_prompt(self, answer):
        with tempfile.TemporaryDirectory() as cache:
            args = [
                "boot",
                "--device",
                "cheetah",
                "--pack",
                "zuma/husky",
                "--override",
                "--dry-run",
                "--offline",
                "--cache-dir",
                cache,
            ]
            job, events = collect(lambda put: runner.Job(args, put))
            prompt = drain(events, until=runner.Prompt)[-1]
            self.assertEqual(prompt.kind, "override")
            self.assertIn("WARNING: INCOMPATIBLE DEVICE / IMAGE", prompt.context)
            job.answer(answer)
            return drain(events)

    def test_real_override_prompt_answered_no(self):
        seen = self.run_override_prompt(False)
        self.assertEqual(seen[-1].code, 1)
        outcome = [e for e in seen if isinstance(e, runner.Outcome)][-1]
        self.assertIn("Override cancelled", outcome.text)

    def test_real_override_prompt_answered_yes_reaches_next_step(self):
        seen = self.run_override_prompt(True)
        lines = [e.text for e in seen if isinstance(e, runner.Line)]
        self.assertIn("Override confirmed: continuing with the selected images.", lines)
        # Offline with an empty cache stops before any download or USB access.
        self.assertTrue(any("rerun without --offline" in line for line in lines))
        self.assertEqual(seen[-1].code, 1)

    @unittest.skipUnless(sys.platform != "win32", "POSIX process groups")
    def test_interrupt_reports_cli_cancellation(self):
        def popen(command, **kwargs):
            script = (
                "import time\n"
                "try:\n    print('ready', flush=True); time.sleep(30)\n"
                "except KeyboardInterrupt:\n    print('Interrupted.'); raise SystemExit(130)\n"
            )
            import subprocess

            return subprocess.Popen([sys.executable, "-c", script], **kwargs)

        job, events = collect(lambda put: runner.Job(["boot"], put, popen=popen))
        self.assertEqual(drain(events, until=runner.Line)[-1].text, "ready")
        job.interrupt()
        seen = drain(events)
        self.assertEqual(seen[-1].code, 130)
        self.assertFalse(job.running)


class GuiCommandTests(unittest.TestCase):
    def test_missing_gtk_reports_install_hint(self):
        with (
            patch.dict(sys.modules, {"gi": None}),
            patch("sys.stderr") as stderr,
        ):
            code = main(["gui"])
        self.assertEqual(code, 1)
        written = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn("PyGObject", written)
