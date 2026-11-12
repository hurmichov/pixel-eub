import struct
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from pixel_eub import EubError
from pixel_eub.images import (
    Sources,
    inspect_file,
    inspect_stage,
    load_bootloader,
    load_recovery,
    parse_fbpk,
)
from pixel_eub.models import model_for
from tests.helpers import fbpk, signed


class ImagesTests(unittest.TestCase):
    def test_fbpk_crc_and_slot_names(self):
        payload = signed("ABL")
        pack = parse_fbpk(fbpk([("abl_a", payload), ("abl_b", payload)]))
        self.assertEqual(pack.platform, "zuma")
        self.assertEqual(pack.images, {"abl": payload})
        tables = parse_fbpk(fbpk([("partition:0", b"GPT"), ("bl1_a", signed("APBL"))]))
        self.assertEqual(sorted(tables.images), ["bl1", "partition:0"])
        with self.assertRaisesRegex(EubError, "Unsupported"):
            parse_fbpk(fbpk([("../abl", payload)]))
        broken = bytearray(fbpk([("abl", payload)]))
        broken[-1] ^= 1
        with self.assertRaisesRegex(EubError, "CRC32"):
            parse_fbpk(broken)

    def test_rejects_bad_headers_sizes_ranges_and_overlap(self):
        data = fbpk([("abl", signed("ABL")), ("pbl", signed("EPBL"))])
        corruptions = [b"junk", data[:100], data[:-1]]
        for offset, fmt, value in (
            (4, "I", 1),
            (8, "I", 42),
            (104, "I", 999999),
            (192, "Q", 0),
            (200, "Q", 2**63),
            (296, "Q", struct.unpack_from("<Q", data, 192)[0]),
        ):
            broken = bytearray(data)
            struct.pack_into("<" + fmt, broken, offset, value)
            corruptions.append(broken)
        for broken in corruptions:
            with self.subTest(length=len(broken)), self.assertRaises(EubError):
                parse_fbpk(broken)

    def test_conflicting_slots_rejected(self):
        with self.assertRaisesRegex(EubError, "Ambiguous"):
            parse_fbpk(
                fbpk([("abl_a", signed("ABL")), ("abl_b", signed("ABL", b"different"))])
            )

    def test_factory_zip_model_platform_and_product(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "factory.zip"
            for platform, product, error in (
                ("gs201", "husky", "platform"),
                ("zuma", "shiba", "different"),
                ("zuma", "husky", None),
            ):
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr(
                        "folder/bootloader-ripcurrent-test.img",
                        fbpk([("abl", signed("ABL"))], platform, product),
                    )
                if error:
                    with self.assertRaisesRegex(EubError, error):
                        load_bootloader(path, model_for("husky"))
                else:
                    self.assertIn(
                        "abl", load_bootloader(path, model_for("husky")).images
                    )
            wrong = Path(temp) / "shiba-test-factory-1234.zip"
            wrong.write_bytes(path.read_bytes())
            with self.assertRaisesRegex(EubError, "filename"):
                load_bootloader(wrong, model_for("husky"))

    def test_zip_ota_and_ambiguous_bootloader(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "x.zip"
            for names in (("payload.bin",), ("bootloader-a.img", "bootloader-b.img")):
                with zipfile.ZipFile(path, "w") as archive:
                    for name in names:
                        archive.writestr(name, b"x")
                with self.assertRaisesRegex(EubError, "exactly one"):
                    load_bootloader(path, model_for("husky"))

    def test_recovery_zip_only_reads_requested_pack(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "release.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("sources/zuma/husky/bl1.img", signed("APBL"))
                archive.writestr("sources/zuma/shiba/bl1.img", b"wrong")
                archive.writestr("sources/zuma/husky/../../escape.img", b"bad")
                archive.writestr("run.sh", b"exit 1")
            images = load_recovery(path, model_for("husky"))
            self.assertEqual(images, {"bl1": signed("APBL")})
            self.assertEqual(list(root.iterdir()), [path])

    def test_header_tags_fix_reversed_gsa_names(self):
        source = Sources(
            [{"gsa": signed("GSAF", b"firmware"), "gsaf": signed("GSA1", b"first")}],
            "zuma",
        )
        self.assertEqual(source.payload("GSA1"), signed("GSA1", b"first"))
        self.assertEqual(source.payload("GSAF"), signed("GSAF", b"firmware"))

    def test_missing_gsaf_is_not_replaced_with_gsa1(self):
        source = Sources([{"gsa": signed("GSA1")}], "gs201")
        with self.assertRaisesRegex(EubError, "No signed image"):
            source.payload("GSAF")

    def test_slices_generation_and_layer_precedence(self):
        high, low = signed("EPBL", b"high"), signed("EPBL", b"low")
        for soc, expected in (("gs101", high), ("gs201", high), ("zuma", high[:4096])):
            source = Sources([{"pbl": high}, {"pbl": low}], soc)
            self.assertEqual(source.payload("EPBL"), expected)
            self.assertEqual(source.payload("EPBB"), b"high")
            self.assertEqual(source.payload("DPM"), bytes(4096))
        self.assertEqual(Sources([{"pbl": high}], "zuma", "full").payload("EPBL"), high)

    def test_unknown_empty_body_retail_bl1_and_short_images(self):
        for stage, images, error in (
            ("WHAT", {}, "Unknown"),
            ("BL1", {"bl1": signed("APBL", flags=0)}, "USB-bootable"),
            ("BL3B", {"bl31": signed("BL31", b"")}, "no code body"),
            ("ABL", {"abl": b"short"}, "shorter"),
        ):
            with self.subTest(stage=stage), self.assertRaisesRegex(EubError, error):
                Sources([images], "zuma").payload(stage)

    def test_usb_check_can_be_skipped_explicitly(self):
        retail = signed("APBL", flags=0x400)
        source = Sources([{"bl1": retail}], "gs101", usb_check=False)
        self.assertEqual(source.payload("BL1"), retail)  # Sent unchanged.


class InspectTests(unittest.TestCase):
    GS201_STAGE = bytes.fromhex(
        "c72ed72cc4136742bab2a493e3fe961e442e20152c6a98a3ab201412d144895d"
    )

    def test_stage_flag_family_and_notes(self):
        retail = inspect_stage("bl1.bin", signed("APBL", flags=0x400))
        self.assertEqual((retail.stage, retail.usb, retail.family), ("BL1", False, ""))
        self.assertIn("retail BL1", retail.notes[0])
        recovery = bytearray(signed("EPBL", b"pbl", flags=0x401))
        recovery[0x200:0x220] = self.GS201_STAGE
        info = inspect_stage("pbl.img", bytes(recovery))
        self.assertEqual((info.stage, info.usb, info.family), ("PBL", True, "gs201"))
        self.assertEqual(info.notes, ())
        broken = signed("BL2", b"body") + b"extra"
        self.assertIn("declares", inspect_stage("bl2", broken).notes[0])
        self.assertEqual(inspect_stage("x", b"short").tag, "")
        self.assertIn("no known stage tag", inspect_stage("x", bytes(5000)).notes[0])

    def test_files_fbpk_and_zip(self):
        with tempfile.TemporaryDirectory() as temp:
            raw = Path(temp) / "bl1.bin"
            raw.write_bytes(signed("APBL"))
            self.assertEqual([i.name for i in inspect_file(raw)], ["bl1.bin"])
            boot = Path(temp) / "bootloader.img"
            boot.write_bytes(fbpk([("bl1_a", signed("APBL")), ("abl", signed("ABL"))]))
            names = [i.name for i in inspect_file(boot)]
            self.assertEqual(names, ["bootloader.img:bl1", "bootloader.img:abl"])
            archive = Path(temp) / "pack.zip"
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("sources/gs201/bl1.img", signed("APBL"))
                z.writestr("readme.txt", "ignored")
            self.assertEqual(
                [i.name for i in inspect_file(archive)], ["sources/gs201/bl1.img"]
            )
            factory = Path(temp) / "bluejay-factory.zip"
            with zipfile.ZipFile(factory, "w") as z:
                z.writestr(
                    "bluejay-x/bootloader-bluejay-x.img",
                    fbpk([("bl1", signed("APBL"))]),
                )
                z.writestr("bluejay-x/radio-bluejay-x.img", b"radio")
            self.assertEqual(
                [i.name for i in inspect_file(factory)],
                ["bluejay-x/bootloader-bluejay-x.img:bl1"],
            )
            with patch("pixel_eub.images.MAX_IMAGE", 10):
                skipped = inspect_file(archive)
            self.assertIn("skipped", skipped[0].notes[0])
            empty = Path(temp) / "empty.zip"
            with zipfile.ZipFile(empty, "w") as z:
                z.writestr("readme.txt", "x")
            with self.assertRaisesRegex(EubError, "no .img"):
                inspect_file(empty)
