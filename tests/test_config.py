"""Tests for :mod:`spitch.config`."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from spitch.config import (
    DEFAULT_CONFIG,
    DEFAULT_TALK_KEY,
    FINALIZE_MIN_S,
    FINALIZE_SLACK_S,
    LINGER_MAX_S,
    ConfigError,
    _finalize_deadlines,
    _release_linger_seconds,
    _section,
    clear_verified,
    config_dir,
    config_path,
    credentials_signature,
    default_config,
    is_complete,
    is_verified,
    load_config,
    mark_verified,
    save_config,
)


class ConfigPathTests(unittest.TestCase):
    def test_xdg_config_home_overrides(self):
        prev = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = "/tmp/spitch-test-xdg"
        try:
            self.assertEqual(config_dir(), Path("/tmp/spitch-test-xdg/spitch"))
            self.assertEqual(
                config_path(), Path("/tmp/spitch-test-xdg/spitch/config.json")
            )
        finally:
            if prev is None:
                del os.environ["XDG_CONFIG_HOME"]
            else:
                os.environ["XDG_CONFIG_HOME"] = prev

    def test_default_path_under_home(self):
        prev = os.environ.pop("XDG_CONFIG_HOME", None)
        try:
            self.assertEqual(config_dir(), Path.home() / ".config" / "spitch")
        finally:
            if prev is not None:
                os.environ["XDG_CONFIG_HOME"] = prev


class LoadConfigTests(unittest.TestCase):
    def test_missing_returns_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "nope.json"
            cfg = load_config(p)
            self.assertEqual(cfg, DEFAULT_CONFIG)

    def test_returned_config_is_a_copy(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "nope.json"
            cfg = load_config(p)
            cfg["doubao"]["app_key"] = "MUTATED"
            self.assertEqual(DEFAULT_CONFIG["doubao"]["app_key"], "")

    def test_partial_merges_with_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.json"
            p.write_text(
                json.dumps({"doubao": {"app_key": "AK"}}), encoding="utf-8"
            )
            cfg = load_config(p)
            self.assertEqual(cfg["doubao"]["app_key"], "AK")
            # untouched defaults preserved
            self.assertEqual(cfg["doubao"]["access_key"], "")
            self.assertEqual(cfg["doubao"]["endpoint"], DEFAULT_CONFIG["doubao"]["endpoint"])
            self.assertEqual(cfg["audio"]["sample_rate"], 16000)
            self.assertEqual(cfg["hotkey"]["talk_key"], DEFAULT_TALK_KEY)
            self.assertEqual(DEFAULT_TALK_KEY, "RightCtrl")
            self.assertEqual(cfg["provider"], "doubao")

    def test_explicit_talk_key_overrides_new_default(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.json"
            p.write_text(
                json.dumps({"hotkey": {"talk_key": "RightAlt"}}),
                encoding="utf-8",
            )
            cfg = load_config(p)
            self.assertEqual(cfg["hotkey"]["talk_key"], "RightAlt")

    def test_invalid_json_raises_config_error(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bad.json"
            p.write_text("{not valid", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(p)

    def test_non_object_raises_config_error(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "list.json"
            p.write_text("[1,2,3]", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(p)


class SaveConfigTests(unittest.TestCase):
    def test_round_trip_and_perms(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "sub" / "config.json"
            cfg = default_config()
            cfg["doubao"]["app_key"] = "AK"
            cfg["doubao"]["access_key"] = "SK"
            saved = save_config(cfg, p)
            self.assertTrue(saved.exists())
            mode = stat.S_IMODE(saved.stat().st_mode)
            self.assertEqual(mode, 0o600)
            reloaded = load_config(p)
            self.assertEqual(reloaded["doubao"]["app_key"], "AK")
            self.assertEqual(reloaded["doubao"]["access_key"], "SK")

    def test_no_temp_files_left_behind(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.json"
            save_config(default_config(), p)
            leftover = sorted(os.listdir(td))
            self.assertEqual(leftover, ["config.json"])

    def test_overwrites_existing(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.json"
            save_config({"provider": "doubao", "doubao": {"app_key": "A"}}, p)
            save_config({"provider": "doubao", "doubao": {"app_key": "B"}}, p)
            self.assertEqual(load_config(p)["doubao"]["app_key"], "B")

    def test_creates_parent_dir(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "nested" / "deeper" / "config.json"
            save_config(default_config(), p)
            self.assertTrue(p.exists())


class IsCompleteTests(unittest.TestCase):
    def test_default_is_incomplete(self):
        self.assertFalse(is_complete(default_config()))

    def test_with_creds_is_complete(self):
        cfg = default_config()
        cfg["doubao"]["app_key"] = "x"
        cfg["doubao"]["access_key"] = "y"
        self.assertTrue(is_complete(cfg))

    def test_missing_endpoint_incomplete(self):
        cfg = default_config()
        cfg["doubao"]["app_key"] = "x"
        cfg["doubao"]["access_key"] = "y"
        cfg["doubao"]["endpoint"] = ""
        self.assertFalse(is_complete(cfg))

    def test_wrong_provider_incomplete(self):
        cfg = default_config()
        cfg["provider"] = "other"
        cfg["doubao"]["app_key"] = "x"
        cfg["doubao"]["access_key"] = "y"
        self.assertFalse(is_complete(cfg))

    def test_grok_complete_with_key_and_endpoint(self):
        cfg = default_config()
        cfg["provider"] = "grok"
        cfg["grok"]["api_key"] = "xai-test-key"
        # endpoint defaulted
        self.assertTrue(is_complete(cfg))

    def test_grok_missing_key_incomplete(self):
        cfg = default_config()
        cfg["provider"] = "grok"
        cfg["grok"]["api_key"] = ""
        self.assertFalse(is_complete(cfg))

    def test_default_includes_grok_section(self):
        cfg = default_config()
        self.assertIn("grok", cfg)
        self.assertEqual(cfg["grok"]["endpoint"], "wss://api.x.ai/v1/stt")
        self.assertTrue(cfg["grok"]["interim_results"])
        self.assertTrue(cfg["grok"]["send_finalize_on_eos"])

    def test_load_merges_missing_grok_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.json"
            p.write_text(
                json.dumps({"provider": "doubao", "doubao": {"app_key": "AK"}}),
                encoding="utf-8",
            )
            cfg = load_config(p)
            self.assertIn("grok", cfg)
            self.assertEqual(cfg["grok"]["endpoint"], DEFAULT_CONFIG["grok"]["endpoint"])


class MarkVerifiedTests(unittest.TestCase):
    def test_sets_iso_z_for_aware_utc(self):
        cfg = default_config()
        moment = datetime(2026, 5, 3, 14, 0, 0, tzinfo=timezone.utc)
        out = mark_verified(cfg, moment)
        self.assertEqual(out["verified_at"], "2026-05-03T14:00:00Z")

    def test_does_not_mutate_input(self):
        cfg = default_config()
        moment = datetime(2026, 5, 3, 14, 0, 0, tzinfo=timezone.utc)
        mark_verified(cfg, moment)
        self.assertIsNone(cfg["verified_at"])

    def test_naive_datetime_treated_as_utc(self):
        cfg = default_config()
        moment = datetime(2026, 5, 3, 14, 0, 0)
        out = mark_verified(cfg, moment)
        self.assertEqual(out["verified_at"], "2026-05-03T14:00:00Z")


class IsVerifiedTests(unittest.TestCase):
    def _stamped(self):
        cfg = default_config()
        cfg["doubao"]["app_key"] = "x"
        cfg["doubao"]["access_key"] = "y"
        return mark_verified(cfg)

    def test_default_not_verified(self):
        self.assertFalse(is_verified(default_config()))

    def test_complete_but_unstamped_not_verified(self):
        cfg = default_config()
        cfg["doubao"]["app_key"] = "x"
        cfg["doubao"]["access_key"] = "y"
        self.assertTrue(is_complete(cfg))
        self.assertFalse(is_verified(cfg))

    def test_stamped_complete_is_verified(self):
        self.assertTrue(is_verified(self._stamped()))

    def test_stamped_but_incomplete_not_verified(self):
        cfg = mark_verified(default_config())
        # creds still empty
        self.assertFalse(is_complete(cfg))
        self.assertFalse(is_verified(cfg))

    def test_clear_drops_verified(self):
        cfg = self._stamped()
        cleared = clear_verified(cfg)
        self.assertIsNone(cleared["verified_at"])
        self.assertFalse(is_verified(cleared))
        # original untouched
        self.assertTrue(is_verified(cfg))

    def test_empty_string_stamp_is_not_verified(self):
        cfg = default_config()
        cfg["doubao"]["app_key"] = "x"
        cfg["doubao"]["access_key"] = "y"
        cfg["verified_at"] = "   "
        self.assertFalse(is_verified(cfg))

    def test_signature_change_invalidates_stamp(self):
        # Stamp is good for these creds…
        cfg = self._stamped()
        self.assertTrue(is_verified(cfg))
        # …but if someone hand-edits the access key, the gate closes.
        cfg["doubao"]["access_key"] = "rotated"
        self.assertFalse(is_verified(cfg))

    def test_legacy_stamp_without_signature_still_verified(self):
        # Older Spitch builds wrote verified_at without verified_signature.
        cfg = default_config()
        cfg["doubao"]["app_key"] = "x"
        cfg["doubao"]["access_key"] = "y"
        cfg["verified_at"] = "2026-05-03T14:00:00Z"
        # No verified_signature key at all — treat as verified.
        self.assertTrue(is_verified(cfg))


class CredentialsSignatureTests(unittest.TestCase):
    def test_signature_changes_with_credentials(self):
        a = default_config()
        a["doubao"]["app_key"] = "AK"
        b = default_config()
        b["doubao"]["app_key"] = "different"
        self.assertNotEqual(credentials_signature(a), credentials_signature(b))

    def test_signature_stable_across_unrelated_changes(self):
        a = default_config()
        a["doubao"]["app_key"] = "AK"
        a["audio"]["sample_rate"] = 16000
        b = default_config()
        b["doubao"]["app_key"] = "AK"
        b["audio"]["sample_rate"] = 22050  # different audio knob
        b["hotkey"]["talk_key"] = "F3"     # different hotkey
        self.assertEqual(credentials_signature(a), credentials_signature(b))

    def test_resource_or_endpoint_change_invalidates_signature(self):
        a = default_config()
        a["doubao"]["app_key"] = "AK"
        b = default_config()
        b["doubao"]["app_key"] = "AK"
        b["doubao"]["resource_id"] = "different.resource"
        self.assertNotEqual(credentials_signature(a), credentials_signature(b))

    def test_grok_signature_is_provider_key_endpoint_only(self):
        a = default_config()
        a["provider"] = "grok"
        a["grok"]["api_key"] = "k"
        a["grok"]["language"] = "en"
        b = default_config()
        b["provider"] = "grok"
        b["grok"]["api_key"] = "k"
        b["grok"]["language"] = "zh"  # non-auth
        b["grok"]["filler_words"] = True
        self.assertEqual(credentials_signature(a), credentials_signature(b))
        self.assertEqual(
            credentials_signature(a),
            ("grok", "k", a["grok"]["endpoint"]),
        )


class VerifiedStampHardeningTests(unittest.TestCase):
    """KD-18: V1–V5 — legacy unsigned stamps are Doubao-only."""

    def _doubao_complete(self):
        cfg = default_config()
        cfg["provider"] = "doubao"
        cfg["doubao"]["app_key"] = "x"
        cfg["doubao"]["access_key"] = "y"
        return cfg

    def _grok_complete(self):
        cfg = default_config()
        cfg["provider"] = "grok"
        cfg["grok"]["api_key"] = "xai-test"
        return cfg

    def test_v1_doubao_unsigned_verified_at_only(self):
        cfg = self._doubao_complete()
        cfg["verified_at"] = "2026-05-03T14:00:00Z"
        # no verified_signature
        self.assertTrue(is_verified(cfg))

    def test_v2_grok_unsigned_verified_at_only(self):
        cfg = self._grok_complete()
        cfg["verified_at"] = "2026-05-03T14:00:00Z"
        self.assertFalse(is_verified(cfg))

    def test_v3_cross_provider_doubao_stamp_does_not_authorize_grok(self):
        cfg = self._doubao_complete()
        cfg["verified_at"] = "2026-05-03T14:00:00Z"
        self.assertTrue(is_verified(cfg))
        cfg["provider"] = "grok"
        cfg["grok"]["api_key"] = "xai-test"
        self.assertFalse(is_verified(cfg))

    def test_v4_grok_matching_signature(self):
        cfg = mark_verified(self._grok_complete())
        self.assertTrue(is_verified(cfg))

    def test_v5_grok_wrong_signature(self):
        cfg = self._grok_complete()
        cfg["verified_at"] = "2026-05-03T14:00:00Z"
        cfg["verified_signature"] = "deadbeefdeadbeef"
        self.assertFalse(is_verified(cfg))


class MappingGuardTests(unittest.TestCase):
    """KD-22: M1–M3 malformed nested sections must not crash."""

    def test_m1_grok_section_non_mapping(self):
        for bad in ("bad", ["x"], None, 42):
            cfg = default_config()
            cfg["provider"] = "grok"
            cfg["grok"] = bad  # type: ignore[assignment]
            self.assertFalse(is_complete(cfg))
            # credentials_signature must not TypeError
            sig = credentials_signature(cfg)
            self.assertEqual(sig[0], "grok")

    def test_m2_doubao_section_list(self):
        cfg = default_config()
        cfg["provider"] = "doubao"
        cfg["doubao"] = ["x"]  # type: ignore[assignment]
        self.assertFalse(is_complete(cfg))
        credentials_signature(cfg)  # no crash

    def test_section_helper(self):
        self.assertEqual(_section({"audio": "bad"}, "audio"), {})
        self.assertEqual(_section({"audio": {"a": 1}}, "audio")["a"], 1)


class FinalizeDeadlineTests(unittest.TestCase):
    """KD-12: linger-safe inequality; M3 / M3b / M3c non-finite inputs."""

    def test_inject_longer_than_controller_plus_linger_plus_slack(self):
        for linger_ms in (0, 300, 1000):
            for final_wait in (5.0, 30.0):
                cfg = default_config()
                cfg["inject"]["final_wait_seconds"] = final_wait
                cfg["audio"]["release_linger_ms"] = linger_ms
                controller_t, inject_t = _finalize_deadlines(cfg)
                linger_s = min(linger_ms / 1000.0, LINGER_MAX_S)
                self.assertGreaterEqual(
                    inject_t,
                    controller_t + linger_s + FINALIZE_SLACK_S - 1e-9,
                )
                self.assertGreaterEqual(controller_t, FINALIZE_MIN_S)
                self.assertTrue(controller_t == controller_t)  # finite
                self.assertTrue(inject_t == inject_t)

    def test_m3_audio_inject_non_mapping_safe_defaults(self):
        cfg = default_config()
        cfg["audio"] = "bad"  # type: ignore[assignment]
        cfg["inject"] = []  # type: ignore[assignment]
        controller_t, inject_t = _finalize_deadlines(cfg)
        self.assertGreaterEqual(controller_t, FINALIZE_MIN_S)
        self.assertGreater(inject_t, controller_t)

    def test_m3b_non_finite_final_wait(self):
        for bad in ("nan", "inf", None, -1, float("nan"), float("inf")):
            cfg = default_config()
            cfg["inject"]["final_wait_seconds"] = bad
            controller_t, inject_t = _finalize_deadlines(cfg)
            import math
            self.assertTrue(math.isfinite(controller_t))
            self.assertTrue(math.isfinite(inject_t))
            self.assertGreater(controller_t, 0)
            self.assertGreater(inject_t, controller_t)

    def test_m3c_non_finite_or_huge_linger(self):
        for bad in ("nan", 1e12, float("nan")):
            cfg = default_config()
            cfg["audio"]["release_linger_ms"] = bad
            controller_t, inject_t = _finalize_deadlines(cfg)
            import math
            self.assertTrue(math.isfinite(inject_t))
            # linger contribution capped
            self.assertLessEqual(
                inject_t - controller_t - FINALIZE_SLACK_S,
                LINGER_MAX_S + 1e-9,
            )

    def test_stock_defaults(self):
        controller_t, inject_t = _finalize_deadlines(default_config())
        self.assertAlmostEqual(controller_t, 30.0)
        self.assertAlmostEqual(inject_t, 30.0 + 0.3 + FINALIZE_SLACK_S)

    def test_huge_linger_deadlines_match_scheduled_linger_helper(self):
        """KD-12: inject budget must cover the same capped linger the Timer uses.

        With ``release_linger_ms=10000`` (above LINGER_MAX_S), both
        ``_release_linger_seconds`` and ``_finalize_deadlines`` clamp to
        5s so inject_t = controller_t + 5 + slack — not the raw 10s.
        """
        cfg = default_config()
        cfg["audio"]["release_linger_ms"] = 10000
        cfg["inject"]["final_wait_seconds"] = 30.0
        scheduled = _release_linger_seconds(cfg)
        self.assertAlmostEqual(scheduled, LINGER_MAX_S)
        controller_t, inject_t = _finalize_deadlines(cfg)
        self.assertAlmostEqual(controller_t, 30.0)
        self.assertAlmostEqual(
            inject_t, controller_t + scheduled + FINALIZE_SLACK_S
        )
        # Raw 10s linger would break the inequality if Timer used it:
        raw_s = 10.0
        self.assertLess(inject_t, controller_t + raw_s + FINALIZE_SLACK_S)


if __name__ == "__main__":
    unittest.main()
