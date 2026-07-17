"""Spitch configuration dialog.

Two layers of operation:

1. **GTK UI** (default) — when PyGObject + GTK 3 are available, we
   pop a small dialog with provider selection, the required credential
   fields for Doubao or Grok, audio sample rate, and the talk hotkey.
   A "Test connection" button runs :func:`spitch.ui.probe.probe_credentials_for_config`
   and surfaces success or the error from the selected provider.

2. **Headless CLI** (``--cli``, or automatic when GTK is missing) —
   reads the same fields from stdin and runs the same probe. This
   lets us drive the auth flow from ``e2e_smoke.sh`` and from CI.

Either way, on success we ``mark_verified`` the saved config so the
engine's ``do_focus_in`` knows it's allowed to enable voice.

Do **not** force ``provider = "doubao"`` on save — the user's selection
is preserved. Grok UI shows a Mandarin unvalidated warning (product
language gate); non-``wss://`` Grok endpoints are rejected by the probe.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from typing import Any
from urllib.parse import urlparse

from ..config import (
    clear_verified,
    config_path,
    credentials_signature,
    default_config,
    is_complete,
    load_config,
    mark_verified,
    save_config,
)
from .probe import probe_credentials_for_config

# Product language gate — shown whenever provider=grok is selected.
_GROK_TITLE = "Grok STT (language support: validate before relying on 中文)"
_GROK_MANDARIN_WARN = (
    "Warning: Mandarin (中文) support is unvalidated — do not rely on Grok "
    "for Chinese dictation until release notes confirm it."
)
_PROVIDERS = ("doubao", "grok")


def _prompt(label: str, default: str = "", *, secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    sys.stderr.write(f"{label}{suffix}: ")
    sys.stderr.flush()
    if secret:
        try:
            val = getpass.getpass("")
        except (EOFError, KeyboardInterrupt):
            return default
    else:
        try:
            val = sys.stdin.readline().rstrip("\n")
        except (EOFError, KeyboardInterrupt):
            return default
    return val.strip() or default


def _grok_endpoint_scheme_ok(endpoint: str) -> bool:
    """True if endpoint uses wss:// (or empty — defaults apply later)."""
    ep = (endpoint or "").strip()
    if not ep:
        return True
    scheme = (urlparse(ep).scheme or "").lower()
    return scheme == "wss"


def run_cli(probe: bool = True) -> int:
    cfg = load_config()
    prior_signature = credentials_signature(cfg)
    current_provider = cfg.get("provider") or "doubao"
    if current_provider not in _PROVIDERS:
        current_provider = "doubao"

    sys.stderr.write(
        "Spitch — configure realtime ASR\n"
        "Press <Enter> to keep the value in [brackets].\n\n"
    )
    provider = _prompt("Provider (doubao/grok)", current_provider).lower()
    if provider not in _PROVIDERS:
        sys.stderr.write(
            f"Unknown provider {provider!r} — choose 'doubao' or 'grok'.\n"
        )
        return 1
    cfg["provider"] = provider

    if provider == "grok":
        sys.stderr.write(f"\n{_GROK_TITLE}\n{_GROK_MANDARIN_WARN}\n\n")
        g = dict(cfg.get("grok") or {}) if isinstance(cfg.get("grok"), dict) else {}
        defaults = default_config()["grok"]
        g["api_key"] = _prompt(
            "Grok API key (Bearer)",
            g.get("api_key", ""),
            secret=True,
        )
        g["endpoint"] = _prompt(
            "WS endpoint (wss:// only)",
            g.get("endpoint", defaults.get("endpoint", "wss://api.x.ai/v1/stt")),
        )
        g["language"] = _prompt(
            "Language (optional, empty = omit)",
            g.get("language", "") or "",
        )
        # Keep other grok options at prior/default values.
        for key, default_val in defaults.items():
            g.setdefault(key, default_val)
        cfg["grok"] = g

        # Defer non-wss handling until after completeness check so an
        # incomplete form cannot clobber a prior good verified config.
    else:
        d = dict(cfg.get("doubao") or {}) if isinstance(cfg.get("doubao"), dict) else {}
        d["app_key"] = _prompt("X-Api-App-Key", d.get("app_key", ""))
        d["access_key"] = _prompt(
            "X-Api-Access-Key", d.get("access_key", ""), secret=True
        )
        d["resource_id"] = _prompt(
            "Resource ID", d.get("resource_id", "volc.bigasr.sauc.duration")
        )
        d["endpoint"] = _prompt(
            "WS endpoint",
            d.get(
                "endpoint",
                "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel",
            ),
        )
        cfg["doubao"] = d

    if credentials_signature(cfg) != prior_signature:
        cfg = clear_verified(cfg)

    if not is_complete(cfg):
        sys.stderr.write("\nIncomplete config — keys are required. Aborting.\n")
        return 1

    # Non-wss Grok endpoints: refuse probe/verify, but only persist when the
    # rest of the form is complete (same as probe-fail path). Incomplete forms
    # already aborted above without saving — preserves prior verified configs.
    if provider == "grok":
        g_ep = (cfg.get("grok") or {}).get("endpoint", "") if isinstance(
            cfg.get("grok"), dict
        ) else ""
        if not _grok_endpoint_scheme_ok(str(g_ep or "")):
            sys.stderr.write(
                "\nGrok endpoint must use wss:// — Bearer tokens must not "
                "ride cleartext remote endpoints.\n"
                "Saving config without verification — voice mode will stay "
                "disabled until a probe succeeds with a wss:// endpoint.\n"
            )
            cfg = clear_verified(cfg)
            save_config(cfg)
            return 2

    if probe:
        label = "Grok STT" if provider == "grok" else "Doubao"
        sys.stderr.write(f"\nProbing {label} endpoint…\n")
        ok, msg = probe_credentials_for_config(cfg)
        sys.stderr.write(f"  → {msg}\n")
        if not ok:
            cfg = clear_verified(cfg)
            sys.stderr.write(
                "Saving config without verification — voice mode will stay "
                "disabled until a probe succeeds.\n"
            )
            save_config(cfg)
            return 2
        cfg = mark_verified(cfg)
    else:
        cfg = clear_verified(cfg)
        sys.stderr.write(
            "Probe skipped (--no-probe); voice mode stays disabled until "
            "you re-run spitch-config and the probe succeeds.\n"
        )

    path = save_config(cfg)
    sys.stderr.write(f"\nSaved {path}\n")
    return 0


