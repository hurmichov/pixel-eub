"""EUB messages and DNW uploads. No writes to persistent phone storage."""

import re
import struct
import time
from dataclasses import dataclass, field

from . import EubError
from .compatibility import CompatibilityOverride
from .models import soc_for


class MessageReader:
    """Preserve partial lines and handle ROM's unterminated clear-to-send C."""

    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data: bytes) -> list[str]:
        messages = []
        for value in data:
            if value in (10, 13, 0):
                if self.buffer:
                    messages.append(self.buffer.decode("ascii", errors="replace"))
                    self.buffer.clear()
            elif value == ord("C") and not self.buffer:
                messages.append("C")
            else:
                self.buffer.append(value)
                if len(self.buffer) > 16384:
                    raise EubError("ROM message exceeds length limit.")
        return messages


def identity(message: str) -> str | None:
    match = re.fullmatch(
        r"(?:exynos_usb_booting:eub|eub:req):([^:]+)(?::[A-Za-z0-9_]+)?",
        message,
        re.IGNORECASE,
    )
    return match[1] if match else None


def frame(payload: bytes, checksum="ffff") -> bytes:
    if not payload or len(payload) > 0xFFFFFFFF - 10:
        raise EubError("Invalid DNW payload size.")
    trailer = 0xFFFF if checksum == "ffff" else sum(payload) & 0xFFFF
    return (
        b"\x1bDNW"
        + struct.pack("<I", len(payload) + 10)
        + payload
        + struct.pack("<H", trailer)
    )


def write_all(link, data: bytes, deadline=30):
    end = time.monotonic() + deadline
    view = memoryview(data)
    while view:
        if time.monotonic() > end:
            raise EubError("USB upload timed out.")
        wrote = link.write(view[:10240])
        if not wrote or wrote < 0 or wrote > len(view[:10240]):
            raise EubError("USB serial write made no progress.")
        view = view[wrote:]
        link.flush()


@dataclass
class Session:
    serial: str | None = None
    soc: str | None = None
    sent: list[str] = field(default_factory=list)
    acknowledged: list[str] = field(default_factory=list)
    attempts: dict[str, int] = field(default_factory=dict)
    compatibility: CompatibilityOverride | None = None

    def note_identity(self, serial: str, expected_soc: str | None):
        soc = soc_for(serial)
        if self.serial is not None and self.serial != serial:
            raise EubError(
                "ROM serial changed: refusing to continue on a different phone."
            )
        if expected_soc and soc != expected_soc:
            mismatch = (
                f"Connected ROM is {soc}, selected model/pack requires {expected_soc}."
            )
            if self.compatibility is None:
                raise EubError(
                    mismatch
                    + " Upload refused; use --override for explicit confirmation."
                )
            self.compatibility.check(mismatch)
        self.serial, self.soc = serial, soc


def probe(link, timeout=10) -> str:
    reader, start, heard, nudged = MessageReader(), time.monotonic(), False, False
    while time.monotonic() - start < timeout:
        data = link.read(4096)
        heard |= bool(data)
        for message in reader.feed(data):
            serial = identity(message)
            if serial:
                soc_for(serial)
                return serial
        if not heard and not nudged and time.monotonic() - start >= 1.5:
            write_all(link, b"\n")
            nudged = True
    raise EubError(
        "No ROM identity received. Check USB cable, ROM Recovery mode and serial permissions."
    )


def serve(
    link,
    sources,
    session: Session,
    expected_soc: str,
    *,
    idle_timeout=15,
    settle=3,
    checksum="ffff",
    verbose=False,
    report=print,
):
    """Serve one connection. A quiet/disconnected port is never proof of fastboot."""
    reader, pending = MessageReader(), None
    started = last = time.monotonic()
    heard, nudged, sent_here, identity_logged = False, False, False, False
    while True:
        data = link.read(4096)
        if data:
            last, heard = time.monotonic(), True
        for message in reader.feed(data):
            if verbose:
                report(f"  ROM < {message!r}")
            serial = identity(message)
            if serial:
                session.note_identity(serial, expected_soc)
                sources.use_soc(session.soc)
                if not identity_logged:
                    report(f"ROM identity verified: {serial} ({session.soc})")
                    identity_logged = True
            if "header fail" in message.lower() or message.lower().startswith(
                ("eub:nak:", "irom_booting_failure")
            ):
                raise EubError(
                    f"ROM rejected boot: {message}. Check signed images, model and anti-rollback level; retrying cannot bypass signatures."
                )
            request = re.fullmatch(
                r"eub:req:([^:]+):([A-Za-z0-9_]+)", message, re.IGNORECASE
            )
            if request:
                stage = request[2].upper()
                if pending and pending != stage:
                    raise EubError(
                        f"ROM changed request from {pending} to {stage} before clear-to-send."
                    )
                if pending != stage:
                    report(f"ROM requests {stage}")
                pending = stage
            elif message == "C" and pending:
                if not session.serial:
                    raise EubError("Refusing upload before ROM identity is verified.")
                stage, pending = pending, None
                session.attempts[stage] = session.attempts.get(stage, 0) + 1
                if session.attempts[stage] > 5:
                    raise EubError(
                        f"ROM repeated {stage} more than five times; stopping retry loop."
                    )
                payload = sources.payload(stage)
                report(f"Sending {stage}: {len(payload)} bytes")
                write_all(link, frame(payload, checksum))
                session.sent.append(stage)
                report(f"Uploaded {stage}: {len(payload)} bytes")
                last, sent_here = time.monotonic(), True
            elif message.lower().startswith("eub:ack:"):
                stage = message.rsplit(":", 1)[-1].upper()
                session.acknowledged.append(stage)
                report(f"ROM acknowledged {stage}")
        now = time.monotonic()
        if not heard and not nudged and now - started >= 1.5:
            write_all(link, b"\n")
            nudged = True
        if sent_here and pending is None and now - last > settle:
            return
        if now - last > idle_timeout or now - started > 180:
            raise EubError(
                f"ROM timed out (pending stage: {pending or 'none'}); fastboot is not confirmed."
            )
