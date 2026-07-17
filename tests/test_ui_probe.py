"""Unit tests for multi-provider UI probe routing — no network."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from spitch.config import default_config
from spitch.ui.probe import (
    probe_credentials,
    probe_credentials_for_config,
    probe_grok_credentials,
)
from spitch.voice.doubao import DoubaoCredentials
from spitch.voice.grok_stt import GrokProtocolError, GrokSttCredentials


class ProbeCredentialsForConfigRoutingTests(unittest.TestCase):
    def test_routes_doubao(self):
        cfg = default_config()
        cfg["provider"] = "doubao"
        cfg["doubao"]["app_key"] = "ak"
        cfg["doubao"]["access_key"] = "sk"

        with patch(
            "spitch.ui.probe.probe_credentials",
            return_value=(True, "Doubao connection succeeded — credentials accepted."),
        ) as mock_probe:
            ok, msg = probe_credentials_for_config(cfg)
        self.assertTrue(ok)
        self.assertIn("Doubao", msg)
        mock_probe.assert_called_once()
        creds = mock_probe.call_args[0][0]
        self.assertIsInstance(creds, DoubaoCredentials)
        self.assertEqual(creds.app_key, "ak")

    def test_routes_grok(self):
        cfg = default_config()
        cfg["provider"] = "grok"
        cfg["grok"]["api_key"] = "xai-test-fake"

        with patch(
            "spitch.ui.probe.probe_grok_credentials",
            return_value=(True, "Grok STT connection succeeded — credentials accepted."),
        ) as mock_probe:
            ok, msg = probe_credentials_for_config(cfg)
        self.assertTrue(ok)
        self.assertIn("Grok", msg)
        mock_probe.assert_called_once()
        creds = mock_probe.call_args[0][0]
        self.assertIsInstance(creds, GrokSttCredentials)
        self.assertEqual(creds.api_key, "xai-test-fake")

    def test_default_provider_is_doubao(self):
        cfg = default_config()
        cfg.pop("provider", None)
        cfg["doubao"]["app_key"] = "ak"
        cfg["doubao"]["access_key"] = "sk"

        with patch(
            "spitch.ui.probe.probe_credentials",
            return_value=(True, "ok"),
        ) as mock_probe:
            ok, _msg = probe_credentials_for_config(cfg)
        self.assertTrue(ok)
        mock_probe.assert_called_once()

    def test_unknown_provider(self):
        cfg = default_config()
        cfg["provider"] = "acme"
        ok, msg = probe_credentials_for_config(cfg)
        self.assertFalse(ok)
        self.assertIn("Unsupported provider", msg)

    def test_grok_rejects_non_wss_endpoint(self):
        cfg = default_config()
        cfg["provider"] = "grok"
        cfg["grok"]["api_key"] = "xai-test-fake"
        cfg["grok"]["endpoint"] = "ws://example.com/v1/stt"

        with patch("spitch.ui.probe.probe_grok_credentials") as mock_probe:
            ok, msg = probe_credentials_for_config(cfg)
        self.assertFalse(ok)
        self.assertIn("wss://", msg.lower() if "wss" in msg.lower() else msg)
        self.assertIn("endpoint", msg.lower())
        mock_probe.assert_not_called()

    def test_grok_rejects_http_endpoint(self):
        cfg = default_config()
        cfg["provider"] = "grok"
        cfg["grok"]["api_key"] = "xai-test"
        cfg["grok"]["endpoint"] = "https://api.x.ai/v1/stt"
        ok, msg = probe_credentials_for_config(cfg)
        self.assertFalse(ok)
        self.assertIn("Invalid Grok endpoint", msg)

    def test_grok_non_mapping_section(self):
        cfg = default_config()
        cfg["provider"] = "grok"
        cfg["grok"] = "bad"  # type: ignore[assignment]
        ok, msg = probe_credentials_for_config(cfg)
        self.assertFalse(ok)
        self.assertIn("Invalid Grok config", msg)


class ProbeGrokCredentialsMessagesTests(unittest.TestCase):
    def test_invalid_endpoint_before_connect(self):
        creds = GrokSttCredentials(
            api_key="xai-test",
            endpoint="ws://remote.example/stt",
        )
        ok, msg = probe_grok_credentials(creds)
        self.assertFalse(ok)
        self.assertIn("Invalid Grok endpoint", msg)

    def test_protocol_error_mapped(self):
        creds = GrokSttCredentials(api_key="xai-test")

        class _FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def probe(self, timeout=8.0):
                raise GrokProtocolError("probe timed out waiting for transcript.done")

        with patch("spitch.ui.probe.GrokSttClient", _FakeClient):
            ok, msg = probe_grok_credentials(creds)
        self.assertFalse(ok)
        self.assertIn("transcript.done", msg)
        self.assertIn("Grok probe failed", msg)

    def test_handshake_401_classified(self):
        creds = GrokSttCredentials(api_key="xai-bad")

        class FakeHandshake(Exception):
            def __init__(self):
                super().__init__("401")
                self.status_code = 401

        class _FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                raise FakeHandshake()

            async def __aexit__(self, *a):
                return None

        with patch("spitch.ui.probe.GrokSttClient", _FakeClient):
            ok, msg = probe_grok_credentials(creds)
        self.assertFalse(ok)
        self.assertIn("401", msg)
        self.assertIn("credentials rejected", msg.lower())

    def test_success_message(self):
        creds = GrokSttCredentials(api_key="xai-test")

        class _FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def probe(self, timeout=8.0):
                return True

        with patch("spitch.ui.probe.GrokSttClient", _FakeClient):
            ok, msg = probe_grok_credentials(creds)
        self.assertTrue(ok)
        self.assertIn("succeeded", msg)


class ProbeDoubaoBackcompatTests(unittest.TestCase):
    def test_probe_credentials_still_works(self):
        creds = DoubaoCredentials(
            app_key="ak",
            access_key="sk",
            resource_id="r",
            endpoint="wss://example.test/ws",
        )

        class _FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def probe(self, timeout=8.0):
                return True

        with patch("spitch.ui.probe.DoubaoClient", _FakeClient):
            ok, msg = probe_credentials(creds)
        self.assertTrue(ok)
        self.assertIn("Doubao", msg)


class RunCliDoesNotForceDoubaoTests(unittest.TestCase):
    """CLI must honor provider=grok and never overwrite with doubao on save."""

    def test_cli_saves_provider_grok(self):
        from spitch.ui import config_dialog

        saved: list[dict] = []

        base = default_config()
        base["provider"] = "doubao"

        answers = iter(
            [
                "grok",  # provider
                "xai-test-fake-key",  # api_key (secret path may differ)
                "wss://api.x.ai/v1/stt",  # endpoint
                "",  # language
            ]
        )

        def fake_prompt(label, default="", *, secret=False):
            try:
                val = next(answers)
            except StopIteration:
                return default
            return val if val != "" else default

        def fake_save(cfg, path=None):
            saved.append(dict(cfg))
            return "/tmp/spitch-test-config.json"

        with (
            patch.object(config_dialog, "load_config", return_value=base),
            patch.object(config_dialog, "_prompt", side_effect=fake_prompt),
            patch.object(config_dialog, "save_config", side_effect=fake_save),
            patch.object(
                config_dialog,
                "probe_credentials_for_config",
                return_value=(True, "Grok STT connection succeeded — credentials accepted."),
            ),
            patch.object(config_dialog.getpass, "getpass", return_value="xai-test-fake-key"),
        ):
            # _prompt with secret=True uses getpass; our fake_prompt replaces _prompt entirely.
            rc = config_dialog.run_cli(probe=True)

        self.assertEqual(rc, 0)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["provider"], "grok")
        self.assertNotEqual(saved[0]["provider"], "doubao")
        self.assertEqual(saved[0]["grok"]["api_key"], "xai-test-fake-key")
        self.assertIsNotNone(saved[0].get("verified_at"))

    def test_cli_rejects_non_wss_without_mark_verified(self):
        from spitch.ui import config_dialog

        saved: list[dict] = []
        base = default_config()

        answers = iter(
            [
                "grok",
                "xai-test-fake-key",
                "ws://example.com/v1/stt",
                "",
            ]
        )

        def fake_prompt(label, default="", *, secret=False):
            try:
                val = next(answers)
            except StopIteration:
                return default
            return val if val != "" else default

        def fake_save(cfg, path=None):
            saved.append(dict(cfg))
            return "/tmp/spitch-test-config.json"

        with (
            patch.object(config_dialog, "load_config", return_value=base),
            patch.object(config_dialog, "_prompt", side_effect=fake_prompt),
            patch.object(config_dialog, "save_config", side_effect=fake_save),
            patch.object(config_dialog, "probe_credentials_for_config") as mock_probe,
        ):
            rc = config_dialog.run_cli(probe=True)

        self.assertEqual(rc, 2)
        mock_probe.assert_not_called()
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["provider"], "grok")
        self.assertIsNone(saved[0].get("verified_at"))

    def test_cli_doubao_still_works(self):
        from spitch.ui import config_dialog

        saved: list[dict] = []
        base = default_config()
        answers = iter(
            [
                "doubao",
                "appk",
                "accessk",
                "volc.bigasr.sauc.duration",
                "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel",
            ]
        )

        def fake_prompt(label, default="", *, secret=False):
            try:
                val = next(answers)
            except StopIteration:
                return default
            return val if val != "" else default

        def fake_save(cfg, path=None):
            saved.append(dict(cfg))
            return "/tmp/spitch-test-config.json"

        with (
            patch.object(config_dialog, "load_config", return_value=base),
            patch.object(config_dialog, "_prompt", side_effect=fake_prompt),
            patch.object(config_dialog, "save_config", side_effect=fake_save),
            patch.object(
                config_dialog,
                "probe_credentials_for_config",
                return_value=(True, "Doubao connection succeeded — credentials accepted."),
            ),
        ):
            rc = config_dialog.run_cli(probe=True)

        self.assertEqual(rc, 0)
        self.assertEqual(saved[0]["provider"], "doubao")
        self.assertEqual(saved[0]["doubao"]["app_key"], "appk")
        self.assertIsNotNone(saved[0].get("verified_at"))


if __name__ == "__main__":
    unittest.main()
