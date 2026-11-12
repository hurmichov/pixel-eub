# Changelog

## 1.0.1 — GS101 recovery

- Add a separate Linux GS101 USB recovery backend and integrate it into the GTK dashboard.
- Provide a GS101 profile check, device-specific udev rule and recovery warnings.
- One Pixel 6a and two Pixel 6 devices were reported recovered; Pixel 6 Pro has a profile but no reported hardware success. The integrated path was subsequently reported working on Pixel 6a.
- GS101 recovery uses modified boot stages; fastboot USB detection does not verify the target phone or permanent repair.


## Unreleased

- Add `pixel-eub gui`, an optional GTK 4 / libadwaita interface with Dashboard,
  Firmware, Diagnostics, Analyzer, Terminal, Settings and Credits pages. It runs
  the same CLI in a child process, shows each exact command, turns YES/NO
  prompts into dialogs that require typing YES, and never saves override,
  sibling-pack or port-recovery switches. The CLI gains no new dependencies.
- Pixel 7, 7 Pro and Fold: stop before any upload when the ROM requests DPM
  before BL1 (BL1 started from UFS; users report GSA/GSC errors after BL3B),
  with reconnect instructions; `--allow-dpm-first` continues. Pixel 7a only
  warns. After BL1, a DPM request prints that the buttons can be released.
- Add `--listen-after-failure SECONDS` to observe how the ROM repeats a failure
  (time and first word of each repeat); GUI: Settings → Research.
- Read the iROM failure dump from the `irom_booting_failure::` line itself,
  where words are NUL-separated (seen in a raw capture), instead of from a
  preceding block; show its first word and `e0000080` count, and code 0.
- Describe iROM failure dumps in one line (code, status, value, address) with
  hypothesised meanings of codes 0x400/0x500/0x600 seen on a Pixel 6a, and add
  the code to the rejection error.
- Add `--capture PATH`: timestamped JSON-lines record of ROM bytes and uploads.
  GUI: Settings → Research folder with descriptor and capture switches.
- Accept `partition:N` entries (UFS partition tables) in newer Pixel FBPK
  bootloaders, such as recent Pixel 6a factory images; they were rejected.
- Add `--dump-usb DIR` to `boot` and `detect` (Linux): save raw and decoded USB
  descriptors per ROM connection from sysfs and log changes between stages,
  without root or USB transfers. GUI: Settings → Research.
- Add `--no-usb-check` (and a session-only GUI switch) to send a BL1 without the
  signed USB-boot flag for experiments; the ROM still enforces the flag.
- Add `inspect` for stage images, FBPK bootloaders, factory ZIPs and recovery
  packs: stage, signed USB-boot flag, SoC family (labelled only for keys seen in
  the pinned recovery pack) and header inconsistencies. The GUI Analyzer uses it.
- Do not send a stage twice when the ROM echoes its request and C right after
  an upload (seen in Pixel 7/8 logs); repeats are served only after an ACK.
- Wait up to `--ack-timeout` (600 s) after an upload instead of the 15 s idle
  limit and the 180 s per-connection cap: GSA1 and BL3B verification can take
  minutes. Progress is printed every 30 s and a vanished port is followed.
- On zuma, `bl1 header fail` mentions the reported April 2025 bootloader limit.
  After confirming fastboot, print the battery voltage and warn below 4200 mV.
- Port recovery without a terminal (for example from the GUI) now asks for the
  administrator password through polkit (`pkexec`) in a desktop session instead
  of failing with `sudo -n`. The password never passes through pixel-eub.

## 1.0.0rc1 — 2026-10-31

Release candidate for 1.0.0, tagged `v1.0.0-RC`. It replaces the unpublished
1.0.0 stable label: physical recovery into bootloader fastboot is still
unconfirmed, so the package is marked Beta.

- Linux port recovery (`sudo setfacl`, `fuser -k -TERM`) is now opt-in with
  `--port-recovery`. `--no-port-recovery` is still accepted and takes priority.
  Access errors without it suggest the flag.