def run_gtk() -> int:  # pragma: no cover - GUI is exercised manually
    import gi  # type: ignore
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, GLib  # type: ignore

    cfg = load_config()
    d = dict(cfg.get("doubao") or {}) if isinstance(cfg.get("doubao"), dict) else {}
    g = dict(cfg.get("grok") or {}) if isinstance(cfg.get("grok"), dict) else {}
    h = dict(cfg.get("hotkey") or {}) if isinstance(cfg.get("hotkey"), dict) else {}
    a = dict(cfg.get("audio") or {}) if isinstance(cfg.get("audio"), dict) else {}
    grok_defaults = default_config()["grok"]

    initial_provider = cfg.get("provider") or "doubao"
    if initial_provider not in _PROVIDERS:
        initial_provider = "doubao"

    win = Gtk.Window(title="Spitch — Configure")
    win.set_default_size(560, 480)
    win.set_border_width(16)

    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    win.add(outer)

    # Provider row
    provider_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    provider_row.pack_start(Gtk.Label(label="Provider", xalign=0.0), False, False, 0)
    provider_combo = Gtk.ComboBoxText()
    for p in _PROVIDERS:
        provider_combo.append_text(p)
    provider_combo.set_active(0 if initial_provider == "doubao" else 1)
    provider_combo.set_hexpand(True)
    provider_row.pack_start(provider_combo, True, True, 0)
    outer.pack_start(provider_row, False, False, 0)

    # Grok Mandarin warning
    grok_warn = Gtk.Label(label="", xalign=0.0)
    grok_warn.set_line_wrap(True)
    grok_warn.set_markup(
        f"<span foreground='#a67c00'>{GLib.markup_escape_text(_GROK_TITLE)}</span>\n"
        f"<span foreground='#a67c00' size='small'>"
        f"{GLib.markup_escape_text(_GROK_MANDARIN_WARN)}</span>"
    )
    outer.pack_start(grok_warn, False, False, 0)

    def make_labeled_entry(
        parent: Any,
        label_text: str,
        entry_text: str = "",
        *,
        is_password: bool = False,
    ):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        label = Gtk.Label(label=label_text, xalign=0.0)
        label.set_size_request(160, -1)
        entry = Gtk.Entry()
        entry.set_text(entry_text)
        if is_password:
            entry.set_visibility(False)
        entry.set_hexpand(True)
        row.pack_start(label, False, False, 0)
        row.pack_start(entry, True, True, 0)
        parent.pack_start(row, False, False, 0)
        return entry

    # Doubao field group
    doubao_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    e_app = make_labeled_entry(doubao_box, "X-Api-App-Key", d.get("app_key", ""))
    e_access = make_labeled_entry(
        doubao_box, "X-Api-Access-Key", d.get("access_key", ""), is_password=True
    )
    e_resource = make_labeled_entry(
        doubao_box,
        "Resource ID",
        d.get("resource_id", "volc.bigasr.sauc.duration"),
    )
    e_d_endpoint = make_labeled_entry(
        doubao_box,
        "WS endpoint",
        d.get(
            "endpoint",
            "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel",
        ),
    )
    outer.pack_start(doubao_box, False, False, 0)

    # Grok field group
    grok_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    e_api_key = make_labeled_entry(
        grok_box, "Grok API key", g.get("api_key", ""), is_password=True
    )
    e_g_endpoint = make_labeled_entry(
        grok_box,
        "WS endpoint (wss://)",
        g.get("endpoint", grok_defaults.get("endpoint", "wss://api.x.ai/v1/stt")),
    )
    e_language = make_labeled_entry(
        grok_box, "Language (optional)", g.get("language", "") or ""
    )
    outer.pack_start(grok_box, False, False, 0)

    # Shared fields
    shared_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    e_rate = make_labeled_entry(
        shared_box, "Audio sample rate", str(a.get("sample_rate", 16000))
    )
    e_talk = make_labeled_entry(
        shared_box, "Push-to-talk key", h.get("talk_key", "Ctrl+Alt")
    )
    outer.pack_start(shared_box, False, False, 0)

    status = Gtk.Label(label="", xalign=0.0)
    status.set_line_wrap(True)
    outer.pack_start(status, False, False, 0)

    btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    btn_test = Gtk.Button(label="Test connection")
    btn_save = Gtk.Button(label="Save")
    btn_close = Gtk.Button(label="Close")
    btn_row.pack_start(btn_test, True, True, 0)
    btn_row.pack_start(btn_save, True, True, 0)
    btn_row.pack_start(btn_close, True, True, 0)
    outer.pack_start(btn_row, False, False, 0)

    def selected_provider() -> str:
        text = provider_combo.get_active_text() or "doubao"
        return text if text in _PROVIDERS else "doubao"

    def apply_provider_visibility(_combo=None) -> None:
        is_grok = selected_provider() == "grok"
        doubao_box.set_visible(not is_grok)
        grok_box.set_visible(is_grok)
        grok_warn.set_visible(is_grok)
        win.set_title(
            "Spitch — Configure Grok STT" if is_grok else "Spitch — Configure Doubao"
        )

    def _section_dict(raw: Any, fallback: dict | None = None) -> dict:
        """KD-22: only copy dict/Mapping sections; non-mappings use fallback."""
        from collections.abc import Mapping as AbcMapping

        if isinstance(raw, AbcMapping) and not isinstance(raw, (str, bytes)):
            return dict(raw)
        return dict(fallback or {})

    def collect() -> dict[str, Any]:
        new_cfg = default_config()
        new_cfg.update(cfg)
        provider = selected_provider()
        new_cfg["provider"] = provider
        # Preserve both credential sections; update the active one.
        # Mapping guards (KD-22): corrupted on-disk sections must not crash.
        new_cfg["doubao"] = _section_dict(
            new_cfg.get("doubao"), default_config()["doubao"]
        )
        new_cfg["grok"] = _section_dict(
            new_cfg.get("grok"), default_config()["grok"]
        )
        if provider == "doubao":
            new_cfg["doubao"] = {
                "app_key": e_app.get_text().strip(),
                "access_key": e_access.get_text().strip(),
                "resource_id": e_resource.get_text().strip(),
                "endpoint": e_d_endpoint.get_text().strip(),
            }
        else:
            new_cfg["grok"] = {
                **dict(grok_defaults),
                **_section_dict(new_cfg.get("grok"), grok_defaults),
                "api_key": e_api_key.get_text().strip(),
                "endpoint": e_g_endpoint.get_text().strip(),
                "language": e_language.get_text().strip(),
            }
        try:
            new_cfg["audio"] = _section_dict(
                new_cfg.get("audio"), default_config()["audio"]
            )
            new_cfg["audio"]["sample_rate"] = int(
                e_rate.get_text().strip() or "16000"
            )
        except ValueError:
            new_cfg["audio"] = _section_dict(
                new_cfg.get("audio"), default_config()["audio"]
            )
            new_cfg["audio"]["sample_rate"] = 16000
        new_cfg["hotkey"] = _section_dict(
            new_cfg.get("hotkey"), default_config()["hotkey"]
        )
        new_cfg["hotkey"]["talk_key"] = e_talk.get_text().strip() or "Ctrl+Alt"
        return new_cfg

    def set_status(msg: str, ok: bool | None = None) -> None:
        ctx = status.get_style_context()
        ctx.remove_class("success")
        ctx.remove_class("error")
        if ok is True:
            ctx.add_class("success")
            status.set_markup(
                f"<span foreground='#3b8632'>{GLib.markup_escape_text(msg)}</span>"
            )
        elif ok is False:
            ctx.add_class("error")
            status.set_markup(
                f"<span foreground='#b00020'>{GLib.markup_escape_text(msg)}</span>"
            )
        else:
            status.set_text(msg)

    # last_probe_ok["sig"] records the credentials signature that the
    # most recent successful probe verified. If the user edits the
    # entries after a successful probe, the saved config's signature
    # will not match and we treat the cached probe as stale.
    last_probe_ok = {"ok": False, "sig": None}

    def on_test(_btn):
        new_cfg = collect()
        if not is_complete(new_cfg):
            if selected_provider() == "grok":
                set_status(
                    "Fill in api_key + endpoint (wss://) first.", ok=False
                )
            else:
                set_status(
                    "Fill in app_key + access_key + endpoint first.", ok=False
                )
            return
        if selected_provider() == "grok":
            ep = (new_cfg.get("grok") or {}).get("endpoint", "")
            if not _grok_endpoint_scheme_ok(ep):
                set_status(
                    "Grok endpoint must use wss:// — cleartext ws:// remote "
                    "endpoints are rejected.",
                    ok=False,
                )
                last_probe_ok["ok"] = False
                last_probe_ok["sig"] = None
                return
        label = "Grok STT" if selected_provider() == "grok" else "Doubao"
        set_status(f"Probing {label}…")
        win.set_sensitive(False)
        sig = credentials_signature(new_cfg)

        def worker():
            try:
                ok, msg = probe_credentials_for_config(new_cfg)
            except Exception as exc:
                # If probe itself blows up (e.g., asyncio internals),
                # don't let the thread die silently — the UI was set
                # insensitive by on_test and we'd leave it stuck.
                ok, msg = False, f"Probe crashed: {exc!r}"

            def done():
                last_probe_ok["ok"] = ok
                last_probe_ok["sig"] = sig if ok else None
                set_status(msg, ok=ok)
                win.set_sensitive(True)
                return False

            GLib.idle_add(done)

        import threading

        threading.Thread(target=worker, daemon=True).start()

    def on_save(_btn):
        new_cfg = collect()
        if not is_complete(new_cfg):
            if selected_provider() == "grok":
                set_status(
                    "Cannot save: api_key and endpoint are required.", ok=False
                )
            else:
                set_status(
                    "Cannot save: app_key, access_key, and endpoint are required.",
                    ok=False,
                )
            return
        if selected_provider() == "grok":
            ep = (new_cfg.get("grok") or {}).get("endpoint", "")
            if not _grok_endpoint_scheme_ok(ep):
                new_cfg = clear_verified(new_cfg)
                path = save_config(new_cfg)
                set_status(
                    f"Saved → {path}\nGrok endpoint must use wss:// — "
                    "voice mode stays disabled (endpoint rejected).",
                    ok=False,
                )
                return
        sig = credentials_signature(new_cfg)
        verified_now = last_probe_ok["ok"] and last_probe_ok["sig"] == sig
        if verified_now:
            new_cfg = mark_verified(new_cfg)
        else:
            new_cfg = clear_verified(new_cfg)
        path = save_config(new_cfg)
        if verified_now:
            set_status(
                f"Saved → {path}\nVerified — voice mode is enabled.",
                ok=True,
            )
        else:
            note = (
                "Saved → {p}\nVoice mode stays disabled until ‘Test connection’ "
                "succeeds with the current values."
            ).format(p=path)
            set_status(note, ok=False)

    def on_close(_btn):
        Gtk.main_quit()

    provider_combo.connect("changed", apply_provider_visibility)
    btn_test.connect("clicked", on_test)
    btn_save.connect("clicked", on_save)
    btn_close.connect("clicked", on_close)
    win.connect("destroy", lambda _w: Gtk.main_quit())

    win.show_all()
    apply_provider_visibility()
    Gtk.main()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="spitch-config", description="Configure Spitch"
    )
    parser.add_argument(
        "--cli", action="store_true", help="force CLI mode (no GTK)"
    )
    parser.add_argument(
        "--no-probe", action="store_true", help="skip auth probe"
    )
    parser.add_argument(
        "--print-path", action="store_true", help="print config path and exit"
    )
    args = parser.parse_args(argv)
    if args.print_path:
        print(config_path())
        return 0
    if args.cli:
        return run_cli(probe=not args.no_probe)
    try:
        import gi  # noqa: F401
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # type: ignore  # noqa: F401
    except Exception:
        sys.stderr.write("(GTK unavailable — falling back to CLI mode)\n")
        return run_cli(probe=not args.no_probe)
    return run_gtk()


if __name__ == "__main__":
    sys.exit(main())
