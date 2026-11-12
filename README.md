# pixel-eub

A command-line utility for loading signed boot stages into a compatible Tensor
Pixel in **Pixel ROM Recovery** mode: a black screen and USB ID `18d1:4f00`.
By default, it serves the ROM's requests over USB serial, follows USB
re-enumeration, and checks for bootloader fastboot only after the serial
transfer phase ends.

English · [Русский](README.ru.md)

**Hardware testing for the original `boot` command:** USB connection and image uploads have been tested on one
physical Pixel 6a (user-reported). Successful recovery into bootloader fastboot
has not been confirmed. This limited coverage does not establish recovery support for
every listed model or bootloader version. A signed USB-bootable BL1 and the
phone's anti-rollback level still determine which images can boot.

Images are uploaded to RAM. The tool does not flash partitions, unlock the
bootloader, erase user data, or run scripts from downloaded archives.

## GS101 recovery

The GTK dashboard supports a separate Linux recovery path for Pixel 6, 6 Pro
and 6a. Select the model and its prepared GS101 profile folder, then use Dry
run or Start recovery. The author reports one successful Pixel 6a test and two
successful Pixel 6 tests; Pixel 6 Pro has a profile but no reported hardware
success. Read [the instructions and warnings](docs/gs101.md) before starting.

On Linux, install the included udev rule to grant the active desktop session
access to the ROM and fastboot USB devices (from the project directory):

```sh
sudo install -m 644 data/69-pixel-gs101.rules /etc/udev/rules.d/69-pixel-gs101.rules
sudo udevadm control --reload-rules
```

