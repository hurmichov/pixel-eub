"""Bounded FBPK v2/ZIP parsing and request-specific signed-image slices."""

import re
import struct
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import EubError
from .models import MODELS, Model

HEADER = 4096
MAX_IMAGE = 128 * 1024**2
MAX_IMAGES = 256 * 1024**2
STAGES = {
    "BL1": ("bl1", "APBL", "full"),
    "DPM": ("dpm", "DPM", "full"),
    "EPBL": ("pbl", "EPBL", "epbl"),
    "EPBB": ("pbl", "EPBL", "body"),
    "BL2": ("bl2", "BL2", "header"),
    "BL2B": ("bl2", "BL2", "body"),
    "GSA1": ("gsa_bl1", "GSA1", "full"),
    "GSAF": ("gsa", "GSAF", "full"),
    "ABL": ("abl", "ABL", "header"),
    "ABLB": ("abl", "ABL", "body"),
    "TZSW": ("tzsw", "TZSW", "header"),
    "TZSB": ("tzsw", "TZSW", "body"),
    "LDFW": ("ldfw", "LDFW", "header"),
    "LDFB": ("ldfw", "LDFW", "body"),
    "BL31": ("bl31", "BL31", "header"),
    "BL3B": ("bl31", "BL31", "body"),
    "GCF": ("gcf", "GCF", "header"),
    "GCFB": ("gcf", "GCF", "body"),
}


def image_tag(data: bytes) -> str:
    if len(data) < HEADER:
        return ""
    tag = data[0x400:0x404].split(b"\0", 1)[0]
    return tag.decode("ascii").upper() if re.fullmatch(rb"[A-Za-z0-9]+", tag) else ""


def partition_name(name: str) -> str:
    return re.sub(r"_[ab]$", "", name.lower())


def bounded_read(path: Path) -> bytes:
    with path.open("rb") as file:
        data = file.read(MAX_IMAGE + 1)
    if len(data) > MAX_IMAGE:
        raise EubError(f"Image too large: {path}")
    if not data:
        raise EubError(f"Empty image: {path}")
    return data


def zip_read(archive: zipfile.ZipFile, entry: zipfile.ZipInfo) -> bytes:
    if entry.file_size > MAX_IMAGE:
        raise EubError(f"ZIP member exceeds image size limit: {entry.filename}")
    with archive.open(entry) as file:
        data = file.read(MAX_IMAGE + 1)
    if len(data) > MAX_IMAGE or len(data) != entry.file_size:
        raise EubError(f"Invalid ZIP member length: {entry.filename}")
    return data


@dataclass
class Bootloader:
    platform: str
    products: set[str]
    images: dict[str, bytes]


def cstring(data: bytes) -> str:
    try:
        return data.split(b"\0", 1)[0].decode("ascii")
    except UnicodeDecodeError as exc:
        raise EubError("Non-ASCII FBPK metadata.") from exc


def parse_fbpk(data: bytes) -> Bootloader:
    if len(data) < 112 or data[:4] != b"FBPK":
        raise EubError("Not an FBPK bootloader image.")
    version, header_size, entry_size = struct.unpack_from("<III", data, 4)
    if (version, header_size, entry_size) != (2, 112, 104):
        raise EubError("Only FBPK v2 with 112/104-byte headers is supported.")
    count, total = struct.unpack_from("<II", data, 104)
    end_table = 112 + count * 104
    if total != len(data) or end_table > len(data):
        raise EubError("Truncated FBPK or inconsistent total size.")
    images, products, ranges = {}, set(), []
    for i in range(count):
        base = 112 + 104 * i
        kind = struct.unpack_from("<I", data, base)[0]
        name = partition_name(cstring(data[base + 4 : base + 40]))
        product = cstring(data[base + 40 : base + 80])
        offset, size, _, crc = struct.unpack_from("<QQII", data, base + 80)
        if offset < end_table or offset + size > len(data) or size > MAX_IMAGE:
            raise EubError(f"FBPK entry {name!r} is out of bounds.")
        if size and any(
            offset < stop and start < offset + size for start, stop in ranges
        ):
            raise EubError("Overlapping FBPK entries.")
        ranges.append((offset, offset + size))
        payload = data[offset : offset + size]
        if crc and zlib.crc32(payload) != crc:
            raise EubError(f"CRC32 mismatch in FBPK partition {name}.")
        if kind == 0:
            continue
        if kind not in (1, 2) or not re.fullmatch(r"[a-z0-9_]+", name):
            raise EubError(f"Unsupported FBPK entry {name!r} (type {kind}).")
        if name in images and images[name] != payload:
            raise EubError(f"Ambiguous slot images for {name}.")
        images[name] = payload
        if product:
            products.add(product)
    if not images:
        raise EubError("FBPK contains no partition images.")
    return Bootloader(cstring(data[16:32]), products, images)


def check_filename(name: str, model: Model):
    # Factory outer ZIP and bootloader filenames may identify a device or a
    # shared bootloader (cloudripper/ripcurrent). Check recognized devices.
    base = PurePosixPath(name).name.lower()
    for other in MODELS:
        if (
            base.startswith(
                (other.codename + "-", "bootloader-" + other.codename + "-")
            )
            and other != model
        ):
            raise EubError(f"Image filename identifies {other.name}, not {model.name}.")


