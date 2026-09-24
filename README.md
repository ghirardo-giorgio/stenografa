# Stenografa

Voice dictation for Linux that types for you: hit a keyboard shortcut (or a
button on your phone), speak, and the transcript is pasted straight into
whatever window has focus. Built on
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) with hands-off
GPU/CPU handling, pairs with a Flutter phone app over your local network, and
can be driven by any MCP client.

> **Status:** Linux is the reference implementation — implemented, verified and
> in daily use. Windows and macOS backends are written but **never tested on a
> real machine**. See [Platform support](#platform-support).

## How it works

```
 microphone ─▶ daemon.py ─▶ faster-whisper (GPU, medium/int8-float16)
                  │               │  VAD strips silence before transcribing
                  │               ▼
                  │         clipboard (wl-copy)
                  │               │
                  │               ▼
                  │         auto-paste into the focused window
                  │         (Ctrl+Shift+V in terminals) + desktop notification
                  │
 phone app (Flutter) ◀── TCP :8765, optional TLS ──▶ the same daemon
 MCP client ◀── stdio ──▶ mcp_server.py ── unix socket ──▶ daemon
```

The daemon is headless: feedback comes from desktop notifications and the phone
app. An optional GNOME Shell extension draws a non-intrusive "recording" pill
on screen while the microphone is open.

## Highlights

- **Toggle dictation** from a GNOME keyboard shortcut, the phone, or by voice.
  The transcript goes to the clipboard and is pasted automatically — with
  `Ctrl+Shift+V` instead of `Ctrl+V` when the focused window is a terminal.
- **GPU-friendly**: the model loads into VRAM *while you are already speaking*
  and unloads after 5 minutes of idleness. If the GPU is busy or absent it
  falls back to the CPU automatically and says so with a notification.
- **Voice activation** ("jarvis" / "jarvis stop", fully user-configurable) via
  a tiny CPU-resident Whisper listener that never touches VRAM.
- **AI voice commands**: dictate "copy" and the configured LLM turns it into
  `ctrl+c`. Ambiguous commands open a choice panel on the phone instead of
  guessing; the interpreter can also launch apps and compose multi-step
  macros, always scoped to the app each dashboard is about.
- **Phone dashboards**: a fully editable grid of buttons — shortcuts, macros,
  text snippets, app launchers, mic, AI command — with push-to-talk, dictation
  history, per-app auto-switching and phone-side wake-word listening.
- **MCP server**: an MCP client (Claude Code, Claude Desktop, …) can compose
  dashboards and change settings from natural-language requests.
- **Private by construction**: dictation history lives in memory only, TLS
  with certificate pinning is one switch away, and neither the phone nor the
  LLM can ever make the daemon execute something arbitrary.

## Repository layout

| File | What it is |
|---|---|
| `daemon.py` | The headless daemon: recording, transcription, pasting, TCP server for the phone app, local control socket, wake-word listener, AI-command engine, media controls. |
| `platform_backend.py` | OS abstraction layer (audio, clipboard, key simulation, notifications, focused-window detection, app enumeration) behind a common `Backend` interface. Linux implemented and verified; Windows/macOS written but untested. |
| `llm_provider.py` | Swappable LLM backends — LM Studio, Ollama, OpenAI, Anthropic, Claude Code CLI — shared by AI voice commands and translation. |
| `mcp_server.py` | MCP (stdio) server exposing dashboard management and daemon settings as tools. |
| `setup_llm.py` | Interactive setup and verification of the LLM backend (`~/.config/stenografa/llm.json`). |
| `key_combo.py` | Cross-platform parsing/validation of key combos (`ctrl+c`, `alt+tab`, …). |
| `toggle.py` | Tiny client that starts/stops dictation over the daemon's unix socket — bind it to a keyboard shortcut. |
| `tests/` | pytest suite over the daemon's pure logic (layout validation, config, AI commands, TLS detection, wake word, …) using fake backends — no audio/GPU/network required. |

The phone app, **[RecordAndPaste](https://github.com/ghirardo-giorgio/RecordAndPaste)**
(Flutter), lives in its own repository and connects to the daemon over TCP
port 8765.

## Requirements (Linux)

- Python **3.10+** (developed and tested on 3.12).
- System commands (package names from Debian/Ubuntu — adjust for your distro):

  | Command | Package | Used for |
  |---|---|---|
  | `pw-record` | pipewire | microphone capture (mono, 16 kHz) |
  | `wl-copy`, `wl-paste` | wl-clipboard | clipboard |
  | `ydotool`, `ydotoold` | ydotool | key simulation — the daemon spawns its own private `ydotoold` instance, but the user needs write access to `/dev/uinput` (typically the `input` group) |
  | `notify-send` | libnotify-bin | desktop notifications |
  | `gdbus`, `gio` | libglib2.0-bin | MPRIS media players, focused-window detection (GNOME D-Bus), app launching |
  | `pactl` | pulseaudio-utils | per-stream muting of unpausable players (works against PipeWire) |
  | `openssl` | openssl | self-signed TLS certificate generation |
  | `magick` *(optional)* | imagemagick | SVG→PNG conversion for app icons (skipped gracefully if missing) |

- Optional GNOME Shell extensions:
  - **Window Calls** (`window-calls@domandoman.xyz`) — enables "follow the
    focused app" dashboard switching and terminal detection for adaptive paste.
  - **stenografa-overlay** — the companion extension that draws the on-screen
    recording pill (not included in this repository).

A CUDA-capable NVIDIA GPU is **optional**: without one the daemon falls back to
the CPU on its own (same model, slower transcription).

## Installation

```bash
git clone https://github.com/ghirardo-giorgio/stenografa.git
cd stenografa
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

For GPU transcription you need the NVIDIA driver plus a CUDA/cuDNN runtime
matching your `ctranslate2` build — see the
[faster-whisper documentation](https://github.com/SYSTRAN/faster-whisper) for
the exact requirements. Without them everything still works on CPU.

### First run

```bash
./daemon.py     # starts listening; refuses to start twice (lock file)
./toggle.py     # starts/stops a dictation
```

Everything lives under `~/.config/stenografa/`:

| File | Purpose |
|---|---|
| `config.json` | phone-app token and hot-reloadable settings (language, TLS, wake word, …) |
| `layout.json` | phone-app dashboards (older single-grid layouts are migrated automatically) |
| `llm.json` | LLM backend choice and credentials — created by `setup_llm.py`, written with mode 0600 |
| `cert.pem`, `key.pem` | self-signed TLS certificate, generated on first start |

To launch it at login, drop an autostart entry:

```ini
# ~/.config/autostart/stenografa.desktop
[Desktop Entry]
Type=Application
Name=Stenografa
Exec=/absolute/path/to/stenografa/daemon.py
X-GNOME-Autostart-enabled=true
```

To stop the daemon: `pkill -f daemon.py`, or send `quit` to its control socket.
(Autostart relaunches it at the next login; the lock file guarantees two
instances never run together.)

### Keyboard shortcut (GNOME)

1. **Settings → Keyboard → Custom shortcuts → "+"**
2. Name: `Stenografa toggle`
3. Command: `/absolute/path/to/stenografa/toggle.py`

Press the shortcut to start recording, press it again to stop: the transcript
lands in the clipboard, gets pasted, and a notification previews it.

## Phone app (RecordAndPaste)

A Flutter app ([separate repository](https://github.com/ghirardo-giorgio/RecordAndPaste))
that connects to the daemon over your local
Wi-Fi: enter the PC's IP, the port (8765) and the token from
`~/.config/stenografa/config.json` (delete the file and restart the daemon to
regenerate it). The UI is available in Italian and English.

Each dashboard is a button grid; swipe horizontally to switch, or let the phone
switch on its own when the app a dashboard matches gains focus on the PC
(requires the Window Calls extension). Seven button kinds:

| Kind | Action |
|---|---|
| `record` | Start/stop dictation and paste the transcript. One shared recording across all mic buttons; at least one must always exist. Optional `auto_enter` presses Enter right after pasting — ideal for chats. |
| `ai_command` | Like `record`, but the transcript is interpreted by the configured LLM into key combos to execute. |
| `keys` | Simulates one fixed key combo. |
| `macro` | Runs up to 20 key combos in sequence, with a configurable pause between steps. |
| `text` | Pastes a fixed snippet (up to 5000 chars) through the same clipboard/paste pipeline. |
| `launch` | Launches an installed app (picked from a searchable list; the id is re-validated on every press). |
| `paste_last` | Re-pastes the last dictation — for when the text landed in the wrong window. |

Edit mode (pencil icon): add buttons by tapping empty cells, drag to move or
swap, long-press to restyle (colors and ~80 Material icons), resize the grid,
rename/duplicate/reorder/delete dashboards, per-dashboard settings.

Extras: **push-to-talk** (hold to record), **vibration** feedback, session
**history** (re-paste any recent dictation into the current window),
**wake-word listening from the phone**, and automatic **on-phone transcription**
when the PC model falls back to CPU. A recording left open by accident is
stopped by the daemon after 3 minutes (`RECORDING_MAX_DURATION_SECONDS`), and
an optional silence timeout closes idle dictations on its own.

## Voice activation

Enabled with `wake_word_enabled`. Two phrases — defaults `jarvis` and
`jarvis stop` — start and stop dictation without touching anything:

- Recognition runs a second Whisper `tiny` model in int8 **on CPU** (about
  0.08 s of work per second of listening on the reference machine): it never
  touches VRAM and cannot interfere with the dictation model or its GPU
  unload.
- Phrases are passed to Whisper as an `initial_prompt` — without it, an
  invented word like "jarvis" gets transcribed as whatever the language
  suggests and would never match.
- Matching tolerates transcription errors (up to roughly a quarter of the
  phrase's letters wrong), and each phrase can list comma-separated variants
  for how the recognizer actually hears it.
- The same two phrases can be listened for **from the phone** (independent
  switch, system speech recognizer, app in foreground) — useful when you are
  away from the PC's microphone. Both listeners can run at once.
- The phrases are stripped from the transcript before pasting, and the
  optional GNOME extension shows a pill while the mic is open — it never takes
  pointer or keyboard focus, so pasting lands in the right window.

## AI voice commands

Speak into an `ai_command` button and the configured LLM returns the key
combinations to execute. Design choices that keep it predictable:

- **Ambiguity is never guessed.** If a phrase matches several actions ("copy"
  → `ctrl+c`, `ctrl+shift+c`, "duplicate"), the daemon proposes the candidates
  in a panel on the phone; nothing runs until you tap one, and the panel
  expires after 90 s without doing anything.
- **Dashboard shortcuts steer the interpreter.** Key-combo buttons in the same
  dashboard act as vocabulary for that app — dictating "invoke" runs the
  dashboard's `ctrl+enter` rather than a generic reading. Long shortcut lists
  can be saved invisibly (`set_dashboard_shortcuts`) so they count for the AI
  without cluttering the grid.
- **App launching resolves real apps.** The model names an app; the daemon
  matches it against the actually-installed ones (the model never sees the
  list and cannot invent ids). Ambiguous matches go to the choice panel; exact
  matches win.
- **Voice-generated macros** handle multi-step tasks, but every step must pass
  the same validation as a `macro` button and is constrained to the current
  dashboard's application — one invented keystroke discards the whole macro
  instead of half-running it.
- **From a panel on the PC itself** (e.g. a Quickshell widget), the same
  engine can also propose shell commands — but they are refused unless the
  panel explicitly confirms (`confirm_shell`), and caps apply (8 commands,
  500 chars each, 120 s timeout). The user always reads the command before it
  runs.

## LLM backends

All LLM features (AI voice commands, translation) go through a single provider
layer. Five backends:

| Backend | API key | Notes |
|---|---|---|
| **LM Studio** | no | Local; uses the already-loaded model (`model: "auto"`). |
| **Ollama** | no | Local; needs a pulled model (`ollama pull`). |
| **OpenAI** | yes | Cloud. |
| **Anthropic** | yes | Cloud; without an explicit key the SDK falls back to `ANTHROPIC_API_KEY` / `ant auth login`. |
| **Claude Code CLI** | no | Uses the already-authenticated CLI; slower (spawns an agent per call). |

```bash
./setup_llm.py            # interactive menu → ~/.config/stenografa/llm.json
./setup_llm.py --show     # show the current configuration
./setup_llm.py --check    # test the selected backend
```

The file stores the parameters of **all** backends plus a `provider` field:
switching backend is a one-word edit and takes effect immediately (the daemon
re-reads the file on every call). Cloud backends can read their key from an
environment variable (`api_key_env`) instead of disk. Local backends answer in
a few hundred milliseconds — the right choice for voice commands; cloud
backends interpret ambiguous commands better.

## Controlling it with MCP

`mcp_server.py` is an MCP stdio server exposing around thirty tools for
dashboard management and daemon settings, so an MCP client can do everything
from natural language ("create a dashboard with the InvokeAI shortcuts"). It
never talks to the phone directly: it goes through the daemon's user-only
control socket, and changes propagate live to connected phones.

```bash
claude mcp add stenografa -- python3 /absolute/path/to/stenografa/mcp_server.py
```

Tool groups:

- **Dashboards**: `list_dashboards`, `create_dashboard` (atomic create +
  populate), `duplicate_dashboard`, `rename_dashboard`, `reorder_dashboard`,
  `remove_dashboard`, `set_grid_size`, `reset_layout`.
- **Buttons**: `add_button`, `add_buttons` (atomic batch), `edit_button`,
  `set_button_style`, `move_button`, `remove_button`.
- **Per-dashboard context**: `set_dashboard_match` (follow-the-focused-app
  patterns), `set_dashboard_shortcuts` (hidden AI-command vocabulary),
  `set_dashboard_vocabulary` (per-dashboard dictation vocabulary).
- **Settings**: `get_config`, `set_language`, `set_translate_enabled/target/engine`,
  `set_vocabulary`, `set_confirm_before_paste`, `set_require_tls`,
  `set_wake_word_enabled`, `set_wake_phrase_start/stop`, `set_notifications`,
  `set_pause_media_while_recording`, `set_restore_clipboard`,
  `set_silence_timeout`.
- **Apps**: `list_launchable_apps`, `launch_app` — every id is validated
  against the freshly enumerated list of installed apps, so an MCP client can
  only *select* an app, never run an arbitrary path or command.
- **Maintenance**: `restart_daemon` (same process, re-exec).

The docstrings in `mcp_server.py` are the complete reference for every
parameter and constraint.

## Configuration reference

Every key below lives in `config.json` and is hot-reloadable — from the phone
app's settings or via MCP, with no daemon restart:

| Key | Default | Meaning |
|---|---|---|
| `language` | `it` | Dictation language: ISO 639-1 code or `auto` (Whisper supports ~100 languages). |
| `restore_clipboard` | off | Restore the pre-dictation clipboard right after the auto-paste (only if the paste succeeded). |
| `pause_media_while_recording` | off | Pause MPRIS players during dictation and mute whatever cannot be paused (PipeWire), unmute afterwards. |
| `notifications` | `all` | Desktop notification level: `all`, `errors`, or `none` (the phone always receives full state either way). |
| `translate_enabled` / `translate_target` / `translate_engine` | off | Translate before pasting. Engine `whisper` translates to English only; engine `llm` goes through the LLM backend and supports any target language. |
| `vocabulary` | empty | Dictation vocabulary (names, jargon) passed to Whisper as `initial_prompt`; per-dashboard vocabularies add on top. Max 800 chars — a hint, not a constraint. |
| `confirm_before_paste` | off | Show the transcript on the phone for review/approval before pasting. |
| `require_tls` | off | Reject cleartext connections from phones (enable once all your phones support TLS). |
| `silence_timeout` | 10 | Seconds of silence before a toggle-mode dictation closes itself (0 = off, 3–120 allowed). Never applies to push-to-talk. |
| `wake_word_enabled` / `wake_phrase_start` / `wake_phrase_stop` | off / `jarvis` / `jarvis stop` | Voice activation; phrases can list comma-separated variants, minimum 3 characters. |

## GPU, VRAM and CPU fallback

Measured on an RTX 4060 Ti with the `medium` model, `int8_float16`:

| | |
|---|---|
| VRAM for the model | ~1.0 GB (peak ~1.1 GB while transcribing) |
| Model load into VRAM | ~4 s — **while you are already speaking** |
| Transcribing 5 s of audio | ~0.3–0.6 s |

- **Lazy load**: VRAM stays free until you actually dictate; the model loads
  in parallel with the recording start.
- **Auto-unload**: after `MODEL_IDLE_TIMEOUT` (300 s) of inactivity the model
  leaves VRAM (~100 MB of CUDA context remain while the daemon lives).
- **Automatic CPU fallback**: if loading on GPU fails — no card, missing
  driver, or VRAM occupied by a game or another model — the same model runs on
  CPU (`int8`) and a notification explains why dictation just got slower. The
  next idle-unload retries the GPU, in case VRAM freed up meanwhile. The
  active device is reported as `model_device` by `get_config`.
- **Phone transcription fallback**: when the model runs on CPU, the phone app
  notices and dictates with the Android system recognizer instead — instant
  text, at the cost of weaker punctuation and no custom vocabulary, with the
  app in the foreground.
- Tunables at the top of `daemon.py`: `MODEL_NAME` (`small` ≈ 0.5 GB, `base`
  ≈ 0.3 GB, `large-v3` ≈ 3 GB), `MODEL_IDLE_TIMEOUT`,
  `MODEL_DEVICE`/`MODEL_COMPUTE_TYPE` (e.g. `cpu` + `int8` to force CPU), and
  the `MODEL_FALLBACK_*` family.

## Platform support

OS-specific operations are isolated in `platform_backend.py` behind a common
interface, so `daemon.py` is identical everywhere.

| | Linux | Windows | macOS |
|---|---|---|---|
| Status | implemented and verified (the only tested environment) | written, **never tested** | written, **never tested** |
| Audio | `pw-record` (PipeWire) | sounddevice / soundfile | sounddevice / soundfile |
| Clipboard | `wl-copy` / `wl-paste` | pyperclip | pyperclip |
| Key simulation | `ydotool` / `ydotoold` | pynput | pynput (+ manual Accessibility permission) |
| Notifications | `notify-send` | plyer | plyer (fallback `osascript`) |
| Focused window | GNOME "Window Calls" via D-Bus | pywin32 + psutil | AppKit (app name only, not window title) |
| App list / launch | `.desktop` files + `gio launch` | PowerShell `Get-StartApps` + `explorer.exe shell:AppsFolder\` | `.app` bundles + `open` |
| Media pause/mute | MPRIS + PipeWire per-stream mute | not implemented | not implemented |
| Extra Python deps | none | `requirements-windows.txt` (+ system VC++ redistributable for ctranslate2) | `requirements-macos.txt` |

Known Windows/macOS caveats, to verify on first contact with a real machine:
macOS silently fails key simulation until you grant the Python interpreter the
**Accessibility** permission manually; on Windows, `launch_app` treats
`explorer.exe` exit codes as unreliable (documented in the code), and the
on-screen recording pill does not exist outside GNOME/Wayland.

## Security and privacy

- Phone authentication uses a 5-digit numeric token (easy to type by hand),
  protected by rate limiting: 5 failed attempts per IP every 60 seconds.
- **TLS on the same port**: the daemon generates a self-signed certificate on
  first start (via `openssl`) and serves both cleartext and TLS on port 8765,
  distinguishing them from the first bytes of each connection. The phone pins
  the certificate fingerprint on first connect (trust on first use) and
  refuses any later change. Once every phone is updated, enable
  `require_tls` to reject cleartext.
- **Dictation history never touches disk**: the last 20 dictations live in
  RAM only and are wiped on daemon restart. The phone receives 300-character
  previews and re-pastes by id, so the full text never travels the network.
- **Closed vocabularies everywhere**: the AI-choice panel accepts only options
  the daemon itself proposed; `launch_app` validates ids against the current
  app list; the LLM can name apps but never construct ids; shell commands from
  the PC panel require an explicit `confirm_shell` and are bounded (count,
  length, timeout).
- `llm.json` is written with mode 0600, and `api_key_env` keeps API keys out
  of the config file entirely.

## Tests

```bash
python3 -m pytest
```

The suite exercises pure logic with fake audio/GPU/network backends: layout
and button-kind validation, per-dashboard settings migration, config handling
(language, vocabulary, clipboard restore), AI voice commands (combos, app
launching, macros), phone-side transcription, silence timeout, wake-word
matching, TLS-vs-cleartext protocol detection, notifications, history
re-paste, and more.

## Documentation

- **`mcp_server.py` docstrings** — exhaustive reference for every MCP tool
  (parameters, constraints, validation rules).

## Project status

- **Linux**: the reference platform — implemented, verified, in daily use.
- **Windows / macOS**: backends written against the documented APIs but never
  exercised on real machines; expect fixes on first contact. Testing through
  Wine/Lutris is not reliable for cross-app pasting and notifications — a real
  machine (or a Windows VM) is needed. There is no macOS virtualization on
  non-Apple hardware.
- **License**: [MIT](LICENSE) — © 2025 Giorgio Ghirardo.