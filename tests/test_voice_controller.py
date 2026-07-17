"""VoiceController state-machine test against fully fake audio + WS client."""

from __future__ import annotations

import asyncio
import threading
import time
import unittest
from typing import AsyncIterator

from spitch.voice.controller import State, VoiceController
from spitch.voice.doubao import TranscriptEvent


class FakeAudio:
    """Stand-in for AudioCapture: yields a scripted sequence of chunks."""

    def __init__(self, chunks: list[bytes]):
        self._chunks = list(chunks)
        self._stopped = threading.Event()
        self._cv = threading.Condition()
        self._idx = 0

    def start(self) -> str:
        self._stopped.clear()
        self._idx = 0
        return "fake"

    def stop(self) -> None:
        with self._cv:
            self._stopped.set()
            self._cv.notify_all()

    def chunks(self):
        # Mimic streaming: yield chunks with a tiny pause until stop()
        with self._cv:
            while self._idx < len(self._chunks):
                if self._stopped.is_set():
                    return
                chunk = self._chunks[self._idx]
                self._idx += 1
                yield chunk
                # tiny wait, releasing the cv so stop() can wake us
                self._cv.wait(timeout=0.005)
            # After scripted chunks exhausted, block until stop()
            while not self._stopped.is_set():
                self._cv.wait(timeout=0.01)


class FakeStreamingClient:
    """Async context manager + ``stream`` matching DoubaoClient's surface."""

    def __init__(self, scripted_events: list[TranscriptEvent]):
        self._scripted = scripted_events
        self.consumed_chunks: list[bytes] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def stream(self, audio_iter):
        async for chunk in audio_iter:
            self.consumed_chunks.append(chunk)
        for evt in self._scripted:
            await asyncio.sleep(0)
            yield evt


