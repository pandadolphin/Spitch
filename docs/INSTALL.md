# Installing Spitch on Ubuntu 24.04

Spitch ships as a Python source tree with a user-level install
script. There is no compilation step — everything runs from Python and
the system tools `wl-copy` (clipboard) and `/dev/uinput` (key
injection) that ship with stock Ubuntu plus one apt package.

**Product note:** Spitch is a **Chinese voice-input** tool. **Doubao is
the default and recommended Chinese path** (`provider: "doubao"`). An
optional **Grok Streaming STT** backend (`provider: "grok"`) is
available; **Mandarin (中文) support for Grok is unvalidated**. Do not
rely on Grok for Chinese until a live checklist passes and release notes
say otherwise. See [Language gate](#language-gate-grok--mandarin).

## 1. System packages

```bash
sudo apt-get install -y python3-evdev wl-clipboard
```

* `python3-evdev` — read keyboard events from `/dev/input/event*`
  (global hotkey detection) and write to `/dev/uinput` (synthetic
  Ctrl+V).
* `wl-clipboard` — provides `wl-copy` / `wl-paste` for clipboard I/O.

Optional but recommended:

```bash
sudo apt-get install -y libnotify-bin   # adds desktop notifications
sudo apt-get install -y playerctl      # pause MPRIS media while talking
```

`playerctl` is used when `audio.pause_media_on_talk` is true in config
(default `true`): the daemon pauses Playing MPRIS players for the talk
session and resumes them on release/cancel. Without `playerctl`, that
feature is skipped with a log warning.

PyGObject (for the GTK config dialog) is supplied by the system
package `python3-gi` if you want a GUI; otherwise `spitch-config`
falls back to a CLI prompt.

## 2. Install Spitch

From a clone of the repo:

```bash
cd /path/to/Spitch
./scripts/install.sh
```

This installs `~/.local/bin/spitch-daemon` and
`~/.local/bin/spitch-config` launchers. Both point `PYTHONPATH` at
this repo's `src/`, so no `pip install` is required.

If `~/.local/bin` is not on your `PATH`, add it:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
exec bash
```

## 3. Configure credentials (multi-provider)

```bash
spitch-config
```

A small GTK dialog opens (or a CLI fallback if PyGObject is missing).
Choose **Provider**: `doubao` (default) or `grok`.

### 3a. Doubao (default — recommended for Chinese)

| Field            | What it is                                                    |
|------------------|---------------------------------------------------------------|
| X-Api-App-Key    | Volcano Engine BigASR **APP ID** from the console            |
| X-Api-Access-Key | Volcano Engine BigASR **Access Token** from the console      |
| Resource ID      | Default `volc.bigasr.sauc.duration` (BigModel realtime)       |
| WS endpoint      | Default `wss://openspeech.bytedance.com/api/v3/sauc/bigmodel` |
| Audio sample rate| 16000 (recommended)                                           |
| Push-to-talk key | Hold this combo to record. Default `Ctrl+Alt`.                |

The console's "Secret Key" is **not** used by this realtime endpoint —
leave it where it is.

### 3b. Grok Streaming STT (optional)

1. Create an API key in the [xAI console](https://console.x.ai/).
2. In `spitch-config`, set Provider to **`grok`**.
3. Fill:

| Field     | What it is                                              |
|-----------|---------------------------------------------------------|
| API key   | xAI Bearer token (`xai-…`)                              |
| Endpoint  | Default `wss://api.x.ai/v1/stt` (**must** be `wss://`)  |
| Language  | Optional; leave empty unless you know a supported code  |

UI labels warn that **language support for Mandarin is unvalidated**.

### 3c. Probe (required for either provider)

Click **Test connection** (or accept the CLI probe). The dialog opens a
real WebSocket to the selected provider and verifies the handshake:

* **Doubao** — server accepts credentials / session setup.
* **Grok** — probe requires a successful session including
  **`transcript.done`** (silence audio is fine). Non-`wss://` endpoints
  are rejected.

Config is written to `~/.config/spitch/config.json` (chmod 600, atomic
write). Probe **success** stamps `verified_at` + `verified_signature`.
A complete form may still be **saved without** verification when the
probe fails, is skipped (`--no-probe`), or a Grok endpoint is not
`wss://` — in those cases any prior stamp is cleared. Incomplete forms
are not saved. The daemon treats the voice path as incomplete until a
matching probe succeeds. Changing keys or switching providers
invalidates verification until you probe again.

### Secret handling

* Store keys **only** in `~/.config/spitch/config.json` (mode 600,
  atomic write). Never commit API keys, paste them into issues, or log
  them.
* Repo `.gitignore` includes `grok-voice-api.key` and `*.key`. Local
  key files are for manual seeding only — do not `git add` them.
* Example **manual** seed (never automate in production; never commit):

  ```bash
  # Requires grok-voice-api.key already gitignored
  # jq --arg k "$(cat grok-voice-api.key)" \
  #   '.provider="grok" | .grok.api_key=$k' \
  #   ~/.config/spitch/config.json > /tmp/spitch-cfg.json
  # # then probe via spitch-config before relying on the daemon
  ```

* Unit tests use fake keys only (`xai-test-…`); they must not open a
  workspace key path.

### Rollback to Doubao

Set `"provider": "doubao"` (via `spitch-config` or hand-edit) with valid
Doubao credentials, run **Test connection**, and restart the daemon:

```bash
# after spitch-config saves provider=doubao + probe OK
systemctl --user restart spitch.service   # if using systemd
# or: kill the daemon and run spitch-daemon again
```

An older binary that does not understand Grok will treat
`provider=grok` as incomplete until you switch back or upgrade.

### Language gate (Grok / Mandarin)

| Claim | Status |
|-------|--------|
| Grok usable as opt-in backend | Available when probed |
| Grok supports 中文 / Mandarin | **Unvalidated** — language gate **closed** |
| Recommended Chinese path | **Doubao** (default) |

No in-repo live EN/ZH checklist pass has been recorded. Do not market
Grok as a Chinese provider until that checklist passes and release notes
say so.

### Finalize wait (after release)

`inject.final_wait_seconds` (default **30.0**) is the **stream budget**
after the session enters FINALIZING (after release linger). The
controller wait is `max(final_wait_seconds, 5.0)`; the inject queue wait
is slightly longer by linger + slack so a late `on_final` is not dropped.
This applies to both providers.

## 4. Grant `/dev/input` read access

The daemon listens for the global hotkey by reading
`/dev/input/event*`. On stock Ubuntu these are `root:input 0660`, so
your user needs to be in the `input` group:

```bash
sudo usermod -aG input $USER
```

Log out and back in for the new group membership to take effect. To
test without logging out, run the daemon under `sg` once:

```bash
sg input -c 'spitch-daemon'
```

## 5. Run the daemon

```bash
spitch-daemon &
```

You should see a "Spitch ready" desktop notification. Test it:

* Focus any text input (browser address bar, gedit, terminal, Feishu,
  VS Code, Slack, …).
* Hold the configured talk key (default Ctrl+Alt — both modifiers,
  no third key). A "🎙 Spitch listening…" notification appears.
* Speak. Release. A "✍ Spitch finalizing…" notification appears
  briefly, then the punctuated final transcript is pasted into the
  focused field.

If you press a third key during the chord (e.g. Ctrl+Alt+T to launch
a terminal), the recording is automatically cancelled and the
shortcut passes through normally.

## 6. (Optional) auto-start at login

Create a systemd user unit at `~/.config/systemd/user/spitch.service`:

```ini
[Unit]
Description=Spitch voice-input daemon
After=graphical-session.target

[Service]
ExecStart=%h/.local/bin/spitch-daemon
Restart=on-failure
RestartSec=2

[Install]
WantedBy=graphical-session.target
```

Then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now spitch.service
journalctl --user -u spitch.service -f    # tail logs
```

## Troubleshooting

* **"no readable keyboard devices found"** when launching the daemon
  → your user is not in the `input` group, or the new membership has
  not taken effect. Verify with `id | grep input`; if missing, run
  the `usermod` from step 4 and log out / back in.
* **Pasting fails (focused app shows nothing)** → check
  `/dev/uinput` is writable: `getfacl /dev/uinput | grep $USER`.
  On Ubuntu 24.04 logind sets the ACL automatically; on other
  distros you may need a udev rule.
* **Hotkey does nothing** → tail
  `${XDG_STATE_HOME:-$HOME/.local/state}/spitch/daemon.log`. If you
  see no key events at all when pressing Ctrl+Alt, the daemon is not
  reading the right device — confirm with
  `cat /proc/bus/input/devices` that your keyboard is enumerated.
* **Clipboard contents are surprising afterwards** → Spitch saves
  and restores the clipboard around each paste, with a 0.3 s
  settle delay. If your focused app is slow to consume the paste,
  the saved clipboard may overwrite Spitch's text. Increase
  `time.sleep(0.3)` in `src/spitch/inject/text_injector.py`.
* **Grok probe fails / non-wss endpoint** → endpoint must start with
  `wss://`. Check key validity in the xAI console; failure messages
  should not echo the full API key.
* **Switched provider but daemon refuses config** → re-run
  `spitch-config` and complete **Test connection** so
  `verified_signature` matches the active provider credentials.
  Legacy unsigned `verified_at` stamps authorize **Doubao only**.

## Uninstall

```bash
./scripts/uninstall.sh
```

Removes the launcher scripts and any systemd unit. Does **not** remove
`~/.config/spitch/` (your saved credentials).
