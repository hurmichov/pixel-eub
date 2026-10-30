import struct
import tempfile
import unittest
import zipfile
from pathlib import Path

from pixel_eub import EubError
from pixel_eub.images import Sources, load_bootloader, load_recovery, parse_fbpk
from pixel_eub.models import model_for
from tests.helpers import fbpk, signed


class ImagesTests(unittest.TestCase):
    def test_fbpk_crc_and_slot_names(self):
        payload = signed("ABL")
        pack = parse_fbpk(fbpk([("abl_a", payload), ("abl_b", payload)]))
        self.assertEqual(pack.platform, "zuma")
        self.assertEqual(pack.images, {"abl": payload})
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
