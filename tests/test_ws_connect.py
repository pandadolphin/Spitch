"""Unit tests for KD-16 ws_connect header compatibility helper."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from spitch.voice.ws_util import _header_kwarg_name, ws_connect


def _make_connect_fn(*, header_param: str):
    """Build a fake connect coroutine whose signature has only one header kwarg."""

    if header_param == "additional_headers":

        async def connect(url, *, additional_headers=None, close_timeout=None, max_size=None, **kwargs):
            return {
                "url": url,
                "additional_headers": additional_headers,
                "close_timeout": close_timeout,
                "max_size": max_size,
                "kwargs": kwargs,
            }

    elif header_param == "extra_headers":

        async def connect(url, *, extra_headers=None, close_timeout=None, max_size=None, **kwargs):
            return {
                "url": url,
                "extra_headers": extra_headers,
                "close_timeout": close_timeout,
                "max_size": max_size,
                "kwargs": kwargs,
            }

    else:
        raise ValueError(header_param)

    return connect


class HeaderKwargNameTests(unittest.TestCase):
    def test_prefers_additional_headers(self):
        fn = _make_connect_fn(header_param="additional_headers")
        self.assertEqual(_header_kwarg_name(fn), "additional_headers")

    def test_falls_back_to_extra_headers(self):
        fn = _make_connect_fn(header_param="extra_headers")
        self.assertEqual(_header_kwarg_name(fn), "extra_headers")


class WsConnectTests(unittest.TestCase):
    def test_uses_additional_headers_when_present(self):
        fake = _make_connect_fn(header_param="additional_headers")

        async def _go():
            with patch("websockets.connect", fake):
                result = await ws_connect(
                    "wss://example.test/stt",
                    headers={"Authorization": "Bearer fake-key"},
                    max_size=None,
                    close_timeout=1.0,
                )
            return result

        result = asyncio.run(_go())
        self.assertEqual(
            result["additional_headers"],
            [("Authorization", "Bearer fake-key")],
        )
        self.assertEqual(result["close_timeout"], 1.0)
        self.assertIsNone(result["max_size"])

    def test_uses_extra_headers_when_present(self):
        fake = _make_connect_fn(header_param="extra_headers")

        async def _go():
            with patch("websockets.connect", fake):
                result = await ws_connect(
                    "wss://example.test/stt",
                    headers=[("X-Api-App-Key", "A"), ("X-Api-Access-Key", "B")],
                    close_timeout=2.5,
                )
            return result

        result = asyncio.run(_go())
        self.assertEqual(
            result["extra_headers"],
            [("X-Api-App-Key", "A"), ("X-Api-Access-Key", "B")],
        )
        self.assertEqual(result["close_timeout"], 2.5)

    def test_close_timeout_passthrough(self):
        """close_timeout is forwarded for both header-kwarg branches."""
        for param in ("additional_headers", "extra_headers"):
            with self.subTest(param=param):
                fake = _make_connect_fn(header_param=param)

                async def _go():
                    with patch("websockets.connect", fake):
                        return await ws_connect(
                            "wss://x",
                            headers={},
                            close_timeout=0.75,
                        )

                result = asyncio.run(_go())
                self.assertEqual(result["close_timeout"], 0.75)


if __name__ == "__main__":
    unittest.main()
