# pixel-eub

English · [Русский](README.ru.md)

Beta 4: with `--override`, `irom_booting_failure` after a completed upload
offers `Continue uploading despite the ROM panic? [YES/NO] (NO):`.
Explicit `YES` sends the remaining **experimental host-ordered chain without
new ROM requests or C**: after BL1, PBL is sent immediately. The plan is shown
before confirmation. Empty input, NO, other answers or EOF cancel. The host
order is not hardware-validated; the phone may reject or ignore the data.
PBL uses EPBL (plus EPBB when split); later images use header/body slices.
Optional images are included only by matching signed tags. Across reconnects,
the same ROM identity is still required. Header failures and NAK remain fatal;
without `--override`, iROM failure is also fatal. Final fastboot verification
remains required for success.

Beta 2 adds `./pixel-eub doctor`: read-only checks for dependencies, ROM USB
port permissions, fastboot location, cache permissions/free space and optional
Linux port-recovery tools. Use `--fastboot PATH` or `--cache-dir PATH` to check
custom paths. No phone is opened or modified. Missing pyserial, denied port
permissions or an unusable cache return exit code 1; missing optional tools or
an unplugged phone do not. `./pixel-eub -h` lists all command options;
`./pixel-eub boot -h` shows just recovery options.

A command-line utility for loading signed boot stages into a compatible Tensor
Pixel in **Pixel ROM Recovery** mode: a black screen and USB ID `18d1:4f00`.
It serves the ROM's requests over USB serial, follows USB re-enumeration, and
checks for bootloader fastboot only after the serial transfer phase ends.

**Experimental: this implementation has not been validated on a physical phone.**
Synthetic tests and a Linux pseudo-terminal emulator check the protocol; they
cannot guarantee recovery of a particular phone. Availability of a signed,
USB-bootable BL1 and the phone's anti-rollback level determine what can boot.

Images are uploaded to RAM. The tool does not flash partitions, unlock the
bootloader, erase user data, or run scripts from downloaded archives.

## Requirements

