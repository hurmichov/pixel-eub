# EUB transport notes

Protocol observations are based on the public tensor-usbdl implementation at
release commit `aebdc39` and checked against pixel-restore. FBPK v2 layout is
defined by Google's published `fbpack.py`. This implementation does not execute
either project or any binaries from their archives.

## Discovery and identity

Pixel ROM Recovery enumerates as USB VID/PID `18d1:4f00`, CDC serial, 115200 8N1,
without flow control. USB identity is used to select/reconnect the serial port;
the ROM identity in messages separately guards every upload against another
phone or SoC. ROM serial is retained as text, never parsed into a bounded integer.

Observed chip identifiers are characters 1–4 after the leading zero in ROM
serial: `9845` gs101, `9855` gs201, `9865` zuma. They do not identify individual
phone models. Inferred identifiers for later chips are deliberately rejected.

`--override` permits known model/SoC mismatches only after the user enters
`YES` at a `YES/NO` prompt. The CLI probes the identity first, closes that
connection, obtains confirmation, and reopens the same USB device to receive
fresh ROM requests. Approvals are cached for that mismatch within the run,
including BL2 reconnects. A changed ROM serial or unknown chip still fails.
EPBL slicing uses the observed ROM SoC rather than the incompatible pack's
SoC, unless explicitly changed with `--epbl`. Local FBPK compatibility metadata
may also be overridden after confirmation; corruption/CRC/SHA checks and ROM
signature failures remain terminal.

## Device messages

| Message | Action |
| --- | --- |
| `exynos_usb_booting:eub:SERIAL` | Verify ROM identity |
| `eub:req:SERIAL:STAGE` | Verify identity and queue stage |
| `C` | Send queued stage; this byte may have no line terminator |
| `eub:ack:SERIAL:STAGE` | Record ACK, continue listening for signature failures |
| `eub:nak:...` | Stop with failure |
| `STAGE header fail` | Stop with signature/anti-rollback diagnostic |
| `irom_booting_failure:...` | Stop; with `--override`, offer confirmed experimental forced chain |

CR, LF and NUL delimit text messages. Text may be split across reads. Duplicate
requests before `C` are coalesced; after uploading, an explicitly repeated
request can be served up to five times per stage. Unknown stages fail closed.
A newline nudge is sent once after 1.5 s only if the connection has received
no bytes. It is never inserted into an expected upload.

### Experimental panic continuation (beta 4)

Only with `--override`, an iROM panic after a completed upload can be followed
by an explicit YES. The host then sends the remaining plan without requests/C.
This is an experimental host policy, not a documented ROM recovery handshake.
The order is BL1, EPBL (plus EPBB when split), BL2/BL2B, optional GCF/GCFB,
optional GSA1, optional GSAF, ABL/ABLB, optional TZSW/TZSB, optional LDFW/LDFB,
BL31/BL3B. Resume after the last completed stage; no synthetic DPM is inserted.
All remaining payloads are validated before prompting. Optional images are
selected only if their signed tags exist. Missing required images stop the
attempt. A fresh identity is verified before automatic resumption across
disconnects. In beta 5, a CLI-matched reconnect that emits only an iROM panic
can instead offer a new confirmation using the cached, previously verified
session identity. A warning identifies that fallback. USB matching uses the
original serial, then location, then node path if neither identifier exists;
node-path matching alone cannot prove ROM identity. Initial/unmatched connections
cannot use this fallback, and any fresh changed identity remains fatal.
A partial write retries that stage, with the existing limit.
NO, invalid/empty input and EOF cancel. Header failures and NAK remain terminal.
Forced uploads do not mean the ROM accepted or executed the chain; final
fastboot product verification still determines success.

## Host upload

```
ESC D N W | uint32_le(payload_length + 10) | payload | uint16_le(trailer)
```

Default trailer: `0xffff`, as used by tensor-usbdl. Optional trailer: byte sum
modulo 65536. Writes are at most 10240 bytes, handle short writes, flush each
chunk and have a deadline. Write timeouts remain errors rather than being
mistaken for the normal read-side disconnect at USB handover.

## Signed stage slicing

Each image starts with a 4096-byte signed header. The four-byte tag at `0x400`
identifies the stage; the flags word is at `0x410`. USB BL1 must have bit 0 set
in its signed flags. The host never patches signed data.

| Request | Image tag | Slice |
| --- | --- | --- |
| BL1 | APBL | full |
| DPM | DPM | full; 4096 zero bytes if absent |
| EPBL | EPBL | full on gs101/gs201, header on zuma |
| EPBB | EPBL | body |
| BL2 / BL2B | BL2 | header / body |
| GCF / GCFB | GCF | header / body |
| GSA1 | GSA1 | full |
| GSAF | GSAF | full, only if requested and available |
| ABL / ABLB | ABL | header / body |
| TZSW / TZSB | TZSW | header / body |
| LDFW / LDFB | LDFW | header / body |
| BL31 / BL3B | BL31 | header / body |

Stage tags take precedence over filenames within each source layer. A filename
cannot select a payload tagged for a different stage. Some gs201 packs contain
GSA1 only: GSAF must not be guessed from that image.

BL2 may take over USB, close the old connection and request more images on a
new port. The session retains ROM identity and upload counters across that
transition. Read-side disconnect or post-upload silence triggers discovery,
not success. A connected fastboot serial and the selected model's `product`
must be observed; a response `is-userspace: yes` is rejected as fastbootd.
Older bootloaders may not implement `is-userspace`, in which case successful
product verification is accepted. Recovery never issues flash/erase/unlock.

Platform-Tools is optional for serial uploads and is only located/executed
after the ROM session ends. There is no initial fastboot inventory. Leave one
target phone connected or use `--fastboot-serial`; ambiguous multiple devices
are rejected. During handover, ROM ports are serviced before final verification. If automatic
verification is unavailable, the tool still uploads the requested stages and
returns an explicit unconfirmed result (exit 1) after the serial session ends.

## Recovery archive integrity

URL:
`https://github.com/JoshuaDoes/tensor-usbdl/releases/download/020/tensor-usbdl-v0.2.0.zip`

SHA-256:
`15dbd6406c1c74c2cd7104388ab745f3605d026ede6fc6c61c754ddb30b194b2`

The checksum was independently checked against the downloaded release while
implementing this tool on 2026-10-29. No firmware is committed to this repository.

Factory ZIPs use Google's row-scoped SHA-256. Local FBPK entries are checked
against stored nonzero CRC32, which detects corruption and is not a replacement
for a cryptographic signature. Final signature enforcement belongs to the ROM.