- Fix `TypeError` when `latest` compared build IDs such as `…019` and `…019a`.
- Log the 4096-byte empty DPM sent when no signed DPM image exists.
- Start both READMEs with an introduction to pixel-eub; move diagnostics and
  experimental panic continuation into their relevant sections.
- Record user-reported USB connection and image-upload testing on one physical
  Pixel 6a. Successful recovery into bootloader fastboot has not been confirmed.
- Keep the host-ordered panic continuation explicitly experimental and require
  `--override` plus a separate YES confirmation.
- Publish the wheel, sdist, source ZIP and SHA-256 checksums.

## 1.0.0b5 — 2026-10-30

- Fix missing panic confirmation after USB re-enumeration when the ROM prints
  hex diagnostics and `irom_booting_failure::` without repeating its identity.
- On a CLI-matched reconnect with `--override`, offer continuation using the
  previously verified session identity, with an explicit cached-identity warning.
  The YES/NO prompt still occurs before any forced upload.
- A fresh identity, when present, is still checked and a different phone is
  rejected. Initial/unmatched protocol connections cannot reuse identity.
- Reproduce the Pixel 6a log in CLI tests, including both YES and NO responses.

## 1.0.0b4 — 2026-10-30

- With `--override`, `irom_booting_failure` after a completed upload offers
  `Continue uploading despite the ROM panic? [YES/NO] (NO):`.
- Explicit YES selects an experimental host-ordered chain: after BL1, send
  PBL immediately and then the remaining stages without ROM requests or C.
  The plan is printed and payloads validated before confirmation.
- Preserve forced-chain progress over USB reconnects, verifying the same ROM
  identity before resuming. Empty/invalid input, NO and EOF cancel.
- Without override, panic remains terminal. Header failures and NAK still stop;
  forced uploads are not proof that the phone accepted or executed an image.

## 1.0.0b3 — 2026-10-30

- Consolidate top-level help into one list of unique command options, with
  the applicable commands shown beside each option. Remove repeated command
  usage and `options:` blocks.
- Explain command-dependent device/build defaults without changing parsing
  or focused `COMMAND -h` output.

## 1.0.0b2 — 2026-10-30

- Show all command arguments in top-level `-h` / `--help`, including their
  descriptions and default values. Command-specific help remains available.
- Describe previously undocumented timeouts, checksum, refresh and verbose options.
- Add read-only `doctor` diagnostics for Python/pyserial, Pixel ROM USB port
  permissions, fastboot location, cache permissions/free space and optional Linux
  port-recovery tools. No phone is opened, no programs are executed, no files
  are created, and no system settings are changed by this command.
- Diagnostics return 1 for missing pyserial, port permission failures or unusable
  cache paths; absence of a phone or optional fastboot/tools is informational.

## 1.0.0b1 — 2026-10-30

First GitHub prerelease: **v1.0 beta**, tagged `v1.0.0-beta.1`.

- Publish a Python wheel, source distribution, portable source ZIP, and
  SHA-256 checksums.
- Update the CLI and package version to `1.0.0b1` (Python's beta notation).
- Retain the recovery features described below; no recovery-protocol changes.

This beta remains experimental. Synthetic tests and CI do not establish
hardware compatibility or guarantee recovery of a phone. Recovery firmware
is downloaded separately and is not included in the release artifacts.

## 0.1.0 — 2026-10-30

Initial public source preparation. This release is experimental and has not
been validated on a physical phone by the project.

- Request-driven EUB/DNW serial uploads, signed-stage slicing, and USB
  reconnection handling.
- Pixel model aliases and explicit recovery-pack selection.
- Official factory-build lookup, SHA-256-verified downloads, reusable caches,
  and offline operation.
- FBPK v2/ZIP parsing and request-to-image matching by signed header tag.
- Explicit `YES/NO` confirmation of model/SoC mismatches with `--override`.
- Linux port-access recovery, with `--no-port-recovery` to disable privileged
  ACL changes and termination of port holders.
- Final fastboot verification after the ROM transfer phase.
- Direct `./pixel-eub` launcher requiring no virtual environment.
- Synthetic unit tests, Linux pseudo-terminal integration tests, and CI
  configuration for Linux, macOS, and Windows.
