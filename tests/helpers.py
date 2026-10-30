import struct
import zlib


def signed(tag, body=b"body", flags=1):
    data = bytearray(4096)
    data[0x400:0x404] = tag.encode().ljust(4, b"\0")
    struct.pack_into("<II", data, 0x40C, len(body), flags)
    return bytes(data) + body


def fbpk(entries, platform="zuma", product="husky"):
    data = bytearray(112 + 104 * len(entries))
    struct.pack_into("<4sIII", data, 0, b"FBPK", 2, 112, 104)
    data[16 : 16 + len(platform)] = platform.encode()
    struct.pack_into("<II", data, 100, 16, len(entries))
    for i, (name, payload) in enumerate(entries):
        data.extend(bytes((-len(data)) % 16))
        offset = len(data)
        data.extend(payload)
        base = 112 + i * 104
        struct.pack_into("<I", data, base, 1)
        data[base + 4 : base + 4 + len(name)] = name.encode()
        data[base + 40 : base + 40 + len(product)] = product.encode()
        struct.pack_into(
            "<QQII", data, base + 80, offset, len(payload), 1, zlib.crc32(payload)
        )
    struct.pack_into("<I", data, 108, len(data))
    return data


class FakeLink:
    def __init__(self, chunks, short_write=None):
        self.chunks = iter(chunks)
        self.output = bytearray()
        self.short_write = short_write
        self.closed = False

    def read(self, count):
        try:
            return next(self.chunks)
        except StopIteration:
            raise OSError(5, "disconnected") from None

    def write(self, data):
        count = (
            min(len(data), self.short_write)
            if self.short_write is not None
            else len(data)
        )
        self.output.extend(data[:count])
        return count

    def flush(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True
