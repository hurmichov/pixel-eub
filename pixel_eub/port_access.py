"""Linux-only recovery of access to a verified Pixel ROM serial device."""

import errno
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

from . import EubError

ACCESS_ERRORS = {errno.EACCES, errno.EPERM, errno.EBUSY, errno.EAGAIN}
SYSTEM_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"


def error_number(error):
    code = getattr(error, "errno", None)
    if code is None and error.args and isinstance(error.args[0], int):
        code = error.args[0]
    return code


def tool(name):
    path = shutil.which(name, path=SYSTEM_PATH)
    if path is None:
        raise EubError(
            f"Port access recovery needs {name}; install it or use --no-port-recovery."
        )
    return path


def protected_pids():
    """Never terminate this process, its invoking shell, or their ancestors."""
    protected = {1, os.getpid()}
    pid = os.getppid()
    while pid > 1 and pid not in protected:
        protected.add(pid)
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()
            pid = int(fields[1])
        except (OSError, ValueError, IndexError):
            break
    return protected


class PortAccessRecovery:
    def __init__(self, report=print):
        self.report = report
        self.attempted = set()

    def privileged(self, command, *, allowed=(0,)):
        if os.geteuid() != 0:
            prefix = [tool("sudo")]
            if not sys.stdin.isatty():
                prefix.append("-n")
            command = prefix + ["--"] + command
        self.report("Port access: " + " ".join(command))
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=60, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise EubError(
                "Timed out obtaining administrator access to the Pixel ROM port."
            ) from exc
        if result.returncode not in allowed:
            detail = result.stderr.strip() or result.stdout.strip()
            raise EubError(
                f"Cannot recover Pixel ROM port access: {detail or 'administrator command failed'}. "
                "Run from an interactive terminal for sudo authentication, "
                "or fix the serial-group/udev permissions."
            )
        return result

    def recover(self, port, initial_error, reopen):
        # Import lazily to keep the OS permission helper independent of serial I/O.
        from .transport import PID, SERIAL_IO_ERRORS, VID, ports

        if sys.platform != "linux" or error_number(initial_error) not in ACCESS_ERRORS:
            raise initial_error
        path = Path(port.device).resolve(strict=True)
        if not re.fullmatch(r"/dev/tty(?:ACM|USB)[0-9]+", str(path)):
            raise EubError(
                "Automatic port recovery is restricted to verified Pixel ROM USB serial nodes."
            )
        node = path.stat()
        if not stat.S_ISCHR(node.st_mode):
            raise EubError(
                "Pixel ROM port is not a character device; refusing access changes."
            )
        matches = [
            p
            for p in ports()
            if p.vid == VID and p.pid == PID and Path(p.device).resolve() == path
        ]
        if len(matches) != 1 or (port.serial and matches[0].serial != port.serial):
            raise EubError(
                "USB identity no longer confirms this Pixel ROM port; refusing to change permissions or release processes."
            )
        fingerprint = (str(path), node.st_rdev, node.st_ino)
        if fingerprint in self.attempted:
            raise EubError(
                f"Port access recovery already attempted for {path}; refusing a repeated privileged operation."
            )
        self.attempted.add(fingerprint)

        def ensure_same_node():
            now = path.stat()
            if (now.st_rdev, now.st_ino) != (node.st_rdev, node.st_ino):
                raise EubError(
                    "Pixel ROM port changed during access recovery; reconnect and retry."
                )

        if error_number(initial_error) in {errno.EACCES, errno.EPERM}:
            ensure_same_node()
            uid = os.getuid()
            self.report(
                f"Pixel ROM port {path}: access denied; granting UID {uid} read/write access to this node."
            )
            self.privileged([tool("setfacl"), "-m", f"u:{uid}:rw", "--", str(path)])
            ensure_same_node()
            try:
                return reopen(port)
            except SERIAL_IO_ERRORS as exc:
                # A process holding a lock produces EBUSY/EAGAIN. Another
                # permission failure (e.g. SELinux) is not fixed by killing it.
                if error_number(exc) not in {errno.EBUSY, errno.EAGAIN}:
                    raise EubError(
                        f"Port remains inaccessible after the ACL update: {exc}. Check serial permissions or system security policy."
                    ) from exc

        ensure_same_node()
        fuser = tool("fuser")
        owners = self.privileged([fuser, "--", str(path)], allowed=(0, 1))
        if not re.fullmatch(r"[\s0-9]*", owners.stdout):
            raise EubError(
                "Unexpected port-owner listing; refusing to signal processes."
            )
        pids = {int(value) for value in owners.stdout.split()}
        protected = pids & protected_pids()
        if protected:
            raise EubError(
                f"Port is held by this command or its parent (PID {', '.join(map(str, sorted(protected)))}); close that handle manually."
            )
        if pids:
            ensure_same_node()
            self.report(
                f"Releasing Pixel ROM port {path}: sending SIGTERM to holders {', '.join(map(str, sorted(pids)))}."
            )
            self.privileged([fuser, "-k", "-TERM", "--", str(path)], allowed=(0, 1))
        else:
            self.report(f"No process currently holds {path}; retrying serial open.")
        deadline = time.monotonic() + 2
        last_error = initial_error
        while time.monotonic() < deadline:
            ensure_same_node()
            try:
                return reopen(port)
            except SERIAL_IO_ERRORS as exc:
                last_error = exc
                if error_number(exc) not in {errno.EBUSY, errno.EAGAIN}:
                    break
            time.sleep(0.1)
        raise EubError(
            f"Pixel ROM port could not be acquired after releasing its holders: {last_error}. A process may be restarting; close it manually and retry."
        )
