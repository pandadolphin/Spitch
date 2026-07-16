"""Pause / resume MPRIS media players around Spitch talk sessions.

Spitch keeps the microphone open continuously for the pre-buffer, so the
audio server cannot detect "user started dictating" from capture streams
alone. Instead we drive MPRIS-compatible players (Spotify, browsers,
Totem, …) through the ``playerctl`` CLI when a talk session is accepted
and when it ends.

Behaviour:

* :meth:`pause` — list players, remember those currently ``Playing``,
  pause each. No-op if already holding a pause session, if disabled, or
  if ``playerctl`` is missing.
* :meth:`resume` — ``play`` only the players we paused. Safe to call
  repeatedly (second call is a no-op).

Never starts a player that was already paused/stopped. Failures against
individual players are logged and ignored so a broken MPRIS target cannot
break the voice path.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Callable, Iterable, Sequence

log = logging.getLogger("spitch.media_pause")

# Type of the injectable runner used by tests. Signature mirrors a thin
# wrapper around ``subprocess.run`` that returns (returncode, stdout).
Runner = Callable[[Sequence[str]], tuple[int, str]]


def _default_runner(argv: Sequence[str]) -> tuple[int, str]:
    try:
        r = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("playerctl invoke failed: %s", exc)
        return (127, "")
    return (int(r.returncode), r.stdout or "")


class MediaPauser:
    """Session-scoped MPRIS pause helper.

    ``enabled`` gates the whole feature. ``playerctl`` is resolved once
    at construction (or on first use if the PATH changes under tests
    that inject a runner).
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        playerctl: str | None = None,
        runner: Runner | None = None,
    ) -> None:
        self.enabled = bool(enabled)
        self._runner: Runner = runner or _default_runner
        # Explicit path wins; otherwise resolve on demand so unit tests
        # that only inject a runner don't need the real binary.
        self._playerctl = playerctl
        self._paused: list[str] = []
        self._active = False
        self._missing_logged = False

    # -- public API ----------------------------------------------------

    def pause(self) -> list[str]:
        """Pause currently-playing MPRIS players. Returns names paused."""
        if not self.enabled:
            return []
        if self._active:
            # Nested / double press — keep the original snapshot.
            return list(self._paused)
        bin_path = self._resolve_playerctl()
        if not bin_path:
            return []
        playing = self._list_playing(bin_path)
        paused: list[str] = []
        for name in playing:
            code, _ = self._runner([bin_path, "-p", name, "pause"])
            if code == 0:
                paused.append(name)
            else:
                log.debug("playerctl pause failed for %s (rc=%s)", name, code)
        self._paused = paused
        self._active = True
        if paused:
            log.info("paused media players: %s", ", ".join(paused))
        else:
            log.debug("no playing MPRIS players to pause")
        return list(paused)

    def resume(self) -> list[str]:
        """Resume players we paused. Returns names we tried to play."""
        if not self._active:
            return []
        names = list(self._paused)
        self._paused = []
        self._active = False
        if not names:
            return []
        bin_path = self._resolve_playerctl()
        if not bin_path:
            return []
        resumed: list[str] = []
        for name in names:
            code, _ = self._runner([bin_path, "-p", name, "play"])
            if code == 0:
                resumed.append(name)
            else:
                log.debug("playerctl play failed for %s (rc=%s)", name, code)
        if resumed:
            log.info("resumed media players: %s", ", ".join(resumed))
        return resumed

    @property
    def is_active(self) -> bool:
        """True while we hold a pause session (even if zero players)."""
        return self._active

    @property
    def paused_players(self) -> list[str]:
        return list(self._paused)

    # -- internals -----------------------------------------------------

    def _resolve_playerctl(self) -> str | None:
        if self._playerctl:
            return self._playerctl
        # When a custom runner is injected (unit tests), default the
        # binary name so we don't require playerctl on the host.
        if self._runner is not _default_runner:
            self._playerctl = "playerctl"
            return self._playerctl
        found = shutil.which("playerctl")
        if not found:
            if not self._missing_logged:
                log.warning(
                    "playerctl not found — media auto-pause disabled "
                    "(install the playerctl package to enable)"
                )
                self._missing_logged = True
            return None
        self._playerctl = found
        return found

    def _list_playing(self, bin_path: str) -> list[str]:
        code, out = self._runner([bin_path, "-l"])
        if code != 0:
            log.debug("playerctl -l failed (rc=%s)", code)
            return []
        names = [line.strip() for line in out.splitlines() if line.strip()]
        playing: list[str] = []
        for name in names:
            sc, status = self._runner([bin_path, "-p", name, "status"])
            if sc != 0:
                continue
            if status.strip().lower() == "playing":
                playing.append(name)
        return playing
