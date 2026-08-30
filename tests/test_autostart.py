"""Tests for spitch.autostart — the systemd user-service helper.

The functions that shell out to systemctl are mocked via
``unittest.mock.patch`` so the suite still passes on hosts without a
running systemd user instance (CI in particular).
"""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from spitch import autostart


class UnitContentTests(unittest.TestCase):
    def test_render_unit_has_required_sections(self):
        s = autostart.render_unit()
        self.assertIn("[Unit]", s)
        self.assertIn("[Service]", s)
        self.assertIn("[Install]", s)
        self.assertIn("Description=Spitch", s)

    def test_render_unit_points_at_user_launcher(self):
        s = autostart.render_unit()
        self.assertIn(str(autostart.daemon_launcher_path()), s)
        # absolute path, otherwise systemd refuses to start the unit
        self.assertIn("ExecStart=/", s)

    def test_render_unit_wants_graphical_session_target(self):
        # graphical-session.target ensures it doesn't start in a tty-only
        # boot — Spitch needs a desktop for the tray + uinput ACL.
        s = autostart.render_unit()
        self.assertIn("WantedBy=graphical-session.target", s)
        self.assertIn("After=graphical-session.target", s)

    def test_render_unit_restarts_on_failure(self):
        s = autostart.render_unit()
        self.assertIn("Restart=on-failure", s)

    def test_render_unit_bounds_stop_timeout(self):
        # Prevents a wedged daemon from holding systemctl stop for the
        # manager default (often 90s). mixed: SIGTERM main, then SIGKILL
        # the cgroup after TimeoutStopSec.
        s = autostart.render_unit()
        self.assertIn("TimeoutStopSec=8", s)
        self.assertIn("KillMode=mixed", s)

    def test_render_unit_does_not_restart_config_exits(self):
        # Exit 2 = not verified / incomplete; 3 = no input devices.
        # systemd Restart=on-failure would otherwise loop every 2s.
        s = autostart.render_unit()
        self.assertIn("RestartPreventExitStatus=2 3", s)


class UnitPathTests(unittest.TestCase):
    def test_xdg_config_home_honored(self):
        prev = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = "/tmp/spitch-test-cfg"
        try:
            self.assertEqual(
                autostart.unit_path(),
                Path("/tmp/spitch-test-cfg/systemd/user/spitch.service"),
            )
        finally:
            if prev is None:
                del os.environ["XDG_CONFIG_HOME"]
            else:
                os.environ["XDG_CONFIG_HOME"] = prev

    def test_default_path_under_home(self):
        prev = os.environ.pop("XDG_CONFIG_HOME", None)
        try:
            self.assertEqual(
                autostart.unit_path(),
                Path.home() / ".config" / "systemd" / "user" / "spitch.service",
            )
        finally:
            if prev is not None:
                os.environ["XDG_CONFIG_HOME"] = prev


