"""Daemon wiring for the auditory cues (docs/sound-cues.md).

The contract under test:

* ``start`` is played by the capture layer's first live chunk — never
  by the hotkey press itself. A rejected press stays silent.
* ``stop`` is played at key-up / cancel of an accepted press.
* ``done`` is played only after a paste that actually landed.

Hotkey listener and voice controller are stubs, as in
test_daemon_release_routing. The wiring test at the end builds the
real AudioCapture through ``_construct_voice`` (backend stubbed) so a
regression that drops ``on_session_live`` from the constructor call
is caught.
"""

from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock, patch

from spitch.config import default_config, mark_verified
from spitch.daemon import SpitchDaemon
from spitch.sounds import SoundCues
from spitch.voice import State
from spitch.voice.audio import AudioCapture


class _FakeVoice:
    def __init__(self, accept_press: bool = True):
        self.accept_press = accept_press
        self.state = State.IDLE
        self.release_calls = 0
        self.cancel_calls = 0

    def press(self) -> bool:
        if not self.accept_press:
            return False
        self.state = State.RECORDING
        return True

    def release(self) -> None:
        self.release_calls += 1
        if self.state == State.RECORDING:
            self.state = State.FINALIZING

    def cancel(self) -> None:
        self.cancel_calls += 1
        self.state = State.IDLE


class _FakeListener:
    def is_quiescent(self) -> bool:
        return True

    def stop(self) -> None:
        pass

    def wait_quiescent(self, timeout=None) -> bool:
        return True


class _FakeSounds:
    def __init__(self):
        self.played: list[str] = []
        self.closed = False

    def play(self, name: str) -> bool:
        self.played.append(name)
        return True

    def close(self) -> None:
        self.closed = True

    def describe(self) -> str:
        return "fake"


