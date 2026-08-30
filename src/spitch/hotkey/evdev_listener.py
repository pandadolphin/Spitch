"""Global keyboard hotkey listener via /dev/input/event* (evdev).

Watches every keyboard device for a configured modifier-pair (e.g.
``Ctrl+Alt``) held simultaneously, with no third non-modifier key
pressed during the chord. Press / release / cancel events fire on the
caller-provided callbacks. The listener runs in its own daemon thread
and is IM-framework-independent — it works on Wayland and X11 alike.

Reading from /dev/input/event* requires the user to be in the ``input``
group (or have an equivalent ACL). The ``start()`` method raises a
descriptive RuntimeError if no readable keyboard is found.
"""

from __future__ import annotations

import logging
import threading
import re
from typing import Callable, Iterable

log = logging.getLogger("spitch.hotkey")


_MOD_KEYS: dict[str, set[int]] = {}

# Generic Alt/Ctrl/Shift/Super fire on everyday shortcuts (Alt+Tab,
# Ctrl+C). These sided keys do not, so they are allowed as a single
# hold-to-talk key. This machine's US layout maps Right Alt to Alt_R,
# not AltGr.
TALK_SINGLE_OK = frozenset({"rightalt", "rightctrl"})

_TOKEN_ALIASES = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "alt": "alt",
    "meta": "alt",
    "shift": "shift",
    "super": "super",
    "win": "super",
    "rightalt": "rightalt",
    "ralt": "rightalt",
    "altgr": "rightalt",
    "leftalt": "leftalt",
    "lalt": "leftalt",
    "rightctrl": "rightctrl",
    "rctrl": "rightctrl",
    "leftctrl": "leftctrl",
    "lctrl": "leftctrl",
}
_SIDE_WORDS = {"left": "left", "l": "left", "right": "right", "r": "right"}
_SIDEABLE = {"alt", "ctrl"}


def _init_codes() -> None:
    global _MOD_KEYS
    if _MOD_KEYS:
        return
    from evdev import ecodes as ec
    _MOD_KEYS = {
        "ctrl":  {ec.KEY_LEFTCTRL, ec.KEY_RIGHTCTRL},
        "alt":   {ec.KEY_LEFTALT, ec.KEY_RIGHTALT},
        "shift": {ec.KEY_LEFTSHIFT, ec.KEY_RIGHTSHIFT},
        "super": {ec.KEY_LEFTMETA, ec.KEY_RIGHTMETA},
        "leftalt": {ec.KEY_LEFTALT},
        "rightalt": {ec.KEY_RIGHTALT},
        "leftctrl": {ec.KEY_LEFTCTRL},
        "rightctrl": {ec.KEY_RIGHTCTRL},
    }


def parse_combo(spec: str) -> list[str]:
    """Parse ``"Ctrl+Alt"`` → ``['ctrl', 'alt']``. Order-insensitive,
    duplicates removed. Unknown tokens are dropped.

    Sided tokens: ``RightAlt`` / ``RAlt`` / ``AltGr`` / ``Right+Alt``
    → ``rightalt`` (KEY_RIGHTALT only). Same pattern for ``RightCtrl``.
    """
    compact = spec.replace(" ", "")
    raw_parts = [
        p.strip().lower()
        for p in compact.replace("-", "+").split("+")
        if p.strip()
    ]
    merged: list[str] = []
    i = 0
    while i < len(raw_parts):
        side = _SIDE_WORDS.get(raw_parts[i])
        if (
            side is not None
            and i + 1 < len(raw_parts)
            and raw_parts[i + 1] in _SIDEABLE
        ):
            merged.append(side + raw_parts[i + 1])
            i += 2
            continue
        merged.append(raw_parts[i])
        i += 1
    out: list[str] = []
    for raw in merged:
        p = _TOKEN_ALIASES.get(raw)
        if p and p not in out:
            out.append(p)
    return out


def parse_talk_keys(spec: str) -> list[list[str]]:
    """Parse one or more talk combos.

    ``"Ctrl+Alt"`` → ``[['ctrl', 'alt']]``.
    ``"RightAlt, RightCtrl"`` / ``"RightAlt or RightCtrl"`` →
    ``[['rightalt'], ['rightctrl']]`` (either key starts a session).
    """
    parts = re.split(r"\s*(?:,|\||\bor\b)\s*", spec.strip(), flags=re.IGNORECASE)
    out: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for part in parts:
        combo = parse_combo(part)
        if not combo:
            continue
        key = tuple(combo)
        if key in seen:
            continue
        seen.add(key)
        out.append(combo)
    return out


def combo_allowed_for_talk(combo: list[str]) -> bool:
    """True if this combo is safe as a paste-path hold-to-talk key."""
    if len(combo) >= 2:
        return True
    return len(combo) == 1 and combo[0] in TALK_SINGLE_OK


_COMBO_LABELS = {
    "rightalt": "Right Alt",
    "rightctrl": "Right Ctrl",
    "leftalt": "Left Alt",
    "leftctrl": "Left Ctrl",
}


def format_talk_keys(combos: Iterable[Iterable[str]]) -> str:
    labels = [
        "+".join(_COMBO_LABELS.get(c, c.title()) for c in combo)
        for combo in combos
    ]
    return " or ".join(labels)


def list_keyboards():
    """All input devices that look like keyboards (have KEY_A + KEY_LEFTCTRL)."""
    from evdev import InputDevice, list_devices, ecodes as ec
    devs = []
    for path in list_devices():
        try:
            d = InputDevice(path)
        except (OSError, PermissionError) as e:
            log.debug("cannot open %s: %s", path, e)
            continue
        caps = d.capabilities().get(ec.EV_KEY, [])
        if ec.KEY_A in caps and ec.KEY_LEFTCTRL in caps:
            devs.append(d)
        else:
            d.close()
    return devs


