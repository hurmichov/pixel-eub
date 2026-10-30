"""USB CDC discovery, reconnect matching and read-only fastboot verification."""

import errno
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import EubError

try:
    import termios
except ImportError:  # Windows
    SERIAL_IO_ERRORS = (OSError,)
else:
    # termios.error is not an OSError on every supported Python version.
    SERIAL_IO_ERRORS = (OSError, termios.error)

VID, PID = 0x18D1, 0x4F00


class NoDevice(EubError):
    """No matching USB port is currently enumerated."""


class PortUnavailable(EubError):
    """An enumerated serial port is not ready to open yet."""


class SerialLink:
    """Normalize OS-specific CDC errors at the transport boundary."""

    def __init__(self, port):
        self.port = port

    def _call(self, name, *args):
        try:
            return getattr(self.port, name)(*args)
        except SERIAL_IO_ERRORS as exc:
            if isinstance(exc, OSError):
                raise
            code = (
                exc.args[0] if exc.args and isinstance(exc.args[0], int) else errno.EIO
            )
            raise OSError(code, str(exc)) from exc

    def read(self, size):
        return self._call("read", size)

    def write(self, data):
        return self._call("write", data)

    def flush(self):
        return self._call("flush")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        try:
            self._call("close")
        except OSError as exc:
            if not disconnected(exc):
                raise


def serial_module():
    try:
        import serial
    except ImportError as exc:
        raise EubError(
            "pyserial is missing from this Python installation. Install the system pyserial package; see README.md (running without venv)."
        ) from exc
    return serial


@dataclass(frozen=True)
class Port:
    device: str
    serial: str | None = None
    location: str | None = None
    description: str = ""
    vid: int | None = None
    pid: int | None = None


def ports(all_ports=False) -> list[Port]:
    serial_module()
    from serial.tools import list_ports

    found = [
        Port(p.device, p.serial_number, p.location, p.description, p.vid, p.pid)
        for p in list_ports.comports()
        if all_ports or (p.vid, p.pid) == (VID, PID)
    ]
    # macOS exposes call-in/call-out twins for the same CDC interface.
    callouts = {
        p.device.replace("/dev/cu.", "/dev/tty.", 1)
        for p in found
        if p.device.startswith("/dev/cu.")
    }
    return sorted(
        (p for p in found if p.device not in callouts), key=lambda p: p.device
    )


def find_port(explicit=None, *, wait=0, original: Port | None = None) -> Port:
    deadline = time.monotonic() + wait
    while True:
        candidates = ports()
        if original:
            if original.serial:
                candidates = [p for p in candidates if p.serial == original.serial]
            elif original.location:
                candidates = [p for p in candidates if p.location == original.location]
            else:
                candidates = [p for p in candidates if p.device == original.device]
        elif explicit:
            candidates = [p for p in candidates if p.device == explicit]
            # Explicit PTYs/unlisted CDC devices still must supply a recognized
            # ROM identity before the first payload can be uploaded.
            if not candidates and (os.name == "nt" or Path(explicit).exists()):
                return Port(explicit)
        if len(candidates) > 1:
            raise EubError(
                "Multiple Pixel ROM devices; choose --port explicitly: "
                + ", ".join(p.device for p in candidates)
            )
        if candidates:
            return candidates[0]
        if time.monotonic() >= deadline:
            raise NoDevice(
                "Pixel ROM Recovery USB device (18d1:4f00) not found. Use detect --all or --port; check the USB cable and ROM mode."
            )
        time.sleep(0.25)


def _open_port_once(port: Port):
    serial = serial_module()
    options = {
        "port": port.device,
        "baudrate": 115200,
        "timeout": 0.2,
        "write_timeout": 10,
        "bytesize": serial.EIGHTBITS,
        "parity": serial.PARITY_NONE,
        "stopbits": serial.STOPBITS_ONE,
        "xonxoff": False,
        "rtscts": False,
        "dsrdtr": False,
    }
    if os.name == "posix":
        options["exclusive"] = True
    return SerialLink(serial.Serial(**options))


def open_port(port: Port, *, recovery=None):
    try:
        return _open_port_once(port)
    except SERIAL_IO_ERRORS as exc:
        if recovery is not None:
            from .port_access import ACCESS_ERRORS, error_number

            if error_number(exc) in ACCESS_ERRORS:
                return recovery.recover(port, exc, _open_port_once)
        raise PortUnavailable(
            f"Cannot open {port.device}: {exc}. Check serial-group permissions and whether ModemManager is using the port."
        ) from exc


def disconnected(exc: OSError) -> bool:
    serial = serial_module()
    if isinstance(exc, serial.SerialTimeoutException):
        return False
    if isinstance(exc, serial.SerialException):
        # pyserial uses SerialException for EOF/removal without an errno.
        return exc.errno in (None, errno.EIO, errno.ENODEV, errno.ENXIO, errno.EBADF)
    return exc.errno in (errno.EIO, errno.ENODEV, errno.ENXIO, errno.EBADF, errno.EPIPE)


class Fastboot:
    def __init__(self, executable="fastboot", serial=None):
        self.executable = shutil.which(executable)
        self.serial = serial
        if not self.executable:
            raise EubError(
                "fastboot not found. Install Android SDK Platform-Tools or pass --fastboot /path/to/fastboot."
            )

    def run(self, *args, serial=None):
        cmd = [self.executable]
        if serial:
            cmd.extend(["-s", serial])
        result = subprocess.run(
            cmd + list(args), capture_output=True, text=True, timeout=3, check=False
        )
        return result.returncode, result.stdout + result.stderr

    def devices(self) -> set[str]:
        code, output = self.run("devices")
        if code:
            raise EubError(f"fastboot devices failed: {output.strip()}")
        return {
            parts[0]
            for line in output.splitlines()
            if len(parts := line.split()) >= 2 and parts[1] == "fastboot"
        }

    def verify(self, model) -> str | None:
        try:
            current = self.devices()
        except subprocess.TimeoutExpired:
            return None
        if self.serial:
            current &= {self.serial}
        if len(current) > 1:
            raise EubError(
                "Several fastboot devices are connected; use --fastboot-serial to select the target."
            )
        if not current:
            return None
        serial = next(iter(current))
        try:
            code, product = self.run("getvar", "product", serial=serial)
            if code or not re.search(
                r"(?:^|\s)product:\s*" + re.escape(model.codename) + r"(?:\s|$)",
                product,
            ):
                raise EubError(
                    f"Fastboot appeared ({serial}), but product does not confirm {model.codename}: {product.strip()}"
                )
            code, userspace = self.run("getvar", "is-userspace", serial=serial)
        except subprocess.TimeoutExpired:
            return None
        if code == 0 and re.search(r"is-userspace:\s*yes", userspace):
            raise EubError("Phone entered fastbootd, not bootloader fastboot.")
        return serial
