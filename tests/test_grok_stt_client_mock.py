"""Mock tests for Grok streaming STT client (no network, fake keys only).

Covers accumulator fixtures 1–11, KD-19 stream wait-set lifecycle (S1–S5),
probe transcript.done requirement, endpoint validation, close abort, raw
invariant, finalize+audio.done ordering, and str/bytes server messages.

Tests must NEVER open grok-voice-api.key or use real API keys.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any, AsyncIterator
from unittest.mock import MagicMock

from spitch.voice.grok_stt import (
    AUDIO_DONE_TYPE,
    FINALIZE_TYPE,
    GrokProtocolError,
    GrokSttClient,
    GrokSttCredentials,
    GrokTranscriptAccumulator,
    _extract_http_status,
    _parse_server_message,
    classify_ws_connect_error,
    validate_grok_endpoint,
)
from spitch.voice.types import TranscriptEvent


# ---------------------------------------------------------------------------
# Fake WS
# ---------------------------------------------------------------------------


class FakeWS:
    """Minimal WS double: records sends, scripts recvs, optional hang close."""

    def __init__(
        self,
        scripted: list[Any] | None = None,
        *,
        hang_close: bool = False,
        send_error_after: int | None = None,
        send_error: BaseException | None = None,
        auto_created: bool = True,
    ):
        self.sent: list[Any] = []
        self._scripted: list[Any] = list(scripted or [])
        if auto_created and not any(
            self._is_type(m, "transcript.created") for m in self._scripted
        ):
            # Prepend created if stream/wait_until_ready will need it and
            # the test didn't supply one.
            self._scripted.insert(0, json.dumps({"type": "transcript.created"}))
        self._closed = False
        self.hang_close = hang_close
        self.send_error_after = send_error_after
        self.send_error = send_error or RuntimeError("send failed")
        self.transport = MagicMock()
        self.transport.abort = MagicMock()
        self._recv_waiters = 0

    @staticmethod
    def _is_type(msg: Any, etype: str) -> bool:
        if isinstance(msg, (str, bytes)):
            try:
                obj = json.loads(msg if isinstance(msg, str) else msg.decode())
            except Exception:
                return False
            return isinstance(obj, dict) and obj.get("type") == etype
        return False

    async def send(self, data: Any) -> None:
        if self._closed:
            raise ConnectionError("ws closed")
        n = len(self.sent)
        if self.send_error_after is not None and n >= self.send_error_after:
            raise self.send_error
        self.sent.append(data)

    async def recv(self) -> Any:
        while not self._scripted:
            if self._closed:
                raise ConnectionError("ws closed")
            await asyncio.sleep(0.01)
        await asyncio.sleep(0)
        return self._scripted.pop(0)

    async def close(self) -> None:
        if self.hang_close:
            # Never return — exercises close_timeout + abort fallback
            await asyncio.Future()
        self._closed = True

    def push(self, msg: Any) -> None:
        self._scripted.append(msg)


def _partial(
    text: str,
    *,
    is_final: bool = False,
    speech_final: bool = False,
    start: float | None = None,
    duration: float | None = None,
    as_bytes: bool = False,
) -> str | bytes:
    obj: dict[str, Any] = {
        "type": "transcript.partial",
        "text": text,
        "is_final": is_final,
        "speech_final": speech_final,
    }
    if start is not None:
        obj["start"] = start
    if duration is not None:
        obj["duration"] = duration
    s = json.dumps(obj)
    return s.encode("utf-8") if as_bytes else s


def _done(text: str = "", *, as_bytes: bool = False) -> str | bytes:
    s = json.dumps({"type": "transcript.done", "text": text})
    return s.encode("utf-8") if as_bytes else s


def _created() -> str:
    return json.dumps({"type": "transcript.created"})


def _error(message: str = "boom") -> str:
    return json.dumps({"type": "error", "message": message})


def _fake_creds(**kwargs) -> GrokSttCredentials:
    """Fake key only — never load grok-voice-api.key."""
    base = dict(
        api_key="xai-fake-test-key-not-real",
        endpoint="wss://api.x.ai/v1/stt",
    )
    base.update(kwargs)
    return GrokSttCredentials(**base)


# ---------------------------------------------------------------------------
# Accumulator fixtures 1–11
# ---------------------------------------------------------------------------


class AccumulatorFixtureTests(unittest.TestCase):
    def test_fixture_1_interim_growth_cjk(self):
        acc = GrokTranscriptAccumulator()
        t1 = acc.on_partial("你", is_final=False, speech_final=False)
        t2 = acc.on_partial("你好", is_final=False, speech_final=False)
        self.assertEqual([t1, t2], ["你", "你好"])

    def test_fixture_2_interim_rewrite_dip(self):
        acc = GrokTranscriptAccumulator()
        t1 = acc.on_partial("你好世界", is_final=False, speech_final=False)
        t2 = acc.on_partial("你好", is_final=False, speech_final=False)
        self.assertEqual(t1, "你好世界")
        self.assertEqual(t2, "你好")
        self.assertEqual(acc.confirmed, [])

    def test_fixture_3_chunk_final_no_double_append(self):
        acc = GrokTranscriptAccumulator()
        t1 = acc.on_partial("hello", is_final=False, speech_final=False)
        t2 = acc.on_partial("hello there", is_final=True, speech_final=False)
        t3 = acc.on_partial(
            "hello there friend", is_final=False, speech_final=False
        )
        self.assertEqual(t1, "hello")
        self.assertEqual(t2, "hello there")
        self.assertEqual(t3, "hello there friend")
        # chunk_final only rewrites current — not confirmed
        self.assertEqual(acc.confirmed, [])

    def test_fixture_4_cjk_speech_final_no_space(self):
        acc = GrokTranscriptAccumulator()
        t1 = acc.on_partial("第一句。", is_final=True, speech_final=True)
        t2 = acc.on_partial("第二", is_final=False, speech_final=False)
        t3 = acc.on_partial("第二句", is_final=False, speech_final=False)
        t4 = acc.on_partial("第二句。", is_final=True, speech_final=True)
        self.assertEqual(t1, "第一句。")
        self.assertEqual(t2, "第一句。第二")
        self.assertEqual(t3, "第一句。第二句")
        self.assertEqual(t4, "第一句。第二句。")

    def test_fixture_5_done_prefers_server_full_text(self):
        acc = GrokTranscriptAccumulator()
        acc.on_partial("部", is_final=False, speech_final=False)
        final = acc.on_done("完整全文。")
        self.assertEqual(final, "完整全文。")

    def test_fixture_6_error_raises_in_stream(self):
        # Covered more fully in StreamLifecycleTests; here assert parse path
        async def _go():
            client = GrokSttClient(_fake_creds())
            ws = FakeWS(
                [_created(), _error("server rejected audio")],
                auto_created=False,
            )
            client._ws = ws

            async def chunks() -> AsyncIterator[bytes]:
                yield b"\x00" * 320
                await asyncio.sleep(10)  # hang until cancelled

            events = []
            async for evt in client.stream(chunks()):
                events.append(evt)

        with self.assertRaises(GrokProtocolError):
            asyncio.run(_go())

    def test_fixture_7_two_yes_different_positions(self):
        acc = GrokTranscriptAccumulator()
        t1 = acc.on_partial(
            "yes", is_final=True, speech_final=True, start=0.0, duration=0.5
        )
        t2 = acc.on_partial(
            "yes", is_final=True, speech_final=True, start=1.2, duration=0.4
        )
        self.assertEqual(t1, "yes")
        self.assertEqual(t2, "yes yes")

    def test_fixture_8_position_dedup_same_start(self):
        acc = GrokTranscriptAccumulator()
        t1 = acc.on_partial(
            "hello", is_final=True, speech_final=True, start=0.5, duration=0.3
        )
        t2 = acc.on_partial(
            "hello", is_final=True, speech_final=True, start=0.5, duration=0.3
        )
        self.assertEqual(t1, "hello")
        self.assertEqual(t2, "hello")
        self.assertEqual(len(acc.confirmed), 1)

    def test_fixture_9_en_space_join(self):
        acc = GrokTranscriptAccumulator()
        t1 = acc.on_partial(
            "hello", is_final=True, speech_final=True, start=0.0, duration=0.3
        )
        t2 = acc.on_partial(
            "world", is_final=True, speech_final=True, start=0.8, duration=0.3
        )
        self.assertEqual(t1, "hello")
        self.assertEqual(t2, "hello world")

    def test_fixture_10_punctuation_space(self):
        acc = GrokTranscriptAccumulator()
        t1 = acc.on_partial(
            "Hello.", is_final=True, speech_final=True, start=0.0, duration=0.3
        )
        t2 = acc.on_partial(
            "World", is_final=True, speech_final=True, start=0.5, duration=0.3
        )
        self.assertEqual(t1, "Hello.")
        self.assertEqual(t2, "Hello. World")

    def test_fixture_11_comma_space(self):
        acc = GrokTranscriptAccumulator()
        t1 = acc.on_partial(
            "yes,", is_final=True, speech_final=True, start=0.0, duration=0.2
        )
        t2 = acc.on_partial(
            "please", is_final=True, speech_final=True, start=0.4, duration=0.3
        )
        self.assertEqual(t1, "yes,")
        self.assertEqual(t2, "yes, please")


# ---------------------------------------------------------------------------
# Parse / endpoint / classify
# ---------------------------------------------------------------------------


class ParseAndEndpointTests(unittest.TestCase):
    def test_parse_str_and_bytes(self):
        obj = {"type": "transcript.partial", "text": "hi"}
        s = json.dumps(obj)
        self.assertEqual(_parse_server_message(s), obj)
        self.assertEqual(_parse_server_message(s.encode("utf-8")), obj)
        self.assertIsNone(_parse_server_message(b"\xff\xfe not utf8"))
        self.assertIsNone(_parse_server_message(None))
        self.assertIsNone(_parse_server_message("[1,2,3]"))

    def test_endpoint_wss_ok(self):
        validate_grok_endpoint("wss://api.x.ai/v1/stt")

    def test_endpoint_reject_ws_remote(self):
        with self.assertRaises(GrokProtocolError):
            validate_grok_endpoint("ws://example.com/v1/stt")

    def test_endpoint_reject_http(self):
        with self.assertRaises(GrokProtocolError):
            validate_grok_endpoint("https://api.x.ai/v1/stt")

    def test_endpoint_localhost_hatch(self):
        with self.assertRaises(GrokProtocolError):
            validate_grok_endpoint("ws://127.0.0.1:9999/stt")
        validate_grok_endpoint(
            "ws://127.0.0.1:9999/stt", allow_insecure_localhost=True
        )
        validate_grok_endpoint(
            "ws://localhost/stt", allow_insecure_localhost=True
        )

    def test_extract_http_status_legacy(self):
        class InvalidStatusCode(Exception):
            def __init__(self, status_code: int):
                self.status_code = status_code

        self.assertEqual(_extract_http_status(InvalidStatusCode(401)), 401)

    def test_extract_http_status_response_shape(self):
        class Exc(Exception):
            def __init__(self):
                self.response = MagicMock(status_code=429)

        self.assertEqual(_extract_http_status(Exc()), 429)

    def test_classify_ws_connect_error(self):
        class E401(Exception):
            status_code = 401

        cat, msg = classify_ws_connect_error(E401())
        self.assertEqual(cat, "credentials_rejected")
        self.assertIn("401", msg)

        class E400(Exception):
            status_code = 400

        cat, msg = classify_ws_connect_error(E400())
        self.assertEqual(cat, "invalid_configuration")
        self.assertIn("400", msg)

        class E403(Exception):
            status_code = 403

        cat, msg = classify_ws_connect_error(E403())
        self.assertEqual(cat, "permission_denied")
        self.assertIn("403", msg)
        self.assertIn("console.x.ai", msg)

        class E429(Exception):
            status_code = 429

        cat, msg = classify_ws_connect_error(E429())
        self.assertEqual(cat, "rate_limited")
        self.assertIn("429", msg)

        class E500(Exception):
            status_code = 503

        cat, msg = classify_ws_connect_error(E500())
        self.assertEqual(cat, "service_unavailable")

        cat, msg = classify_ws_connect_error(OSError("down"))
        self.assertEqual(cat, "network")

        # errno 111 (ECONNREFUSED) must not become "HTTP 111"
        cat, msg = classify_ws_connect_error(
            ConnectionRefusedError(111, "Connection refused")
        )
        self.assertEqual(cat, "network")
        self.assertEqual(msg, "cannot reach endpoint")
        self.assertNotIn("111", msg)
        self.assertIsNone(
            _extract_http_status(ConnectionRefusedError(111, "Connection refused"))
        )

    def test_classify_response_shaped_401(self):
        """Response-object status_code (websockets 14+ InvalidStatus shape)."""

        class InvalidStatusLike(Exception):
            def __init__(self):
                self.response = MagicMock(status_code=401)

        cat, msg = classify_ws_connect_error(InvalidStatusLike())
        self.assertEqual(cat, "credentials_rejected")
        self.assertIn("401", msg)


# ---------------------------------------------------------------------------
# Stream lifecycle S1–S5 + happy path
# ---------------------------------------------------------------------------


class StreamLifecycleTests(unittest.TestCase):
    def _client_with_ws(self, ws: FakeWS) -> GrokSttClient:
        client = GrokSttClient(_fake_creds())
        client._ws = ws
        return client

    def test_happy_path_finalize_then_audio_done(self):
        async def _go():
            ws = FakeWS(
                [
                    _created(),
                    _partial("hi"),
                    _partial("hello", is_final=True, speech_final=True, start=0.0),
                    _done("hello"),
                ],
                auto_created=False,
            )
            client = self._client_with_ws(ws)

            async def chunks() -> AsyncIterator[bytes]:
                yield b"\x00" * 320
                yield b"\x01" * 320

            events: list[TranscriptEvent] = []
            async for evt in client.stream(chunks()):
                events.append(evt)
            return events, ws.sent

        events, sent = asyncio.run(_go())
        self.assertTrue(events)
        self.assertTrue(events[-1].is_final)
        self.assertEqual(events[-1].text, "hello")
        # Only transcript.done yields is_final=True
        self.assertFalse(any(e.is_final for e in events[:-1]))
        # Audio binary then finalize then audio.done
        control = [s for s in sent if isinstance(s, str)]
        self.assertEqual(len(control), 2)
        self.assertEqual(json.loads(control[0])["type"], FINALIZE_TYPE)
        self.assertEqual(json.loads(control[1])["type"], AUDIO_DONE_TYPE)
        audio = [s for s in sent if isinstance(s, (bytes, bytearray))]
        self.assertEqual(len(audio), 2)

    def test_str_and_bytes_server_messages(self):
        async def _go():
            ws = FakeWS(
                [
                    _created(),
                    _partial("a", as_bytes=True),
                    _partial("ab", as_bytes=False),
                    _done("ab", as_bytes=True),
                ],
                auto_created=False,
            )
            client = self._client_with_ws(ws)

            async def chunks() -> AsyncIterator[bytes]:
                yield b"\x00" * 64

            events = []
            async for evt in client.stream(chunks()):
                events.append(evt)
            return events

        events = asyncio.run(_go())
        self.assertEqual([e.text for e in events], ["a", "ab", "ab"])
        self.assertTrue(events[-1].is_final)

    def test_no_utterances_in_raw(self):
        async def _go():
            ws = FakeWS(
                [
                    _created(),
                    _partial("x"),
                    _done("x"),
                ],
                auto_created=False,
            )
            client = self._client_with_ws(ws)

            async def chunks() -> AsyncIterator[bytes]:
                yield b"\x00" * 32

            events = []
            async for evt in client.stream(chunks()):
                events.append(evt)
            return events

        events = asyncio.run(_go())
        for e in events:
            raw = e.raw
            self.assertNotIn("utterances", raw)
            result = raw.get("result") if isinstance(raw, dict) else None
            if isinstance(result, dict):
                self.assertNotIn("utterances", result)

    def test_s1_audio_iter_raises(self):
        async def _go():
            ws = FakeWS([_created()], auto_created=False)
            # Keep recv hanging so sender failure is what ends the stream
            client = self._client_with_ws(ws)

            async def bad_chunks() -> AsyncIterator[bytes]:
                yield b"\x00" * 32
                raise RuntimeError("mic died")

            async for _ in client.stream(bad_chunks()):
                pass

        with self.assertRaises(RuntimeError) as cm:
            asyncio.run(_go())
        self.assertIn("mic died", str(cm.exception))

    def test_s2_send_fails(self):
        async def _go():
            ws = FakeWS(
                [_created()],
                auto_created=False,
                send_error_after=0,
                send_error=ConnectionError("send fail"),
            )
            client = self._client_with_ws(ws)

            async def chunks() -> AsyncIterator[bytes]:
                yield b"\x00" * 32
                await asyncio.sleep(5)

            async for _ in client.stream(chunks()):
                pass

        with self.assertRaises(ConnectionError):
            asyncio.run(_go())

    def test_s3a_silent_post_eos_cancel_no_client_timeout(self):
        """Sender completes EOS; server silent; aclose/cancel — no timeout error."""

        async def _go():
            # Only created; never done — silent after EOS
            ws = FakeWS([_created()], auto_created=False)
            client = self._client_with_ws(ws)

            async def chunks() -> AsyncIterator[bytes]:
                yield b"\x00" * 32

            agen = client.stream(chunks())
            # Drain nothing yet — start stream in a task
            task = asyncio.create_task(agen.__anext__())
            # Let sender finish EOS
            await asyncio.sleep(0.05)
            # Cancel: should end cleanly without GrokProtocolError timeout
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, StopAsyncIteration):
                pass
            # aclose the generator
            await agen.aclose()
            # Confirm EOS frames were sent
            control = [s for s in ws.sent if isinstance(s, str)]
            self.assertTrue(
                any(json.loads(c).get("type") == AUDIO_DONE_TYPE for c in control)
            )
            return "ok"

        result = asyncio.run(_go())
        self.assertEqual(result, "ok")

    def test_s3c_probe_requires_transcript_done(self):
        async def _go():
            # created only — no transcript.done
            ws = FakeWS([_created()], auto_created=False)
            client = self._client_with_ws(ws)
            await client.probe(timeout=0.3)

        with self.assertRaises(GrokProtocolError) as cm:
            asyncio.run(_go())
        self.assertIn("transcript.done", str(cm.exception).lower())

    def test_probe_fails_on_clean_close_without_done(self):
        """Clean close after EOS without transcript.done is probe failure (KD-20)."""

        class CloseAfterEosWS(FakeWS):
            async def send(self, data):
                await super().send(data)
                # After audio.done, close the socket (no transcript.done).
                if isinstance(data, str):
                    try:
                        if json.loads(data).get("type") == AUDIO_DONE_TYPE:
                            self._closed = True
                            self._scripted.clear()
                    except Exception:
                        pass

            async def recv(self):
                if self._closed and not self._scripted:
                    raise ConnectionError("ws closed without transcript.done")
                return await super().recv()

        async def _go():
            ws = CloseAfterEosWS([_created()], auto_created=False)
            client = self._client_with_ws(ws)
            await client.probe(timeout=2.0)

        with self.assertRaises(GrokProtocolError) as cm:
            asyncio.run(_go())
        msg = str(cm.exception).lower()
        self.assertTrue(
            "incomplete" in msg or "closed" in msg,
            f"expected incomplete/closed failure, got: {cm.exception!r}",
        )

    def test_probe_success_on_done(self):
        async def _go():
            ws = FakeWS(
                [
                    _created(),
                    # after silence + audio.done the server replies done
                    _done(""),
                ],
                auto_created=False,
            )
            client = self._client_with_ws(ws)
            return await client.probe(timeout=2.0)

        self.assertTrue(asyncio.run(_go()))

    def test_s4_dual_complete_same_wait(self):
        """send_task and recv transcript.done finish in same wait window."""

        async def _go():
            # Pre-load done so it can complete immediately when recv starts
            # after audio is fully drained (or race).
            ws = FakeWS(
                [
                    _created(),
                    _partial("ok", speech_final=True, is_final=True, start=0.0),
                    _done("ok"),
                ],
                auto_created=False,
            )
            client = self._client_with_ws(ws)

            async def chunks() -> AsyncIterator[bytes]:
                yield b"\x00" * 16

            events = []
            async for evt in client.stream(chunks()):
                events.append(evt)
            return events

        events = asyncio.run(_go())
        self.assertTrue(events[-1].is_final)
        self.assertEqual(events[-1].text, "ok")

    def test_s5_no_spin_after_eos(self):
        """After EOS only recv stays in wait set; partials then done process."""

        async def _go():
            ws = FakeWS([_created()], auto_created=False)
            client = self._client_with_ws(ws)

            async def chunks() -> AsyncIterator[bytes]:
                yield b"\x00" * 16

            events: list[TranscriptEvent] = []

            async def consume():
                async for evt in client.stream(chunks()):
                    events.append(evt)

            task = asyncio.create_task(consume())
            # Wait until EOS control frames are out
            for _ in range(200):
                control = [s for s in ws.sent if isinstance(s, str)]
                if any(
                    json.loads(c).get("type") == AUDIO_DONE_TYPE for c in control
                ):
                    break
                await asyncio.sleep(0.01)
            # Push late partials then done (post-EOS only recv in wait set)
            ws.push(_partial("late"))
            ws.push(_done("late"))
            await asyncio.wait_for(task, timeout=2.0)
            return events

        events = asyncio.run(_go())
        self.assertTrue(any(e.text == "late" for e in events))
        self.assertTrue(events[-1].is_final)

    def test_close_hang_triggers_abort(self):
        async def _go():
            client = GrokSttClient(_fake_creds())
            ws = FakeWS(hang_close=True, auto_created=False)
            client._ws = ws
            await client.__aexit__(None, None, None)
            return ws

        ws = asyncio.run(_go())
        ws.transport.abort.assert_called()
        # Reference dropped
        # (client._ws cleared inside _close_ws)


class ConnectValidationTests(unittest.TestCase):
    def test_aenter_rejects_non_wss(self):
        async def _go():
            client = GrokSttClient(
                _fake_creds(endpoint="ws://evil.example/stt")
            )
            await client.__aenter__()

        with self.assertRaises(GrokProtocolError):
            asyncio.run(_go())


if __name__ == "__main__":
    unittest.main()
