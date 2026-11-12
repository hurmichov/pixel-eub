"""EUB messages and DNW uploads. No writes to persistent phone storage."""

import re
import struct
import time
from dataclasses import dataclass, field

from . import EubError
from .compatibility import CompatibilityOverride
from .images import EMPTY_DPM
from .models import soc_for

HEX_DIGITS = frozenset(b"0123456789abcdefABCDEF")


class MessageReader:
    """Preserve partial lines and handle ROM's unterminated clear-to-send C.

    CR, LF and NUL end a message, except inside an iROM failure dump: there
    NUL separates the 32-bit words of one line
    (``irom_booting_failure::\\0 00000001\\0 e0000080\\0 ...\\r\\n``).
    """

    def __init__(self):
        self.buffer = bytearray()

    def in_dump(self):
        return self.buffer.lower().startswith(b"irom_booting_failure") or bool(
            re.fullmatch(rb"[0-9a-fA-F\0]+", self.buffer)
        )

    def flush(self, messages):
        text = self.buffer.rstrip(b"\0")
        if text:
            messages.append(text.decode("ascii", errors="replace"))
        self.buffer.clear()

    def feed(self, data: bytes) -> list[str]:
        messages = []
        for value in data:
            if value in (10, 13):
                self.flush(messages)
            elif value == 0:
                if self.buffer and self.in_dump():
                    if not self.buffer.endswith(b"\0"):
                        self.buffer.append(0)
                else:
                    self.flush(messages)
            elif value == ord("C") and not self.buffer:
                messages.append("C")
            else:
                if value not in HEX_DIGITS and 0 in self.buffer and self.in_dump():
                    # A dump ended without a newline: the next message began
                    # after its last NUL.
                    split = self.buffer.rindex(0)
                    rest = self.buffer[split + 1 :]
                    del self.buffer[split:]
                    self.flush(messages)
                    self.buffer.extend(rest)
                self.buffer.append(value)
                if len(self.buffer) > 16384:
                    raise EubError("ROM message exceeds length limit.")
        return messages


def dump_words(text: str) -> list[str] | None:
    """Lower-case 32-bit words when text is only NUL-separated hex words."""
    words = [word for word in text.split("\0") if word]
    if words and all(re.fullmatch(r"[0-9a-fA-F]{8}", word) for word in words):
        return [word.lower() for word in words]
    return None


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
    forced_pending: list[str] | None = None
    first_request: str | None = None
    release_hint: bool = False
    # Research listening after irom_booting_failure (see listen_after).
    failure_at: float | None = None
    listen_until: float | None = None
    listen_disconnected: bool = False

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


DPM_FIRST = (
    "ROM requested DPM before BL1: the phone started BL1 from internal storage "
    "(UFS), not over USB. Users report that this BL1 breaks GSA/GSC communication "
    "with recovery images: the chain stalls after BL3B and then boots with errors."
)
HOLD_BUTTONS = (
    "Unplug the phone, hold Power and both volume buttons, plug USB in and keep "
    "holding until the ROM requests BL1; release after it requests DPM."
)


# Field 7 of the iROM failure dump, as observed on gs101 with three different
# BL1 uploads. The meanings are a hypothesis from that pattern, not documented.
PANIC_MARKERS = ["a0031111", "a0031112", "a0121113"]
PANIC_CODES = {
    0: "no code recorded; observed consistently after one signed header byte was changed",
    0x400: "likely header or tag rejected",
    0x500: "likely USB boot not permitted for this image",
    0x600: "likely signature or key rejected",
}


def panic_fields(trace):
    """Code, status, value and address after the marker words, when present."""
    for index in range(len(trace) - 3):
        if trace[index : index + 3] == PANIC_MARKERS:
            fields = [int(word, 16) for word in trace[index + 3 : index + 7]]
            return fields + [None] * (4 - len(fields))
    return None


def panic_code(trace):
    fields = panic_fields(trace)
    return fields[0] if fields else None


def describe_panic(trace):
    """One readable line for the 32-bit words of an iROM failure dump."""
    if not trace:
        return "ROM failure dump: no words received."
    fields, words = panic_fields(trace), " ".join(trace)
    if not fields or fields[0] is None:
        return f"ROM failure dump (unrecognised layout): {words}"
    code, status, value, address = fields
    meaning = PANIC_CODES.get(code, "unknown code")
    if trace and trace[0] != "e0000080":
        meaning += f"; first word {trace[0]}"
    entries = trace.count("e0000080")
    if entries:
        meaning += f"; {entries}x e0000080"
    details = "".join(
        f", {name} {number:#010x}"
        for name, number in (("status", status), ("value", value), ("address", address))
        if number is not None
    )
    return f"ROM failure dump: code {code:#x} ({meaning}; hypothesis){details}. Words: {words}"


