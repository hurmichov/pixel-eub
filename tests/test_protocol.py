import struct
import unittest
from unittest.mock import patch

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
        for chunks, error in (
            ([b"eub:req:09865001:BL1\nC"] * 6, "five"),
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