class HotkeyListener:
    """Detect a hold-to-talk modifier-pair hotkey on the global keyboard.

    Fires ``on_press`` the moment all configured modifiers (e.g. Ctrl
    AND Alt) are held simultaneously. Fires ``on_release`` as soon as
    any one of them is released. If a non-modifier key is pressed
    during the chord, fires ``on_cancel`` and the next combo arrival
    is required to re-fire ``on_press`` — this lets system shortcuts
    like Ctrl+Alt+T pass through cleanly.
    """

    def __init__(
        self,
        combo: Iterable[str] | None = None,
        *,
        alternatives: Iterable[Iterable[str]] | None = None,
        on_press: Callable[[], None],
        on_release: Callable[[], None],
        on_cancel: Callable[[], None] | None = None,
        allow_single_mod: bool = False,
    ):
        _init_codes()
        if alternatives is not None:
            self._combos = [list(c) for c in alternatives]
        elif combo is not None:
            self._combos = [list(combo)]
        else:
            raise ValueError("combo or alternatives is required")
        if not self._combos:
            raise ValueError("combo or alternatives is required")
        for wanted in self._combos:
            if len(wanted) < 2 and not allow_single_mod:
                # Single-modifier push-to-talk is normally unusable: Ctrl
                # / Alt / Shift / Super get pressed dozens of times per
                # minute for system shortcuts and would each kick off a
                # bogus recording. ``allow_single_mod=True`` opts into it
                # for salmon-mode and for sided singles (RightAlt / RightCtrl).
                raise ValueError(
                    "combo must contain at least two distinct modifier keys "
                    "(got %r) — pass allow_single_mod=True to opt into a "
                    "single-modifier hold" % wanted
                )
        names = [m for c in self._combos for m in c]
        self._wanted_codes: set[int] = set().union(
            *(_MOD_KEYS[m] for m in names)
        ) if names else set()
        self._all_mod_codes: set[int] = set().union(*_MOD_KEYS.values())
        self._on_press = on_press
        self._on_release = on_release
        self._on_cancel = on_cancel or (lambda: None)
        self._held: dict[str, bool] = {m: False for m in names}
        self._talk_active = False
        self._stop = threading.Event()
        # Set whenever none of the wanted modifiers is currently held.
        # Lets the inject thread block on Event.wait() instead of
        # busy-polling is_quiescent().
        self._quiescent_event = threading.Event()
        self._quiescent_event.set()
        self._thread: threading.Thread | None = None
        self._devices: list = []

    def start(self) -> None:
        self._devices = list_keyboards()
        if not self._devices:
            raise RuntimeError(
                "no readable keyboard devices found — add the user to "
                "the 'input' group: 'sudo usermod -aG input $USER' "
                "and log out / back in"
            )
        log.info("listening on %d keyboard device(s)", len(self._devices))
        self._thread = threading.Thread(
            target=self._run, name="spitch-hotkey", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        for d in self._devices:
            try:
                d.close()
            except Exception:
                pass
        self._devices = []

    def is_quiescent(self) -> bool:
        """True when none of the wanted modifiers is currently held."""
        return not any(self._held.values())

    def wait_quiescent(self, timeout: float | None = None) -> bool:
        """Block until all wanted modifiers are released.

        Returns ``True`` if quiescence was observed within ``timeout``,
        ``False`` if the timeout fired first. ``None`` waits forever.
        Used by the inject thread instead of a busy-poll over
        ``is_quiescent()`` so the daemon idle-burns 0% CPU between
        releases.
        """
        return self._quiescent_event.wait(timeout=timeout)

    def _run(self) -> None:
        from evdev import ecodes as ec
        from select import select
        fds = {d.fd: d for d in self._devices}
        while not self._stop.is_set():
            try:
                r, _, _ = select(list(fds.keys()), [], [], 0.5)
            except (OSError, ValueError):
                return
            for fd in r:
                d = fds.get(fd)
                if d is None:
                    continue
                try:
                    for ev in d.read():
                        if ev.type == ec.EV_KEY:
                            self._on_key(ev.code, ev.value)
                except OSError:
                    fds.pop(fd, None)

    def _on_key(self, code: int, value: int) -> None:
        # value: 0=release, 1=press, 2=autorepeat
        is_press = value == 1
        is_release = value == 0
        wanted_mod: str | None = None
        for name in self._held:
            if code in _MOD_KEYS[name]:
                wanted_mod = name
                break
        if wanted_mod is not None:
            if is_press:
                self._held[wanted_mod] = True
            elif is_release:
                self._held[wanted_mod] = False
            # Maintain the quiescent event in lockstep with _held so a
            # blocked wait_quiescent() returns the moment the user
            # finishes releasing the chord.
            if any(self._held.values()):
                self._quiescent_event.clear()
            else:
                self._quiescent_event.set()
            any_combo = any(
                all(self._held[m] for m in combo) for combo in self._combos
            )
            if any_combo and not self._talk_active:
                self._talk_active = True
                self._safe(self._on_press)
            elif not any_combo and self._talk_active:
                self._talk_active = False
                self._safe(self._on_release)
            return
        # Non-wanted key event. If during a chord and it's a real key
        # (not a different modifier like Shift), the user meant a
        # shortcut — cancel the talk session.
        if (
            is_press
            and self._talk_active
            and code not in self._all_mod_codes
        ):
            self._talk_active = False
            self._safe(self._on_cancel)

    @staticmethod
    def _safe(fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:
            log.exception("hotkey callback raised")
