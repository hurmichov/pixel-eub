"""Raw serial capture for research: every ROM byte and every upload, timestamped.

Each line of the capture file is one JSON object. Received bytes are kept in
full; uploads are recorded by length, first 64 bytes and SHA-256 so captures
stay small and never contain whole firmware images.
"""

import hashlib
import json
import time
from pathlib import Path


class Capture:
    def __init__(self, target: Path):
        target = target.expanduser()
        if target.is_dir():
            target = target / f"capture-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        self.path = target
        self.start = time.monotonic()
        self.connection = 0

    def record(self, **fields):
        fields = {
            "t": round(time.monotonic() - self.start, 4),
            "conn": self.connection,
            **fields,
        }
        # Open per record: nothing is lost if the run is interrupted.
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(fields) + "\n")

    def wrap(self, link, port):
        self.connection += 1
        self.record(event="open", port=port)
        return CaptureLink(link, self)


class CaptureLink:
    def __init__(self, link, capture):
        self.link = link
        self.capture = capture

    def read(self, size):
        try:
            data = self.link.read(size)
        except OSError as exc:
            self.capture.record(event="read-error", detail=str(exc))
            raise
        if data:
            self.capture.record(rx=bytes(data).hex())
        return data

    def write(self, data):
        data = bytes(data)
        try:
            count = self.link.write(data)
        except OSError as exc:
            self.capture.record(event="write-error", detail=str(exc), length=len(data))
            raise
        sent = data[: count or 0]
        self.capture.record(
            tx=len(sent), head=sent[:64].hex(), sha256=hashlib.sha256(sent).hexdigest()
        )
        return count

    def flush(self):
        return self.link.flush()