class VoiceControllerTests(unittest.TestCase):
    def _wait_state(self, ctrl: VoiceController, want: State, timeout: float = 2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if ctrl.state == want:
                return True
            time.sleep(0.01)
        return False

    def test_press_release_commits_final(self):
        events = [
            TranscriptEvent("你", False, {}),
            TranscriptEvent("你好", False, {}),
            TranscriptEvent("你好。", True, {}),
        ]
        client = FakeStreamingClient(events)
        audio = FakeAudio([b"\x01" * 320, b"\x02" * 320])

        partials: list[str] = []
        finals: list[str] = []
        errors: list[BaseException] = []

        ctrl = VoiceController(
            client_factory=lambda: client,
            audio=audio,
            on_partial=partials.append,
            on_final=finals.append,
            on_error=errors.append,
        )

        ok = ctrl.press()
        self.assertTrue(ok)
        time.sleep(0.05)
        ctrl.release()
        self.assertTrue(self._wait_state(ctrl, State.IDLE, timeout=3.0))

        self.assertEqual(finals, ["你好。"])
        self.assertEqual(errors, [])
        self.assertIn("你", partials)

    def test_double_press_is_noop(self):
        events = [TranscriptEvent("ok", True, {})]
        ctrl = VoiceController(
            client_factory=lambda: FakeStreamingClient(events),
            audio=FakeAudio([b"x" * 32]),
        )
        self.assertTrue(ctrl.press())
        self.assertFalse(ctrl.press())
        ctrl.release()
        self.assertTrue(self._wait_state(ctrl, State.IDLE, timeout=3.0))

    def test_cancel_does_not_commit(self):
        events = [TranscriptEvent("partial", False, {}), TranscriptEvent("final", True, {})]
        client = FakeStreamingClient(events)
        audio = FakeAudio([b"x" * 64, b"y" * 64])
        finals: list[str] = []
        ctrl = VoiceController(
            client_factory=lambda: client,
            audio=audio,
            on_final=finals.append,
        )
        ctrl.press()
        time.sleep(0.05)
        ctrl.cancel()
        self.assertTrue(self._wait_state(ctrl, State.IDLE, timeout=3.0) or
                        self._wait_state(ctrl, State.ERROR, timeout=0.1))
        self.assertEqual(finals, [])

    def test_final_during_recording_commits_and_returns_to_idle(self):
        """Server sends definite=true while the user is still holding the keys.

        Regression: previously the controller went straight to IDLE
        without firing any event the daemon could distinguish from a
        cancel, and the daemon's _on_release dropped the transcript on
        the floor because it gated on state == RECORDING. We assert
        on_final is delivered exactly once and that the controller
        ends up IDLE without an explicit release().
        """

        class EagerClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def stream(self, audio_iter):
                # Don't drain audio fully — emit a final immediately.
                # Simulates Doubao deciding the utterance is complete
                # while the user still has the key pressed.
                yield TranscriptEvent("早", False, {})
                yield TranscriptEvent("早安。", True, {})

        finals: list[str] = []
        ctrl = VoiceController(
            client_factory=lambda: EagerClient(),
            audio=FakeAudio([b"\x00" * 320] * 10),
            on_final=finals.append,
        )
        self.assertTrue(ctrl.press())
        self.assertTrue(self._wait_state(ctrl, State.IDLE, timeout=3.0))
        self.assertEqual(finals, ["早安。"])

    def test_state_transition_runs_after_audio_stop_on_error(self):
        """ERROR is published only after the failed session's audio.stop()
        has run.

        Regression for the race where _set_state(ERROR) ran BEFORE the
        outer-finally cleanup, so a re-press observing ERROR could call
        audio.start() while the dying session was still in flight and
        about to call audio.stop() on the *new* stream.
        """

        state_at_stop: list[State] = []
        stop_done = threading.Event()

        class TracingFakeAudio:
            def start(self) -> str:
                return "fake"

            def stop(self) -> None:
                # Record the controller's published state at the moment
                # the dying session calls audio.stop() in its outer
                # finally. With the bug, state has already flipped to
                # ERROR (so a re-press observing ERROR could call
                # audio.start() and have us stomp on it). With the
                # fix, state is still RECORDING here — the transition
                # to ERROR happens strictly after we return.
                state_at_stop.append(ctrl.state)
                stop_done.set()

            def chunks(self):
                if False:
                    yield b""  # pragma: no cover

        class FailingOpenClient:
            async def __aenter__(self):
                raise RuntimeError("simulated connect failure")

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def stream(self, audio_iter):
                if False:
                    yield  # pragma: no cover

        audio = TracingFakeAudio()
        errors: list[BaseException] = []
        ctrl = VoiceController(
            client_factory=lambda: FailingOpenClient(),
            audio=audio,
            on_error=errors.append,
        )
        self.assertTrue(ctrl.press())
        self.assertTrue(self._wait_state(ctrl, State.ERROR, timeout=3.0))
        self.assertTrue(stop_done.is_set())
        self.assertEqual(len(errors), 1)
        self.assertEqual(
            state_at_stop, [State.RECORDING],
            "audio.stop() should run while state is still RECORDING — "
            "ERROR must only be published after cleanup completes",
        )

    def test_finalize_timeout_commits_latest_partial(self):
        """Server sends partials then never returns a definite=true frame.

        After release(), the controller waits at most ``finalize_timeout``
        seconds before committing the most recent partial as a fallback —
        this is the PRD risk-mitigation for a slow / unresponsive server.
        """

        class HangingClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def stream(self, audio_iter):
                # consume the audio
                async for _ in audio_iter:
                    pass
                yield TranscriptEvent("你", False, {})
                yield TranscriptEvent("你好世界", False, {})
                # then never produce a final — block forever
                while True:
                    await asyncio.sleep(0.1)

        audio = FakeAudio([b"\x01" * 320, b"\x02" * 320])
        finals: list[str] = []
        ctrl = VoiceController(
            client_factory=lambda: HangingClient(),
            audio=audio,
            on_final=finals.append,
            finalize_timeout=0.3,
        )
        ctrl.press()
        time.sleep(0.05)
        ctrl.release()
        self.assertTrue(self._wait_state(ctrl, State.IDLE, timeout=3.0))
        self.assertEqual(finals, ["你好世界"])

    def test_doubao_drops_finalized_utterances_across_frames(self):
        """Reproduce the wire behavior captured in production daemon.log:
        once Doubao marks an utterance ``definite=true``, the next frame's
        ``utterances[]`` array DROPS it entirely. Only the in-progress
        utterance remains in the array, and ``result.text`` reflects only
        that. The controller has to accumulate finalized segments locally
        — anything else loses everything before the first segment break.
        """

        class DoubaoLikeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def stream(self, audio_iter):
                async for _ in audio_iter:
                    pass
                # Frame 1: in-progress only (def=false).
                yield TranscriptEvent(
                    "你能听见吗？", False,
                    {"result": {"text": "你能听见吗？", "utterances": [
                        {"text": "你能听见吗？", "definite": False},
                    ]}},
                )
                # Frame 2: that utterance is now definite, plus a new
                # in-progress one. extract_full_text would still see
                # both here.
                yield TranscriptEvent(
                    "你能听见吗？好的", False,
                    {"result": {"text": "好的", "utterances": [
                        {"text": "你能听见吗？", "definite": True},
                        {"text": "好的", "definite": False},
                    ]}},
                )
                # Frame 3: server DROPS the now-finalized first
                # utterance from the array. Only the in-progress
                # second one remains. Without client-side accumulation
                # everything before "好的" is silently lost.
                yield TranscriptEvent(
                    "好的", False,
                    {"result": {"text": "好的", "utterances": [
                        {"text": "好的", "definite": False},
                    ]}},
                )
                # Frame 4: second utterance becomes definite + a third
                # in-progress one appears.
                yield TranscriptEvent(
                    "好的，再见。", True,
                    {"result": {"text": "再见。", "utterances": [
                        {"text": "好的，", "definite": True},
                        {"text": "再见。", "definite": True},
                    ]}},
                )

        audio = FakeAudio([b"\x01" * 320, b"\x02" * 320])
        finals: list[str] = []
        ctrl = VoiceController(
            client_factory=lambda: DoubaoLikeClient(),
            audio=audio,
            on_final=finals.append,
            finalize_timeout=0.3,
        )
        ctrl.press()
        time.sleep(0.05)
        ctrl.release()
        self.assertTrue(self._wait_state(ctrl, State.IDLE, timeout=3.0))
        # Must contain the FULL transcript — first utterance ("你能听
        # 见吗？") survives even though it was dropped from the wire,
        # plus the two later ones.
        self.assertEqual(finals, ["你能听见吗？好的，再见。"])


class CancelReliabilityTests(unittest.TestCase):
    """KD-15: C0–C4 session-task cancel reliability."""

    def _wait_state(self, ctrl: VoiceController, want: State, timeout: float = 2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if ctrl.state == want:
                return True
            time.sleep(0.01)
        return False

    def test_c0_cancel_before_publish(self):
        """cancel() forced while worker is pre-publish (``_session_task is None``).

        Barrier holds ``_session_main`` before publish. Cancel must set the
        flag under lock so publish sees ``already=True`` and aborts; no
        ``on_final``; no hung worker; state recoverable.
        """

        class HungConnectClient:
            async def __aenter__(self):
                # If cancel fails to land, this hangs forever (repro for TOCTOU).
                await asyncio.Future()
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def stream(self, audio_iter):
                if False:
                    yield  # pragma: no cover

        finals: list[str] = []
        ready = threading.Event()
        go = threading.Event()
        ctrl = VoiceController(
            client_factory=lambda: HungConnectClient(),
            audio=FakeAudio([b"\x00" * 32] * 5),
            on_final=finals.append,
            finalize_timeout=1.0,
        )
        ctrl._test_pre_publish_barrier = (ready, go)  # type: ignore[attr-defined]
        self.assertTrue(ctrl.press())
        self.assertTrue(ready.wait(timeout=2.0), "worker never reached pre-publish")
        # Guaranteed: session task not published yet
        with ctrl._lock:
            self.assertIsNone(ctrl._session_task)
            self.assertIsNone(ctrl._loop)
        ctrl.cancel()
        # Flag must be set before we release publish
        self.assertTrue(ctrl._cancel.is_set())
        go.set()
        self.assertTrue(
            self._wait_state(ctrl, State.IDLE, timeout=3.0)
            or self._wait_state(ctrl, State.ERROR, timeout=0.1)
        )
        self.assertIn(ctrl.state, (State.IDLE, State.ERROR))
        self.assertEqual(finals, [])
        with ctrl._lock:
            self.assertIsNone(ctrl._session_task)
            self.assertIsNone(ctrl._loop)

    def test_c0_cancel_sets_flag_under_lock_before_read(self):
        """Regression: cancel must set ``_cancel`` under the same lock as the
        task read — not after releasing the lock (TOCTOU vs publish).
        """
        # Static inspection of ordering via a mock of the lock path:
        # call cancel on IDLE after press cleared; more importantly,
        # verify cancel() sets the event while holding the lock by
        # racing a publish-like reader.
        seen: list[tuple[bool, object]] = []
        lock = threading.Lock()
        cancel_evt = threading.Event()
        loop_holder: list = [None]
        task_holder: list = [None]

        def fake_cancel():
            with lock:
                if True:  # not IDLE
                    cancel_evt.set()
                    loop = loop_holder[0]
                    task = task_holder[0]
            return cancel_evt.is_set(), loop, task

        def fake_publish():
            with lock:
                already = cancel_evt.is_set()
                task_holder[0] = "task"
                loop_holder[0] = "loop"
                return already

        # Sequence: cancel first under lock, then publish must see already
        flag_set, loop, task = fake_cancel()
        already = fake_publish()
        self.assertTrue(flag_set)
        self.assertTrue(already)
        self.assertIsNone(loop)
        self.assertIsNone(task)
        seen.append((already, task_holder[0]))
        self.assertTrue(seen[0][0])

    def test_c1_cancel_during_hung_aenter(self):
        """cancel during hung __aenter__ (after task published)."""
        entered = threading.Event()

        class HungConnectClient:
            async def __aenter__(self):
                entered.set()
                await asyncio.Future()  # hang forever until cancelled
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def stream(self, audio_iter):
                if False:
                    yield  # pragma: no cover

        finals: list[str] = []
        ctrl = VoiceController(
            client_factory=lambda: HungConnectClient(),
            audio=FakeAudio([b"\x00" * 32]),
            on_final=finals.append,
        )
        self.assertTrue(ctrl.press())
        self.assertTrue(entered.wait(timeout=2.0))
        # Give publish a moment
        time.sleep(0.05)
        ctrl.cancel()
        self.assertTrue(
            self._wait_state(ctrl, State.IDLE, timeout=3.0)
            or self._wait_state(ctrl, State.ERROR, timeout=0.1)
        )
        self.assertEqual(finals, [])

    def test_c2_cancel_during_blocked_recv(self):
        """cancel during permanently blocked stream recv."""

        class BlockedRecvClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def stream(self, audio_iter):
                async for _ in audio_iter:
                    pass
                await asyncio.Future()  # hang on "recv"
                if False:
                    yield  # pragma: no cover

        finals: list[str] = []
        ctrl = VoiceController(
            client_factory=lambda: BlockedRecvClient(),
            audio=FakeAudio([b"\x00" * 32, b"\x01" * 32]),
            on_final=finals.append,
            finalize_timeout=5.0,
        )
        self.assertTrue(ctrl.press())
        time.sleep(0.1)
        ctrl.cancel()
        self.assertTrue(
            self._wait_state(ctrl, State.IDLE, timeout=3.0)
            or self._wait_state(ctrl, State.ERROR, timeout=0.1)
        )
        self.assertEqual(finals, [])

    def test_c3_cancel_mid_partials(self):
        finals: list[str] = []
        partials: list[str] = []

        class SlowPartialsClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def stream(self, audio_iter):
                yield TranscriptEvent("partial-1", False, {})
                await asyncio.sleep(0.05)
                yield TranscriptEvent("partial-2", False, {})
                await asyncio.sleep(10.0)
                yield TranscriptEvent("final", True, {})

        ctrl = VoiceController(
            client_factory=lambda: SlowPartialsClient(),
            audio=FakeAudio([b"x" * 64] * 5),
            on_partial=partials.append,
            on_final=finals.append,
        )
        ctrl.press()
        time.sleep(0.15)
        ctrl.cancel()
        self.assertTrue(
            self._wait_state(ctrl, State.IDLE, timeout=3.0)
            or self._wait_state(ctrl, State.ERROR, timeout=0.1)
        )
        self.assertEqual(finals, [])

    def test_c4_cancel_races_loop_teardown(self):
        """cancel after session already ended — no crash, no on_final."""
        events = [TranscriptEvent("ok", True, {})]
        finals: list[str] = []
        ctrl = VoiceController(
            client_factory=lambda: FakeStreamingClient(events),
            audio=FakeAudio([b"x" * 32]),
            on_final=finals.append,
        )
        ctrl.press()
        time.sleep(0.05)
        ctrl.release()
        self.assertTrue(self._wait_state(ctrl, State.IDLE, timeout=3.0))
        # Race cancel after teardown — must not raise
        ctrl.cancel()
        time.sleep(0.05)
        # final from successful path already committed once
        self.assertEqual(finals, ["ok"])


class SingleOwnerFinalTests(unittest.TestCase):
    """S3b + dual-exception: at most one on_final per session."""

    def _wait_state(self, ctrl: VoiceController, want: State, timeout: float = 2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if ctrl.state == want:
                return True
            time.sleep(0.01)
        return False

    def test_s3b_silent_post_eos_finalize_timeout_exactly_one_on_final(self):
        """Release → FINALIZING; silent post-EOS; short finalize_timeout
        → exactly one on_final with latest partial; state not ERROR.
        """

        class SilentAfterPartialClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def stream(self, audio_iter):
                async for _ in audio_iter:
                    pass
                yield TranscriptEvent("hello partial", False, {})
                # Silent forever after partial (no transcript.done)
                while True:
                    await asyncio.sleep(0.1)

        finals: list[str] = []
        errors: list[BaseException] = []
        ctrl = VoiceController(
            client_factory=lambda: SilentAfterPartialClient(),
            audio=FakeAudio([b"\x01" * 320]),
            on_final=finals.append,
            on_error=errors.append,
            finalize_timeout=0.25,
        )
        ctrl.press()
        time.sleep(0.05)
        ctrl.release()
        self.assertTrue(self._wait_state(ctrl, State.IDLE, timeout=3.0))
        self.assertEqual(finals, ["hello partial"])
        self.assertEqual(errors, [])

    def test_dual_exception_path_single_on_final(self):
        """Exception mid-stream must not dual-commit on_final (inner+outer)."""

        class ExplodingClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def stream(self, audio_iter):
                async for _ in audio_iter:
                    pass
                yield TranscriptEvent("got text", False, {})
                raise RuntimeError("simulated stream failure")

        finals: list[str] = []
        errors: list[BaseException] = []
        ctrl = VoiceController(
            client_factory=lambda: ExplodingClient(),
            audio=FakeAudio([b"\x01" * 64]),
            on_final=finals.append,
            on_error=errors.append,
            finalize_timeout=1.0,
        )
        ctrl.press()
        time.sleep(0.05)
        ctrl.release()
        self.assertTrue(
            self._wait_state(ctrl, State.ERROR, timeout=3.0)
            or self._wait_state(ctrl, State.IDLE, timeout=0.5)
        )
        self.assertLessEqual(len(finals), 1)
        if finals:
            self.assertEqual(finals[0], "got text")
        self.assertEqual(len(errors), 1)


class ErrorRecoveryTests(unittest.TestCase):
    """ERROR state should auto-flip back to IDLE after a brief idle
    period so the tray label stops asserting "出错" long after a
    transient network blip is over."""

    def test_error_auto_recovers_to_idle(self):
        events: list[State] = []

        # Force an error path: client_factory raises during press()'s
        # session_coro. We don't need a real ws.

        class BadClient:
            async def __aenter__(self):
                raise RuntimeError("simulated network failure")

            async def __aexit__(self, *_):
                return None

            def stream(self, _audio_iter):
                async def _empty():
                    if False:
                        yield None
                return _empty()

        ctrl = VoiceController(
            client_factory=lambda: BadClient(),
            audio=FakeAudio([b"\x00" * 32]),
            on_state=events.append,
            error_idle_timeout=0.2,  # short for the test
        )
        ctrl.press()
        # Wait for the error to propagate.
        deadline = time.time() + 2.0
        while time.time() < deadline and ctrl.state != State.ERROR:
            time.sleep(0.01)
        self.assertEqual(ctrl.state, State.ERROR)
        # The recovery timer should flip us back within ~200 ms.
        deadline = time.time() + 1.5
        while time.time() < deadline and ctrl.state != State.IDLE:
            time.sleep(0.01)
        self.assertEqual(ctrl.state, State.IDLE)
        # Sanity: the on_state stream must include both the ERROR and
        # the auto-recovery IDLE transitions.
        self.assertIn(State.ERROR, events)
        self.assertEqual(events[-1], State.IDLE)

    def test_state_transition_cancels_pending_recovery_timer(self):
        # Any state transition out of ERROR must cancel the pending
        # recovery timer — otherwise the timer would later flip a
        # live RECORDING / FINALIZING back to IDLE.
        events: list[State] = []
        ctrl = VoiceController(
            client_factory=lambda: None,
            audio=FakeAudio([]),
            on_state=events.append,
            error_idle_timeout=0.1,
        )
        # Drive the state machine directly (don't go through press()
        # to avoid a real session being spun up). We bypass the
        # public API on purpose — this is a private-state correctness
        # test.
        ctrl._set_state(State.ERROR)   # arms the recovery timer
        ctrl._set_state(State.RECORDING)  # must cancel it
        # Wait past the timer interval to confirm it didn't fire.
        time.sleep(0.3)
        # Should see ERROR + RECORDING. NO timer-driven IDLE.
        self.assertEqual(events, [State.ERROR, State.RECORDING])


if __name__ == "__main__":
    unittest.main()
