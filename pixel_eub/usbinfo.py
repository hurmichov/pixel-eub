"""Passive USB descriptor dumps for research (Linux sysfs, no root, no transfers).

The kernel reads every descriptor at enumeration and exposes the raw bytes in
sysfs. Reading them never talks to the phone, so it cannot disturb the ROM.
"""

import struct
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import EubError

ATTRIBUTES = (
    "idVendor",
    "idProduct",
    "bcdDevice",
    "version",
    "speed",
    "manufacturer",
    "product",
    "serial",
    "configuration",
    "bNumConfigurations",
    "bMaxPower",
    "busnum",
    "devnum",
    "devpath",
)
# Fields compared between connections to show what a stage changed.
COMPARED = ("idVendor", "idProduct", "bcdDevice", "speed", "manufacturer", "product")


@dataclass
class UsbInfo:
    sysfs: str
    attributes: dict[str, str]
    raw: bytes
    lines: list[str] = field(default_factory=list)


def usb_device_dir(port: str, sys_root: Path = Path("/sys")) -> Path:
    """Map /dev/ttyACM0 to its USB device directory in sysfs."""
    if not sys.platform.startswith("linux") and sys_root == Path("/sys"):
        raise EubError("USB descriptor dumps need Linux sysfs.")
    tty = sys_root / "class" / "tty" / Path(port).resolve().name / "device"
    if not tty.exists():
        raise EubError(f"No sysfs USB entry for {port}.")
    path = tty.resolve()
    # tty -> USB interface (1-2:1.0) -> USB device (1-2), which owns descriptors.
    for candidate in (path, *path.parents):
        if (candidate / "descriptors").is_file() and (candidate / "idVendor").is_file():
            return candidate
        if candidate == sys_root:
            break
    raise EubError(f"No USB descriptors found for {port}.")


def read_info(device_dir: Path) -> UsbInfo:
    attributes = {}
    for name in ATTRIBUTES:
        try:
            attributes[name] = (device_dir / name).read_text().strip()
        except OSError:
            continue
    raw = (device_dir / "descriptors").read_bytes()[:65536]
    return UsbInfo(str(device_dir), attributes, raw, decode(raw))


def decode(raw: bytes) -> list[str]:
    """Readable standard descriptors; unknown ones are kept as hex."""
    lines, offset = [], 0
    while offset + 2 <= len(raw):
        length, kind = raw[offset], raw[offset + 1]
        if length < 2 or offset + length > len(raw):
            lines.append(f"@{offset}: malformed descriptor (length {length})")
            break
        d = raw[offset : offset + length]
        if kind == 1 and length >= 18:
            usb, cls, sub, proto, packet, vid, pid, rev, im, ip, iser, configs = (
                struct.unpack_from("<HBBBBHHHBBBB", d, 2)
            )
            lines.append(
                f"DEVICE usb {usb:04x} class {cls:02x}/{sub:02x}/{proto:02x} "
                f"ep0 {packet} id {vid:04x}:{pid:04x} bcdDevice {rev:04x} "
                f"strings m{im} p{ip} s{iser} configs {configs}"
            )
        elif kind == 2 and length >= 9:
            total, interfaces, value, istr, attrs, power = struct.unpack_from(
                "<HBBBBB", d, 2
            )
            lines.append(
                f"CONFIG {value} total {total} interfaces {interfaces} "
                f"attributes {attrs:02x} max {power * 2} mA string {istr}"
            )
        elif kind == 4 and length >= 9:
            number, alt, endpoints, cls, sub, proto, istr = struct.unpack_from(
                "<BBBBBBB", d, 2
            )
            lines.append(
                f"  INTERFACE {number} alt {alt} endpoints {endpoints} "
                f"class {cls:02x}/{sub:02x}/{proto:02x} string {istr}"
            )
        elif kind == 5 and length >= 7:
            address, attrs, packet, interval = struct.unpack_from("<BBHB", d, 2)
            direction = "IN" if address & 0x80 else "OUT"
            transfer = ("control", "isochronous", "bulk", "interrupt")[attrs & 3]
            lines.append(
                f"    ENDPOINT {address:02x} {direction} {transfer} "
                f"max {packet & 0x7FF} interval {interval}"
            )
        elif kind == 0x0B and length >= 8:
            first, count, cls, sub, proto = struct.unpack_from("<BBBBB", d, 2)
            lines.append(
                f"  ASSOCIATION interfaces {first}+{count} "
                f"class {cls:02x}/{sub:02x}/{proto:02x}"
            )
        elif kind == 0x24:
            lines.append(f"    CDC subtype {d[2]:02x}: {d[3:].hex()}")
        else:
            lines.append(f"  descriptor type {kind:02x}: {d[2:].hex()}")
        offset += length
    if offset < len(raw):
        lines.append(f"{len(raw) - offset} trailing bytes: {raw[offset:].hex()}")
    return lines


def save(info: UsbInfo, folder: Path, label: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"usb-{time.strftime('%Y%m%d-%H%M%S')}-{label}"
    (folder / f"{stem}.bin").write_bytes(info.raw)
    text = [f"sysfs: {info.sysfs}"]
    text += [f"{key}: {value}" for key, value in info.attributes.items()]
    text += ["", *info.lines, "", info.raw.hex()]
    (folder / f"{stem}.txt").write_text("\n".join(text) + "\n", encoding="utf-8")
    return folder / f"{stem}.txt"


def changes(previous: UsbInfo | None, current: UsbInfo) -> list[str]:
    if previous is None:
        return []
    result = [
        f"{key} {previous.attributes.get(key, '-')} -> {current.attributes.get(key, '-')}"
        for key in COMPARED
        if previous.attributes.get(key) != current.attributes.get(key)
    ]
    if previous.raw != current.raw:
        result.append(f"descriptors {len(previous.raw)} -> {len(current.raw)} bytes")
    return result


def summary(info: UsbInfo) -> str:
    a = info.attributes
    return (
        f"{a.get('idVendor', '?')}:{a.get('idProduct', '?')} "
        f"bcdDevice {a.get('bcdDevice', '?')} USB {a.get('version', '?').strip()} "
        f"{a.get('speed', '?')} Mbit/s, product {a.get('product', '-')!r}, "
        f"{len(info.raw)} descriptor bytes"
    )