def _wait_until(predicate, timeout: float = 1.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _cfg() -> dict:
    cfg = default_config()
    cfg["doubao"]["app_key"] = "ak"
    cfg["doubao"]["access_key"] = "sk"
    cfg["audio"]["release_linger_ms"] = 0
    cfg["inject"]["final_wait_seconds"] = 0.5
    cfg["inject"]["restore_clipboard_delay_ms"] = 0
    return mark_verified(cfg)


def _build(accept_press: bool = True) -> tuple[SpitchDaemon, _FakeSounds]:
    d = SpitchDaemon(_cfg())
    d._sounds.close()
    sounds = _FakeSounds()
    d._sounds = sounds
    d._voice = _FakeVoice(accept_press=accept_press)
    d._listener = _FakeListener()
    return d, sounds


class CueRoutingTests(unittest.TestCase):
    def test_press_itself_is_silent_until_mic_is_live(self):
        daemon, sounds = _build()
        daemon._on_press()
        self.assertTrue(daemon._press_accepted)
        self.assertEqual(sounds.played, [])  # the invariant
        daemon._on_capture_live()
        self.assertEqual(sounds.played, ["start"])

    def test_full_session_plays_start_stop_done(self):
        daemon, sounds = _build()
        with patch("spitch.daemon.inject_text", return_value=(True, "")):
            daemon._on_press()
            daemon._on_capture_live()
            daemon._on_release()
            self.assertEqual(sounds.played, ["start", "stop"])
            daemon._on_final("你好。")
            self.assertTrue(_wait_until(lambda: sounds.played == ["start", "stop", "done"]))

    def test_failed_inject_has_no_done(self):
        daemon, sounds = _build()
        with patch("spitch.daemon.inject_text", return_value=(False, "no clipboard")), \
                patch("spitch.daemon._notify"):
            daemon._on_press()
            daemon._on_capture_live()
            daemon._on_release()
            daemon._on_final("你好。")
            time.sleep(0.15)
        self.assertEqual(sounds.played, ["start", "stop"])

    def test_empty_transcript_has_no_done(self):
        daemon, sounds = _build()
        with patch("spitch.daemon.inject_text") as inject:
            daemon._on_press()
            daemon._on_capture_live()
            daemon._on_release()
            daemon._on_state(State.IDLE)  # Grok-style: no on_final at all
            time.sleep(0.15)
            inject.assert_not_called()
        self.assertEqual(sounds.played, ["start", "stop"])

    def test_rejected_press_is_fully_silent(self):
        daemon, sounds = _build(accept_press=False)
        daemon._on_press()
        daemon._on_release()
        self.assertEqual(sounds.played, [])

    def test_cancel_of_accepted_press_plays_stop_once(self):
        daemon, sounds = _build()
        daemon._on_press()
        daemon._on_capture_live()
        daemon._on_cancel()
        daemon._on_release()  # user lets go after the cancel — no second stop
        self.assertEqual(sounds.played, ["start", "stop"])

    def test_cancel_without_press_is_silent(self):
        daemon, sounds = _build()
        daemon._on_cancel()
        self.assertEqual(sounds.played, [])

    def test_salmon_session_plays_start_and_stop(self):
        daemon, sounds = _build()
        daemon._on_salmon_press_actual()
        self.assertEqual(daemon._active_source, "salmon")
        self.assertEqual(sounds.played, [])
        daemon._on_capture_live()
        daemon._on_salmon_release()
        self.assertEqual(sounds.played, ["start", "stop"])

    def test_salmon_watchdog_plays_stop(self):
        daemon, sounds = _build()
        daemon._on_salmon_press_actual()
        daemon._cancel_salmon_watchdog()
        daemon._salmon_watchdog_fire()
        self.assertEqual(sounds.played, ["stop"])

    def test_live_latency_is_logged(self):
        daemon, sounds = _build()
        daemon._on_press()
        with self.assertLogs("spitch.daemon", level="INFO") as logs:
            daemon._on_capture_live()
        self.assertTrue(any("mic live" in line for line in logs.output))


class WiringTests(unittest.TestCase):
    def test_construct_voice_routes_first_live_chunk_to_start_cue(self):
        daemon, sounds = _build()
        audio, voice, _ = daemon._construct_voice(daemon._cfg)
        self.assertIsInstance(audio, AudioCapture)
        # No hardware: stub the backend open and drive the callback
        # the way sounddevice / arecord would.
        with patch.object(AudioCapture, "_open_backend", return_value="test") as opened:
            def _fake_open(self_):
                self_._mic_open = True
                self_._backend = "test"
                return "test"
            opened.side_effect = lambda: _fake_open(audio)
            audio.start()
        self.assertEqual(sounds.played, [])
        audio._on_audio(b"\x00\x00" * 160)
        self.assertEqual(sounds.played, ["start"])
        audio._on_audio(b"\x00\x00" * 160)
        self.assertEqual(sounds.played, ["start"])
        audio.stop()

    def test_shutdown_closes_sounds(self):
        daemon, sounds = _build()
        daemon._shutdown()
        self.assertTrue(sounds.closed)


class ReloadTests(unittest.TestCase):
    @patch("spitch.daemon._notify")
    @patch("spitch.daemon.load_config")
    def test_reload_rebuilds_sounds_and_closes_old(self, load, _notify):
        daemon, old = _build()
        daemon._audio = MagicMock()
        daemon._construct_voice = MagicMock(return_value=(MagicMock(), _FakeVoice(), 31.3))
        daemon._start_hotkeys = MagicMock()
        daemon._stop_hotkeys = MagicMock()
        new_cfg = _cfg()
        new_cfg["sounds"]["enabled"] = False
        load.return_value = new_cfg
        resp = daemon.reload_config()
        self.assertTrue(resp["ok"], resp)
        self.assertTrue(old.closed)
        self.assertIsInstance(daemon._sounds, SoundCues)
        self.assertFalse(daemon._sounds.enabled)
        daemon._sounds.close()


if __name__ == "__main__":
    unittest.main()
