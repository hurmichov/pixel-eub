"""Read-only environment diagnostics; never open or modify the phone."""

import os
import shutil
import sys
from pathlib import Path

from . import EubError, __version__
from .transport import ports, serial_module


def diagnose(cache_dir: Path, fastboot: str = "fastboot") -> int:
    failures = 0

    def report(status, message):
        nonlocal failures
        print(f"[{status}] {message}")
        if status == "FAIL":
            failures += 1

    print(f"pixel-eub {__version__} doctor")
    report("OK", f"Python {sys.version.split()[0]}: {sys.executable}")
    try:
        serial = serial_module()
        report("OK", f"pyserial {getattr(serial, '__version__', 'unknown')}")
        found = ports()
    except (EubError, OSError) as exc:
        report("FAIL", str(exc))
        found = []
    if not found:
        report(
            "INFO",
            "No Pixel ROM USB port (18d1:4f00). If a phone is connected, "
            "check its ROM mode and USB data cable; try 'pixel-eub detect --all'.",
        )
    for port in found:
        if os.name == "posix":
            if os.access(port.device, os.R_OK | os.W_OK):
                report("OK", f"ROM port {port.device}: read/write permission available")
            else:
                report(
                    "FAIL",
                    f"ROM port {port.device}: no read/write permission. "
                    "Configure the serial group/udev rule or use boot's port recovery.",
                )
        else:
            report("INFO", f"ROM port {port.device}: access must be checked by boot")
    report(
        "INFO", "Ports were not opened; busy-port status and recovery are not tested."
    )

    executable = shutil.which(os.path.expanduser(fastboot))
    if executable:
        report("OK", f"fastboot executable found: {executable} (not executed)")
    else:
        report(
            "WARN",
            "fastboot not found. Install Platform-Tools or pass --fastboot PATH. "
            "ROM uploads can run, but the final result cannot be confirmed.",
        )

    target = cache_dir.expanduser().absolute()
    ancestor = target
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if not ancestor.is_dir():
        report("FAIL", f"Cache path {target}: {ancestor} is not a directory")
    elif not os.access(ancestor, os.W_OK | os.X_OK):
        report("FAIL", f"Cache path {target}: no write/search permission on {ancestor}")
    else:
        report("OK", f"Cache path {target}: permission check passed (no files created)")
        try:
            free = shutil.disk_usage(ancestor).free / 1024**3
            report(
                "INFO",
                f"Cache disk free: {free:.2f} GiB; factory ZIPs may need several GiB",
            )
        except OSError as exc:
            report("WARN", f"Cannot read disk free space: {exc}")

    if sys.platform.startswith("linux"):
        for tool in ("sudo", "setfacl", "fuser"):
            location = shutil.which(tool)
            report(
                "OK" if location else "WARN",
                f"Optional port-recovery tool {tool}: {location or 'not found'}",
            )
    print(
        f"Diagnostics finished: {failures} failure(s). No USB uploads or system changes performed."
    )
    return 1 if failures else 0
