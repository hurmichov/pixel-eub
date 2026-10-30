import errno
import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pixel_eub import EubError
from pixel_eub.port_access import PortAccessRecovery
from pixel_eub.transport import PID, VID, Port, PortUnavailable, open_port


@unittest.skipUnless(sys.platform == "linux", "Linux port permission recovery")
class PortAccessTests(unittest.TestCase):
    def setUp(self):
        self.port = Port("/dev/ttyACM0", "PIXEL", "1-2", "Pixel ROM Recovery", VID, PID)
        self.node = SimpleNamespace(
            st_mode=stat.S_IFCHR | 0o660, st_rdev=123, st_ino=456
        )
        self.recovery = PortAccessRecovery(report=Mock())
        for target, options in (
            ("pixel_eub.port_access.sys.platform", {"new": "linux"}),
            ("pixel_eub.transport.ports", {"return_value": [self.port]}),
            (
                "pixel_eub.port_access.Path.resolve",
                {"return_value": Path(self.port.device)},
            ),
            ("pixel_eub.port_access.Path.stat", {"return_value": self.node}),
            (
                "pixel_eub.port_access.tool",
                {"side_effect": lambda name: "/usr/bin/" + name},
            ),
            ("pixel_eub.port_access.protected_pids", {"return_value": {1, 999}}),
        ):
            context = patch(target, **options)
            context.start()
            self.addCleanup(context.stop)

    def test_permission_denied_only_grants_current_user_acl(self):
        self.recovery.privileged = Mock()
        reopen = Mock(return_value="SERIAL_LINK")
        result = self.recovery.recover(
            self.port, PermissionError(errno.EACCES, "denied"), reopen
        )
        self.assertEqual(result, "SERIAL_LINK")
        self.recovery.privileged.assert_called_once_with(
            ["/usr/bin/setfacl", "-m", f"u:{os.getuid()}:rw", "--", self.port.device]
        )
        reopen.assert_called_once_with(self.port)

    def test_busy_port_releases_only_holders_with_sigterm(self):
        self.recovery.privileged = Mock(
            side_effect=[
                SimpleNamespace(stdout=" 123 456 "),
                SimpleNamespace(stdout=""),
            ]
        )
        reopen = Mock(return_value="SERIAL_LINK")
        self.assertEqual(
            self.recovery.recover(self.port, OSError(errno.EBUSY, "busy"), reopen),
            "SERIAL_LINK",
        )
        commands = [call.args[0] for call in self.recovery.privileged.call_args_list]
        self.assertEqual(
            commands,
            [
                ["/usr/bin/fuser", "--", self.port.device],
                ["/usr/bin/fuser", "-k", "-TERM", "--", self.port.device],
            ],
        )
        self.assertFalse(any("-m" in cmd or "-KILL" in cmd for cmd in commands))

    def test_acl_then_busy_port_is_released(self):
        self.recovery.privileged = Mock(
            side_effect=[
                SimpleNamespace(stdout=""),
                SimpleNamespace(stdout="123"),
                SimpleNamespace(stdout=""),
            ]
        )
        reopen = Mock(side_effect=[OSError(errno.EAGAIN, "locked"), "SERIAL_LINK"])
        self.assertEqual(
            self.recovery.recover(
                self.port, PermissionError(errno.EACCES, "denied"), reopen
            ),
            "SERIAL_LINK",
        )
        self.assertEqual(len(self.recovery.privileged.call_args_list), 3)

    def test_permission_failure_after_acl_does_not_kill_processes(self):
        self.recovery.privileged = Mock()
        reopen = Mock(side_effect=PermissionError(errno.EACCES, "security policy"))
        with self.assertRaisesRegex(EubError, "security policy"):
            self.recovery.recover(
                self.port, PermissionError(errno.EACCES, "denied"), reopen
            )
        self.recovery.privileged.assert_called_once()

    def test_unverified_usb_and_regular_files_are_never_modified(self):
        self.recovery.privileged = Mock()
        reopen = Mock()
        with (
            patch("pixel_eub.transport.ports", return_value=[]),
            self.assertRaisesRegex(EubError, "identity"),
        ):
            self.recovery.recover(
                self.port, PermissionError(errno.EACCES, "denied"), reopen
            )
        regular = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_rdev=0, st_ino=456)
        with (
            patch("pixel_eub.port_access.Path.stat", return_value=regular),
            self.assertRaisesRegex(EubError, "character device"),
        ):
            self.recovery.recover(self.port, OSError(errno.EBUSY, "busy"), reopen)
        self.recovery.privileged.assert_not_called()
        reopen.assert_not_called()

    def test_protected_parent_and_invalid_owner_listing_are_not_signalled(self):
        for listing, error in (("999", "parent"), ("unexpected", "Unexpected")):
            with self.subTest(listing=listing):
                recovery = PortAccessRecovery(report=Mock())
                recovery.privileged = Mock(return_value=SimpleNamespace(stdout=listing))
                with self.assertRaisesRegex(EubError, error):
                    recovery.recover(self.port, OSError(errno.EBUSY, "busy"), Mock())
                recovery.privileged.assert_called_once()

    def test_changed_node_and_repeated_recovery_stop(self):
        self.recovery.privileged = Mock()
        changed = SimpleNamespace(st_mode=stat.S_IFCHR | 0o660, st_rdev=123, st_ino=999)
        reopen = Mock()
        with (
            patch(
                "pixel_eub.port_access.Path.stat",
                side_effect=[self.node, self.node, changed],
            ),
            self.assertRaisesRegex(EubError, "changed"),
        ):
            self.recovery.recover(
                self.port, PermissionError(errno.EACCES, "denied"), reopen
            )
        reopen.assert_not_called()
        with self.assertRaisesRegex(EubError, "already attempted"):
            self.recovery.recover(
                self.port, PermissionError(errno.EACCES, "denied"), reopen
            )

    def test_non_access_io_failure_is_not_recovered(self):
        self.recovery.privileged = Mock()
        with self.assertRaises(OSError):
            self.recovery.recover(self.port, OSError(errno.EIO, "disconnected"), Mock())
        self.recovery.privileged.assert_not_called()

    def test_transport_calls_recovery_only_for_access_failure(self):
        recovery = Mock()
        recovery.recover.return_value = "SERIAL_LINK"
        error = PermissionError(errno.EACCES, "denied")
        with patch("pixel_eub.transport._open_port_once", side_effect=error) as reopen:
            self.assertEqual(open_port(self.port, recovery=recovery), "SERIAL_LINK")
        recovery.recover.assert_called_once_with(self.port, error, reopen)
        recovery.reset_mock()
        with (
            patch(
                "pixel_eub.transport._open_port_once",
                side_effect=OSError(errno.EIO, "gone"),
            ),
            self.assertRaises(PortUnavailable),
        ):
            open_port(self.port, recovery=recovery)
        recovery.recover.assert_not_called()
        with (
            patch("pixel_eub.transport._open_port_once", side_effect=error),
            self.assertRaises(PortUnavailable),
        ):
            open_port(self.port)


