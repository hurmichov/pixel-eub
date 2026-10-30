import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pixel_eub import EubError
from pixel_eub.downloads import Cache, parse_catalog, select_image


def row(
    device="husky",
    build="ap1a.240505.005",
    description="14.0.0 (AP1A.240505.005, May 2024)",
    digest="a" * 64,
):
    return f'<tr><td>{description}</td><td><a href="https://dl.google.com/dl/android/aosp/{device}-{build}-factory-1234abcd.zip">Link</a></td><td>{digest}</td></tr>'


class Response(io.BytesIO):
    def __init__(self, payload, length=None):
        super().__init__(payload)
        self.headers = {
            "Content-Length": str(len(payload) if length is None else length)
        }


class DownloadsTests(unittest.TestCase):
    def test_catalog_scopes_hash_to_same_row_and_google_host(self):
        page = (
            row()
            + row("shiba", digest="b" * 64)
            + row(digest="")
            + row().replace("dl.google.com", "evil.example")
        )
        rows = parse_catalog(page)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].sha256, "a" * 64)
        self.assertEqual(rows[1].device, "shiba")

    def test_build_selection_exact_latest_generic_and_ambiguity(self):
        rows = parse_catalog(
            row()
            + row(
                build="bp1a.250505.001",
                description="15.0.0 (BP1A.250505.001, May 2025)",
            )
            + row(
                build="bp1a.250505.001.v1",
                description="15.0.0 (BP1A.250505.001.V1, May 2025, Verizon)",
            )
        )
        self.assertEqual(select_image(rows, "husky", None).build, "bp1a.250505.001")
        self.assertEqual(
            select_image(rows, "husky", "AP1A.240505.005").build, "ap1a.240505.005"
        )
        with self.assertRaises(EubError):
            select_image(rows, "husky", "unknown")
        with self.assertRaisesRegex(EubError, "Several"):
            select_image(rows + [rows[-2]], "husky", None)

    def test_valid_cache_reused_and_offline_corruption_rejected(self):
        payload = b"download body"
        digest = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch(
                "pixel_eub.downloads.open_https", return_value=Response(payload)
            ) as request:
                path = Cache(root).download("https://example.com/image.zip", digest)
                self.assertEqual(
                    Cache(root, True).download("https://example.com/image.zip", digest),
                    path,
                )
                request.assert_called_once()
            path.write_bytes(b"damaged")
            with self.assertRaisesRegex(EubError, "intact cached"):
                Cache(root, True).download("https://example.com/image.zip", digest)

    def test_partial_wrong_hash_and_oversized_download_not_published(self):
        for payload, length, limit, digest in (
            (b"short", 100, 1000, "a" * 64),
            (b"wrong", 5, 1000, "a" * 64),
            (b"too big", 7, 3, "a" * 64),
        ):
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                with (
                    patch(
                        "pixel_eub.downloads.open_https",
                        return_value=Response(payload, length),
                    ),
                    self.assertRaises(EubError),
                ):
                    Cache(root).download(
                        "https://example.com/image.zip", digest, max_size=limit
                    )
                self.assertEqual(list(root.iterdir()), [])

    def test_download_network_failure_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as temp:
            with (
                patch(
                    "pixel_eub.downloads.open_https",
                    side_effect=OSError("network down"),
                ),
                self.assertRaises(OSError),
            ):
                Cache(Path(temp)).download("https://example.com/image.zip", "a" * 64)
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_catalog_cache_and_offline(self):
        with tempfile.TemporaryDirectory() as temp:
            cache = Cache(Path(temp))
            with patch(
                "pixel_eub.downloads.open_https", return_value=Response(row().encode())
            ) as request:
                self.assertEqual(len(cache.catalog()), 1)
                self.assertEqual(len(cache.catalog()), 1)
                request.assert_called_once()
            self.assertEqual(len(Cache(Path(temp), True).catalog()), 1)

    def test_missing_hash_rejected_before_network(self):
        with patch("pixel_eub.downloads.open_https") as request:
            with self.assertRaises(EubError):
                Cache(Path("unused")).download("https://example.com/x", "")
            request.assert_not_called()
