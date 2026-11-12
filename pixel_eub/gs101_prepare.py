#!/usr/bin/env python3
"""
Google Pixel 6 / 6 Pro / 6a (GS101) Factory Bootloader Downloader, Unpacker & USB Recovery Prep Tool.

Supports:
  - bluejay (Pixel 6a)
  - oriole  (Pixel 6)
  - raven   (Pixel 6 Pro)

Workflow:
  1. Fetch latest official Google factory image link from developers.google.com/android/images
     (or use user-supplied --zip / --bootloader).
  2. Download ONLY bootloader-<device>-*.img (~12 MB) via HTTP Range requests from the factory zip
     (or full factory zip with --full-zip).
  3. Unpack FBPK container into partitions/*.img and split 4KB headers vs bodies into split_stages/*.bin.
  4. Dynamically locate AArch64 instructions by semantic patterns and prepare USB RAM-boot stages:
     - BL1: header[0x410]=1, stageVerifyStub@0x02022600, usbBootStub@0x02024900 (callback 0x020200b0),
            and 3 stage validation adjustments for RAM boot.
     - EPBL (pbl.img): 3 USB-boot stage adjustments (body check, SHA256 check, GSA IPC check).
     - BL2B: GSA wait loop skip (if present).
"""

import argparse
import html
import io
import json
import re
import shutil
import struct
import sys
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SUPPORTED_DEVICES = {
    "oriole": "Pixel 6",
    "raven": "Pixel 6 Pro",
    "bluejay": "Pixel 6a",
}

IMAGES_PAGE_URL = "https://developers.google.com/android/images"
IMAGES_COOKIE = "devsite_wall_acks=nexus-image-tos,nexus-ota-tos"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) GS101-Recovery-Prep/1.0"

# Verified precompiled AArch64 machine code from stage_verify_stub.c (0x02022600) and usb_boot_stub.s (0x02024900)
# - stageVerifyStub writes out_size=header[0x40c], isEncrypted(0x02020299)=1, cryptMemSize(0x02020290)=memSize, returns memSize>0
STAGE_VERIFY_STUB_BIN = bytes.fromhex(
    "290c44b9"  # ldr  w9, [x1, #1036]
    "08528052"  # mov  w8, #0x290
    "2a008052"  # mov  w10, #1
    "4840a072"  # movk w8, #0x202, lsl #16
    "3f0100f1"  # cmp  x9, #0
    "490000f9"  # str  x9, [x2]
    "e0079f1a"  # cset w0, ne
    "0a250039"  # strb w10, [x8, #9]
    "090100b9"  # str  w9, [x8]
    "c0035fd6"  # ret
)

# - usbBootStub restores 0x02021990=0xa340, 0x02021a00=0xc4f8, 0x020200b0=0x02022600,
#   verified flags 0x0202029d/0x0202029a=1, stack LR 0x020213d8=0x5664, ret
USB_BOOT_STUB_BIN = bytes.fromhex(
    "003283d2"  # mov  x0, #0x1990
    "4040a0f2"  # movk x0, #0x202, lsl #16
    "01689452"  # mov  w1, #0xa340
    "010000b9"  # str  w1, [x0]
    "004083d2"  # mov  x0, #0x1a00
    "4040a0f2"  # movk x0, #0x202, lsl #16
    "019f9852"  # mov  w1, #0xc4f8
    "010000f9"  # str  x1, [x0]
    "001680d2"  # mov  x0, #0x00b0
    "4040a0f2"  # movk x0, #0x202, lsl #16
    "01c08452"  # mov  w1, #0x2600
    "4140a072"  # movk w1, #0x202, lsl #16
    "010000b9"  # str  w1, [x0]
    "005280d2"  # mov  x0, #0x0290
    "4040a0f2"  # movk x0, #0x202, lsl #16
    "21008052"  # mov  w1, #1
    "01340039"  # strb w1, [x0, #13]
    "01280039"  # strb w1, [x0, #10]
    "007b82d2"  # mov  x0, #0x13d8
    "4040a0f2"  # movk x0, #0x202, lsl #16
    "81cc8a52"  # mov  w1, #0x5664
    "010000f9"  # str  x1, [x0]
    "c0035fd6"  # ret
)

