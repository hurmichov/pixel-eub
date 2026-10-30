# Changelog

## 1.0.0 — 2026-10-30

First stable release, retaining the recovery functionality and fixes from beta.5.

- Start both READMEs with an introduction to pixel-eub; move diagnostics and
  experimental panic continuation into their relevant sections.
- Record user-reported USB connection and image-upload testing on one physical
  Pixel 6a. Successful recovery into bootloader fastboot has not been confirmed.
- Keep the host-ordered panic continuation explicitly experimental and require
  `--override` plus a separate YES confirmation.
- Publish the stable wheel, sdist, source ZIP and SHA-256 checksums.

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