def load_bootloader(path: Path, model: Model, compatibility=None) -> Bootloader:
    mismatches = []

    def filename(name):
        try:
            check_filename(name, model)
        except EubError as exc:
            mismatches.append(str(exc))

    filename(path.name)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            entries = [
                i
                for i in archive.infolist()
                if PurePosixPath(i.filename).name.startswith("bootloader")
                and i.filename.endswith(".img")
            ]
            if len(entries) != 1:
                raise EubError(
                    "Factory ZIP must contain exactly one bootloader*.img; OTA ZIPs are not supported."
                )
            filename(entries[0].filename)
            data = zip_read(archive, entries[0])
    else:
        data = bounded_read(path)
    pack = parse_fbpk(data)
    if pack.platform != model.soc:
        mismatches.append(
            f"FBPK platform {pack.platform!r} does not match {model.name} ({model.soc})."
        )
    known_products = {m.codename for m in MODELS}
    if pack.products & (known_products - {model.codename}):
        mismatches.append("FBPK partition product identifies a different Pixel model.")
    if mismatches:
        message = " ".join(mismatches)
        if compatibility is None:
            raise EubError(message)
        compatibility.check(message)
    return pack


def load_directory(path: Path) -> dict[str, bytes]:
    images = {}
    for file in sorted(path.glob("*.img")):
        images[partition_name(file.stem)] = bounded_read(file)
        if sum(map(len, images.values())) > MAX_IMAGES:
            raise EubError("Image directory exceeds size limit.")
    if not images:
        raise EubError(f"No *.img stage images in {path}")
    return images


def load_recovery(path: Path, model: Model) -> dict[str, bytes]:
    if path.is_dir():
        if model.pack and (path / "sources" / model.pack).is_dir():
            return load_directory(path / "sources" / model.pack)
        return load_directory(path)
    if not model.pack:
        raise EubError(
            f"No public recovery-pack mapping for {model.name}; provide --dir with compatible signed images."
        )
    images = {}
    prefix = f"sources/{model.pack}/"
    with zipfile.ZipFile(path) as archive:
        for entry in archive.infolist():
            rest = entry.filename.removeprefix(prefix)
            if (
                not entry.filename.startswith(prefix)
                or "/" in rest
                or not rest.endswith(".img")
            ):
                continue
            name = partition_name(rest[:-4])
            if name in images:
                raise EubError(f"Duplicate recovery image: {name}")
            images[name] = zip_read(archive, entry)
            if sum(map(len, images.values())) > MAX_IMAGES:
                raise EubError("Recovery pack exceeds size limit.")
    if not images:
        raise EubError(f"Recovery ZIP has no {prefix} images.")
    return images


class Sources:
    def __init__(self, layers: list[dict[str, bytes]], soc: str, epbl=None):
        self.layers = layers  # highest priority first
        self.epbl = epbl
        self.use_soc(soc)

    def use_soc(self, soc: str):
        self.split = self.epbl == "split" or (self.epbl is None and soc == "zuma")

    def find(self, stage: str) -> bytes:
        if stage not in STAGES:
            raise EubError(f"Unknown ROM stage {stage!r}; refusing to guess a payload.")
        name, tag, _ = STAGES[stage]
        for layer in self.layers:
            tagged = [b for b in layer.values() if image_tag(b) == tag]
            if len(tagged) == 1:
                return tagged[0]
            if len(tagged) > 1:
                raise EubError(f"Ambiguous signed image tag {tag}.")
            aliases = {
                "pbl": ("pbl", "epbl"),
                "gsa_bl1": ("gsa_bl1", "gsa1", "gsa"),
                "gsa": ("gsaf", "gsa"),
            }.get(name, (name,))
            for alias in aliases:
                if alias in layer:
                    data = layer[alias]
                    actual = image_tag(data)
                    if actual and actual != tag:
                        continue  # never select an image signed for a different stage
                    return data
        if stage == "DPM":
            return bytes(HEADER)
        raise EubError(f"No signed image available for ROM stage {stage}.")

    def payload(self, stage: str) -> bytes:
        data = self.find(stage)
        mode = STAGES[stage][2]
        if mode == "epbl":
            mode = "header" if self.split else "full"
        if len(data) < HEADER:
            raise EubError(
                f"{stage} image is shorter than its signed 4096-byte header."
            )
        if stage == "BL1" and not struct.unpack_from("<I", data, 0x410)[0] & 1:
            raise EubError(
                "BL1 lacks the signed USB-bootable flag. Retail BL1 cannot start ROM recovery; use a compatible recovery pack."
            )
        if mode == "header":
            return data[:HEADER]
        if mode == "body":
            if len(data) == HEADER:
                raise EubError(f"{stage} image has no code body.")
            return data[HEADER:]
        return data

    def panic_chain(self, sent: list[str]) -> list[str]:
        """Experimental host order; never used by normal request-driven uploads."""
        chain = ["BL1", "EPBL"]
        if self.split:
            chain.append("EPBB")
        chain += ["BL2", "BL2B"]
        # Optional images are included only when a matching signed tag exists.
        for stage, body in (
            ("GCF", "GCFB"),
            ("GSA1", None),
            ("GSAF", None),
            ("ABL", "ABLB"),
            ("TZSW", "TZSB"),
            ("LDFW", "LDFB"),
            ("BL31", "BL3B"),
        ):
            if stage not in ("ABL", "BL31"):
                tag = STAGES[stage][1]
                if not any(
                    image_tag(data) == tag
                    for layer in self.layers
                    for data in layer.values()
                ):
                    continue
            chain.append(stage)
            if body:
                chain.append(body)
        if not sent or sent[-1] not in chain:
            raise EubError(
                "Cannot choose a panic continuation: no completed chain stage."
            )
        remaining = chain[chain.index(sent[-1]) + 1 :]
        if not remaining:
            raise EubError("No boot-chain stages remain after the ROM panic.")
        # Validate the whole plan before asking for or sending forced uploads.
        for stage in remaining:
            self.payload(stage)
        return remaining
