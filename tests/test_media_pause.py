"""Tests for :mod:`spitch.media_pause`."""

from __future__ import annotations

import unittest
from typing import Sequence

from spitch.media_pause import MediaPauser


class FakePlayerctl:
    """In-memory stand-in for the playerctl CLI."""

    def __init__(self, players: dict[str, str] | None = None):
        # name -> status (Playing / Paused / Stopped)
        self.players = dict(players or {})
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> tuple[int, str]:
        args = list(argv)
        self.calls.append(args)
        # argv[0] is the binary name
        if len(args) < 2:
            return (1, "")
        cmd = args[1:]
        if cmd == ["-l"]:
            return (0, "\n".join(self.players.keys()) + ("\n" if self.players else ""))
        if len(cmd) >= 3 and cmd[0] == "-p":
            name, action = cmd[1], cmd[2]
            if name not in self.players:
                return (1, "")
            if action == "status":
                return (0, self.players[name] + "\n")
            if action == "pause":
                if self.players[name].lower() == "playing":
                    self.players[name] = "Paused"
                return (0, "")
            if action == "play":
                self.players[name] = "Playing"
                return (0, "")
        return (1, "")


class MediaPauserTests(unittest.TestCase):
    def test_pause_only_playing_and_resume_only_those(self):
        fake = FakePlayerctl(
            {
                "spotify": "Playing",
                "chrome": "Paused",
                "totem": "Playing",
            }
        )
        mp = MediaPauser(enabled=True, playerctl="playerctl", runner=fake)
        paused = mp.pause()
        self.assertEqual(sorted(paused), ["spotify", "totem"])
        self.assertEqual(fake.players["spotify"], "Paused")
        self.assertEqual(fake.players["totem"], "Paused")
        self.assertEqual(fake.players["chrome"], "Paused")  # was already
        self.assertTrue(mp.is_active)

        resumed = mp.resume()
        self.assertEqual(sorted(resumed), ["spotify", "totem"])
        self.assertEqual(fake.players["spotify"], "Playing")
        self.assertEqual(fake.players["totem"], "Playing")
        self.assertFalse(mp.is_active)
        # Second resume is a no-op.
        self.assertEqual(mp.resume(), [])

    def test_double_pause_keeps_first_snapshot(self):
        fake = FakePlayerctl({"a": "Playing"})
        mp = MediaPauser(enabled=True, playerctl="playerctl", runner=fake)
        self.assertEqual(mp.pause(), ["a"])
        # a is now Paused; a second pause must not forget it.
        fake.players["b"] = "Playing"
        self.assertEqual(mp.pause(), ["a"])
        mp.resume()
        self.assertEqual(fake.players["a"], "Playing")
        # b was never part of our snapshot
        self.assertEqual(fake.players["b"], "Playing")

    def test_disabled_is_noop(self):
        fake = FakePlayerctl({"spotify": "Playing"})
        mp = MediaPauser(enabled=False, playerctl="playerctl", runner=fake)
        self.assertEqual(mp.pause(), [])
        self.assertEqual(fake.players["spotify"], "Playing")
        self.assertEqual(mp.resume(), [])
        self.assertEqual(fake.calls, [])

    def test_no_players(self):
        fake = FakePlayerctl({})
        mp = MediaPauser(enabled=True, playerctl="playerctl", runner=fake)
        self.assertEqual(mp.pause(), [])
        self.assertTrue(mp.is_active)  # session still opens
        self.assertEqual(mp.resume(), [])
        self.assertFalse(mp.is_active)

    def test_default_config_key_exists(self):
        from spitch.config import DEFAULT_CONFIG, load_config
        import tempfile
        from pathlib import Path

        self.assertTrue(DEFAULT_CONFIG["audio"]["pause_media_on_talk"])
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "c.json"
            p.write_text("{}", encoding="utf-8")
            cfg = load_config(p)
            self.assertTrue(cfg["audio"]["pause_media_on_talk"])


if __name__ == "__main__":
    unittest.main()
