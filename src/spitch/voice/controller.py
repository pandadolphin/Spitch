"""Push-to-talk voice controller — the bridge between hotkey events,
the audio capture layer, and a provider-agnostic streaming client.

The controller's lifecycle:

    IDLE  --press_talk-->  RECORDING  --release_talk-->  FINALIZING  -->  IDLE

Streaming partial transcripts are pushed to a caller-provided
``on_partial(text)`` callback. The final text is pushed to
``on_final(text)``. The controller is hotkey-source-agnostic — the
daemon wires it up to evdev events; tests wire it up to a fake client.

Concurrency: capture runs in a daemon thread, the asyncio event loop
runs in another daemon thread, so the caller's main thread stays
responsive while a recording is in flight. The controller exposes
``press()`` / ``release()`` / ``cancel()`` from the main thread and
is otherwise fully internal.

KD-15: cancel actively cancels the published session task (including
hang during ``__aenter__`` / blocked ``recv``). Publish loop+task under
lock as early as possible; if ``_cancel`` is already set at publish,
cancel the task immediately (C0).
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import AsyncIterator, Callable, Iterable, Iterator, Protocol

from .audio import AudioCapture, AudioConfig, AudioCaptureError


class State(Enum):
    IDLE = "idle"
    RECORDING = "recording"
    FINALIZING = "finalizing"
    ERROR = "error"


@dataclass
class TranscriptUpdate:
    text: str
    is_final: bool


class StreamingClient(Protocol):
    """The slice of DoubaoClient / GrokSttClient we depend on."""

    async def __aenter__(self) -> "StreamingClient": ...
    async def __aexit__(self, exc_type, exc, tb) -> None: ...
    def stream(self, audio_iter) -> AsyncIterator: ...  # yields .text/.is_final


class VoiceController:
    """State machine for hold-to-talk transcription.

    ``client_factory`` returns a fresh streaming client per press —
    typically ``make_client_factory(cfg, sample_rate=...)``. Tests
    can pass a fake client.

    ``audio`` is an :class:`AudioCapture` (or duck-typed equivalent —
    must implement ``start()``, ``stop()``, ``chunks()``).

    All callbacks fire on the controller's own thread — they should be
    cheap and not raise.
    """

    def __init__(
        self,
        client_factory: Callable[[], StreamingClient],
        audio: AudioCapture | None = None,
        *,
        on_partial: Callable[[str], None] | None = None,
        on_final: Callable[[str], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
        on_state: Callable[[State], None] | None = None,
        finalize_timeout: float = 2.0,
        audio_config: AudioConfig | None = None,
        error_idle_timeout: float = 30.0,
    ):
        self._client_factory = client_factory
        self._audio = audio or AudioCapture(audio_config)
        self._on_partial = on_partial or (lambda _t: None)
        self._on_final = on_final or (lambda _t: None)
        self._on_error = on_error or (lambda _e: None)
        self._on_state = on_state or (lambda _s: None)
        self._finalize_timeout = finalize_timeout
        # How long ERROR can sit before we silently flip it back to IDLE
        # so the tray label stops shouting "出错" after a transient
        # network blip is long since over. The next press is also
        # accepted while in ERROR, so this is purely cosmetic — but
        # the cosmetic difference matters: a stuck "出错" badge makes
        # users think the app is still broken when it isn't.
        self._error_idle_timeout = error_idle_timeout

        self._state = State.IDLE
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._latest_text = ""
        # Commit-once guard: at most one on_final per session (rev 7).
        # Cleared on press; set by _commit_final on success/fallback paths.
        self._final_committed = False
        self._worker: threading.Thread | None = None
        self._error_recovery_timer: threading.Timer | None = None
        # KD-15: session loop + task published under lock for cross-thread cancel.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session_task: asyncio.Task | None = None

    # -- introspection -------------------------------------------------

    @property
    def state(self) -> State:
        return self._state

    @property
    def latest_text(self) -> str:
        return self._latest_text

    # -- main API ------------------------------------------------------

    def press(self) -> bool:
        """Start a recording session. Returns False if already recording.

        ERROR is treated as a soft latch — the next press resets and
        starts fresh. This keeps the daemon usable after transient
        ASR / WebSocket / network failures without forcing a
        process restart.
        """
        with self._lock:
            if self._state not in (State.IDLE, State.ERROR):
                return False
            self._cancel.clear()
            self._latest_text = ""
            self._final_committed = False
            self._loop = None
            self._session_task = None
            self._set_state(State.RECORDING)
        try:
            self._audio.start()
        except Exception as exc:
            # Catch broadly: AudioCaptureError is the documented case,
            # but the underlying backend can throw OSError (audio device
            # gone), RuntimeError (thread/proc spawn refused), etc. If
            # any of those leak, state is stuck at RECORDING with no
            # active session and the next press() refuses forever.
            self._on_error(exc)
            self._set_state(State.ERROR)
            return False
        try:
            self._worker = threading.Thread(
                target=self._run_session, name="spitch-voice-worker", daemon=True
            )
            self._worker.start()
        except Exception as exc:
            # Worker spawn failed — undo the audio start so we don't
            # leak the open mic into the next press.
            try:
                self._audio.stop()
            except Exception:
                pass
            self._on_error(exc)
            self._set_state(State.ERROR)
            return False
        return True

    def release(self) -> None:
        """Signal end-of-stream; the worker thread finishes finalizing."""
        with self._lock:
            if self._state != State.RECORDING:
                return
            self._set_state(State.FINALIZING)
        # stop capture so the audio iterator drains and the WS sender
        # writes its terminal frame.
        self._audio.stop()

    def cancel(self) -> None:
        """Abort: stop capture, signal cancellation, cancel session task.

        KD-15: set ``_cancel`` under the same lock as reading loop/task so
        a concurrent publish always either (a) sees ``already=True`` and
        cancels the new task (C0), or (b) is visible here as a non-None
        task we schedule cancel on. Then stop audio and
        ``call_soon_threadsafe(task.cancel)`` (C4: RuntimeError-safe).
        """
        with self._lock:
            if self._state == State.IDLE:
                return
            # Flag first under lock — must not race publish's already check.
            self._cancel.set()
            loop = self._loop
            task = self._session_task
        self._audio.stop()
        if task is not None and loop is not None:
            try:
                if not loop.is_closed():
                    loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                # Loop closed between is_closed() check and schedule, or
                # call_soon_threadsafe rejected a dead loop — treat as
                # already-terminating session (C4). Do not raise to caller.
                pass

    # -- internals -----------------------------------------------------

    def _set_state(self, s: State) -> None:
        self._state = s
        # Manage the ERROR-idle recovery timer in lockstep with state
        # transitions. Any transition out of ERROR cancels a pending
        # timer; entering ERROR (re-)arms one.
        if self._error_recovery_timer is not None:
            try:
                self._error_recovery_timer.cancel()
            except Exception:
                pass
            self._error_recovery_timer = None
        if s == State.ERROR and self._error_idle_timeout > 0:
            self._error_recovery_timer = threading.Timer(
                self._error_idle_timeout, self._recover_from_error,
            )
            self._error_recovery_timer.daemon = True
            self._error_recovery_timer.start()
        try:
            self._on_state(s)
        except Exception:
            pass

    def _recover_from_error(self) -> None:
        """Fired by the error-idle timer. Flip ERROR → IDLE if nothing
        else has moved the state in the meantime."""
        with self._lock:
            if self._state != State.ERROR:
                return
            self._set_state(State.IDLE)

    def _commit_final(self, text: str) -> bool:
        """Deliver ``on_final`` at most once per session (no dual commit).

        Returns True if this call was the owner that committed. Cancel
        path and empty text never commit. Cancel is re-checked under
        the same lock that sets ``_final_committed`` so a concurrent
        ``cancel()`` cannot land between the flag check and commit.
        """
        if not text:
            return False
        with self._lock:
            if self._cancel.is_set() or self._final_committed:
                return False
            self._final_committed = True
        try:
            self._on_final(text)
        except Exception:
            pass
        return True

    def _audio_iter(self) -> Iterator[bytes]:
        """PCM iterator that yields until capture stops or cancel fires."""
        for chunk in self._audio.chunks():
            if self._cancel.is_set():
                return
            yield chunk

    def _run_session(self) -> None:
        loop = asyncio.new_event_loop()
        errored = False
        try:
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self._session_main(loop))
            except asyncio.CancelledError:
                # Cancel path — clean, not an error.
                pass
            except Exception as exc:
                errored = True
                # Re-raise the original — _on_error wraps in a richer
                # message (type + repr) so callers can tell ECONNRESET
                # apart from a websockets-library bug.
                wrapped = type(exc).__name__ + ": " + (str(exc) or repr(exc))
                self._on_error(RuntimeError(wrapped))
            finally:
                # Drain async-generator finalizers before tearing the
                # loop down — otherwise we'd leak "Task was destroyed
                # but it is pending!" warnings into the test output.
                try:
                    loop.run_until_complete(loop.shutdown_asyncgens())
                except Exception:
                    pass
        finally:
            with self._lock:
                self._session_task = None
                self._loop = None
            try:
                loop.close()
            except Exception:
                pass
            # Belt-and-suspenders: stop the mic regardless of how we
            # exited. A clean exit (server sent definite=true while
            # still RECORDING, never reached release()) would otherwise
            # leak the capture stream until the next press.
            try:
                self._audio.stop()
            except Exception:
                pass
            # Publish the new state AFTER audio.stop. If we set ERROR /
            # IDLE first, a press() observing the new state could call
            # self._audio.start() and open a fresh stream — then our
            # stop() above would tear down the *new* session's mic.
            self._set_state(State.ERROR if errored else State.IDLE)

    async def _session_main(self, loop: asyncio.AbstractEventLoop) -> None:
        """Publish session task under lock ASAP, then await it (KD-15 / C0)."""
        # Test hook: ``_test_pre_publish_barrier = (ready_evt, go_evt)``
        # lets unit tests force cancel while ``_session_task is None``.
        barrier = getattr(self, "_test_pre_publish_barrier", None)
        if barrier is not None:
            ready_evt, go_evt = barrier
            ready_evt.set()
            await loop.run_in_executor(None, go_evt.wait)
        session_task = loop.create_task(self._session_body())
        with self._lock:
            self._loop = loop
            self._session_task = session_task
            already = self._cancel.is_set()
        if already:
            # Cancel was requested before publish — do not start hung work
            session_task.cancel()
        try:
            await session_task
        except asyncio.CancelledError:
            # Cancel path: no on_final (guarded also by _commit_final)
            return

    async def _session_body(self) -> None:
        import logging
        log = logging.getLogger("spitch.voice")
        log.info("session: starting client_factory")
        client = self._client_factory()
        # Convert the sync chunk iterator into an async one without
        # blocking the loop: hand off reads to a thread.
        chunks = self._audio_iter()

        async def _async_chunks():
            loop = asyncio.get_running_loop()
            while True:
                try:
                    chunk = await loop.run_in_executor(None, next, chunks, b"__END__")
                except StopIteration:
                    return
                if chunk == b"__END__" or chunk is None:
                    return
                if self._cancel.is_set():
                    return
                yield chunk

        log.info("session: connecting to ASR endpoint")
        async with client as live:
            if self._cancel.is_set():
                return
            log.info("session: connected, starting stream")
            chunks_gen = _async_chunks()
            stream = live.stream(chunks_gen).__aiter__()

            async def _consume() -> bool:
                """Drain events until cancel or end-of-stream.

                Doubao does NOT keep finalized utterances visible across
                frames. Once a server frame marks an utterance
                ``definite=true``, the *next* frame's ``utterances[]``
                drops it entirely — the array contains only the in-progress
                utterance, and ``result.text`` likewise reflects only
                that. So even ``extract_full_text`` (which concatenates
                the current frame's array) can't see previously
                finalized segments — they're already gone from the wire.

                We have to track them client-side. ``confirmed_finals``
                accumulates every ``definite=true`` utterance we've
                seen, with end-dedup so a finalized utterance staying
                in the array for multiple frames isn't double-appended.
                The tray label / inject text always show:

                    "".join(confirmed_finals) + current_in_progress

                Multi-provider note (KD-5): the non-utterance branch
                (``evt.text``) is the Grok / test / provider-neutral
                path. Clients that do not publish Doubao-shaped
                ``result.utterances`` must put session-normalized text
                on ``evt.text``. Grok never attaches utterances.
                """
                confirmed_finals: list[str] = []
                last_text = ""
                try:
                    while True:
                        try:
                            evt = await stream.__anext__()
                        except StopAsyncIteration:
                            break
                        if self._cancel.is_set():
                            return False
                        # Pull utterances + current text from the raw payload
                        # (TranscriptEvent.raw is the full server dict).
                        payload = evt.raw if isinstance(evt.raw, dict) else {}
                        result = payload.get("result") or {}
                        utterances = result.get("utterances") or []
                        current_in_progress = ""
                        saw_utterances = False
                        if isinstance(utterances, list) and utterances:
                            saw_utterances = True
                            for u in utterances:
                                if not isinstance(u, dict):
                                    continue
                                u_text = u.get("text", "")
                                if not isinstance(u_text, str) or not u_text:
                                    continue
                                if u.get("definite") is True:
                                    # End-dedup: a definite utterance can
                                    # appear in N consecutive frames before
                                    # the server drops it. Only append once.
                                    if not confirmed_finals or confirmed_finals[-1] != u_text:
                                        confirmed_finals.append(u_text)
                                else:
                                    if not current_in_progress:
                                        current_in_progress = u_text
                        if saw_utterances:
                            full = "".join(confirmed_finals) + current_in_progress
                        else:
                            # No utterances[] — multi-provider / test path:
                            # trust evt.text as session-normalized text
                            # (Grok accumulator full text; FakeStreamingClient).
                            full = evt.text or ""
                        if full:
                            last_text = full
                            self._latest_text = full
                            self._on_partial(full)
                    # Stream ended normally — commit the accumulated text.
                    if last_text and not self._cancel.is_set():
                        self._commit_final(last_text)
                        return True
                    return False
                except asyncio.CancelledError:
                    # Cancel path: no on_final
                    raise
                except Exception:
                    # Single-owner: _commit_final is commit-once so outer
                    # except cannot dual-fire on_final (rev 7).
                    if not self._cancel.is_set() and (last_text or self._latest_text):
                        self._commit_final(last_text or self._latest_text)
                    raise

            consume_task: asyncio.Task | None = None
            try:
                # Race the stream against the finalize-wall: if the user
                # has released the talk key (state FINALIZING) and the
                # server still hasn't sent a session final after
                # finalize_timeout seconds, we commit the latest partial
                # rather than block the daemon indefinitely.
                consume_task = asyncio.create_task(_consume())
                while not consume_task.done():
                    if self._cancel.is_set():
                        consume_task.cancel()
                        try:
                            await consume_task
                        except (asyncio.CancelledError, Exception):
                            pass
                        return
                    if self._state == State.FINALIZING:
                        try:
                            committed = await asyncio.wait_for(
                                asyncio.shield(consume_task),
                                timeout=self._finalize_timeout,
                            )
                            if not committed and not self._cancel.is_set() and self._latest_text:
                                self._commit_final(self._latest_text)
                            return
                        except asyncio.TimeoutError:
                            consume_task.cancel()
                            try:
                                await consume_task
                            except (asyncio.CancelledError, Exception):
                                pass
                            if not self._cancel.is_set() and self._latest_text:
                                self._commit_final(self._latest_text)
                            return
                    else:
                        # still RECORDING — short tick so we re-check state.
                        try:
                            await asyncio.wait_for(
                                asyncio.shield(consume_task), timeout=0.1
                            )
                        except asyncio.TimeoutError:
                            continue
                committed = consume_task.result() if not consume_task.cancelled() else False
                if not committed and not self._cancel.is_set() and self._latest_text:
                    self._commit_final(self._latest_text)
            except asyncio.CancelledError:
                # Session-task cancel (user cancel / C0) — no on_final.
                raise
            except Exception:
                if not self._cancel.is_set() and self._latest_text:
                    self._commit_final(self._latest_text)
                raise
            finally:
                # Drive the async generators through their cleanup path
                # before the loop tears down — otherwise their pending
                # athrow tasks leak as "Task was destroyed but it is
                # pending!" warnings, masking real future leaks.
                if consume_task is not None and not consume_task.done():
                    consume_task.cancel()
                    try:
                        await consume_task
                    except (asyncio.CancelledError, Exception):
                        pass
                for ag in (stream, chunks_gen):
                    aclose = getattr(ag, "aclose", None)
                    if aclose is None:
                        continue
                    try:
                        await aclose()
                    except (asyncio.CancelledError, Exception):
                        pass