class IsSupportedTests(unittest.TestCase):
    def test_no_systemctl_returns_false(self):
        with patch("spitch.autostart.shutil.which", return_value=None):
            self.assertFalse(autostart.is_supported())

    def test_running_user_instance_supported(self):
        def fake_run(cmd, **_):
            if cmd[:3] == ["systemctl", "--user", "is-system-running"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="running\n", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        with patch("spitch.autostart.shutil.which", return_value="/usr/bin/systemctl"):
            with patch("spitch.autostart.subprocess.run", side_effect=fake_run):
                self.assertTrue(autostart.is_supported())

    def test_offline_user_bus_unsupported(self):
        def fake_run(cmd, **_):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        with patch("spitch.autostart.shutil.which", return_value="/usr/bin/systemctl"):
            with patch("spitch.autostart.subprocess.run", side_effect=fake_run):
                self.assertFalse(autostart.is_supported())

    def test_systemctl_timeout_unsupported(self):
        def fake_run(cmd, **_):
            raise subprocess.TimeoutExpired(cmd, 2)
        with patch("spitch.autostart.shutil.which", return_value="/usr/bin/systemctl"):
            with patch("spitch.autostart.subprocess.run", side_effect=fake_run):
                self.assertFalse(autostart.is_supported())


class IsEnabledTests(unittest.TestCase):
    def test_no_unit_file_means_disabled(self):
        with patch("spitch.autostart.unit_path", return_value=Path("/nonexistent")):
            self.assertFalse(autostart.is_enabled())

    def test_enabled_when_systemctl_says_enabled(self, *_):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "spitch.service"
            p.write_text("dummy", encoding="utf-8")

            def fake_run(cmd, **_):
                return subprocess.CompletedProcess(cmd, 0, stdout="enabled\n", stderr="")

            with patch("spitch.autostart.unit_path", return_value=p):
                with patch("spitch.autostart.shutil.which", return_value="/usr/bin/systemctl"):
                    with patch("spitch.autostart.subprocess.run", side_effect=fake_run):
                        self.assertTrue(autostart.is_enabled())

    def test_disabled_when_systemctl_says_so(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "spitch.service"
            p.write_text("dummy", encoding="utf-8")

            def fake_run(cmd, **_):
                return subprocess.CompletedProcess(cmd, 1, stdout="disabled\n", stderr="")

            with patch("spitch.autostart.unit_path", return_value=p):
                with patch("spitch.autostart.shutil.which", return_value="/usr/bin/systemctl"):
                    with patch("spitch.autostart.subprocess.run", side_effect=fake_run):
                        self.assertFalse(autostart.is_enabled())


class EnableDisableTests(unittest.TestCase):
    def test_enable_writes_unit_and_runs_systemctl(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            unit = Path(td) / "spitch.service"
            launcher = Path(td) / "spitch-daemon"
            launcher.write_text("#!/bin/sh\n", encoding="utf-8")
            launcher.chmod(0o755)

            calls = []

            def fake_run(cmd, **_):
                calls.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            with patch("spitch.autostart.unit_path", return_value=unit), \
                 patch("spitch.autostart.daemon_launcher_path", return_value=launcher), \
                 patch("spitch.autostart.unit_dir", return_value=Path(td)), \
                 patch("spitch.autostart.shutil.which", return_value="/usr/bin/systemctl"), \
                 patch("spitch.autostart.subprocess.run", side_effect=fake_run):
                ok, msg = autostart.enable()
            self.assertTrue(ok, msg)
            self.assertTrue(unit.exists())
            self.assertIn(str(launcher), unit.read_text(encoding="utf-8"))
            # Saw both daemon-reload and enable --now
            self.assertTrue(any("daemon-reload" in c for c in calls))
            self.assertTrue(any("enable" in c and "--now" in c for c in calls))

    def test_enable_fails_when_launcher_missing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            with patch("spitch.autostart.daemon_launcher_path",
                       return_value=Path(td) / "missing-launcher"):
                ok, msg = autostart.enable()
            self.assertFalse(ok)
            self.assertIn("install.sh", msg)

    def test_enable_fails_without_systemctl(self):
        with patch("spitch.autostart.shutil.which", return_value=None):
            ok, msg = autostart.enable()
        self.assertFalse(ok)
        self.assertIn("systemctl", msg)

    def test_disable_runs_systemctl_and_removes_unit(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            unit = Path(td) / "spitch.service"
            unit.write_text("dummy", encoding="utf-8")
            calls = []

            def fake_run(cmd, **_):
                calls.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            with patch("spitch.autostart.unit_path", return_value=unit), \
                 patch("spitch.autostart.shutil.which", return_value="/usr/bin/systemctl"), \
                 patch("spitch.autostart.subprocess.run", side_effect=fake_run):
                ok, msg = autostart.disable()
            self.assertTrue(ok, msg)
            self.assertFalse(unit.exists())
            self.assertTrue(any("disable" in c and "--now" in c for c in calls))

    def test_disable_idempotent_when_unit_already_gone(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            unit = Path(td) / "spitch.service"  # never created
            with patch("spitch.autostart.unit_path", return_value=unit), \
                 patch("spitch.autostart.shutil.which", return_value="/usr/bin/systemctl"), \
                 patch("spitch.autostart.subprocess.run",
                       return_value=subprocess.CompletedProcess([], 0, "", "")):
                ok, msg = autostart.disable()
            self.assertTrue(ok, msg)


class IsDaemonArgvTests(unittest.TestCase):
    def test_python_m_spitch(self):
        self.assertTrue(autostart.is_daemon_argv(["python3", "-m", "spitch"]))

    def test_absolute_python(self):
        self.assertTrue(
            autostart.is_daemon_argv(["/usr/bin/python3", "-m", "spitch"])
        )

    def test_daemon_module_alias(self):
        self.assertTrue(autostart.is_daemon_argv(["python3", "-m", "spitch.daemon"]))
        self.assertTrue(
            autostart.is_daemon_argv(["python3", "-m", "spitch.__main__"])
        )

    def test_console_is_not_daemon(self):
        self.assertFalse(
            autostart.is_daemon_argv(["python3", "-m", "spitch.ui.console"])
        )

    def test_cli_and_config_are_not_daemon(self):
        self.assertFalse(autostart.is_daemon_argv(["python3", "-m", "spitch.cli"]))
        self.assertFalse(
            autostart.is_daemon_argv(
                ["python3", "-m", "spitch.ui.config_dialog"]
            )
        )

    def test_empty_and_unrelated(self):
        self.assertFalse(autostart.is_daemon_argv([]))
        self.assertFalse(autostart.is_daemon_argv(["spitch-console"]))


class IsActiveTests(unittest.TestCase):
    def test_no_systemctl_returns_false(self):
        with patch("spitch.autostart.shutil.which", return_value=None):
            self.assertFalse(autostart.is_active())

    def test_active_when_systemctl_says_active(self):
        def fake_run(cmd, **_):
            return subprocess.CompletedProcess(cmd, 0, stdout="active\n", stderr="")

        with patch("spitch.autostart.shutil.which", return_value="/usr/bin/systemctl"):
            with patch("spitch.autostart.subprocess.run", side_effect=fake_run):
                self.assertTrue(autostart.is_active())

    def test_inactive(self):
        def fake_run(cmd, **_):
            return subprocess.CompletedProcess(
                cmd, 3, stdout="inactive\n", stderr=""
            )

        with patch("spitch.autostart.shutil.which", return_value="/usr/bin/systemctl"):
            with patch("spitch.autostart.subprocess.run", side_effect=fake_run):
                self.assertFalse(autostart.is_active())


class RestartTests(unittest.TestCase):
    def test_restart_if_running_restarts_loose_daemon(self):
        with patch("spitch.autostart.is_active", return_value=False), \
             patch("spitch.autostart._daemon_pids", return_value=[123]), \
             patch(
                 "spitch.autostart.restart", return_value=(True, "restarted")
             ) as restart:
            self.assertEqual(autostart.restart_if_running(), (True, "restarted"))
        restart.assert_called_once_with()

    def test_restart_if_running_does_not_start_stopped_daemon(self):
        with patch("spitch.autostart.is_active", return_value=False), \
             patch("spitch.autostart._daemon_pids", return_value=[]), \
             patch("spitch.autostart.restart") as restart:
            ok, msg = autostart.restart_if_running()
        self.assertTrue(ok)
        self.assertIn("未运行", msg)
        restart.assert_not_called()

    def test_systemd_restart_when_unit_present_and_active(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            unit = Path(td) / "spitch.service"
            unit.write_text("old", encoding="utf-8")
            calls = []

            def fake_run(cmd, **_):
                calls.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            with patch("spitch.autostart.is_supported", return_value=True), \
                 patch("spitch.autostart.unit_path", return_value=unit), \
                 patch("spitch.autostart.is_active", return_value=True), \
                 patch("spitch.autostart._stop_loose_daemons") as stop, \
                 patch("spitch.autostart.subprocess.run", side_effect=fake_run):
                ok, msg = autostart.restart()
            self.assertTrue(ok, msg)
            self.assertIn("systemd", msg)
            stop.assert_not_called()
            self.assertTrue(any("daemon-reload" in c for c in calls))
            self.assertTrue(any("restart" in c for c in calls))
            self.assertIn("TimeoutStopSec=8", unit.read_text(encoding="utf-8"))

    def test_systemd_inactive_stops_loose_daemons_first(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            unit = Path(td) / "spitch.service"
            unit.write_text("old", encoding="utf-8")

            def fake_run(cmd, **_):
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            with patch("spitch.autostart.is_supported", return_value=True), \
                 patch("spitch.autostart.unit_path", return_value=unit), \
                 patch("spitch.autostart.is_active", return_value=False), \
                 patch("spitch.autostart._stop_loose_daemons") as stop, \
                 patch("spitch.autostart.subprocess.run", side_effect=fake_run):
                ok, msg = autostart.restart()
            self.assertTrue(ok, msg)
            stop.assert_called_once()

    def test_systemd_restart_failure_is_reported(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            unit = Path(td) / "spitch.service"
            unit.write_text("old", encoding="utf-8")

            def fake_run(cmd, **_):
                if "restart" in cmd:
                    return subprocess.CompletedProcess(
                        cmd, 1, stdout="", stderr="Job failed"
                    )
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            with patch("spitch.autostart.is_supported", return_value=True), \
                 patch("spitch.autostart.unit_path", return_value=unit), \
                 patch("spitch.autostart.is_active", return_value=True), \
                 patch("spitch.autostart.subprocess.run", side_effect=fake_run):
                ok, msg = autostart.restart()
            self.assertFalse(ok)
            self.assertIn("Job failed", msg)

    def test_fallback_spawns_launcher(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            launcher = Path(td) / "spitch-daemon"
            launcher.write_text("#!/bin/sh\n", encoding="utf-8")
            popen_cmds = []

            def fake_popen(cmd, **_):
                popen_cmds.append(cmd)
                return subprocess.CompletedProcess(cmd, 0)

            with patch("spitch.autostart.is_supported", return_value=False), \
                 patch("spitch.autostart.daemon_launcher_path", return_value=launcher), \
                 patch("spitch.autostart._stop_loose_daemons") as stop, \
                 patch("spitch.autostart.subprocess.Popen", side_effect=fake_popen):
                ok, msg = autostart.restart()
            self.assertTrue(ok, msg)
            stop.assert_called_once()
            self.assertEqual(popen_cmds, [[str(launcher)]])

    def test_fallback_fails_when_launcher_missing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            with patch("spitch.autostart.is_supported", return_value=False), \
                 patch(
                     "spitch.autostart.daemon_launcher_path",
                     return_value=Path(td) / "missing",
                 ), \
                 patch("spitch.autostart._stop_loose_daemons"):
                ok, msg = autostart.restart()
            self.assertFalse(ok)
            self.assertIn("install.sh", msg)


if __name__ == "__main__":
    unittest.main()
