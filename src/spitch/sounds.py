"""Short auditory cues for the talk session: mic live, stopped, inserted.

Dictation is an eyes-off interaction. The user is looking at another
monitor, at the document they are dictating into, or at nothing at
all — the tray label is not where their attention is. A short tick
that means *the microphone is capturing right now* lets them start
talking without checking the screen. Background and product survey:
``docs/beep.md``; design and wiring: ``docs/sound-cues.md``.

The invariant that makes the start cue trustworthy:

    START cue plays only after the capture layer has pushed the first
    live PCM chunk of the new session into the session queue.

It is never played on "hotkey received". The daemon wires
:class:`~spitch.voice.audio.AudioCapture` ``on_session_live`` to
:meth:`SoundCues.play` — so anything the user says after hearing the
tick is in the stream, and hearing nothing means nothing is being
recorded (press rejected during FINALIZING, mic dead, reload in
progress).

Playback is fire-and-forget on one worker thread: callers (the
PortAudio callback, the evdev thread, the inject thread) only enqueue
a name. Backends are tried in order — ``sounddevice`` (in-process, the
same PortAudio the capture side uses), then the ``paplay`` /
``pw-play`` / ``aplay`` CLIs with pre-rendered WAV files in the cache
dir. A failing backend is logged once and dropped; with no backend at
all the cues are silently disabled. Audio output must never break
voice input.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import queue
import shutil
import subprocess
import sys
import threading
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .config import _finite_float, _section

log = logging.getLogger("spitch.sounds")

CUE_NAMES: tuple[str, ...] = ("start", "stop", "done")

# Built-in cues are rendered at this rate. 48 kHz is what PulseAudio /
# PipeWire run their sinks at on stock desktops, so no resampling.
SAMPLE_RATE = 48000

DEFAULT_VOLUME = 0.3

# Custom cue files longer than this are refused — a cue is a tick, not
# a jingle, and the whole clip is held in memory.
_MAX_CUE_SECONDS = 10.0


# ----------------------------------------------------------------------
# clips

@dataclass(frozen=True)
class Clip:
    """PCM ready to hand to a backend: int16 little-endian, gain applied."""

    rate: int
    channels: int
    pcm: bytes

    @property
    def frames(self) -> int:
        return len(self.pcm) // (2 * self.channels)

    @property
    def duration_ms(self) -> float:
        return self.frames * 1000.0 / self.rate


def _decaying_tone(
    freq_hz: float,
    ms: float,
    *,
    gain: float = 1.0,
    attack_ms: float = 3.0,
    tau_ms: float = 30.0,
    rate: int = SAMPLE_RATE,
) -> list[float]:
    """A sine with a fast linear attack and exponential decay.

    The last 4 ms fade linearly to zero so the clip never ends
    mid-cycle — that is what a click at the end of a beep is.
    """
    n = int(rate * ms / 1000.0)
    attack = max(1, int(rate * attack_ms / 1000.0))
    tau = max(1.0, rate * tau_ms / 1000.0)
    tail_fade = max(1, int(rate * 0.004))
    out: list[float] = []
    for i in range(n):
        env = min(1.0, i / attack) * math.exp(-i / tau)
        fade = min(1.0, (n - i) / tail_fade)
        out.append(gain * env * fade * math.sin(2.0 * math.pi * freq_hz * i / rate))
    return out


def _builtin_samples(name: str) -> list[float]:
    """Unity-gain float samples for a built-in cue.

    * ``start`` — a bright short tick: "mic is live, talk".
    * ``stop`` — lower and softer: "stopped listening".
    * ``done`` — two rising notes, softer still: "text landed".
    """
    if name == "start":
        return _decaying_tone(1320.0, 90.0, tau_ms=35.0)
    if name == "stop":
        return _decaying_tone(880.0, 70.0, gain=0.8, tau_ms=25.0)
    if name == "done":
        return (
            _decaying_tone(1047.0, 55.0, gain=0.7, tau_ms=40.0)
            + _decaying_tone(1568.0, 80.0, gain=0.7, tau_ms=40.0)
        )
    raise KeyError(name)


def _to_pcm16(samples: Iterable[float], volume: float) -> bytes:
    out = array("h")
    for s in samples:
        v = int(round(s * volume * 32767.0))
        out.append(max(-32768, min(32767, v)))
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


def builtin_clip(name: str, volume: float) -> Clip:
    return Clip(
        rate=SAMPLE_RATE,
        channels=1,
        pcm=_to_pcm16(_builtin_samples(name), volume),
    )


def load_wav_clip(path: str | os.PathLike, volume: float) -> Clip:
    """Load a 16-bit PCM WAV (mono or stereo) and apply ``volume``.

    Raises ``ValueError`` for anything else (8/24/32-bit, float, or
    compressed WAVs, or a clip longer than :data:`_MAX_CUE_SECONDS`)
    and ``OSError`` if the file cannot be read. Callers fall back to
    the built-in cue.
    """
    with wave.open(os.fspath(path), "rb") as wf:
        width = wf.getsampwidth()
        channels = wf.getnchannels()
        rate = wf.getframerate()
        nframes = wf.getnframes()
        if width != 2:
            raise ValueError(f"{path}: need 16-bit PCM WAV, got {width * 8}-bit")
        if channels not in (1, 2):
            raise ValueError(f"{path}: need mono or stereo, got {channels} channels")
        if rate <= 0:
            raise ValueError(f"{path}: invalid sample rate {rate}")
        if nframes / rate > _MAX_CUE_SECONDS:
            raise ValueError(
                f"{path}: cue is {nframes / rate:.1f}s, max {_MAX_CUE_SECONDS:.0f}s"
            )
        raw = wf.readframes(nframes)
    samples = array("h")
    samples.frombytes(raw[: len(raw) - (len(raw) % 2)])
    if sys.byteorder == "big":
        samples.byteswap()  # WAV is little-endian on disk
    return Clip(
        rate=rate,
        channels=channels,
        pcm=_to_pcm16((s / 32768.0 for s in samples), volume),
    )


# ----------------------------------------------------------------------
# backends

Backend = Callable[[Clip, str], None]
"""Play ``clip`` (cue ``name`` is for file naming / logs). Raise on failure."""

# ``(argv) -> (returncode, stderr)`` — injectable for tests.
Runner = Callable[[Sequence[str]], tuple[int, str]]


def _default_runner(argv: Sequence[str]) -> tuple[int, str]:
    r = subprocess.run(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=5.0,
        check=False,
    )
    return int(r.returncode), (r.stderr or b"").decode("utf-8", errors="replace")


def default_cache_dir() -> Path:
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "spitch" / "cues"


def write_wav(path: Path, clip: Clip) -> None:
    """Atomically write ``clip`` as a WAV file (tmp sibling + replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with wave.open(os.fspath(tmp), "wb") as wf:
        wf.setnchannels(clip.channels)
        wf.setsampwidth(2)
        wf.setframerate(clip.rate)
        wf.writeframes(clip.pcm)
    os.replace(tmp, path)


class SounddeviceBackend:
    """In-process playback through PortAudio (``python-sounddevice``).

    Same library the capture side prefers, so when it is installed
    the cue path has no fork/exec. ``ImportError`` on first use makes
    the chain drop this backend.
    """

    name = "sounddevice"

    def __call__(self, clip: Clip, name: str) -> None:
        import sounddevice as sd  # type: ignore

        stream = sd.RawOutputStream(
            samplerate=clip.rate, channels=clip.channels, dtype="int16",
        )
        stream.start()
        try:
            # Blocking write; stop() then waits for the buffer to drain.
            stream.write(clip.pcm)
        finally:
            try:
                stream.stop()
            finally:
                stream.close()


class CliBackend:
    """Play through a command-line player with a cached WAV file.

    The WAV for each cue is written once per (backend, clip) into
    ``cache_dir`` — a volume change or a reload produces a new clip
    and a rewrite. Writes are atomic so a player still reading the
    previous file keeps its inode.
    """

    def __init__(
        self,
        argv: Sequence[str],
        *,
        cache_dir: Path,
        runner: Runner | None = None,
    ) -> None:
        self.argv = list(argv)
        self.name = os.path.basename(self.argv[0])
        self._cache_dir = cache_dir
        self._runner = runner or _default_runner
        self._written: dict[str, str] = {}  # cue name -> content digest

    def _path_for(self, clip: Clip, name: str) -> Path:
        digest = hashlib.sha1(
            f"{clip.rate}:{clip.channels}:".encode() + clip.pcm
        ).hexdigest()[:12]
        path = self._cache_dir / f"{name}.wav"
        if self._written.get(name) != digest or not path.exists():
            write_wav(path, clip)
            self._written[name] = digest
        return path

    def __call__(self, clip: Clip, name: str) -> None:
        path = self._path_for(clip, name)
        rc, err = self._runner(self.argv + [os.fspath(path)])
        if rc != 0:
            raise RuntimeError(
                f"{self.name} exited {rc}: {err.strip()[-200:] or 'no stderr'}"
            )


# Asking the sound server for a small buffer roughly halves the
# key-to-tick time: measured 2026-09-02 on PulseAudio 35 with the
# 90 ms start cue, ``paplay`` went from 219 ms to 112 ms wall time
# and ``pw-play`` from 176 ms to 134 ms. A player that rejects the
# flag exits non-zero and the chain moves on to the next one.
_CLI_PLAYERS: tuple[tuple[str, ...], ...] = (
    ("paplay", "--latency-msec=30"),
    ("pw-play", "--latency=30ms"),
    ("aplay", "-q"),
)


def available_backends(
    *, cache_dir: Path | None = None, runner: Runner | None = None,
) -> list[Backend]:
    """Backends worth trying on this host, most preferred first."""
    out: list[Backend] = [SounddeviceBackend()]
    cache = cache_dir or default_cache_dir()
    for argv in _CLI_PLAYERS:
        exe = shutil.which(argv[0])
        if exe:
            out.append(CliBackend((exe,) + argv[1:], cache_dir=cache, runner=runner))
    return out


class BackendChain:
    """Try backends in order; a failing one is logged once and dropped.

    Never raises. Once the list is empty every play is a silent no-op
    (one warning). This is the daemon's default backend.
    """

    def __init__(self, backends: Iterable[Backend]) -> None:
        self._backends: list[Backend] = list(backends)
        self._exhausted_logged = False

    @property
    def names(self) -> list[str]:
        return [getattr(b, "name", type(b).__name__) for b in self._backends]

    def __call__(self, clip: Clip, name: str) -> None:
        while self._backends:
            backend = self._backends[0]
            try:
                backend(clip, name)
                return
            except ImportError:
                # Optional module missing (sounddevice). Not worth a warning.
                log.debug("sound backend %s unavailable", self.names[0])
            except Exception as exc:
                log.warning(
                    "sound backend %s failed (%s) — trying next", self.names[0], exc,
                )
            self._backends.pop(0)
        if not self._exhausted_logged:
            self._exhausted_logged = True
            log.warning(
                "no working sound backend — cues disabled (install "
                "python-sounddevice, pulseaudio-utils, pipewire-bin or alsa-utils)"
            )


# ----------------------------------------------------------------------
# the player

class SoundCues:
    """Owns the rendered clips and a worker thread that plays them.

    ``play(name)`` only enqueues; it is safe to call from any thread,
    including a real-time audio callback. Unknown, disabled, or
    post-``close()`` cues return ``False`` and do nothing.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        volume: float = DEFAULT_VOLUME,
        cues: Mapping[str, bool] | None = None,
        files: Mapping[str, str] | None = None,
        backend: Backend | None = None,
        cache_dir: Path | None = None,
    ) -> None:
        self.volume = max(0.0, min(1.0, float(volume)))
        self._clips: dict[str, Clip] = {}
        self._enabled = bool(enabled) and self.volume > 0.0
        self._backend: Backend = backend or BackendChain(
            available_backends(cache_dir=cache_dir)
        )
        self._queue: "queue.Queue[str | None]" = queue.Queue()
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()
        self._closed = False
        self._failed_logged: set[str] = set()
        if not self._enabled:
            return
        want = dict(cues or {})
        want_files = dict(files or {})
        for name in CUE_NAMES:
            if not want.get(name, True):
                continue
            self._clips[name] = self._load_clip(name, want_files.get(name) or "")

    def _load_clip(self, name: str, file: str) -> Clip:
        if file:
            try:
                clip = load_wav_clip(os.path.expanduser(file), self.volume)
                log.info(
                    "sound cue %s: %s (%.0f ms, %d Hz)",
                    name, file, clip.duration_ms, clip.rate,
                )
                return clip
            except (OSError, ValueError, wave.Error, EOFError) as exc:
                log.warning(
                    "sound cue %s: cannot use %s (%s) — using built-in tone",
                    name, file, exc,
                )
        return builtin_clip(name, self.volume)

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any], **kwargs: Any) -> "SoundCues":
        """Build from the ``sounds`` config section (missing keys → defaults)."""
        s = _section(cfg, "sounds")
        volume = _finite_float(s.get("volume", DEFAULT_VOLUME), DEFAULT_VOLUME)
        cues = {name: bool(s.get(name, True)) for name in CUE_NAMES}
        files = {}
        for name in CUE_NAMES:
            raw = s.get(f"{name}_file")
            files[name] = raw.strip() if isinstance(raw, str) else ""
        return cls(
            enabled=bool(s.get("enabled", True)),
            volume=volume,
            cues=cues,
            files=files,
            **kwargs,
        )

    # -- introspection -------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled and not self._closed

    @property
    def cue_names(self) -> list[str]:
        return [n for n in CUE_NAMES if n in self._clips]

    def describe(self) -> str:
        if not self.enabled:
            return "sound cues: off"
        names = getattr(self._backend, "names", None)
        backend = ", ".join(names) if names else type(self._backend).__name__
        return (
            f"sound cues: {'/'.join(self.cue_names) or 'none'} "
            f"(volume={self.volume:.2f}, backends: {backend or 'none'})"
        )

    # -- main API ------------------------------------------------------

    def play(self, name: str) -> bool:
        """Queue cue ``name``. Returns True if it will be played."""
        if self._closed or not self._enabled or name not in self._clips:
            return False
        # Soft cap: a wedged backend must not turn key events into an
        # ever-growing backlog of stale beeps.
        if self._queue.qsize() >= 8:
            return False
        self._ensure_worker()
        self._queue.put(name)
        return True

    def close(self) -> None:
        """Stop the worker. Idempotent; never raises."""
        self._closed = True
        with self._worker_lock:
            worker = self._worker
        if worker is None:
            return
        self._queue.put(None)
        worker.join(timeout=1.0)

    # -- internals -----------------------------------------------------

    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if self._worker is not None:
                return
            self._worker = threading.Thread(
                target=self._run, name="spitch-sound-cues", daemon=True,
            )
            self._worker.start()

    def _run(self) -> None:
        while True:
            name = self._queue.get()
            if name is None:
                return
            clip = self._clips.get(name)
            if clip is None:
                continue
            try:
                self._backend(clip, name)
            except Exception as exc:
                # Custom backends may raise; BackendChain never does.
                if name not in self._failed_logged:
                    self._failed_logged.add(name)
                    log.warning("sound cue %s failed: %s", name, exc)
