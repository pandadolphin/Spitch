"""Hot-reload of provider / hotkey config without restarting the daemon."""

from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock, patch

from spitch.config import default_config, mark_verified
from spitch.cmdsock import describe_reload, request_reload
from spitch.daemon import SpitchDaemon, validate_runtime_config
from spitch.voice import State


def _doubao_cfg() -> dict:
    cfg = default_config()
    cfg["doubao"]["app_key"] = "ak"
    cfg["doubao"]["access_key"] = "sk"
    cfg["doubao"]["endpoint"] = "wss://example/doubao"
    return mark_verified(cfg)


def _grok_cfg() -> dict:
    cfg = default_config()
    cfg["provider"] = "grok"
    cfg["grok"]["api_key"] = "xai-test"
    cfg["grok"]["endpoint"] = "wss://api.x.ai/v1/stt"
    return mark_verified(cfg)


class _FakeVoice:
    def __init__(self, state: State = State.IDLE):
        self.state = state
        self.cancel_calls = 0

    def cancel(self) -> None:
        self.cancel_calls += 1
        self.state = State.IDLE


class ValidateRuntimeConfigTests(unittest.TestCase):
    def test_verified_doubao_ok(self):
        self.assertIsNone(validate_runtime_config(_doubao_cfg()))

    def test_verified_grok_ok(self):
        self.assertIsNone(validate_runtime_config(_grok_cfg()))

    def test_unverified_grok_rejected(self):
        cfg = default_config()
        cfg["provider"] = "grok"
        cfg["grok"]["api_key"] = "xai-test"
        cfg["grok"]["endpoint"] = "wss://api.x.ai/v1/stt"
        err = validate_runtime_config(cfg)
        self.assertIsNotNone(err)
        self.assertIn("not verified", err)

    def test_incomplete_rejected(self):
        err = validate_runtime_config(default_config())
        self.assertIsNotNone(err)
        self.assertIn("incomplete", err)


class DescribeReloadTests(unittest.TestCase):
    def test_offline(self):
        msg = describe_reload({"ok": False, "offline": True, "error": "no sock"})
        self.assertIn("not running", msg)

    def test_rejected(self):
        msg = describe_reload({"ok": False, "error": "not verified"})
        self.assertIn("previous config", msg)
        self.assertIn("not verified", msg)

    def test_deferred(self):
        msg = describe_reload(
            {"ok": True, "deferred": True, "provider": "grok"}
        )
        self.assertIn("session ends", msg)
        self.assertIn("grok", msg)

    def test_applied(self):
        msg = describe_reload({"ok": True, "applied": True, "provider": "grok"})
        self.assertIn("provider=grok", msg)


class RequestReloadOfflineTests(unittest.TestCase):
    def test_missing_socket(self):
        from pathlib import Path
        resp = request_reload(path=Path("/tmp/spitch-no-such-reload.sock"))
        self.assertFalse(resp["ok"])
        self.assertTrue(resp.get("offline"))


class ReloadConfigTests(unittest.TestCase):
    def setUp(self):
        self.doubao = _doubao_cfg()
        self.grok = _grok_cfg()
        self.daemon = SpitchDaemon(self.doubao)
        self.daemon._voice = _FakeVoice()
        self.daemon._audio = MagicMock()
        self.daemon._listener = MagicMock()
        new_audio = MagicMock()
        new_voice = _FakeVoice()
        self.daemon._construct_voice = MagicMock(
            return_value=(new_audio, new_voice, 31.3)
        )
        self.daemon._start_hotkeys = MagicMock()
        self.daemon._stop_hotkeys = MagicMock()

    @patch("spitch.daemon._notify")
    @patch("spitch.daemon.load_config")
    def test_switches_provider(self, load, _notify):
        load.return_value = self.grok
        resp = self.daemon.reload_config()
        self.assertTrue(resp["ok"], resp)
        self.assertTrue(resp["applied"])
        self.assertEqual(resp["provider"], "grok")
        self.assertEqual(self.daemon._cfg["provider"], "grok")
        self.daemon._stop_hotkeys.assert_called()
        self.daemon._start_hotkeys.assert_called()
        self.assertTrue(self.daemon._warmup_kick.is_set())

    @patch("spitch.daemon.load_config")
    def test_unverified_keeps_running_provider(self, load):
        unverified = default_config()
        unverified["provider"] = "grok"
        unverified["grok"]["api_key"] = "xai-test"
        unverified["grok"]["endpoint"] = "wss://api.x.ai/v1/stt"
        load.return_value = unverified
        resp = self.daemon.reload_config()
        self.assertFalse(resp["ok"])
        self.assertIn("not verified", resp["error"])
        self.assertEqual(self.daemon._cfg["provider"], "doubao")
        self.daemon._construct_voice.assert_not_called()

    @patch("spitch.daemon.load_config")
    def test_busy_session_defers(self, load):
        load.return_value = self.grok
        self.daemon._voice.state = State.RECORDING
        resp = self.daemon.reload_config()
        self.assertTrue(resp["ok"])
        self.assertTrue(resp["deferred"])
        self.assertEqual(self.daemon._cfg["provider"], "doubao")
        self.assertTrue(self.daemon._pending_reload)
        self.daemon._construct_voice.assert_not_called()

    @patch("spitch.daemon.load_config")
    def test_idle_runs_deferred_reload(self, load):
        load.return_value = self.grok
        self.daemon._pending_reload = True
        with patch.object(
            self.daemon, "reload_config", wraps=None
        ) as mocked:
            mocked.return_value = {"ok": True, "applied": True}
            self.daemon._on_state(State.IDLE)
            deadline = time.time() + 1.0
            while time.time() < deadline and mocked.call_count == 0:
                time.sleep(0.01)
            mocked.assert_called()
