"""HTTPS downloads, row-scoped factory metadata and verified atomic caches."""

import hashlib
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from . import EubError

INDEX_URL = "https://developers.google.com/android/images"
PACK_URL = "https://github.com/JoshuaDoes/tensor-usbdl/releases/download/020/tensor-usbdl-v0.2.0.zip"
PACK_SHA256 = "15dbd6406c1c74c2cd7104388ab745f3605d026ede6fc6c61c754ddb30b194b2"
USER_AGENT = "pixel-eub/1.0.0b3"


def default_cache() -> Path:
    if os.environ.get("PIXEL_EUB_CACHE"):
        return Path(os.environ["PIXEL_EUB_CACHE"]).expanduser()
    if sys.platform == "darwin":
        base = Path.home() / "Library/Caches"
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "pixel-eub"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def open_https(url: str, *, headers=None, timeout=30):
    if urlparse(url).scheme != "https":
        raise EubError("Only HTTPS downloads are allowed.")
    response = urlopen(
        Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})}),
        timeout=timeout,
    )
    if urlparse(response.geturl()).scheme != "https":
        response.close()
        raise EubError("Refusing a download redirected away from HTTPS.")
    return response


class Cache:
    def __init__(self, root: Path, offline=False):
        self.root = root
        self.offline = offline

    def download(self, url: str, checksum: str, *, max_size=16 * 1024**3) -> Path:
        if not re.fullmatch(r"[a-fA-F0-9]{64}", checksum):
            raise EubError("Missing or invalid SHA-256; refusing unverified download.")
        checksum = checksum.lower()
        filename = Path(urlparse(url).path).name
        if not re.fullmatch(r"[A-Za-z0-9._-]+", filename):
            raise EubError("Invalid download filename.")
        target = self.root / f"{checksum[:16]}-{filename}"
        if target.is_file() and sha256_file(target) == checksum:
            print(f"Using verified cache: {target}", file=sys.stderr)
            return target
        if self.offline:
            raise EubError(
                f"No intact cached download for {filename}; rerun without --offline."
            )
        self.root.mkdir(parents=True, exist_ok=True)
        print(f"Downloading {url}", file=sys.stderr)
        fd, temporary = tempfile.mkstemp(prefix=".download-", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as output, open_https(url) as response:
                total_header = response.headers.get("Content-Length")
                total = int(total_header) if total_header is not None else None
                if total is not None and (total < 0 or total > max_size):
                    raise EubError("Download exceeds the size limit.")
                digest, size, last_report = hashlib.sha256(), 0, time.monotonic()
                while block := response.read(1024 * 1024):
                    size += len(block)
                    if size > max_size:
                        raise EubError("Download exceeds the size limit.")
                    output.write(block)
                    digest.update(block)
                    if time.monotonic() - last_report > 5:
                        print(f"  {size / 1024**2:.0f} MiB downloaded", file=sys.stderr)
                        last_report = time.monotonic()
                if total is not None and size != total:
                    raise EubError(f"Truncated download: {size}/{total} bytes.")
                if digest.hexdigest() != checksum:
                    raise EubError(
                        "SHA-256 mismatch; downloaded file will not be used."
                    )
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return target

    def catalog(self, refresh=False) -> list["FactoryImage"]:
        path = self.root / "factory-index.html"
        fresh = path.is_file() and time.time() - path.stat().st_mtime < 86400
        if path.is_file() and (self.offline or (fresh and not refresh)):
            data = path.read_text(encoding="utf-8")
        elif self.offline:
            raise EubError("Factory catalog is not cached; rerun without --offline.")
        else:
            with open_https(
                INDEX_URL, headers={"Cookie": "devsite_wall_acks=nexus-image-tos"}
            ) as response:
                raw = response.read(16 * 1024**2 + 1)
            if len(raw) > 16 * 1024**2:
                raise EubError("Factory catalog exceeds size limit.")
            data = raw.decode("utf-8")
            if not parse_catalog(data):
                raise EubError(
                    f"No verified factory-image rows found at {INDEX_URL}; catalog format may have changed."
                )
            self.root.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".index-", dir=self.root)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as file:
                    file.write(data)
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)
        images = parse_catalog(data)
        if not images:
            raise EubError(
                "Cached factory catalog is invalid; use --refresh without --offline."
            )
        return images


@dataclass(frozen=True)
class FactoryImage:
    device: str
    build: str
    description: str
    url: str
    sha256: str


class CatalogParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.images = []
        self.row = None
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row, self.links = [], []
        if tag == "a" and self.row is not None:
            self.links.append(dict(attrs).get("href", ""))

    def handle_data(self, data):
        if self.row is not None:
            self.row.append(data)

    def handle_endtag(self, tag):
        if tag != "tr" or self.row is None:
            return
        text = " ".join(" ".join(self.row).split())
        hashes = re.findall(r"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])", text)
        for url in self.links:
            match = re.fullmatch(
                r"https://dl\.google\.com/dl/android/aosp/([a-z0-9]+)-([a-zA-Z0-9.]+)-factory-[a-fA-F0-9]+\.zip",
                url,
            )
            if match and len(hashes) == 1:
                self.images.append(
                    FactoryImage(
                        match[1], match[2].lower(), text, url, hashes[0].lower()
                    )
                )
        self.row = None


def parse_catalog(html: str) -> list[FactoryImage]:
    parser = CatalogParser()
    parser.feed(html)
    return list(dict.fromkeys(parser.images))


def select_image(
    images: list[FactoryImage], device: str, build: str | None
) -> FactoryImage:
    candidates = [i for i in images if i.device == device]
    if build and build.lower() != "latest":
        candidates = [i for i in candidates if i.build == build.lower()]
    else:
        # A row containing just the standard version/date has one parenthesized
        # comma. Regional/carrier rows carry more qualifiers; do not guess them.
        candidates = [
            i for i in candidates if re.search(r"\([^,()]+,\s*[^,()]+\)", i.description)
        ]
    if not candidates:
        raise EubError(
            "No matching unambiguous factory build; use 'pixel-eub builds --device ...' and --build."
        )

    def key(image):
        parts = image.build.split(".")
        if len(parts) < 3 or not parts[1].isdigit():
            raise EubError(
                f"Cannot determine build date for {image.build}; specify a build."
            )
        return (int(parts[1]), tuple(int(p) if p.isdigit() else p for p in parts[2:3]))

    if not build or build.lower() == "latest":
        newest = max(key(i) for i in candidates)
        candidates = [i for i in candidates if key(i) == newest]
    if len(candidates) != 1:
        raise EubError(
            "Several builds/variants match; specify the full build ID with --build."
        )
    return candidates[0]
