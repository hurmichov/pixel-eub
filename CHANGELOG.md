# Changelog

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
