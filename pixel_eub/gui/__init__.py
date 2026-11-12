"""Optional GTK4/libadwaita interface. Needs system PyGObject, GTK 4 and libadwaita."""

from .. import EubError

INSTALL_HINT = (
    "The GUI needs PyGObject with GTK 4 and libadwaita 1.6 or newer. "
    "Fedora: sudo dnf install python3-gobject gtk4 libadwaita; "
    "Debian/Ubuntu: sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1."
)


def run(argv=None) -> int:
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw

        if (Adw.get_major_version(), Adw.get_minor_version()) < (1, 6):
            raise ImportError("libadwaita is older than 1.6")
    except (ImportError, ValueError) as exc:
        raise EubError(f"{INSTALL_HINT} ({exc})") from exc
    from .app import main

    return main(argv)