- Python **3.10 or newer** and **pySerial 3.5** (supported range: `>=3.5,<4`).
- A USB data cable and a phone already exposing Pixel ROM Recovery.
- Android SDK [Platform-Tools](https://developer.android.com/tools/releases/platform-tools)
  for the final fastboot check. Serial uploads can run without `fastboot`, but
  the result will be reported as **unconfirmed**.
- On Linux, optional automatic port repair uses `sudo`, `setfacl`, and `fuser`.
  See [port access](#linux-port-access) before starting.

The code includes serial backends for Linux, macOS, and Windows. The CI matrix
is configured for those systems on Python 3.10 and 3.14; real-device recovery
and every OS/device combination have not been validated.

## Run without a virtual environment

Download or clone this repository, then open a terminal in its directory.
Install pySerial for your system Python using your distribution's package
manager. For example, on Fedora:

```sh
sudo dnf install python3-pyserial
```

Check the dependency and run the included launcher:

```sh
python3 -c 'import serial; print(serial.__version__)'
chmod +x pixel-eub
./pixel-eub --help
./pixel-eub models
```

No installation of pixel-eub itself is needed. The executable uses `python3`
from PATH and the package next to it. If a downloaded source archive lost its
executable bit, run `chmod +x pixel-eub` or use `python3 pixel-eub --help`.
On Windows, use `py pixel-eub --help` and install pySerial with
`py -m pip install 'pyserial>=3.5,<4'` if needed.

An optional isolated installation is also supported:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
pixel-eub --help
```

Use `.\.venv\Scripts\Activate.ps1` to activate it in PowerShell.

## Quick start

Choose your actual model, by name or codename:

```sh
./pixel-eub detect
./pixel-eub boot --device "Pixel 7 Pro" --pack auto --wait
```

For Pixel 8 and Pixel 8 Pro, explicit pack names are:

```sh
./pixel-eub boot --device "Pixel 8" --pack zuma/shiba --wait
./pixel-eub boot --device "Pixel 8 Pro" --pack zuma/husky --wait
```

To enter ROM mode, disconnect the phone, hold Power and both volume buttons,
connect USB, and keep holding for approximately 15 seconds. `--wait` scans for
up to 60 seconds; use `--wait-timeout 120` for a longer window.

The log shows three phases:

1. Find the Pixel ROM USB serial device and verify its ROM identity before upload.
2. Serve requested stages, logging requests, byte counts, completed uploads,
   and ACKs; follow the same device when BL2 re-enumerates USB.
3. Locate and invoke `fastboot`, check its product, and reject fastbootd.

No fastboot command runs before the serial session ends. A serial disconnect
alone is not success. Keep only the target phone connected, or select it with
`--fastboot-serial SERIAL`, because no initial fastboot inventory is taken.

`"Pixel 7 Pro"`, `cheetah`, and `pixel-7-pro` select the same model. Omitting
`--device` probes the SoC and asks for the actual model in an interactive
terminal; ROM identity does not uniquely identify the phone model.

## Compatibility

| Models | SoC | Recovery source |
| --- | --- | --- |
| Pixel 6 / 6 Pro / 6a | gs101 | No USB-bootable BL1 in the pinned public release |
| Pixel 7 / 7 Pro | gs201 | Shared `gs201` pack |
| Pixel 7a / Fold / Tablet | gs201 | Sibling pack; requires `--allow-experimental` or confirmed override |
| Pixel 8 | zuma | `zuma/shiba` |
| Pixel 8 Pro | zuma | `zuma/husky` |
| Pixel 8a | zuma | Sibling `zuma/husky` pack; experimental |
| Pixel 9 family / 9a | zumapro | No pack mapping; ROM ID not verified here |
| Pixel 10 family | laguna | No pack mapping; protocol not verified here |

This table describes sources, not hardware validation by this project. Model
recognition or availability of a factory ZIP does not establish EUB support.
Only observed chip IDs `9845`, `9855`, and `9865` are accepted. Unknown chips
are rejected even with `--override`.

The ROM requires a signed USB-bootable BL1. A normal retail BL1 often lacks
that flag and cannot replace a recovery BL1. Old recovery images may also be
rejected by anti-rollback; a newer factory ZIP does not make an older recovery
BL1 acceptable. See Google's [factory image instructions](https://developers.google.com/android/images)
and the pinned [tensor-usbdl release](https://github.com/JoshuaDoes/tensor-usbdl/releases/tag/020).

## Downloads and cache

`--pack auto` picks the model's mapped recovery pack. The first use downloads
tensor-usbdl v0.2.0 and verifies its pinned SHA-256. Later runs reuse the verified
archive; only selected `.img` members are read. No upstream programs are run.
Firmware is not bundled with this repository or its Python distributions.

```sh
./pixel-eub boot --device cheetah --cache-dir ./cache --dry-run
./pixel-eub boot --device cheetah --cache-dir ./cache --offline --wait
```

The cache defaults to `PIXEL_EUB_CACHE`, then the platform cache directory:
`$XDG_CACHE_HOME/pixel-eub` or `~/.cache/pixel-eub` on Linux,
`~/Library/Caches/pixel-eub` on macOS, and `%LOCALAPPDATA%\pixel-eub` on Windows.
`--cache-dir` takes precedence. `--offline` never fetches from the network.
Incomplete or corrupt downloads are not accepted; interrupted downloads restart.

For normal flashing after recovery, list or download an official factory ZIP:

```sh
./pixel-eub builds --device "Pixel 8 Pro"
./pixel-eub fetch --device husky --build latest
```

`--build` also accepts an exact build ID from `builds`. Downloads use the
SHA-256 from the same catalog row. `latest` excludes qualified carrier/region
rows and fails on ambiguity. Factory ZIPs may be several gigabytes and are
subject to Google's terms. Nothing is flashed automatically.

## Advanced sources and overrides

`boot` accepts a local recovery ZIP/directory with `--pack PATH`, a factory
ZIP or FBPK v2 file with `--image PATH`, and loose signed images with
`--dir PATH`. Source priority is `--dir`, recovery pack, then factory FBPK.
Providing `--dir` disables the automatic pack unless a local pack is explicit.
`--pack none` disables recovery packs entirely. These options are described
in the [Russian guide](README.ru.md) and [protocol notes](docs/protocol.md).

Known model/SoC mismatches are rejected by default. `--override` prints a
warning and requires a full `YES` at a `YES/NO` prompt before allowing the
incompatible selection. `NO`, empty input, invalid input, or EOF cancels.
The phone is probed and the port closed before human confirmation, then
reconnected for fresh requests. The same mismatch is approved once per run.

Override does not disable archive integrity, ROM identity continuity, the
signed BL1 USB flag, or the phone's signature and rollback checks. It does
not make images from another SoC compatible. Arbitrary loose images do not
always expose enough metadata to identify their intended SoC.

`--dry-run` prepares and inspects sources without opening USB; it requires an
explicit model. `unpack bootloader.img` inspects an FBPK and verifies stored
nonzero CRC32 values without extracting files.

## Linux port access

Automatic port recovery is **enabled by default** for verified Pixel ROM
serial nodes. On permission failure it may request `sudo` to grant the current
user `rw` ACL access with `setfacl`. If a port is locked, it lists holders and
uses `fuser -k -TERM` to terminate processes holding that node before retrying.

These actions change permissions or terminate processes. A terminated process
may also have been managing other devices. Current/ancestor processes are
protected; unrelated serial nodes are not selected. A persistent permission
failure after ACL repair does not trigger process termination.

Disable the behavior when managing access yourself:

```sh
./pixel-eub boot --device cheetah --wait --no-port-recovery
```

For lasting access, configure your distribution's serial group or a udev rule
for `18d1:4f00`. If ModemManager repeatedly reclaims the device, configure a
per-device ignore rule. The tool does not change service or driver settings.

## Troubleshooting and development

- `detect --all` lists serial ports and USB IDs.
- `--port /dev/ttyACM0` or `--port COM3` selects a serial port explicitly.
- `--verbose` shows raw ROM lines; redact serial numbers before sharing logs.
- `--reconnect-wait` controls the ROM handover window.
- `--verify-timeout` controls the final fastboot check.
- `--fastboot /path/to/fastboot` selects Platform-Tools explicitly.
- `--override` has **two leading hyphens**.

Exit codes: `0` success (confirmed bootloader fastboot for a normal `boot`),
`1` failure or unconfirmed outcome, `2` invalid arguments, `130` interruption.
A dry-run success does not establish hardware compatibility.

See [CONTRIBUTING.md](CONTRIBUTING.md) for tests, formatting and builds,
[CHANGELOG.md](CHANGELOG.md) for changes, and [SECURITY.md](SECURITY.md) for
private vulnerability reporting guidance.
See the [publishing guide](docs/publishing.md) for the first GitHub push.

## License and credits

The Python code is MIT licensed; see [LICENSE](LICENSE) and [NOTICE.md](NOTICE.md).
Protocol observations come from JoshuaDoes' tensor-usbdl and
hack-different/pixel-restore; FBPK layout follows Google's published tools.
Their firmware and source materials have their own terms and are not bundled.
This project is not affiliated with Google or Samsung.
