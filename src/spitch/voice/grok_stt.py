"""xAI Grok Streaming Speech-to-Text client.

Endpoint: ``wss://api.x.ai/v1/stt`` (Streaming STT — not Voice Agent Realtime).

Protocol sketch (hold-to-talk / dictation)::

    1. Connect with ``Authorization: Bearer <api_key>`` and query params
       (sample_rate, encoding=pcm, interim_results, …).
    2. Wait for ``transcript.created`` before sending any audio (warmup /
       stream readiness).
    3. Stream raw PCM binary frames.
    4. On audio iterator end (release / cancel drain): send ``finalize``
       then ``audio.done`` (default PTT path).
    5. Session terminal is ``transcript.done`` — not the first
       ``speech_final``. Live stream has **no** client EOS→done timeout
       (controller owns finalize budget; KD-19). Probe keeps a strict
       done-wait.

Finalize frame type casing: live-validated constant below. Client Messages
table uses lowercase; send lowercase first until a live dump confirms
otherwise.

KD references: KD-3/6/7/8/9/16/17/19/20/21/23 — see design doc.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Iterable, Mapping
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .types import TranscriptEvent
from .ws_util import ws_connect

logger = logging.getLogger(__name__)

# Live-validated once; document winner here after first successful probe.
# Client Messages table uses lowercase; PTT example uses "Finalize".
FINALIZE_TYPE = "finalize"
AUDIO_DONE_TYPE = "audio.done"

CLOSE_TIMEOUT_S = 1.0
_POSITION_EPS = 1e-3

# Sentence/clause punctuation that should be followed by a space before Latin.
_PUNCT_END = frozenset(".?!,:;")
_CLOSING_QUOTE = frozenset("\"')")


class GrokProtocolError(Exception):
    """Raised when the Grok STT server reports an error or protocol violation."""


@dataclass
class GrokSttCredentials:
    api_key: str
    endpoint: str = "wss://api.x.ai/v1/stt"
    language: str = ""  # e.g. "en"; empty = omit param
    interim_results: bool = True
    endpointing_ms: int | None = None  # omit → server default
    filler_words: bool = False
    # Default True: PTT path per xAI guidance (finalize on release, then audio.done)
    send_finalize_on_eos: bool = True


def validate_grok_endpoint(
    endpoint: str, *, allow_insecure_localhost: bool = False
) -> None:
    """Reject non-TLS endpoints that would leak the Bearer token (KD-21).

    - Require ``wss://`` for production configs.
    - Optional escape hatch: ``ws://127.0.0.1`` / ``ws://localhost`` /
      ``ws://[::1]`` only when ``allow_insecure_localhost=True``
      (tests / local mock). Never allow ``ws://`` to remote hosts.
    """
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise GrokProtocolError("Grok endpoint must be a non-empty wss:// URL")
    parsed = urlparse(endpoint.strip())
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if scheme == "wss":
        return
    if scheme == "ws" and allow_insecure_localhost:
        if host in ("127.0.0.1", "localhost", "::1"):
            return
        raise GrokProtocolError(
            f"insecure ws:// only allowed for localhost, got host={host!r}"
        )
    raise GrokProtocolError(
        f"Grok endpoint must use wss:// (got {scheme!r}); "
        "Bearer tokens must not ride cleartext remote endpoints"
    )


def build_connect_url(
    endpoint: str,
    *,
    sample_rate: int = 16000,
    interim_results: bool = True,
    language: str = "",
    endpointing_ms: int | None = None,
    filler_words: bool = False,
) -> str:
    """Build the Grok STT WebSocket URL with required/optional query params."""
    parsed = urlparse(endpoint)
    q = dict(parse_qsl(parsed.query, keep_blank_values=True))
    q["sample_rate"] = str(sample_rate)
    q["encoding"] = "pcm"
    q["interim_results"] = "true" if interim_results else "false"
    if language:
        q["language"] = language
    if endpointing_ms is not None:
        q["endpointing"] = str(int(endpointing_ms))
    if filler_words:
        q["filler_words"] = "true"
    return urlunparse(parsed._replace(query=urlencode(q)))


def auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _parse_server_message(raw: Any) -> dict | None:
    """Normalize a WS recv payload to a JSON object dict, or None to ignore."""
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    elif isinstance(raw, str):
        text = raw
    else:
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _event_timing(
    obj: Mapping[str, Any],
) -> tuple[float | None, float | None]:
    """Extract (start, duration) from a partial event when present."""
    start = obj.get("start")
    duration = obj.get("duration")
    if start is None or duration is None:
        words = obj.get("words")
        if isinstance(words, list) and words:
            first = words[0] if isinstance(words[0], Mapping) else None
            last = words[-1] if isinstance(words[-1], Mapping) else None
            if first is not None and start is None:
                start = first.get("start")
            if last is not None and duration is None and start is not None:
                end = last.get("end")
                if end is None and last.get("start") is not None:
                    ld = last.get("duration")
                    if ld is not None:
                        end = float(last["start"]) + float(ld)
                if end is not None:
                    duration = float(end) - float(start)
    try:
        start_f = float(start) if start is not None else None
    except (TypeError, ValueError):
        start_f = None
    try:
        dur_f = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        dur_f = None
    return start_f, dur_f


def _needs_space(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left[-1].isspace() or right[0].isspace():
        return False
    r0 = right[0]
    # Latin alnum–alnum
    if left[-1].isalnum() and left[-1].isascii() and r0.isalnum() and r0.isascii():
        return True
    # Punctuation then Latin (covers "Hello."+"World", "yes,"+"please")
    if left[-1] in _PUNCT_END and r0.isalnum() and r0.isascii():
        return True
    # Closing quote then Latin (covers splits after quoted clauses, e.g. Hello." )
    if left[-1] in _CLOSING_QUOTE and r0.isalnum() and r0.isascii():
        return True
    return False


def _join_segments(parts: list[str], current: str) -> str:
    """Join confirmed parts + current (KD-23 punctuation-aware EN join)."""
    segs = [p for p in parts if p] + ([current] if current else [])
    if not segs:
        return ""
    out = segs[0]
    for nxt in segs[1:]:
        if _needs_space(out, nxt):
            out = out + " " + nxt
        else:
            out = out + nxt
    return out


@dataclass
class ConfirmedSeg:
    text: str
    start: float | None  # seconds, if known
    end: float | None  # start + duration, if known


class GrokTranscriptAccumulator:
    """Deterministic provisional state machine for Grok partials (KD-6/17/23).

    Confirmed segments never shrink; ``current`` hypothesis may rewrite.
    Dedup by server position, not text equality (KD-17). EN join is
    punctuation-aware (KD-23).
    """

    def __init__(self) -> None:
        self.confirmed: list[ConfirmedSeg] = []
        self.current: str = ""

    def full(self) -> str:
        return _join_segments([s.text for s in self.confirmed], self.current)

    def on_partial(
        self,
        text: str,
        *,
        is_final: bool,
        speech_final: bool,
        start: float | None = None,
        duration: float | None = None,
    ) -> str:
        text = text if isinstance(text, str) else ""
        if speech_final:
            segment = text or self.current
            end = (
                (start + duration)
                if (start is not None and duration is not None)
                else None
            )
            if segment and not self._is_duplicate_final(segment, start=start, end=end):
                self.confirmed.append(ConfirmedSeg(segment, start, end))
            self.current = ""
            return self.full()
        # Interim or chunk_final: replace current hypothesis (not append).
        self.current = text
        return self.full()

    def _is_duplicate_final(
        self, text: str, *, start: float | None, end: float | None
    ) -> bool:
        """KD-17: never collapse two real utterances that share the same text."""
        if not self.confirmed:
            return False
        prev = self.confirmed[-1]
        if start is not None and prev.start is not None:
            if abs(prev.start - start) < _POSITION_EPS:
                if end is not None and prev.end is not None:
                    return abs(prev.end - end) < _POSITION_EPS
                return True  # same start, treat as re-emit
            return False  # different position → keep both even if text equal
        # No reliable position → do not drop on text equality (KD-17)
        return False

    def on_done(self, text: str | None) -> str:
        if isinstance(text, str) and text.strip():
            return text  # server full-session transcript preferred
        return self.full()


def _extract_http_status(exc: BaseException) -> int | None:
    """Best-effort HTTP status across websockets generations."""
    # Legacy: InvalidStatusCode.status_code
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    # Newer: response.status_code / response.status
    resp = getattr(exc, "response", None)
    if resp is not None:
        for attr in ("status_code", "status"):
            val = getattr(resp, attr, None)
            if isinstance(val, int):
                return val
    # Some wrappers put status on the exception message / args
    for arg in getattr(exc, "args", ()) or ():
        if isinstance(arg, int) and 100 <= arg <= 599:
            return arg
    return None


def classify_ws_connect_error(exc: BaseException) -> tuple[str, str]:
    """Return (category, user_message) for a connect/handshake failure (KD-20)."""
    status = _extract_http_status(exc)
    if status == 401:
        return "credentials_rejected", "credentials rejected (HTTP 401)"
    if status == 400:
        return "invalid_configuration", "invalid configuration (HTTP 400)"
    if status == 429:
        return "rate_limited", "rate limited (HTTP 429); retry later"
    if status is not None and 500 <= status <= 599:
        return "service_unavailable", f"service error (HTTP {status})"
    if status is not None:
        return "handshake_failed", f"WebSocket handshake failed (HTTP {status})"
    if isinstance(exc, (OSError, TimeoutError, ConnectionError, asyncio.TimeoutError)):
        return "network", "cannot reach endpoint"
    return "handshake_failed", "WebSocket handshake failed"


def _assert_no_doubao_utterances(raw: Mapping[str, Any]) -> None:
    """Hard invariant: Grok raw must never look like Doubao result.utterances."""
    result = raw.get("result")
    if isinstance(result, Mapping) and "utterances" in result:
        raise GrokProtocolError(
            "Grok TranscriptEvent.raw must not contain result.utterances "
            "(Doubao-shaped payload would break controller accumulation)"
        )


def _is_ws_close_error(exc: BaseException) -> bool:
    """True for normal connection-close failures after EOS (not protocol bugs)."""
    if isinstance(exc, (ConnectionError, EOFError, TimeoutError, OSError)):
        return True
    # websockets.ConnectionClosed* (and similar) across versions
    name = type(exc).__name__
    if "ConnectionClosed" in name or name in ("ConnectionClosedOK", "ConnectionClosedError"):
        return True
    # Module-path check without hard import
    mod = getattr(type(exc), "__module__", "") or ""
    if "websockets" in mod and "ConnectionClosed" in name:
        return True
    return False



class GrokSttClient:
    """StreamingClient for xAI Grok STT WebSocket.

    Contract:
      - TranscriptEvent.text is session-facing text for tray/inject.
      - TranscriptEvent.raw is the raw Grok JSON event dict.
      - raw MUST NOT contain Doubao-shaped result.utterances.
      - is_final=True only on transcript.done.
    """

    _CONNECT_BACKOFF_S = (0.0, 1.0, 3.0, 6.0)  # match Doubao

    def __init__(
        self,
        creds: GrokSttCredentials,
        *,
        sample_rate: int = 16000,
        allow_insecure_localhost: bool = False,
    ):
        self._creds = creds
        self._sample_rate = sample_rate
        self._allow_insecure_localhost = allow_insecure_localhost
        self._ws = None  # populated in __aenter__
        self._ready = False

    async def __aenter__(self) -> "GrokSttClient":
        """Open WS only (DNS/TLS/upgrade). Does NOT wait for transcript.created.

        Use warmup() for readiness; stream() waits for created before audio.
        Must be cancellable so controller cancel during connect does not hang.
        """
        validate_grok_endpoint(
            self._creds.endpoint,
            allow_insecure_localhost=self._allow_insecure_localhost,
        )
        try:
            import websockets  # noqa: F401 — ensure available
        except ImportError as exc:
            raise RuntimeError(
                "websockets package required for live Grok STT calls; "
                "install it via pip install websockets"
            ) from exc

        url = build_connect_url(
            self._creds.endpoint,
            sample_rate=self._sample_rate,
            interim_results=self._creds.interim_results,
            language=self._creds.language,
            endpointing_ms=self._creds.endpointing_ms,
            filler_words=self._creds.filler_words,
        )
        headers = auth_headers(self._creds.api_key)
        last_exc: BaseException | None = None
        for attempt, delay in enumerate(self._CONNECT_BACKOFF_S):
            if delay:
                await asyncio.sleep(delay)
            try:
                self._ws = await ws_connect(
                    url,
                    headers=headers,
                    max_size=None,
                    close_timeout=CLOSE_TIMEOUT_S,
                )
                self._ready = False
                return self
            except (OSError, TimeoutError, asyncio.TimeoutError, ConnectionError) as exc:
                last_exc = exc
                if attempt + 1 < len(self._CONNECT_BACKOFF_S):
                    continue
                raise
            except asyncio.CancelledError:
                raise
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("connect retry loop exited without attempt")

    async def __aexit__(self, exc_type, exc, tb) -> None:
        """Close WS: library close_timeout + wait_for + transport abort fallback."""
        await self._close_ws()

    async def _close_ws(self) -> None:
        ws = self._ws
        self._ws = None
        self._ready = False
        if ws is None:
            return
        try:
            await asyncio.wait_for(ws.close(), timeout=CLOSE_TIMEOUT_S)
        except asyncio.CancelledError:
            # Cancel during close: still abort the transport, then re-raise.
            logger.debug("ws close cancelled; aborting transport")
            self._abort_transport(ws)
            raise
        except asyncio.TimeoutError:
            logger.warning("ws close timed out; aborting transport")
            self._abort_transport(ws)
        except Exception as exc:
            logger.warning("ws close failed (%s); aborting transport", exc)
            self._abort_transport(ws)

    @staticmethod
    def _abort_transport(ws: Any) -> None:
        """Best-effort transport abort across websockets versions."""
        try:
            transport = getattr(ws, "transport", None)
            if transport is not None and hasattr(transport, "abort"):
                transport.abort()
                return
            # Legacy protocol path
            protocol = getattr(ws, "protocol", None) or getattr(ws, "ws_protocol", None)
            if protocol is not None:
                t = getattr(protocol, "transport", None)
                if t is not None and hasattr(t, "abort"):
                    t.abort()
                    return
            writer = getattr(ws, "writer", None)
            if writer is not None:
                t = getattr(writer, "transport", None)
                if t is not None and hasattr(t, "abort"):
                    t.abort()
        except Exception:
            # never raise from abort fallback
            pass

    async def wait_until_ready(self, timeout: float = 5.0) -> None:
        """Recv until type==transcript.created or raise."""
        if self._ws is None:
            raise RuntimeError("GrokSttClient.wait_until_ready outside context manager")
        if self._ready:
            return
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GrokProtocolError(
                    "timed out waiting for transcript.created"
                )
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
            except asyncio.TimeoutError as exc:
                raise GrokProtocolError(
                    "timed out waiting for transcript.created"
                ) from exc
            obj = _parse_server_message(raw)
            if obj is None:
                continue
            etype = obj.get("type")
            if etype == "transcript.created":
                self._ready = True
                return
            if etype == "error":
                raise GrokProtocolError(
                    f"server error while waiting for created: {obj.get('message') or obj!r}"
                )

    async def warmup(self, timeout: float = 5.0) -> float:
        """Connect + wait_until_ready + close. Returns elapsed seconds."""
        t0 = time.monotonic()
        async with self:
            await self.wait_until_ready(timeout=timeout)
        return time.monotonic() - t0

    async def probe(self, timeout: float = 8.0) -> bool:
        """Auth + readiness + silence + finalize/audio.done; require transcript.done.

        Strict done-wait uses the remaining probe wall-clock budget (KD-20).
        """
        if self._ws is None:
            raise RuntimeError("GrokSttClient.probe called outside context manager")
        deadline = time.monotonic() + timeout
        remaining = lambda: max(0.0, deadline - time.monotonic())

        await self.wait_until_ready(timeout=remaining())
        if remaining() <= 0:
            raise GrokProtocolError("probe timed out after transcript.created")

        # ~250 ms silence at sample_rate, 16-bit mono
        n_samples = max(1, int(self._sample_rate * 0.25))
        silence = b"\x00\x00" * n_samples
        await self._ws.send(silence)
        if self._creds.send_finalize_on_eos:
            await self._ws.send(json.dumps({"type": FINALIZE_TYPE}))
        await self._ws.send(json.dumps({"type": AUDIO_DONE_TYPE}))

        while True:
            rem = remaining()
            if rem <= 0:
                raise GrokProtocolError(
                    "probe timed out waiting for transcript.done"
                )
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=rem)
            except asyncio.TimeoutError as exc:
                raise GrokProtocolError(
                    "probe timed out waiting for transcript.done"
                ) from exc
            except Exception as exc:
                raise GrokProtocolError(
                    f"incomplete probe / server closed early: {exc}"
                ) from exc
            obj = _parse_server_message(raw)
            if obj is None:
                continue
            etype = obj.get("type")
            if etype == "error":
                raise GrokProtocolError(
                    f"server rejected: {obj.get('message') or obj!r}"
                )
            if etype == "transcript.done":
                return True
        # unreachable

    async def stream(
        self, audio_iter: AsyncIterator[bytes] | Iterable[bytes]
    ) -> AsyncIterator[TranscriptEvent]:
        """Stream PCM chunks and yield TranscriptEvents (KD-19 wait-set lifecycle).

        Live stream has **no** client terminal timeout after EOS — silent
        post-EOS is owned by VoiceController.finalize_timeout.
        """
        if self._ws is None:
            raise RuntimeError("GrokSttClient.stream called outside context manager")

        await self.wait_until_ready()

        acc = GrokTranscriptAccumulator()
        send_task = asyncio.create_task(self._send_audio(audio_iter))
        recv_task: asyncio.Task | None = None
        sender_done_ok = False

        try:
            while True:
                # Drain sender outcome once without re-waiting forever
                if not sender_done_ok and send_task.done():
                    if (exc := send_task.exception()) is not None:
                        if recv_task is not None and not recv_task.done():
                            recv_task.cancel()
                        raise exc
                    sender_done_ok = True

                wait_set: set[asyncio.Task] = set()
                if not sender_done_ok and not send_task.done():
                    wait_set.add(send_task)

                if recv_task is None:
                    recv_task = asyncio.create_task(self._ws.recv())
                wait_set.add(recv_task)

                done, _pending = await asyncio.wait(
                    wait_set, return_when=asyncio.FIRST_COMPLETED
                )

                # When BOTH finish in the same wait: process sender first
                if send_task in done and not sender_done_ok:
                    if (exc := send_task.exception()) is not None:
                        if recv_task is not None and not recv_task.done():
                            recv_task.cancel()
                        raise exc
                    sender_done_ok = True
                    # send_task NEVER re-enters wait_set after this

                if recv_task is not None and recv_task in done:
                    try:
                        raw = recv_task.result()
                    except Exception as exc:
                        # After clean EOS: known close types end the stream
                        # without a fabricated timeout (controller owns budget).
                        # Unexpected post-EOS errors are re-raised for diagnosis.
                        if sender_done_ok:
                            if _is_ws_close_error(exc):
                                logger.info(
                                    "stream ended after EOS without transcript.done "
                                    "(%s: %s)",
                                    type(exc).__name__,
                                    exc,
                                )
                                return
                            logger.warning(
                                "unexpected recv error after EOS: %s: %s",
                                type(exc).__name__,
                                exc,
                            )
                            raise
                        raise
                    recv_task = None
                    obj = _parse_server_message(raw)
                    if obj is None:
                        continue
                    _assert_no_doubao_utterances(obj)
                    etype = obj.get("type")
                    if etype == "error":
                        raise GrokProtocolError(
                            f"server error: {obj.get('message') or obj!r}"
                        )
                    if etype == "transcript.partial":
                        text = obj.get("text") or ""
                        if not isinstance(text, str):
                            text = ""
                        is_final_flag = bool(obj.get("is_final"))
                        speech_final = bool(obj.get("speech_final"))
                        start, duration = _event_timing(obj)
                        full = acc.on_partial(
                            text,
                            is_final=is_final_flag,
                            speech_final=speech_final,
                            start=start,
                            duration=duration,
                        )
                        yield TranscriptEvent(text=full, is_final=False, raw=dict(obj))
                    elif etype == "transcript.done":
                        done_text = obj.get("text")
                        full = acc.on_done(
                            done_text if isinstance(done_text, str) else None
                        )
                        yield TranscriptEvent(text=full, is_final=True, raw=dict(obj))
                        return
                    # transcript.created already handled; ignore other types
        finally:
            if not send_task.done():
                send_task.cancel()
                try:
                    await send_task
                except (asyncio.CancelledError, Exception):
                    pass
            if recv_task is not None and not recv_task.done():
                recv_task.cancel()
                try:
                    await recv_task
                except (asyncio.CancelledError, Exception):
                    pass

    async def _send_audio(
        self, audio_iter: AsyncIterator[bytes] | Iterable[bytes]
    ) -> None:
        """Feed PCM then EOS control frames (finalize + audio.done)."""
        assert self._ws is not None

        async def _drain_chunks() -> AsyncIterator[bytes]:
            if hasattr(audio_iter, "__aiter__"):
                async for chunk in audio_iter:  # type: ignore[union-attr]
                    yield chunk
            else:
                for chunk in audio_iter:  # type: ignore[union-attr]
                    yield chunk
                    await asyncio.sleep(0)

        try:
            async for chunk in _drain_chunks():
                await self._ws.send(chunk)
        except Exception:
            # Propagate iterator / send failures to the stream consumer
            raise

        # EOS — best-effort; send finalize and audio.done independently so a
        # finalize failure does not skip audio.done while the socket is open.
        if self._creds.send_finalize_on_eos:
            try:
                await self._ws.send(json.dumps({"type": FINALIZE_TYPE}))
            except Exception:
                pass
        try:
            await self._ws.send(json.dumps({"type": AUDIO_DONE_TYPE}))
        except Exception:
            pass
