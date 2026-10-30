# Contributing

Read the [README](README.md), [protocol notes](docs/protocol.md), and
[security policy](SECURITY.md) before changing recovery behavior.

## Development setup

The runtime launcher does not require a virtual environment. For development,
an optional isolated environment keeps build and lint dependencies separate:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
```

PowerShell users can activate `.\.venv\Scripts\Activate.ps1`.

## Checks

Run from the repository root:

```sh
python -m unittest discover -v
ruff check pixel-eub pixel_eub tests
ruff format --check pixel-eub pixel_eub tests
python -m build
```

Tests use synthetic images and mocked permission commands. The Linux
integration tests use pseudo-terminals and byte-check uploaded frames.
They do not require a phone, firmware downloads, sudo, or network access.
Tests that depend on Linux facilities are skipped on other platforms.

## Pull requests

Explain the observed problem, resulting behavior, and validation. Preserve
ROM identity checks, request-driven uploads, archive integrity checks, and
the separation between image transfer and final fastboot verification.
Do not add partition flashing or device erasure to the recovery path.

Use synthetic fixtures for parser/protocol changes. Keep firmware, captures
containing personal device identifiers, credentials, and local caches out of
commits. Hardware reports should distinguish an upload ACK from successful
signature verification and confirmed bootloader fastboot.

## Release preparation

1. Keep the version in `pyproject.toml`, `pixel_eub/__init__.py`, and the user
   agent consistent. Add a changelog entry.
2. Run the checks above from a clean checkout.
3. Inspect the wheel and source archive: neither may contain firmware,
   credentials, logs, or local environments.
4. Install the wheel and run `pixel-eub --help` outside the checkout.
5. Enable GitHub's private vulnerability reporting if it is available for
   the repository, and set the repository description/topics as appropriate.

The CI workflow builds distribution artifacts for review. It does not publish
packages, create releases, or send firmware to a device.