Disconnect and reconnect the phone after installing the rule. See
[USB access details](docs/gs101.md#usb-access-on-linux).

## Requirements

- Python **3.10 or newer** and **pySerial 3.5** (supported range: `>=3.5,<4`).
- A USB data cable and a phone already exposing Pixel ROM Recovery.
- Android SDK [Platform-Tools](https://developer.android.com/tools/releases/platform-tools)
  for the final fastboot check. Serial uploads can run without `fastboot`, but
  the result will be reported as **unconfirmed**.
- On Linux, opt-in port repair (`--port-recovery`) uses `sudo` (or `pkexec`
  without a terminal), `setfacl`, and `fuser`.
  See [port access](#linux-port-access) before starting.

The code includes serial backends for Linux, macOS, and Windows. The CI matrix
is configured for those systems on Python 3.10 and 3.14; hardware coverage is currently limited
to the single reported Pixel 6a test.

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

**Pixel 7, 7 Pro and Fold (possibly 7a):** keep holding all three buttons until
the ROM requests **BL1**, and release only after it requests DPM; the log says
when. Users report that when the first request is DPM, the phone has already
started BL1 from internal storage (UFS); that BL1 breaks GSA/GSC communication
with the recovery images, so the chain stalls after BL3B and boots with errors.
For Pixel 7, 7 Pro and Fold, pixel-eub therefore stops before uploading
anything when DPM comes first and asks you to reconnect; `--allow-dpm-first`
continues anyway. On Pixel 7a it only warns. Pixel Tablet reports show
DPM-first sessions that still reach fastboot, so no check applies there.

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

## Diagnostics and help

`./pixel-eub doctor` performs read-only checks for dependencies, ROM USB
port permissions, fastboot location, cache permissions/free space and optional
Linux port-recovery tools. Use `--fastboot PATH` or `--cache-dir PATH` to check
custom paths. No phone is opened or modified. Missing pyserial, denied port
permissions or an unusable cache return exit code 1; missing optional tools or
an unplugged phone do not. `./pixel-eub -h` lists all command options;
`./pixel-eub boot -h` shows just recovery options.

## Graphical interface (Linux)

```sh
./pixel-eub gui
```

The optional GTK 4 / libadwaita window covers every CLI command: recovery with
live phase and stage progress, recovery-pack and factory-image selection,
`doctor`/`detect`, the image analyzer (`inspect`), a full log and all settings. Each action
runs the same CLI in a child process and shows the exact command in the
Terminal page, so behaviour and safety checks are identical. YES/NO questions
become dialogs that still require typing `YES`. Override, sibling-pack and
port-recovery switches apply to the current session only and are never saved.
In the GUI, port recovery asks for the administrator password through the
desktop's own polkit dialog (`pkexec`); the GUI never sees the password.
A udev rule avoids the prompt entirely.

Requirements: PyGObject, GTK 4 and libadwaita 1.6+ from your distribution
(not pip): `sudo dnf install python3-gobject gtk4 libadwaita` on Fedora or
`sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1` on Debian/Ubuntu.
The CLI does not need them. For an application-menu entry, copy
`data/io.github.hurmichov.PixelEub.desktop` to `~/.local/share/applications/`
and `pixel_eub/gui/icons/hicolor` into `~/.local/share/icons/`.

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
That release lists Pixel 8 / 8 Pro support only for bootloaders from April
2025 or earlier; users with newer bootloaders report `bl1 header fail`, and
pixel-eub mentions this in the error on zuma.

User reports, not validated by this project:

- After GSA1 (and sometimes BL3B) the phone can stay silent for 2-5 minutes
  or longer while it verifies; this is normal. pixel-eub waits up to
  `--ack-timeout` seconds (600 by default) and prints progress meanwhile.
- Charge to at least 4200 mV before flashing: below that, `fastboot flash` has
  failed. After confirming fastboot, pixel-eub prints the battery voltage.
- Pixel Tablet can stop at BL31 without reaching fastboot: hold Power for about
  30 seconds until it leaves USB, then try again.

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

### Experimental panic continuation

With `--override`, `irom_booting_failure` after a completed upload
offers `Continue uploading despite the ROM panic? [YES/NO] (NO):`.
Explicit `YES` sends the remaining **experimental host-ordered chain without
new ROM requests or C**: after BL1, PBL is sent immediately. The plan is shown
before confirmation. Empty input, NO, other answers or EOF cancel. The experimental host
order has no confirmed hardware validation; the phone may reject or ignore the data.
PBL uses EPBL (plus EPBB when split); later images use header/body slices.
Optional images are included only by matching signed tags. The prompt is also offered when a matched USB reconnect prints panic diagnostics without a
fresh ROM identity: it warns and uses the previously verified session identity
only after YES. Any fresh identity is checked, and a changed identity stops
the attempt. Header failures and NAK remain fatal;
without `--override`, iROM failure is also fatal. Final fastboot verification
remains required for success.

## Linux port access

Automatic port recovery is **disabled by default**. With `--port-recovery`,
for verified Pixel ROM serial nodes only: on permission failure it may request
`sudo` (from a terminal) or `pkexec` (polkit dialog, without a terminal) to grant the current user `rw` ACL access with `setfacl`. If a port is
locked, it lists holders and uses `fuser -k -TERM` to terminate processes
holding that node before retrying.

These actions change permissions or terminate processes. A terminated process
may also have been managing other devices. Current/ancestor processes are
protected; unrelated serial nodes are not selected. A persistent permission
failure after ACL repair does not trigger process termination.

Enable it only when you accept these actions:

```sh
./pixel-eub boot --device cheetah --wait --port-recovery
```

`--no-port-recovery` is still accepted and wins over `--port-recovery`.

For lasting access, configure your distribution's serial group or a udev rule
for `18d1:4f00`. If ModemManager repeatedly reclaims the device, configure a
per-device ignore rule. The tool does not change service or driver settings.

## Troubleshooting and development

- `detect --all` lists serial ports and USB IDs.
- `inspect FILE...` describes stage images, FBPK bootloaders, factory ZIPs and
  recovery packs: stage, signed USB-boot flag and SoC family (known only for
  keys in the pinned recovery pack). Use it on images from unknown sources: a
  BL1 without the USB flag cannot boot over USB, and setting the flag by hand
  breaks the signature.
- `--port /dev/ttyACM0` or `--port COM3` selects a serial port explicitly.
- `--verbose` shows raw ROM lines; redact serial numbers before sharing logs.
- `--reconnect-wait` controls the ROM handover window.
- `--verify-timeout` controls the final fastboot check.
- `--ack-timeout` controls how long the ROM may stay silent after an upload.
- `--dump-usb DIR` (Linux, research) saves the USB descriptors at every ROM
  connection as `.bin` plus a decoded `.txt`, and logs what changed since the
  previous connection (for example after BL1 or BL2). The bytes are read
  passively from sysfs, where the kernel stored them at enumeration: no root,
  no driver unloading, no USB transfers to the phone. The files include the USB
  serial number; redact it before sharing. `detect --dump-usb DIR` does the same
  for the ports found now.
- `--capture PATH` (research) records every byte from the ROM and every upload
  (length, first 64 bytes, SHA-256) with timestamps and connection numbers as
  JSON lines; `PATH` may be a folder. No firmware is stored in full.
- When the ROM sends a failure dump with `irom_booting_failure`, pixel-eub
  shows it as one line with the code, status, value and address. Codes seen on
  a Pixel 6a suggest `0x400` header or tag rejected, `0x500` USB boot not
  permitted, `0x600` signature or key rejected; this is a hypothesis from three
  attempts, not documented behaviour.
- `--listen-after-failure SECONDS` (research) keeps reading after
  `irom_booting_failure` and reports each repeated failure line with its time
  and first word, plus any other ROM messages. Nothing is uploaded meanwhile;
  the run still ends as a failure.
- `--no-usb-check` sends a BL1 without the signed USB-boot flag anyway, for
  experiments only. The ROM checks that flag itself as part of the signature and
  is expected to answer `bl1 header fail`; the image only goes to RAM.
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
