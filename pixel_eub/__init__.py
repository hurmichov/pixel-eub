"""Pixel EUB recovery. Firmware is never bundled or executed on the host."""

__version__ = "1.0.0b2"


class EubError(Exception):
    """An actionable failure that should not produce a CLI traceback."""
