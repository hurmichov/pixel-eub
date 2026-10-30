# Security policy

## Reporting a vulnerability

Use GitHub's **Report a vulnerability** feature if private vulnerability
reporting is enabled for this repository. If it is unavailable, open an issue
asking for a private reporting channel without including exploit details,
credentials, or private device data.

Ordinary compatibility failures belong in a bug report. Redact serial numbers,
usernames, home-directory paths, IMEI information, and other personal data
from logs. Do not upload firmware or device backups.

## Relevant security boundaries

- Firmware downloads require HTTPS and a SHA-256 match. Recovery downloads
  use a fixed release and pinned digest.
- ZIP/FBPK readers validate sizes, ranges and stored checksums. These checks
  do not replace signatures verified by the phone.
- `--override` permits confirmed model/SoC metadata mismatches and an explicitly
  confirmed experimental host-ordered continuation after `irom_booting_failure`.
  Forced continuation omits ROM request/C gating; it does not establish acceptance.
  It must
  not bypass archive integrity, a changed phone identity, unknown ROM chips,
  or a ROM-reported signature/rollback failure.
- On Linux, automatic port recovery may invoke `sudo setfacl` and
  `sudo fuser -k -TERM` for a verified Pixel ROM node. It changes permissions
  or terminates processes. Users can disable it with `--no-port-recovery`.
- Recovery uploads go to RAM. The tool does not issue flash, erase or unlock
  commands. A serial disconnect alone is never proof of successful recovery.

The project is experimental. There is no promise of a vulnerability response
deadline or guaranteed recovery on physical hardware.
