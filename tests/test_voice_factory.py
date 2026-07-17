"""Unit tests for :mod:`spitch.voice.factory` — no network."""

from __future__ import annotations

import unittest

from spitch.config import ConfigError, default_config
from spitch.voice.doubao import DoubaoClient
from spitch.voice.factory import make_client_factory, make_streaming_client
from spitch.voice.grok_stt import GrokProtocolError, GrokSttClient


class MakeStreamingClientTests(unittest.TestCase):
    def test_provider_doubao(self):
        cfg = default_config()
        cfg["provider"] = "doubao"
        cfg["doubao"]["app_key"] = "ak"
        cfg["doubao"]["access_key"] = "sk"
        client = make_streaming_client(cfg, sample_rate=16000)
        self.assertIsInstance(client, DoubaoClient)

    def test_provider_grok(self):
        cfg = default_config()
        cfg["provider"] = "grok"
        cfg["grok"]["api_key"] = "xai-test-fake"
        client = make_streaming_client(cfg, sample_rate=16000)
        self.assertIsInstance(client, GrokSttClient)

    def test_default_provider_is_doubao(self):
        cfg = default_config()
        # no provider key treated as doubao by factory (or explicit default)
        cfg.pop("provider", None)
        cfg["doubao"]["app_key"] = "ak"
        cfg["doubao"]["access_key"] = "sk"
        client = make_streaming_client(cfg, sample_rate=16000)
        self.assertIsInstance(client, DoubaoClient)

    def test_unknown_provider_raises(self):
        cfg = default_config()
        cfg["provider"] = "acme"
        with self.assertRaises(RuntimeError) as cm:
            make_streaming_client(cfg, sample_rate=16000)
        self.assertIn("unsupported provider", str(cm.exception))

    def test_m4_non_mapping_grok_raises_config_error_not_attribute_error(self):
        cfg = default_config()
        cfg["provider"] = "grok"
        for bad in ("bad", ["x"], 42):
            cfg["grok"] = bad  # type: ignore[assignment]
            with self.assertRaises(ConfigError) as cm:
                make_streaming_client(cfg, sample_rate=16000)
            self.assertNotIsInstance(cm.exception, AttributeError)
            self.assertIn("grok", str(cm.exception).lower())

    def test_m4_non_mapping_doubao_raises_config_error(self):
        cfg = default_config()
        cfg["provider"] = "doubao"
        cfg["doubao"] = "bad"  # type: ignore[assignment]
        with self.assertRaises(ConfigError):
            make_streaming_client(cfg, sample_rate=16000)

    def test_grok_insecure_remote_endpoint_rejected(self):
        cfg = default_config()
        cfg["provider"] = "grok"
        cfg["grok"]["api_key"] = "xai-test"
        cfg["grok"]["endpoint"] = "ws://example.com/v1/stt"
        with self.assertRaises(GrokProtocolError):
            make_streaming_client(cfg, sample_rate=16000)

    def test_client_factory_callable(self):
        cfg = default_config()
        cfg["provider"] = "grok"
        cfg["grok"]["api_key"] = "xai-test"
        factory = make_client_factory(cfg, sample_rate=16000)
        a = factory()
        b = factory()
        self.assertIsInstance(a, GrokSttClient)
        self.assertIsInstance(b, GrokSttClient)
        self.assertIsNot(a, b)


if __name__ == "__main__":
    unittest.main()