def listen_after(link, reader, deadline, origin, report, verbose=False):
    """Research: read until deadline, timing ROM messages from the failure (origin).

    Returns True when the port closed before the deadline.
    """
    repeats = 0
    try:
        while time.monotonic() < deadline:
            try:
                data = link.read(4096)
            except OSError as exc:
                report(f"Port closed at +{time.monotonic() - origin:.2f} s: {exc}")
                return True
            for message in reader.feed(data):
                if verbose:
                    report(f"  ROM < {message.replace(chr(0), ' ')!r}")
                elapsed = time.monotonic() - origin
                if message.lower().startswith("irom_booting_failure"):
                    repeats += 1
                    words = dump_words(message.partition("::")[2]) or []
                    first = words[0] if words else "-"
                    code = panic_code(words)
                    report(
                        f"Repeated failure {repeats} at +{elapsed:.2f} s: first word {first}"
                        + (f", code {code:#x}" if code is not None else "")
                    )
                elif not dump_words(message):
                    report(f"ROM after the failure at +{elapsed:.2f} s: {message!r}")
        return False
    finally:
        report(f"{repeats} repeated failure line(s) on this connection.")


def check_first_request(session, stage, policy, report):
    """Apply the model's BL1-before-DPM expectation to the session's first request."""
    first = session.first_request is None
    if first:
        session.first_request = stage
    if policy == "allow":
        return
    if first and stage == "DPM":
        if policy == "stop":
            raise EubError(
                f"{DPM_FIRST} Nothing was uploaded. {HOLD_BUTTONS} "
                "Use --allow-dpm-first to continue anyway."
            )
        report(f"WARNING: {DPM_FIRST} {HOLD_BUTTONS}")
    elif stage == "DPM" and session.first_request == "BL1" and not session.release_hint:
        session.release_hint = True
        report("ROM booted USB BL1 and requests DPM: you can release the buttons now.")


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
    confirm_panic=None,
    allow_cached_identity=False,
    dpm_first="allow",
    ack_timeout=600,
    present=None,
    listen_after_failure=0,
):
    """Serve one connection. A quiet/disconnected port is never proof of fastboot.

    After an upload the ROM may stay silent for minutes (GSA1 and BL3B
    verification are reported to take 2-5 minutes or longer), so that wait uses
    ack_timeout instead of idle_timeout. present() tells whether this ROM port
    is still enumerated; without it, a quiet port after an upload ends the
    connection after settle seconds.
    """
    reader, pending = MessageReader(), None
    started = last = time.monotonic()
    heard, nudged, sent_here, identity_logged = False, False, False, False
    # Stage uploaded but not yet acknowledged: the ROM echoes its request and C
    # right after an upload; answering that would send the stage twice.
    awaiting_ack = None
    last_notice = last_presence = started
    trace = []  # 32-bit words the ROM prints before irom_booting_failure

    def upload(stage, forced=False):
        if not session.serial:
            raise EubError("Refusing upload before ROM identity is verified.")
        session.attempts[stage] = session.attempts.get(stage, 0) + 1
        if session.attempts[stage] > 5:
            raise EubError(
                f"ROM repeated {stage} more than five times; stopping retry loop."
            )
        payload = sources.payload(stage)
        label = "Experimental forced upload (no ROM request/C)" if forced else "Sending"
        note = (
            " (empty placeholder: no signed DPM image available)"
            if stage == "DPM" and payload == EMPTY_DPM
            else ""
        )
        report(f"{label} {stage}: {len(payload)} bytes{note}")
        write_all(link, frame(payload, checksum))
        session.sent.append(stage)
        report(f"Uploaded {stage}: {len(payload)} bytes")

    def upload_forced():
        nonlocal last, sent_here
        while session.forced_pending:
            stage = session.forced_pending[0]
            upload(stage, forced=True)
            session.forced_pending.pop(0)
            last, sent_here = time.monotonic(), True

    while True:
        data = link.read(4096)
        if data:
            last, heard = time.monotonic(), True
        for message in reader.feed(data):
            if verbose:
                report(f"  ROM < {message.replace(chr(0), ' ')!r}")
            words = dump_words(message)
            if words:
                # A dump block whose irom_booting_failure:: prefix was missed.
                trace = [*trace, *words][-32:]
                continue
            if message.lower().startswith("irom_booting_failure"):
                # The dump normally follows the prefix in the same line.
                trace = dump_words(message.partition("::")[2]) or trace
                report(describe_panic(trace))
            serial = identity(message)
            if serial:
                session.note_identity(serial, expected_soc)
                sources.use_soc(session.soc)
                if not identity_logged:
                    report(f"ROM identity verified: {serial} ({session.soc})")
                    identity_logged = True
                # Reconnects still require the same phone's fresh ROM identity.
                upload_forced()
            if message.lower().startswith("irom_booting_failure") and confirm_panic:
                if (
                    not session.serial
                    or not session.soc
                    or (not identity_logged and not allow_cached_identity)
                ):
                    raise EubError(
                        "ROM panic before identity verification; refusing forced uploads."
                    )
                if not identity_logged:
                    report(
                        "WARNING: reconnected port has not repeated its ROM identity; "
                        f"using cached session identity {session.serial} ({session.soc}) "
                        "for the explicitly confirmed experimental continuation."
                    )
                    sources.use_soc(session.soc)
                remaining = (
                    session.forced_pending
                    if session.forced_pending is not None
                    else sources.panic_chain(session.sent)
                )
                if not remaining:
                    raise EubError(
                        "ROM panic after the forced chain ended; fastboot is not confirmed."
                    )
                report(f"WARNING: ROM PANIC: {message}")
                report("Experimental remaining chain: " + " -> ".join(remaining))
                confirm_panic()
                session.forced_pending = remaining
                pending = None
                started = last = time.monotonic()
                upload_forced()
                continue
            if "header fail" in message.lower() or message.lower().startswith(
                ("eub:nak:", "irom_booting_failure")
            ):
                hint = (
                    " Pixel 8/8 Pro bootloaders newer than April 2025 are reported to "
                    "reject the public recovery pack (anti-rollback)."
                    if session.soc == "zuma" and "header fail" in message.lower()
                    else ""
                )
                code = panic_code(trace)
                if code is not None:
                    hint += (
                        f" Failure code {code:#x}: {PANIC_CODES.get(code, 'unknown')}."
                    )
                if listen_after_failure and message.lower().startswith(
                    "irom_booting_failure"
                ):
                    session.failure_at = time.monotonic()
                    session.listen_until = session.failure_at + listen_after_failure
                    report(
                        f"Listening {listen_after_failure:g} s after the failure "
                        "(times are from this line); nothing will be uploaded."
                    )
                    session.listen_disconnected = listen_after(
                        link,
                        reader,
                        session.listen_until,
                        session.failure_at,
                        report,
                        verbose,
                    )
                shown = message.split("\0", 1)[0]  # Dump words are reported above.
                raise EubError(
                    f"ROM rejected boot: {shown}. Check signed images, model and anti-rollback level; retrying cannot bypass signatures.{hint}"
                )
            request = re.fullmatch(
                r"eub:req:([^:]+):([A-Za-z0-9_]+)", message, re.IGNORECASE
            )
            if request:
                if session.forced_pending is not None:
                    continue  # The user explicitly selected the host-driven chain.
                stage = request[2].upper()
                check_first_request(session, stage, dpm_first, report)
                if stage == awaiting_ack:
                    continue
                awaiting_ack = None
                if pending and pending != stage:
                    raise EubError(
                        f"ROM changed request from {pending} to {stage} before clear-to-send."
                    )
                if pending != stage:
                    report(f"ROM requests {stage}")
                pending = stage
            elif message == "C" and pending:
                stage, pending = pending, None
                upload(stage)
                awaiting_ack = stage
                last, sent_here = time.monotonic(), True
            elif message.lower().startswith("eub:ack:"):
                awaiting_ack = None
                stage = message.rsplit(":", 1)[-1].upper()
                session.acknowledged.append(stage)
                report(f"ROM acknowledged {stage}")
        now = time.monotonic()
        if not heard and not nudged and now - started >= 1.5:
            write_all(link, b"\n")
            nudged = True
        waiting = sent_here and pending is None
        quiet = now - last
        if waiting and quiet > settle:
            if present is None:
                return
            if now - last_presence >= 2:
                last_presence = now
                if not present():
                    return  # The ROM left this port: follow the handover.
        if quiet > (ack_timeout if waiting else idle_timeout):
            stage = session.sent[-1] if waiting else pending or "none"
            raise EubError(
                f"ROM was silent for {quiet:.0f} s (last stage: {stage}); "
                "fastboot is not confirmed."
            )
        if waiting and quiet > max(settle, idle_timeout) and now - last_notice >= 30:
            last_notice = now
            report(
                f"Waiting for the ROM after {session.sent[-1]}: {quiet:.0f} s. "
                "Verification (for example GSA1 or BL3B) can take several minutes; "
                "keep the phone connected."
            )
