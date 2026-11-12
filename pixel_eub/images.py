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
EMPTY_DPM = bytes(HEADER)
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
        # Newer Pixel bootloaders also pack UFS partition tables as "partition:N".
        if kind not in (1, 2) or not re.fullmatch(r"[a-z0-9_]+(?::[0-9]+)?", name):
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
    def __init__(
        self, layers: list[dict[str, bytes]], soc: str, epbl=None, usb_check=True
    ):
        self.layers = layers  # highest priority first
        self.epbl = epbl
        # The ROM enforces the signed USB flag itself; this check only avoids a
        # pointless attempt and can be skipped to observe the ROM's answer.
        self.usb_check = usb_check
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
            # Without a debug-policy image, answer with an empty header as
            # tensor-usbdl does; serve() logs this placeholder explicitly.
            return EMPTY_DPM
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
        if (
            stage == "BL1"
            and self.usb_check
            and not struct.unpack_from("<I", data, 0x410)[0] & 1
        ):
            raise EubError(
                "BL1 lacks the signed USB-bootable flag. Retail BL1 cannot start ROM recovery; use a compatible recovery pack, or --no-usb-check to send it anyway and see the ROM's answer."
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


# Header field at 0x200 as observed in the pinned tensor-usbdl v0.2.0 recovery
# pack. It is constant per SoC family and stage group; it is only used to label
# images and never to accept or reject them. Other values are reported unknown.
KNOWN_FINGERPRINTS = {
    "c72ed72cc4136742bab2a493e3fe961e442e20152c6a98a3ab201412d144895d": "gs201",
    "cb92e3eeff6161492fd20873e03017782a843fbefa1b41a5417755aee3f0eaec": "gs201",
    "b39550686ffb7998759b7e0be5e5716c51202fc755a69e062784177548a1be8c": "gs201",
    "fcb8ecb2aa4d009c2183d696863cd89416d87f8d668ef985c849f9a9e65a157e": "zuma",
    "c9118646d95452bec2bb7369f1e130257e8cfa29ad309d3fe64ecd1d7e8de886": "zuma",
    "29b1d27812c71520f67caace6c7e815cec40f9f52095b553f6ed5bd91e7ba9c7": "zuma",
    "2aa0202bc96361af4252781e53b2718bd85cb533c8127c286eb228a5da80dc3f": "zuma",
}
TAG_NAMES = {
    "APBL": "BL1",
    "EPBL": "PBL",
    "DPM": "DPM",
    "BL2": "BL2",
    "GSA1": "GSA",
    "GSAF": "GSA firmware",
    "ABL": "ABL",
    "TZSW": "TZSW",
    "LDFW": "LDFW",
    "BL31": "BL31",
    "GCF": "GCF",
}


@dataclass
class StageInfo:
    name: str
    size: int
    tag: str = ""
    flags: int = 0
    usb: bool = False
    family: str = ""
    notes: tuple[str, ...] = ()

    @property
    def stage(self):
        return TAG_NAMES.get(self.tag, "")


def inspect_stage(name: str, data: bytes) -> StageInfo:
    """Describe one signed stage image without changing or trusting it."""
    if len(data) < HEADER:
        return StageInfo(
            name, len(data), notes=("shorter than a 4096-byte stage header",)
        )
    tag = image_tag(data)
    body, flags = struct.unpack_from("<II", data, 0x40C)
    info = StageInfo(
        name,
        len(data),
        tag,
        flags,
        bool(flags & 1),
        KNOWN_FINGERPRINTS.get(data[0x200:0x220].hex(), ""),
    )
    notes = []
    if tag not in TAG_NAMES:
        notes.append("no known stage tag at 0x400; probably not a boot stage")
    elif body != len(data) - HEADER:
        notes.append(
            f"header declares a {body}-byte body, file has {len(data) - HEADER}"
        )
    if tag == "APBL" and not info.usb:
        notes.append(
            "retail BL1: no signed USB-boot flag, the ROM will not boot it over USB"
        )
    info.notes = tuple(notes)
    return info


def inspect_file(path: Path) -> list[StageInfo]:
    """Stage image, FBPK bootloader or ZIP (factory image or recovery pack)."""
    if zipfile.is_zipfile(path):
        results = []
        with zipfile.ZipFile(path) as archive:
            entries = [
                e
                for e in archive.infolist()
                if not e.is_dir() and e.filename.endswith((".img", ".bin"))
            ]
            # A factory ZIP: only its bootloader matters (radio images are huge).
            bootloaders = [
                e
                for e in entries
                if PurePosixPath(e.filename).name.startswith("bootloader")
            ]
            for entry in bootloaders or entries:
                if entry.file_size > MAX_IMAGE:
                    note = f"skipped: larger than {MAX_IMAGE // 1024**2} MiB"
                    results.append(
                        StageInfo(entry.filename, entry.file_size, notes=(note,))
                    )
                    continue
                results += inspect_blob(entry.filename, zip_read(archive, entry))
        if not results:
            raise EubError("ZIP contains no .img or .bin files.")
        return results
    return inspect_blob(path.name, bounded_read(path))


def inspect_blob(name: str, data: bytes) -> list[StageInfo]:
    if data[:4] == b"FBPK":
        pack = parse_fbpk(data)
        return [
            inspect_stage(f"{name}:{part}", image)
            for part, image in pack.images.items()
        ]
    return [inspect_stage(name, data)]
