import struct
import unittest
from unittest.mock import Mock, patch

from pixel_eub import EubError
from pixel_eub.images import Sources
from pixel_eub.models import choose_model, model_for, soc_for
from pixel_eub.protocol import (
    MessageReader,
    Session,
    frame,
    identity,
    probe,
    serve,
    write_all,
)
from tests.helpers import FakeLink, signed


class ProtocolTests(unittest.TestCase):
    def test_model_aliases_and_unknown_models(self):
        for name in (
            "husky",
            "Pixel 8 Pro",
            "pixel-8-pro",
            "Google Pixel 8 Pro",
            "PIXEL8PRO",
        ):
            self.assertEqual(model_for(name).codename, "husky")
        with self.assertRaises(EubError):
            model_for("Pixel 99")

    def test_rom_chip_ids_and_noninteractive_ambiguity(self):
        self.assertEqual(soc_for("09865001"), "zuma")
        self.assertEqual(soc_for("09855001"), "gs201")
        for serial in ("", "09875001", "x9865001", "01234001"):
            with self.assertRaises(EubError):
                soc_for(serial)
        with self.assertRaisesRegex(EubError, "exact model"):
            choose_model("zuma", False)
        with patch("builtins.print"):
            self.assertEqual(
                choose_model("zuma", True, lambda _: "2").codename, "husky"
            )
            for answer in ("0", "-1", "500", "x"):
                with self.assertRaises(EubError):
                    choose_model("zuma", True, lambda _, answer=answer: answer)

    def test_fragmented_message_and_bare_clear_to_send(self):
        data = b"exynos_usb_booting:eub:09865001\r\neub:req:09865001:BL1\r\nC"
        reader, messages = MessageReader(), []
        for value in data:
            messages.extend(reader.feed(bytes([value])))
        self.assertEqual(
            messages, ["exynos_usb_booting:eub:09865001", "eub:req:09865001:BL1", "C"]
        )
        self.assertEqual(identity(messages[0]), "09865001")
        self.assertEqual(identity(messages[1]), "09865001")
        with self.assertRaises(EubError):
            reader.feed(b"x" * 16385)

    def test_dnw_wire_format_and_checksums(self):
        self.assertEqual(frame(b"abc"), b"\x1bDNW\x0d\x00\x00\x00abc\xff\xff")
        self.assertEqual(frame(b"abc", "sum16")[-2:], struct.pack("<H", 294))

    def test_short_writes_and_zero_progress(self):
        link = FakeLink([], short_write=3)
        write_all(link, bytes(range(256)) * 100)
        self.assertEqual(link.output, bytes(range(256)) * 100)
        with self.assertRaisesRegex(EubError, "no progress"):
            write_all(FakeLink([], short_write=0), b"x")

    def test_duplicate_requests_wait_for_clear_and_ack(self):
        image = signed("APBL")
        link = FakeLink(
            [
                b"eub:req:09865001:BL1\neub:req:09865001:BL1\n",
                b"C",
                b"eub:ack:09865001:BL1\n",
            ]
        )
        session = Session()
        with self.assertRaises(OSError):
            serve(
                link,
                Sources([{"bl1": image}], "zuma"),
                session,
                "zuma",
                report=lambda _: None,
            )
        self.assertEqual(link.output, frame(image))
        self.assertEqual(session.sent, ["BL1"])
        self.assertEqual(session.acknowledged, ["BL1"])

    def serve_requests(self, chunks, policy, session=None):
        link = FakeLink(chunks)
        report = Mock()
        session = session or Session()
        sources = Sources([{"bl1": signed("APBL"), "pbl": signed("EPBL")}], "gs201")
        with self.assertRaises(OSError):  # FakeLink disconnects when drained.
            serve(link, sources, session, "gs201", report=report, dpm_first=policy)
        return link, session, [call.args[0] for call in report.call_args_list]

    def test_dpm_first_stops_before_any_upload_when_required(self):
        link = FakeLink([b"eub:req:09855001:DPM\nC"])
        with self.assertRaisesRegex(EubError, "DPM before BL1.*Nothing was uploaded"):
            serve(
                link,
                Sources([{"bl1": signed("APBL")}], "gs201"),
                Session(),
                "gs201",
                report=lambda _: None,
                dpm_first="stop",
            )
        self.assertEqual(link.output, b"")

    def test_dpm_first_warns_or_is_ignored_by_policy(self):
        for policy, warned in (("warn", True), ("allow", False)):
            with self.subTest(policy=policy):
                link, session, lines = self.serve_requests(
                    [b"eub:req:09855001:DPM\nC"], policy
                )
                self.assertEqual(session.sent, ["DPM"])
                self.assertEqual(
                    any(
                        line.startswith("WARNING: ROM requested DPM") for line in lines
                    ),
                    warned,
                )

    def test_bl1_first_then_dpm_tells_user_to_release_buttons_once(self):
        link, session, lines = self.serve_requests(
            [
                b"eub:req:09855001:BL1\nC",
                b"eub:ack:09855001:BL1\neub:req:09855001:DPM\neub:req:09855001:DPM\nC",
            ],
            "stop",
        )
        self.assertEqual(session.sent, ["BL1", "DPM"])
        hints = [line for line in lines if "release the buttons" in line]
        self.assertEqual(len(hints), 1)
        # Only the session's first request matters, also across reconnects.
        link, session, lines = self.serve_requests(
            [b"eub:req:09855001:EPBL\nC"], "stop", session=session
        )
        self.assertEqual(session.sent[-1], "EPBL")

    # Dumps recorded from a Pixel 6a after three different BL1 uploads.
    DUMPS = {
        0x400: "00000400 b4e0008d 00006000 00824ca0",
        0x500: "00000500 b4e0008d 00002000 00829a30",
        0x600: "00000600 b4e0108d 00018000 00823640",
    }
    PREFIX = "e0000080 e0000080 00000000 a0031111 a0031112 a0121113"

    # Bytes captured from a Pixel 6a after a BL1 with one changed header byte.
    CAPTURED_BLOCK = bytes.fromhex(
        "6530303030303830006530303030303830006530303030303830003030303030303030"
        "006130303331313131006130303331313132006130313231313133003030303030303030"
        "003030303030303030003030303030303030003030303030303030003030303030303030"
        "003030303030303030003030303030303030000d0a00"
    )
    CAPTURED_LINE = b"\r\nirom_booting_failure::\x0000000001\x00" + CAPTURED_BLOCK

    def test_reader_keeps_nul_separated_dump_words_in_one_message(self):
        reader = MessageReader()
        messages = []
        for value in self.CAPTURED_BLOCK + self.CAPTURED_LINE + b"eub:req:0984:bl1\nC":
            messages += reader.feed(bytes([value]))
        self.assertEqual(len(messages), 4)
        self.assertEqual(messages[0].split("\0")[:3], ["e0000080"] * 3)
        self.assertTrue(
            messages[1].startswith("irom_booting_failure::\x0000000001\x00")
        )
        self.assertEqual(messages[2:], ["eub:req:0984:bl1", "C"])
        # Outside a dump, NUL still ends a message; a dump without a newline ends
        # at the first non-hex byte.
        self.assertEqual(
            MessageReader().feed(b"eub:ack:0984:bl1\x00C"), ["eub:ack:0984:bl1", "C"]
        )
        self.assertEqual(
            MessageReader().feed(b"e0000080\x00eub:req:0984:bl1\n"),
            ["e0000080", "eub:req:0984:bl1"],
        )

    def test_dump_inside_failure_line_takes_precedence(self):
        stale = "\n".join(f"{self.PREFIX} {self.DUMPS[0x500]}".split()) + "\n"
        link = FakeLink([stale.encode(), self.CAPTURED_LINE])
        lines = []
        session = Session(serial="09845001", soc="gs101", sent=["BL1"])
        with self.assertRaisesRegex(EubError, "Failure code 0x0: no code recorded"):
            serve(link, Sources([{}], "gs101"), session, "gs101", report=lines.append)
        dump = next(line for line in lines if line.startswith("ROM failure dump"))
        self.assertIn("first word 00000001", dump)
        self.assertIn("3x e0000080", dump)

    def test_listen_after_failure_reports_repeats_without_uploading(self):
        def line(counter):
            return self.CAPTURED_LINE.replace(b"00000001", counter.encode(), 1)

        link = FakeLink(
            [
                line("00000001"),
                line("00000002"),
                line("00000003"),
                b"eub:req:09845001:bl1\n",
            ]
        )
        lines = []
        session = Session(serial="09845001", soc="gs101", sent=["BL1"])
        with self.assertRaisesRegex(EubError, "Failure code 0x0"):
            serve(
                link,
                Sources([{"bl1": signed("APBL")}], "gs101"),
                session,
                "gs101",
                report=lines.append,
                listen_after_failure=5,
            )
        repeats = [x for x in lines if x.startswith("Repeated failure")]
        self.assertEqual(len(repeats), 2)
        self.assertIn("first word 00000002, code 0x0", repeats[0])
        self.assertIn("first word 00000003", repeats[1])
        self.assertTrue(any("ROM after the failure" in x and "bl1" in x for x in lines))
        self.assertTrue(any(x.startswith("Port closed at +") for x in lines))
        self.assertIn("2 repeated failure line(s) on this connection", lines[-1])
        self.assertTrue(session.listen_disconnected)
        self.assertGreater(session.listen_until, session.failure_at)
        self.assertEqual(link.output, b"")  # Nothing uploaded while listening.

    def test_failure_dump_is_described(self):
        from pixel_eub.protocol import describe_panic, panic_code

        for code, tail in self.DUMPS.items():
            words = f"{self.PREFIX} {tail} 00000000 00000000 00000000".split()
            self.assertEqual(panic_code(words), code)
            line = describe_panic(words)
            self.assertIn(f"code {code:#x}", line)
            self.assertIn("hypothesis", line)
        self.assertIn(
            "address 0x00829a30",
            describe_panic(f"{self.PREFIX} {self.DUMPS[0x500]}".split()),
        )
        self.assertIn("unrecognised", describe_panic(["deadbeef"]))
        self.assertIsNone(panic_code(["deadbeef"]))

    def test_rejection_reports_dump_and_code(self):
        words = f"{self.PREFIX} {self.DUMPS[0x500]} 00000000".split()
        link = FakeLink([("\n".join(words) + "\nirom_booting_failure::\n").encode()])
        lines = []
        session = Session(serial="09845001", soc="gs101", sent=["BL1"])
        with self.assertRaisesRegex(EubError, "Failure code 0x500: likely USB boot"):
            serve(link, Sources([{}], "gs101"), session, "gs101", report=lines.append)
        self.assertTrue(
            any(line.startswith("ROM failure dump: code 0x500") for line in lines)
        )

    def test_missing_dpm_placeholder_is_reported(self):
        for images, placeholder in (({}, True), ({"dpm": signed("DPM")}, False)):
            link = FakeLink([b"eub:req:09865001:DPM\nC"])
            report = Mock()
            with self.subTest(placeholder=placeholder), self.assertRaises(OSError):
                serve(link, Sources([images], "zuma"), Session(), "zuma", report=report)
            sending = next(
                call.args[0]
                for call in report.call_args_list
                if call.args[0].startswith("Sending DPM")
            )
            self.assertEqual("empty placeholder" in sending, placeholder)

    def test_wrong_chip_and_changed_phone_upload_nothing(self):
        for serial, session in (
            ("09855001", Session()),
            ("09865002", Session("09865001", "zuma")),
        ):
            link = FakeLink([f"eub:req:{serial}:BL1\nC".encode()])
            with self.assertRaises(EubError):
                serve(
                    link,
                    Sources([{"bl1": signed("APBL")}], "zuma"),
                    session,
                    "zuma",
                    report=lambda _: None,
                )
            self.assertEqual(link.output, b"")

    def test_rom_rejections_are_terminal_even_after_ack(self):
        for error in (
            b"bl1 header fail\n",
            b"eub:nak:09865001:BL1\n",
            b"irom_booting_failure:123\n",
        ):
            link = FakeLink(
                [b"eub:req:09865001:BL1\nC", b"eub:ack:09865001:BL1\n" + error]
            )
            with self.assertRaisesRegex(EubError, "ROM rejected"):
                serve(
                    link,
                    Sources([{"bl1": signed("APBL")}], "zuma"),
                    Session(),
                    "zuma",
                    report=lambda _: None,
                )

    def test_repeat_limit_and_request_change(self):
        retry = [b"eub:req:09865001:BL1\nC", b"eub:ack:09865001:BL1\n"]
        for chunks, error in (
            (retry * 6, "five"),
            ([b"eub:req:09865001:BL1\neub:req:09865001:ABL\n"], "changed request"),
        ):
            with self.assertRaisesRegex(EubError, error):
                serve(
                    FakeLink(chunks),
                    Sources([{"bl1": signed("APBL")}], "zuma"),
                    Session(),
                    "zuma",
                    report=lambda _: None,
                )

    def test_request_echo_before_ack_does_not_send_the_stage_twice(self):
        # Observed on Pixel 7/8: request and C repeat right after an upload.
        image, pbl = signed("APBL"), signed("EPBL", b"pbl")
        link = FakeLink(
            [
                b"eub:req:09855001:BL1\nC",
                b"eub:req:09855001:BL1\nC",
                b"eub:ack:09855001:BL1\neub:req:09855001:EPBL\nC",
                b"eub:req:09855001:EPBL\nC",
            ]
        )
        session = Session()
        with self.assertRaises(OSError):
            serve(
                link,
                Sources([{"bl1": image, "pbl": pbl}], "gs201"),
                session,
                "gs201",
                report=lambda _: None,
            )
        self.assertEqual(session.sent, ["BL1", "EPBL"])
        self.assertEqual(link.output, frame(image) + frame(pbl))

    def test_long_silence_after_upload_waits_while_port_is_present(self):
        clock = [0.0]

        class QuietLink(FakeLink):
            def read(self, count):
                clock[0] += 1
                try:
                    return next(self.chunks)
                except StopIteration:
                    return b""

        def run(present, ack_timeout=600):
            clock[0] = 0.0
            lines = []
            link = QuietLink([b"eub:req:09855001:GSA1\nC", b"eub:ack:09855001:GSA1\n"])
            with patch(
                "pixel_eub.protocol.time.monotonic", side_effect=lambda: clock[0]
            ):
                serve(
                    link,
                    Sources([{"gsa": signed("GSA1")}], "gs201"),
                    Session(),
                    "gs201",
                    report=lines.append,
                    ack_timeout=ack_timeout,
                    present=present,
                )
            return clock[0], lines

        # GSA1 verification: minutes of silence are not an idle timeout.
        state = {"calls": 0}

        def gone_after_four_minutes():
            state["calls"] += 1
            return clock[0] < 240

        elapsed, lines = run(gone_after_four_minutes)
        self.assertGreaterEqual(elapsed, 240)
        self.assertTrue(
            any(line.startswith("Waiting for the ROM after GSA1") for line in lines)
        )
        with self.assertRaisesRegex(EubError, "silent for .* GSA1"):
            run(lambda: True, ack_timeout=120)

    def panic_sources(self):
        return Sources(
            [
                {
                    "bl1": signed("APBL"),
                    "pbl": signed("EPBL", b"pbl"),
                    "bl2": signed("BL2", b"bl2"),
                    "abl": signed("ABL", b"abl"),
                    "bl31": signed("BL31", b"bl31"),
                }
            ],
            "gs201",
        )

    def test_confirmed_panic_sends_pbl_and_rest_without_new_request_or_c(self):
        sources, session = self.panic_sources(), Session()
        link = FakeLink([b"eub:req:09855001:BL1\nC", b"irom_booting_failure:123\n"])
        confirm, logs = Mock(), []
        with self.assertRaises(OSError):
            serve(
                link,
                sources,
                session,
                "gs201",
                confirm_panic=confirm,
                report=logs.append,
            )
        expected = ["BL1", "EPBL", "BL2", "BL2B", "ABL", "ABLB", "BL31", "BL3B"]
        self.assertEqual(session.sent, expected)
        self.assertEqual(
            link.output, b"".join(frame(sources.payload(s)) for s in expected)
        )
        self.assertEqual(session.forced_pending, [])
        confirm.assert_called_once()
        self.assertTrue(any("no ROM request/C" in line for line in logs))

    def test_panic_decline_keeps_only_original_bl1_upload(self):
        from pixel_eub.cli import confirm_rom_panic

        for answer in ("NO", "", "y", None):
            with self.subTest(answer=answer):
                sources, session = self.panic_sources(), Session()
                link = FakeLink(
                    [b"eub:req:09855001:BL1\nC", b"irom_booting_failure:123\n"]
                )
                with (
                    patch(
                        "builtins.input",
                        side_effect=EOFError if answer is None else None,
                        return_value=answer,
                    ),
                    patch("builtins.print"),
                    self.assertRaisesRegex(EubError, "cancelled"),
                ):
                    serve(
                        link,
                        sources,
                        session,
                        "gs201",
                        confirm_panic=confirm_rom_panic,
                        report=lambda _: None,
                    )
                self.assertEqual(link.output, frame(sources.payload("BL1")))
                self.assertIsNone(session.forced_pending)

    def test_forced_chain_reconnect_requires_same_rom_identity(self):
        sources, session = self.panic_sources(), Session()
        first = FakeLink([b"eub:req:09855001:BL1\nC", b"irom_booting_failure:123\n"])
        original_write = first.write

        def disconnect_before_bl2(data):
            if len(session.sent) >= 2:
                raise OSError(19, "No such device")
            return original_write(data)

        first.write = disconnect_before_bl2
        with self.assertRaises(OSError):
            serve(
                first,
                sources,
                session,
                "gs201",
                confirm_panic=Mock(),
                report=lambda _: None,
            )
        self.assertEqual(session.sent, ["BL1", "EPBL"])
        wrong = FakeLink([b"exynos_usb_booting:eub:09855002\n"])
        with self.assertRaisesRegex(EubError, "different phone"):
            serve(
                wrong,
                sources,
                session,
                "gs201",
                confirm_panic=Mock(),
                allow_cached_identity=True,
                report=lambda _: None,
            )
        self.assertFalse(wrong.output)

        untrusted = FakeLink([b"irom_booting_failure::\n"])
        confirm = Mock()
        with self.assertRaisesRegex(EubError, "before identity"):
            serve(
                untrusted,
                sources,
                session,
                "gs201",
                confirm_panic=confirm,
                report=lambda _: None,
            )
        confirm.assert_not_called()
        self.assertFalse(untrusted.output)
        resumed = FakeLink([b"exynos_usb_booting:eub:09855001\n"])
        with self.assertRaises(OSError):
            serve(
                resumed,
                sources,
                session,
                "gs201",
                confirm_panic=Mock(),
                report=lambda _: None,
            )
        self.assertEqual(session.forced_pending, [])
        self.assertEqual(
            resumed.output,
            b"".join(
                frame(sources.payload(s))
                for s in ["BL2", "BL2B", "ABL", "ABLB", "BL31", "BL3B"]
            ),
        )

    def test_explicit_rejections_still_stop_with_panic_override_callback(self):
        for message in (b"bl1 header fail\n", b"eub:nak:09855001:BL1\n"):
            link = FakeLink([b"eub:req:09855001:BL1\nC", message])
            confirm = Mock()
            with self.assertRaisesRegex(EubError, "ROM rejected"):
                serve(
                    link,
                    self.panic_sources(),
                    Session(),
                    "gs201",
                    confirm_panic=confirm,
                    report=lambda _: None,
                )
            confirm.assert_not_called()

    def test_panic_before_completed_upload_cannot_start_forced_chain(self):
        confirm = Mock()
        link = FakeLink(
            [b"exynos_usb_booting:eub:09855001\nirom_booting_failure:123\n"]
        )
        with self.assertRaisesRegex(EubError, "no completed"):
            serve(
                link,
                self.panic_sources(),
                Session(),
                "gs201",
                confirm_panic=confirm,
                report=lambda _: None,
            )
        self.assertFalse(link.output)
        confirm.assert_not_called()

    def test_probe_does_not_send_payload(self):
        link = FakeLink([b"eub:req:09865001:BL1\nC"])
        self.assertEqual(probe(link), "09865001")
        self.assertEqual(link.output, b"")

    def test_silent_session_times_out_without_claiming_success(self):
        link = FakeLink([b""] * 100)
        clock = iter([i * 0.5 for i in range(100)])
        with (
            patch("pixel_eub.protocol.time.monotonic", side_effect=lambda: next(clock)),
            self.assertRaisesRegex(EubError, "not confirmed"),
        ):
            serve(
                link,
                Sources([], "zuma"),
                Session(),
                "zuma",
                idle_timeout=3,
                report=lambda _: None,
            )
        self.assertLessEqual(len(link.output), 1)