@unittest.skipUnless(sys.platform == "linux", "Linux administrator commands")
class PrivilegedCommandTests(unittest.TestCase):
    def test_noninteractive_sudo_never_prompts_in_a_pipe(self):
        recovery = PortAccessRecovery(report=Mock())
        result = subprocess.CompletedProcess([], 0, "", "")
        with (
            patch("pixel_eub.port_access.os.geteuid", return_value=1000),
            patch("pixel_eub.port_access.sys.stdin.isatty", return_value=False),
            patch("pixel_eub.port_access.tool", return_value="/usr/bin/sudo"),
            patch("pixel_eub.port_access.subprocess.run", return_value=result) as run,
        ):
            recovery.privileged(
                ["/usr/bin/setfacl", "-m", "u:1000:rw", "--", "/dev/ttyACM0"]
            )
        self.assertEqual(run.call_args.args[0][:3], ["/usr/bin/sudo", "-n", "--"])

    def test_admin_failure_and_timeout_are_actionable_errors(self):
        for failure in (
            subprocess.CompletedProcess([], 1, "", "sudo: a password is required"),
            subprocess.TimeoutExpired("sudo", 60),
        ):
            with self.subTest(failure=failure):
                recovery = PortAccessRecovery(report=Mock())
                with (
                    patch("pixel_eub.port_access.os.geteuid", return_value=0),
                    patch(
                        "pixel_eub.port_access.subprocess.run",
                        **(
                            {"side_effect": failure}
                            if isinstance(failure, Exception)
                            else {"return_value": failure}
                        ),
                    ),
                    self.assertRaises(EubError),
                ):
                    recovery.privileged(["/usr/bin/setfacl"])