INSN_NOP = bytes.fromhex("1f2003d5")
INSN_MOV_X0_1 = bytes.fromhex("200080d2")


def encode_b(from_off: int, to_off: int) -> bytes:
    delta = to_off - from_off
    if delta % 4 != 0:
        raise ValueError(f"Unaligned branch delta {delta}")
    imm26 = (delta // 4) & 0x03FFFFFF
    return struct.pack("<I", 0x14000000 | imm26)


def find_all(data: bytes, pattern: bytes) -> list[int]:
    hits = []
    pos = 0
    while True:
        idx = data.find(pattern, pos)
        if idx == -1:
            break
        hits.append(idx)
        pos = idx + 1
    return hits


def http_get_bytes(
    url: str, headers: dict[str, str] | None = None, timeout: int = 30
) -> tuple[bytes, dict]:
    req_headers = {"User-Agent": USER_AGENT}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(), dict(resp.info())


def fetch_factory_links(device: str) -> list[dict[str, str]]:
    """Fetch official factory image links for `device` from developers.google.com/android/images."""
    print(
        f"[*] Fetching official Google factory images list from {IMAGES_PAGE_URL} ..."
    )
    raw, _ = http_get_bytes(
        IMAGES_PAGE_URL, headers={"Cookie": IMAGES_COOKIE}, timeout=45
    )
    page = raw.decode("utf-8", errors="replace")

    # Find device section if present, otherwise search full page
    anchor_re = re.compile(rf'id=["\']{re.escape(device)}["\']', re.IGNORECASE)
    m_anchor = anchor_re.search(page)
    section = page[m_anchor.start() :] if m_anchor else page
    if m_anchor:
        # Cut at next device header section if any (e.g. another <h2 id="...">)
        next_h2 = re.search(
            r'<h2\s+[^>]*id=["\'][a-z0-9_-]+["\']', section[50:], re.IGNORECASE
        )
        if next_h2:
            section = section[: 50 + next_h2.start()]

    link_re = re.compile(
        rf'href=["\'](https://dl\.google\.com/dl/android/aosp/{re.escape(device)}-([a-z0-9._-]+)-factory-([0-9a-f]+)\.zip)["\']',
        re.IGNORECASE,
    )
    results = []
    seen = set()
    for m in link_re.finditer(section):
        url, build_id, sha_prefix = m.group(1), m.group(2), m.group(3)
        if url in seen:
            continue
        seen.add(url)
        # Try to capture human-readable version label from surrounding <tr>
        row_start = section.rfind("<tr", 0, m.start())
        row_end = section.find("</tr>", m.end())
        label = build_id
        if row_start != -1 and row_end != -1:
            row_text = re.sub(r"<[^>]+>", " ", section[row_start:row_end])
            row_text = " ".join(html.unescape(row_text).split())
            if row_text:
                label = row_text[:120]
        results.append(
            {"url": url, "build": build_id, "sha_prefix": sha_prefix, "label": label}
        )

    if not results:
        raise RuntimeError(
            f"Could not find factory image links for {device} on {IMAGES_PAGE_URL}"
        )
    return results


class HttpRangeReader(io.RawIOBase):
    """Seekable read-only file-like object backed by HTTP Range requests (for zipfile)."""

    def __init__(self, url: str):
        self.url = url
        req = urllib.request.Request(
            url, method="HEAD", headers={"User-Agent": USER_AGENT}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            self.length = int(resp.headers.get("Content-Length", "0"))
            accept_ranges = resp.headers.get("Accept-Ranges", "").lower()
        if self.length <= 0 or accept_ranges == "none":
            raise RuntimeError(
                "Server does not support HTTP Range or Content-Length is missing"
            )
        self.pos = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            self.pos = offset
        elif whence == io.SEEK_CUR:
            self.pos += offset
        elif whence == io.SEEK_END:
            self.pos = self.length + offset
        self.pos = max(0, min(self.pos, self.length))
        return self.pos

    def tell(self) -> int:
        return self.pos

    def read(self, size: int = -1) -> bytes:
        if self.pos >= self.length or size == 0:
            return b""
        end = self.length - 1 if size < 0 else min(self.length - 1, self.pos + size - 1)
        data, _ = http_get_bytes(
            self.url, headers={"Range": f"bytes={self.pos}-{end}"}, timeout=60
        )
        self.pos += len(data)
        return data


def download_full_file(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"[*] Downloading full factory archive: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as out:
        total = int(resp.headers.get("Content-Length", "0"))
        done = 0
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if total > 0:
                pct = done * 100.0 / total
                print(
                    f"\r    {done / (1024 * 1024):.1f} / {total / (1024 * 1024):.1f} MB ({pct:.1f}%)",
                    end="",
                    flush=True,
                )
        print()
    return dest


def fetch_bootloader_from_remote_zip(
    url: str, device: str, out_path: Path, full_zip: bool = False
) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not full_zip:
        try:
            print(
                f"[*] Reading remote ZIP directory via HTTP Range (skipping ~2.5 GB download): {url}"
            )
            rr = HttpRangeReader(url)
            with zipfile.ZipFile(rr, "r") as zf:
                bl_entries = [
                    n
                    for n in zf.namelist()
                    if re.search(
                        rf"(?:^|/)bootloader-{re.escape(device)}-[^/]+\.img$", n
                    )
                ]
                if not bl_entries:
                    raise RuntimeError(
                        f"bootloader-{device}-*.img not found inside ZIP"
                    )
                entry = bl_entries[0]
                info = zf.getinfo(entry)
                print(
                    f"[*] Extracting {entry} ({info.file_size / (1024 * 1024):.2f} MB)..."
                )
                data = zf.read(entry)
                out_path.write_bytes(data)
                print(f"[+] Saved: {out_path} ({len(data)} bytes)")
                return out_path
        except Exception as e:
            print(
                f"[!] Fast HTTP Range extraction failed ({e}); falling back to full archive download..."
            )

    zip_dest = out_path.parent / Path(urllib.parse.urlparse(url).path).name
    download_full_file(url, zip_dest)
    return extract_bootloader_from_local_zip(zip_dest, device, out_path)


def extract_bootloader_from_local_zip(
    zip_path: Path, device: str, out_path: Path
) -> Path:
    with zipfile.ZipFile(zip_path, "r") as zf:
        bl_entries = [
            n
            for n in zf.namelist()
            if re.search(rf"(?:^|/)bootloader-{re.escape(device)}-[^/]+\.img$", n)
        ]
        if not bl_entries:
            bl_entries = [
                n for n in zf.namelist() if "bootloader-" in n and n.endswith(".img")
            ]
        if not bl_entries:
            raise RuntimeError(f"bootloader-*.img not found inside {zip_path}")
        data = zf.read(bl_entries[0])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(data)
        print(
            f"[+] Extracted from ZIP: {bl_entries[0]} -> {out_path} ({len(data)} bytes)"
        )
        return out_path


def unpack_fbpk(bootloader_path: Path, partitions_dir: Path, split_dir: Path) -> dict:
    """Unpack Google FBPK v2 bootloader image into individual stages."""
    data = bootloader_path.read_bytes()
    magic, ver, hdr_sz, ent_sz = struct.unpack_from("<4sIII", data, 0)
    if magic != b"FBPK":
        raise ValueError(f"{bootloader_path}: expected FBPK header, got {magic!r}")
    soc = data[0x10:0x20].split(b"\x00", 1)[0].decode(errors="replace")
    fw_ver = data[0x20:0x60].split(b"\x00", 1)[0].decode(errors="replace")
    n_ent = struct.unpack_from("<I", data, 0x68)[0]

    print(f"[+] FBPK v{ver}: soc={soc}, bootloader={fw_ver}, entries={n_ent}")
    partitions_dir.mkdir(parents=True, exist_ok=True)
    split_dir.mkdir(parents=True, exist_ok=True)

    extracted = {}
    off = hdr_sz
    for _ in range(n_ent):
        ent = data[off : off + ent_sz]
        off += ent_sz
        name = ent[4:36].split(b"\x00", 1)[0].decode(errors="replace")
        data_off, data_sz = struct.unpack_from("<QQ", ent, 0x50)
        if not name or data_off + data_sz > len(data):
            continue
        blob = data[data_off : data_off + data_sz]
        out_img = partitions_dir / f"{name}.img"
        out_img.write_bytes(blob)
        extracted[name] = len(blob)

    for required in ("bl1", "pbl", "bl2", "bl31", "abl", "tzsw", "ldfw", "gsa"):
        if required not in extracted:
            raise RuntimeError(f"Missing required stage {required}.img in FBPK")

    # Split 4KB Exynos headers from bodies for BL2, BL31, ABL
    for tag, src_name in [("BL2", "bl2"), ("BL31", "bl31"), ("ABL", "abl")]:
        blob = (partitions_dir / f"{src_name}.img").read_bytes()
        if len(blob) <= 4096:
            raise RuntimeError(f"{src_name}.img is too small: {len(blob)} bytes")
        (split_dir / f"{tag}.bin").write_bytes(blob[:4096])
        (split_dir / f"{tag}B.bin").write_bytes(blob[4096:])

    shutil.copy2(partitions_dir / "bl1.img", split_dir / "BL1.bin")
    (split_dir / "DPM.bin").write_bytes(b"\x00" * 4096)

    bl2_hdr = (split_dir / "BL2.bin").read_bytes()
    gsa_hdr = (partitions_dir / "gsa.img").read_bytes()
    ap_ar_ns = struct.unpack_from("<I", bl2_hdr, 0x408)[0]
    ap_ar_s = struct.unpack_from("<I", gsa_hdr, 0x408)[0]
    print(
        f"[+] Unpacked to {partitions_dir} and {split_dir} (ARB: ap-ar-ns={ap_ar_ns}, ap-ar-s={ap_ar_s})"
    )

    return {
        "soc": soc,
        "bootloader_version": fw_ver,
        "ap_ar_ns": ap_ar_ns,
        "ap_ar_s": ap_ar_s,
        "stages": extracted,
    }


def patch_bl1(bl1_orig: bytes) -> tuple[bytes, dict]:
    """Prepare BL1 stage dynamically for USB RAM-boot recovery."""
    buf = bytearray(bl1_orig)
    buf[0x410] = 0x01

    # Embed stageVerifyStub (0x02022600) and usbBootStub (0x02024900)
    buf[0x600 : 0x600 + len(STAGE_VERIFY_STUB_BIN)] = STAGE_VERIFY_STUB_BIN
    buf[0x2900 : 0x2900 + len(USB_BOOT_STUB_BIN)] = USB_BOOT_STUB_BIN

    # Patch 1: mov w4, #0x1001 (24008252); bl <hdr_load>; tbz w0, #0, ... -> nop
    p1_hits = []
    for idx in find_all(bl1_orig, bytes.fromhex("24008252")):
        if idx + 12 <= len(bl1_orig):
            bl_op, tbz_op = struct.unpack_from("<II", bl1_orig, idx + 4)
            if (bl_op & 0xFC000000) == 0x94000000 and (
                tbz_op & 0xFFF8001F
            ) == 0x36000000:
                p1_hits.append(idx + 8)
    if not p1_hits:
        raise RuntimeError(
            "BL1: Patch 1 pattern not found (mov w4,#0x1001 + bl + tbz w0,#0)"
        )
    p1_off = p1_hits[0]  # first occurrence checks header load result before body read
    buf[p1_off : p1_off + 4] = INSN_NOP

    # Patch 3: and w8, w8, #0x3 (08050012); cmp w8, #1 (1f050071); b.ne -> nop
    p3_candidates = []
    for idx in find_all(bl1_orig, bytes.fromhex("080500121f050071")):
        if idx + 12 <= len(bl1_orig):
            bne_op = struct.unpack_from("<I", bl1_orig, idx + 8)[0]
            if (bne_op & 0xFF00001F) == 0x54000001:
                p3_candidates.append((idx + 8, idx - 4))
    if len(p3_candidates) != 1:
        raise RuntimeError(
            f"BL1: expected 1 match for Patch 3, found {len(p3_candidates)}"
        )
    p3_off, ok_block = p3_candidates[0]
    buf[p3_off : p3_off + 4] = INSN_NOP

    # Patch 2: tbnz wX, #0, ok_block -> b ok_block
    p2_hits = []
    for off in range(0, len(bl1_orig), 4):
        op = struct.unpack_from("<I", bl1_orig, off)[0]
        if (op & 0xFFF80000) == 0x37000000:
            imm14 = (op >> 5) & 0x3FFF
            if imm14 & 0x2000:
                imm14 -= 0x4000
            if off + imm14 * 4 == ok_block:
                p2_hits.append(off)
    if len(p2_hits) != 1:
        raise RuntimeError(
            f"BL1: expected 1 match for Patch 2 (tbnz -> 0x{ok_block:x}), found {len(p2_hits)}"
        )
    p2_off = p2_hits[0]
    buf[p2_off : p2_off + 4] = encode_b(p2_off, ok_block)

    info = {
        "header_flag": "0x410=0x01",
        "patch1_nop": hex(p1_off),
        "patch2_b": f"{hex(p2_off)}->{hex(ok_block)}",
        "patch3_nop": hex(p3_off),
    }
    print(f"[+] BL1 patched: {info}")
    return bytes(buf), info


def patch_pbl(pbl_orig: bytes) -> tuple[bytes, dict]:
    """Prepare EPBL (pbl.img) dynamically for USB RAM-boot recovery."""
    buf = bytearray(pbl_orig)

    # Patch 1: ldr w8, [x8, #180] (08b540b9); blr x8 (00013fd6) -> mov x0, #1
    p1_hits = find_all(pbl_orig, bytes.fromhex("08b540b900013fd6"))
    if len(p1_hits) != 1:
        raise RuntimeError(
            f"PBL: expected 1 match for Patch 1 (0x020200b4 blr x8), found {len(p1_hits)}"
        )
    p1_off = p1_hits[0] + 4
    buf[p1_off : p1_off + 4] = INSN_MOV_X0_1

    # Patch 2: eor w11, w11, w12; orr w8, w8, w11; b loop; cbz w8, ok -> b ok
    p2_hits = []
    for idx in find_all(pbl_orig, bytes.fromhex("6b010c4a08010b2a")):
        if idx + 16 <= len(pbl_orig):
            b_op, cbz_op = struct.unpack_from("<II", pbl_orig, idx + 8)
            if (b_op & 0xFC000000) == 0x14000000 and (
                cbz_op & 0xFF00001F
            ) == 0x34000008:
                p2_off = idx + 12
                imm19 = (cbz_op >> 5) & 0x7FFFF
                if imm19 & 0x40000:
                    imm19 -= 0x80000
                p2_hits.append((p2_off, p2_off + imm19 * 4))
    if len(p2_hits) != 1:
        raise RuntimeError(
            f"PBL: expected 1 match for Patch 2 (SHA256 cbz), found {len(p2_hits)}"
        )
    p2_off, p2_target = p2_hits[0]
    buf[p2_off : p2_off + 4] = encode_b(p2_off, p2_target)

    # Patch 3: add x0, sp, #8; add x1, x1, #0x80; bl <gsa_ipc>; mov w8, w0 -> nop
    p3_hits = []
    for idx in find_all(pbl_orig, bytes.fromhex("e023009121000291")):
        if idx + 16 <= len(pbl_orig):
            bl_op, mov_op = struct.unpack_from("<II", pbl_orig, idx + 8)
            if (bl_op & 0xFC000000) == 0x94000000 and mov_op == 0x2A0003E8:
                p3_hits.append(idx + 8)
    if len(p3_hits) != 1:
        raise RuntimeError(
            f"PBL: expected 1 match for Patch 3 (GSA IPC), found {len(p3_hits)}"
        )
    p3_off = p3_hits[0]
    buf[p3_off : p3_off + 4] = INSN_NOP

    info = {
        "patch1_mov1": hex(p1_off),
        "patch2_b": f"{hex(p2_off)}->{hex(p2_target)}",
        "patch3_nop": hex(p3_off),
    }
    print(f"[+] EPBL (pbl.img) patched: {info}")
    return bytes(buf), info


def patch_bl2b(bl2b_orig: bytes) -> tuple[bytes, dict]:
    """Apply GSA wait-loop NOP configuration dynamically to BL2B.bin."""
    buf = bytearray(bl2b_orig)
    pat = bytes.fromhex("800681d2c0e8a2f2000040b9a0ffff36")
    hits = find_all(bl2b_orig, pat)
    if not hits:
        print(
            "[i] BL2B: GSA wait loop (0x17460834) not found (older BL2B revision), skipping"
        )
        return bytes(buf), {"gsa_patch_nop": None}
    if len(hits) != 1:
        raise RuntimeError(f"BL2B: expected 1 match for GSA loop, found {len(hits)}")
    off = hits[0] + 12
    buf[off : off + 4] = INSN_NOP
    print(f"[+] BL2B patched: gsa_wait_nop={hex(off)}")
    return bytes(buf), {"gsa_patch_nop": hex(off)}


def patch_abl(abl_orig: bytes) -> tuple[bytes, dict]:
    """Patch ABLB.bin so Fastboot does not fail when NOS/Titan M2 is offline."""
    buf = bytearray(abl_orig)
    info = {}

    # Patch 1: init_nos -> ret (keeps is_nos_present == 0 when Titan M2/NOS IPC is unavailable)
    tail_pat = bytes.fromhex("e0031faae1031faae2031faae3031faae4031faae5031faa")
    prologue_pat = bytes.fromhex("fd7bbfa9fd030091")
    tail_hits = find_all(abl_orig, tail_pat)
    if len(tail_hits) != 1:
        raise RuntimeError(
            f"ABL: expected 1 match for init_nos, found {len(tail_hits)}"
        )
    t_off = tail_hits[0]
    init_off = abl_orig.rfind(prologue_pat, max(0, t_off - 80), t_off)
    if init_off == -1 or (init_off % 4) != 0:
        raise RuntimeError(f"ABL: init_nos prologue not found before 0x{t_off:x}")
    buf[init_off : init_off + 4] = bytes.fromhex("c0035fd6")  # ret
    info["init_nos_ret"] = hex(init_off)

    # Patch 2: get_lock_state(uint32_t *out) -> *out = 0 (unlocked); return 0;
    lock_pat = bytes.fromhex("1f05007181000054400080523f0000b9")
    lock_hits = find_all(abl_orig, lock_pat)
    if len(lock_hits) != 1:
        raise RuntimeError(
            f"ABL: expected 1 match for get_lock_state, found {len(lock_hits)}"
        )
    lock_off = lock_hits[0] - 12
    if lock_off < 0:
        raise RuntimeError(f"ABL: invalid get_lock_state offset: {lock_off}")
    # str wzr, [x0]; mov w0, wzr; ret
    buf[lock_off : lock_off + 12] = bytes.fromhex("1f0000b9e0031f2ac0035fd6")
    info["get_lock_state_unlocked"] = hex(lock_off)

    # Patch 3: is_prod_mode(uint8_t *out) -> *out = 0 (non-prod); return 0;
    # Enables `fastboot oem reboot-rom` / `reboot-bl1rec` and raw partition writes in RAM fastboot
    prod_body_pat = bytes.fromhex("e0031faae1031faae3031faae4031faae5031faaff130039")
    prod_hits = find_all(abl_orig, prod_body_pat)
    if len(prod_hits) != 1:
        raise RuntimeError(
            f"ABL: expected 1 match for is_prod_mode, found {len(prod_hits)}"
        )
    p_off = prod_hits[0]
    stp_off = abl_orig.rfind(bytes.fromhex("fd7b01a9"), max(0, p_off - 96), p_off)
    if (
        stp_off < 4
        or (struct.unpack_from("<I", abl_orig, stp_off - 4)[0] & 0xFF0003FF)
        != 0xD10003FF
    ):
        raise RuntimeError(f"ABL: is_prod_mode prologue not found before 0x{p_off:x}")
    prod_fn = stp_off - 4
    # strb wzr, [x0]; mov w0, wzr; ret
    buf[prod_fn : prod_fn + 12] = bytes.fromhex("1f000039e0031f2ac0035fd6")
    info["is_prod_mode_nonprod"] = hex(prod_fn)

    print(f"[+] ABLB patched: {info}")
    return bytes(buf), info


def prepare_device(
    device: str,
    out_dir: Path,
    bootloader_path: Path | None = None,
    zip_path: Path | None = None,
    full_zip: bool = False,
    list_only: bool = False,
    build_id: str | None = None,
) -> Path:
    dev_dir = out_dir / device
    dev_dir.mkdir(parents=True, exist_ok=True)
    bl_img = dev_dir / f"bootloader-{device}.img"

    if bootloader_path:
        if bootloader_path.resolve() != bl_img.resolve():
            shutil.copy2(bootloader_path, bl_img)
        print(f"[*] Using local bootloader: {bl_img}")
    elif zip_path:
        extract_bootloader_from_local_zip(zip_path, device, bl_img)
    else:
        links = fetch_factory_links(device)
        if list_only:
            print(
                f"\nAvailable Google factory builds for {device} ({SUPPORTED_DEVICES[device]}):"
            )
            for i, item in enumerate(links, 1):
                print(f"  [{i:2d}] {item['build']:18s} | {item['label']}")
            return dev_dir
        chosen = links[-1]
        if build_id:
            matches = [x for x in links if build_id.lower() in x["build"].lower()]
            if not matches:
                raise RuntimeError(
                    f"Build '{build_id}' not found for {device}. Run with --list."
                )
            chosen = matches[-1]
        print(f"[+] Selected Google build: {chosen['build']} ({chosen['label']})")
        print(f"    URL: {chosen['url']}")
        fetch_bootloader_from_remote_zip(
            chosen["url"], device, bl_img, full_zip=full_zip
        )

    parts_dir = dev_dir / "partitions"
    split_dir = dev_dir / "split_stages"
    meta = unpack_fbpk(bl_img, parts_dir, split_dir)

    bl1_patched, bl1_info = patch_bl1((parts_dir / "bl1.img").read_bytes())
    (dev_dir / "bl1_recovery.bin").write_bytes(bl1_patched)

    pbl_patched, pbl_info = patch_pbl((parts_dir / "pbl.img").read_bytes())
    (dev_dir / "pbl_recovery.img").write_bytes(pbl_patched)

    bl2b_patched, bl2b_info = patch_bl2b((split_dir / "BL2B.bin").read_bytes())
    (dev_dir / "BL2B_recovery.bin").write_bytes(bl2b_patched)

    abl_patched, abl_info = patch_abl((split_dir / "ABLB.bin").read_bytes())
    (dev_dir / "ABLB_recovery.bin").write_bytes(abl_patched)

    meta["device"] = device
    meta["model"] = SUPPORTED_DEVICES[device]
    meta["patches"] = {
        "bl1": bl1_info,
        "pbl": pbl_info,
        "bl2b": bl2b_info,
        "abl": abl_info,
    }
    (dev_dir / "metadata.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False)
    )
    print(f"\n[+] Device {device} ({SUPPORTED_DEVICES[device]}) is ready in {dev_dir}!")
    return dev_dir


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pixel 6 / 6 Pro / 6a (GS101) Factory Bootloader Prep Tool"
    )
    parser.add_argument(
        "--device",
        choices=sorted(SUPPORTED_DEVICES.keys()),
        help="Device codename (oriole, raven, bluejay)",
    )
    parser.add_argument(
        "--bootloader", type=Path, help="Local bootloader-<device>-*.img file"
    )
    parser.add_argument(
        "--zip", type=Path, help="Local official factory-<sha>.zip archive"
    )
    parser.add_argument(
        "--full-zip",
        action="store_true",
        help="Download full factory .zip (~2.5 GB) instead of fast HTTP Range bootloader extraction",
    )
    parser.add_argument(
        "--build", help="Select specific Build ID (defaults to latest release)"
    )
    parser.add_argument(
        "--list", action="store_true", help="List available Google builds and exit"
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=BASE_DIR / "devices",
        help="Output directory (default: devices/)",
    )
    args = parser.parse_args()

    device = args.device
    if not device:
        print("Select Google Pixel (GS101) device:")
        print("  1) oriole  (Pixel 6)")
        print("  2) raven   (Pixel 6 Pro)")
        print("  3) bluejay (Pixel 6a)")
        choice = input("Enter number or codename [1/2/3]: ").strip().lower()
        mapping = {
            "1": "oriole",
            "2": "raven",
            "3": "bluejay",
            **dict.fromkeys(SUPPORTED_DEVICES, None),
        }
        if choice in SUPPORTED_DEVICES:
            device = choice
        elif choice in mapping and mapping[choice]:
            device = mapping[choice]
        else:
            print("[-] Invalid selection.", file=sys.stderr)
            return 1

    prepare_device(
        device=device,
        out_dir=args.out_dir,
        bootloader_path=args.bootloader,
        zip_path=args.zip,
        full_zip=args.full_zip,
        list_only=args.list,
        build_id=args.build,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
