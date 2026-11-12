"""Adapter for the separately supplied GS101 USB recovery implementation."""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import EubError

MODELS = ("bluejay", "oriole", "raven")
REQUIRED = (
    "bl1_recovery.bin",
    "pbl_recovery.img",
    "BL2B_recovery.bin",
    "ABLB_recovery.bin",
    "split_stages/BL2.bin",
    "split_stages/BL31.bin",
    "split_stages/BL31B.bin",
    "split_stages/ABL.bin",
    "partitions/tzsw.img",
    "partitions/ldfw.img",
    "partitions/gsa.img",
)
WARNING = (
    "GS101 uses modified boot stages and changes security-related checks. "
    "ABL reports unlocked/non-production in the temporary recovery environment. "
    "Recovery can damage the device or cause data loss; subsequent flashing is separate. "
    "Connect only the intended phone and disconnect other fastboot devices."
)


def validate_profile(device, profile):
    profile = Path(profile).expanduser().resolve()
    if device not in MODELS:
        raise EubError("GS101 device must be bluejay, oriole, or raven")
    missing = [name for name in REQUIRED if not (profile / name).is_file()]
    if missing:
        raise EubError("Incomplete GS101 profile: " + ", ".join(missing))
    metadata = profile / "metadata.json"
    if not metadata.is_file():
        raise EubError(
            "Profile metadata.json is required to check the selected device."
        )
    try:
        data = json.loads(metadata.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise EubError(f"Invalid profile metadata: {exc}") from exc
    if data.get("device") != device:
        raise EubError(
            f"Profile device {data.get('device')!r} does not match {device}."
        )
    return profile


def run(device, profile):
    if sys.platform != "linux":
        raise EubError("GS101 recovery currently requires Linux and libusb.")
    profile = validate_profile(device, profile)
    compiler = shutil.which("gcc")
    if not compiler:
        raise EubError(
            "Install gcc and libusb-1.0 development headers to build GS101 recovery."
        )
    source = Path(__file__).parent / "gs101_assets" / "gs101_usb_revive.c"
    print(WARNING, flush=True)
    print(
        "USB access is required; configure permissions before starting. Stop with Ctrl+C.",
        flush=True,
    )
    with tempfile.TemporaryDirectory(prefix="pixel-eub-gs101-") as work:
        binary = Path(work) / "gs101_usb_revive"
        subprocess.run(
            [compiler, "-Wall", "-O2", str(source), "-o", str(binary), "-lusb-1.0"],
            check=True,
        )
        print(
            "Recovery logs are written to ./logs. Fastboot USB detection does not verify model identity.",
            flush=True,
        )
        return subprocess.run([str(binary), "--dir", str(profile)]).returncode
