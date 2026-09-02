"""Tests for :mod:`spitch.sounds` — the auditory cues.

No audio hardware: backends are injected callables. What we pin down
is the config surface, the clip rendering (no clicks, volume scaling,
custom WAV fallback), the fire-and-forget worker, and the backend
fallback chain.
"""

from __future__ import annotations

import struct
import tempfile
import threading
import time
import unittest
import wave
from array import array
from pathlib import Path

from spitch.config import default_config
from spitch.sounds import (
    CUE_NAMES,
    SAMPLE_RATE,
    BackendChain,
    CliBackend,
    Clip,
    SoundCues,
    builtin_clip,
    load_wav_clip,
)


class _RecordingBackend:
    """Backend stand-in that records (name, clip) and signals each play."""

    def __init__(self, fail_names: set[str] | None = None):
        self.played: list[tuple[str, Clip]] = []
        self.fail_names = set(fail_names or ())
        self.event = threading.Event()

    def __call__(self, clip: Clip, name: str) -> None:
        if name in self.fail_names:
            raise RuntimeError(f"boom {name}")
        self.played.append((name, clip))
        self.event.set()

    def wait(self, n: int, timeout: float = 1.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if len(self.played) >= n:
                return True
            time.sleep(0.01)
        return len(self.played) >= n


def _samples(clip: Clip) -> array:
    out = array("h")
    out.frombytes(clip.pcm)
    return out


def _write_wav(path: Path, *, width: int = 2, channels: int = 1, rate: int = 22050,
               frames: int = 2205, amplitude: int = 16384) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(width)
        wf.setframerate(rate)
        if width == 2:
            frame = struct.pack("<h", amplitude) * channels
        else:
            frame = bytes([128]) * channels
        wf.writeframes(frame * frames)


class BuiltinClipTests(unittest.TestCase):
    def test_every_cue_renders_mono_48k(self):
        for name in CUE_NAMES:
            clip = builtin_clip(name, 1.0)
            self.assertEqual(clip.rate, SAMPLE_RATE)
            self.assertEqual(clip.channels, 1)
            self.assertGreater(clip.frames, 0)
            # A cue is a tick, not a notification: well under 200 ms.
            self.assertLess(clip.duration_ms, 200.0, name)

    def test_start_cue_is_about_90ms(self):
        self.assertAlmostEqual(builtin_clip("start", 1.0).duration_ms, 90.0, delta=1.0)

    def test_clips_start_and_end_near_zero(self):
        """No DC step at either edge — that is what a click is."""
        for name in CUE_NAMES:
            s = _samples(builtin_clip(name, 1.0))
            self.assertEqual(s[0], 0, name)
            self.assertLess(abs(s[-1]), 50, name)
            self.assertLess(max(abs(v) for v in s[-8:]), 400, name)

    def test_volume_scales_linearly_and_clamps(self):
        full = max(abs(v) for v in _samples(builtin_clip("start", 1.0)))
        half = max(abs(v) for v in _samples(builtin_clip("start", 0.5)))
        self.assertGreater(full, 25000)  # authored at (near) full scale
        self.assertAlmostEqual(half / full, 0.5, delta=0.01)
        self.assertEqual(max(abs(v) for v in _samples(builtin_clip("start", 0.0))), 0)

    def test_unknown_cue_raises(self):
        with self.assertRaises(KeyError):
            builtin_clip("nope", 1.0)


class CustomWavTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_loads_16bit_wav_with_gain(self):
        p = self.dir / "cue.wav"
        _write_wav(p, rate=22050, frames=2205, amplitude=16384)
        clip = load_wav_clip(p, 0.5)
        self.assertEqual(clip.rate, 22050)
        self.assertEqual(clip.channels, 1)
        self.assertEqual(clip.frames, 2205)
        self.assertEqual(_samples(clip)[10], 8192)

    def test_stereo_is_kept(self):
        p = self.dir / "st.wav"
        _write_wav(p, channels=2, frames=100)
        clip = load_wav_clip(p, 1.0)
        self.assertEqual(clip.channels, 2)
        self.assertEqual(clip.frames, 100)

    def test_8bit_rejected(self):
        p = self.dir / "u8.wav"
        _write_wav(p, width=1)
        with self.assertRaises(ValueError):
            load_wav_clip(p, 1.0)

    def test_too_long_rejected(self):
        p = self.dir / "long.wav"
        _write_wav(p, rate=8000, frames=8000 * 11)
        with self.assertRaises(ValueError):
            load_wav_clip(p, 1.0)

    def test_bad_file_falls_back_to_builtin_with_warning(self):
        p = self.dir / "u8.wav"
        _write_wav(p, width=1)
        backend = _RecordingBackend()
        with self.assertLogs("spitch.sounds", level="WARNING") as logs:
            cues = SoundCues(files={"start": str(p)}, backend=backend)
        self.assertTrue(any("built-in" in line for line in logs.output))
        self.assertIn("start", cues.cue_names)
        cues.play("start")
        self.assertTrue(backend.wait(1))
        self.assertEqual(backend.played[0][1].rate, SAMPLE_RATE)
        cues.close()

    def test_missing_file_falls_back_to_builtin(self):
        with self.assertLogs("spitch.sounds", level="WARNING"):
            cues = SoundCues(
                files={"stop": str(self.dir / "absent.wav")},
                backend=_RecordingBackend(),
            )
        self.assertIn("stop", cues.cue_names)
        cues.close()

    def test_good_file_is_used(self):
        p = self.dir / "mine.wav"
        _write_wav(p, rate=22050, frames=500)
        backend = _RecordingBackend()
        cues = SoundCues(files={"done": str(p)}, backend=backend)
        cues.play("done")
        self.assertTrue(backend.wait(1))
        self.assertEqual(backend.played[0][1].rate, 22050)
        cues.close()


class ConfigSurfaceTests(unittest.TestCase):
    def test_defaults_present_in_default_config(self):
        s = default_config()["sounds"]
        self.assertTrue(s["enabled"])
        self.assertEqual(s["volume"], 0.3)
        for name in CUE_NAMES:
            self.assertTrue(s[name])
            self.assertEqual(s[f"{name}_file"], "")

    def test_from_default_config_enables_all_three(self):
        cues = SoundCues.from_config(default_config(), backend=_RecordingBackend())
        self.assertTrue(cues.enabled)
        self.assertEqual(cues.cue_names, list(CUE_NAMES))
        self.assertAlmostEqual(cues.volume, 0.3)
        cues.close()

    def test_missing_section_means_defaults(self):
        cues = SoundCues.from_config({}, backend=_RecordingBackend())
        self.assertTrue(cues.enabled)
        self.assertEqual(cues.cue_names, list(CUE_NAMES))
        cues.close()

    def test_non_mapping_section_means_defaults(self):
        cues = SoundCues.from_config({"sounds": "yes please"}, backend=_RecordingBackend())
        self.assertTrue(cues.enabled)
        cues.close()

    def test_disabled(self):
        cues = SoundCues.from_config({"sounds": {"enabled": False}}, backend=_RecordingBackend())
        self.assertFalse(cues.enabled)
        self.assertEqual(cues.cue_names, [])
        self.assertFalse(cues.play("start"))
        cues.close()

    def test_zero_volume_disables(self):
        cues = SoundCues.from_config({"sounds": {"volume": 0}}, backend=_RecordingBackend())
        self.assertFalse(cues.enabled)
        cues.close()

    def test_volume_clamped_and_nan_defaulted(self):
        cues = SoundCues.from_config({"sounds": {"volume": 7}}, backend=_RecordingBackend())
        self.assertEqual(cues.volume, 1.0)
        cues.close()
        cues = SoundCues.from_config({"sounds": {"volume": "nan"}}, backend=_RecordingBackend())
        self.assertAlmostEqual(cues.volume, 0.3)
        cues.close()
        cues = SoundCues.from_config({"sounds": {"volume": -1}}, backend=_RecordingBackend())
        self.assertFalse(cues.enabled)
        cues.close()

    def test_per_cue_toggle(self):
        cues = SoundCues.from_config(
            {"sounds": {"stop": False, "done": 0}}, backend=_RecordingBackend(),
        )
        self.assertEqual(cues.cue_names, ["start"])
        self.assertFalse(cues.play("stop"))
        self.assertTrue(cues.play("start"))
        cues.close()

    def test_describe_mentions_state(self):
        cues = SoundCues.from_config({"sounds": {"enabled": False}}, backend=_RecordingBackend())
        self.assertEqual(cues.describe(), "sound cues: off")
        cues.close()
        cues = SoundCues.from_config({}, backend=_RecordingBackend())
        self.assertIn("start/stop/done", cues.describe())
        self.assertIn("volume=0.30", cues.describe())
        cues.close()


class PlayerTests(unittest.TestCase):
    def test_play_is_delivered_off_thread(self):
        backend = _RecordingBackend()
        caller = threading.get_ident()
        seen: list[int] = []

        def spy(clip: Clip, name: str) -> None:
            seen.append(threading.get_ident())
            backend(clip, name)

        cues = SoundCues(backend=spy)
        self.assertTrue(cues.play("start"))
        self.assertTrue(backend.wait(1))
        self.assertEqual(backend.played[0][0], "start")
        self.assertNotEqual(seen[0], caller)
        cues.close()

    def test_unknown_name_is_refused(self):
        cues = SoundCues(backend=_RecordingBackend())
        self.assertFalse(cues.play("nope"))
        cues.close()

    def test_play_after_close_is_refused(self):
        backend = _RecordingBackend()
        cues = SoundCues(backend=backend)
        cues.close()
        self.assertFalse(cues.enabled)
        self.assertFalse(cues.play("start"))
        self.assertEqual(backend.played, [])

    def test_close_is_idempotent_without_worker(self):
        cues = SoundCues(backend=_RecordingBackend())
        cues.close()
        cues.close()

    def test_backend_failure_does_not_kill_worker(self):
        backend = _RecordingBackend(fail_names={"start"})
        cues = SoundCues(backend=backend)
        with self.assertLogs("spitch.sounds", level="WARNING"):
            cues.play("start")
            cues.play("stop")
            self.assertTrue(backend.wait(1))
        self.assertEqual([n for n, _ in backend.played], ["stop"])
        cues.close()

    def test_sequence_preserved(self):
        backend = _RecordingBackend()
        cues = SoundCues(backend=backend)
        for name in ("start", "stop", "done"):
            cues.play(name)
        self.assertTrue(backend.wait(3))
        self.assertEqual([n for n, _ in backend.played], ["start", "stop", "done"])
        cues.close()


class BackendChainTests(unittest.TestCase):
    def test_first_failure_falls_through_and_is_dropped(self):
        calls: list[str] = []

        class Bad:
            name = "bad"

            def __call__(self, clip, name):
                calls.append("bad")
                raise RuntimeError("nope")

        class Good:
            name = "good"

            def __call__(self, clip, name):
                calls.append("good")

        chain = BackendChain([Bad(), Good()])
        clip = builtin_clip("start", 0.1)
        with self.assertLogs("spitch.sounds", level="WARNING"):
            chain(clip, "start")
        chain(clip, "start")
        self.assertEqual(calls, ["bad", "good", "good"])
        self.assertEqual(chain.names, ["good"])

    def test_import_error_is_quiet(self):
        class Missing:
            name = "sounddevice"

            def __call__(self, clip, name):
                raise ImportError("No module named sounddevice")

        played = []

        class Good:
            name = "good"

            def __call__(self, clip, name):
                played.append(name)

        chain = BackendChain([Missing(), Good()])
        chain(builtin_clip("stop", 0.1), "stop")
        self.assertEqual(played, ["stop"])

    def test_exhausted_chain_warns_once_and_never_raises(self):
        chain = BackendChain([])
        clip = builtin_clip("done", 0.1)
        with self.assertLogs("spitch.sounds", level="WARNING") as logs:
            chain(clip, "done")
            chain(clip, "done")
        self.assertEqual(len(logs.output), 1)


class CliBackendTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_writes_wav_once_and_invokes_player(self):
        argv_seen: list[list[str]] = []

        def runner(argv):
            argv_seen.append(list(argv))
            return 0, ""

        backend = CliBackend(["paplay"], cache_dir=self.dir, runner=runner)
        clip = builtin_clip("start", 0.2)
        backend(clip, "start")
        backend(clip, "start")
        self.assertEqual(len(argv_seen), 2)
        self.assertEqual(argv_seen[0][0], "paplay")
        path = Path(argv_seen[0][1])
        self.assertEqual(path, self.dir / "start.wav")
        with wave.open(str(path), "rb") as wf:
            self.assertEqual(wf.getframerate(), SAMPLE_RATE)
            self.assertEqual(wf.getnchannels(), 1)
            self.assertEqual(wf.getsampwidth(), 2)
            self.assertEqual(wf.getnframes(), clip.frames)
        self.assertFalse((self.dir / "start.wav.tmp").exists())

    def test_volume_change_rewrites_file(self):
        def runner(argv):
            return 0, ""

        backend = CliBackend(["paplay"], cache_dir=self.dir, runner=runner)
        backend(builtin_clip("stop", 0.2), "stop")
        first = (self.dir / "stop.wav").read_bytes()
        backend(builtin_clip("stop", 0.4), "stop")
        second = (self.dir / "stop.wav").read_bytes()
        self.assertNotEqual(first, second)

    def test_nonzero_exit_raises_with_stderr(self):
        def runner(argv):
            return 1, "Connection refused"

        backend = CliBackend(["paplay"], cache_dir=self.dir, runner=runner)
        with self.assertRaises(RuntimeError) as ctx:
            backend(builtin_clip("done", 0.2), "done")
        self.assertIn("Connection refused", str(ctx.exception))
        self.assertIn("paplay", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
