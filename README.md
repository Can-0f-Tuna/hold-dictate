# hold-dictate

Hold **Alt+Space**, speak, let go. The text is typed at your cursor, in any app.

Works on **Windows, macOS and Linux** (Wayland and X11). Handles mixed-language speech, for example Hebrew with English words.

## Install

macOS / Linux (Terminal):

```sh
curl -fsSL https://hold-dictate.vercel.app/install.sh | sh
```

Windows (PowerShell):

```powershell
irm https://hold-dictate.vercel.app/install.ps1 | iex
```

The script installs [uv](https://docs.astral.sh/uv/), the system helpers it needs and the latest release of hold-dictate, starts it (and at every login), then opens the settings page in your browser. Sign in there with Google (a free account, once per computer), then add your API key. Running the script again reinstalls the latest release.

With an AI coding agent (Claude Code, Codex, Cursor, ...): paste the prompt from [install/agent.md](install/agent.md), also at <https://hold-dictate.vercel.app/agent.md>. The agent installs it, walks you through the key, runs the checks and opens a GitHub issue for anything it can't fix.

Manual install: `uv tool install --python 3.13 git+https://github.com/Can-0f-Tuna/hold-dictate`, then `hold-dictate service install` and `hold-dictate settings`.

## Settings

A local page at `http://127.0.0.1:47811` that only this computer can open. Open it from the tray icon next to the clock (Windows), the menu-bar icon (macOS), **hold-dictate settings** in the app menu (Linux), or `hold-dictate settings`. It sets:

- speech service and API key (with a check that the key works)
- the model: Automatic uses the one we recommend for that service; **Choose another** lets you pick one
- the languages you speak (pick as many as you like, for example Hebrew and English), the hotkey (press the keys to record it), and how it works: **hold** to talk (the default), **tap or hold** (a quick tap starts, the next tap stops; holding longer works like hold) or **tap to start and stop**. In the tap modes it can stop by itself after a few seconds of quiet
- when the text is typed: all at once when you finish (the default), or as you speak, into the text box you are in, in steps of a few seconds. Typed words can't be corrected, so it can be a little less accurate. It sends a short request every few seconds, which counts toward your speech service's limits. On Windows and macOS, while the shortcut is held, the text waits until you let go. It is not used with a text hook, or the copy-only and do-nothing app rules
- what happens to music and videos while you dictate: turned down (the default), silenced, or left alone
- sounds, and the recording pill: style (Dots, the default, or the original Lines), colour, position and size. On Windows the pill is off until you turn it on, because it hasn't been tried there yet
- automatic updates
- text clean-up: drop phrases Whisper invents from noise ("Thanks for watching", on by default), remove filler words (um, uh, אה, אממ), turn spoken punctuation into symbols ("comma" / "פסיק", "new line" / "שורה חדשה"), and your own word replacements (a Hebrew word also matches with a joined prefix: "ורסל" → "Vercel" turns "בורסל" into "ב-Vercel")
- pasting: press Enter after pasting, and rules per app (paste without Enter, paste and press Enter, Ctrl+Shift+V, copy only, or do nothing). The page shows the last dictation and which app it went to
- history: how long dictations are kept (see [History](#history)), with search, copy, delete and clear
- health: the same checks as `hold-dictate doctor`, with one fix for each failed item and a **Check again** button. On macOS it names the permission that is missing. It looks only at this computer, so the key is checked as saved; **Check and save** under Transcription tries it with the service
- a text hook (Advanced): a shell command that gets the text on stdin; what it prints is pasted instead, empty output keeps the text, exit code 77 pastes nothing. It has 5 seconds and sees `HOLD_DICTATE_APP` and `HOLD_DICTATE_PROVIDER`

Changes apply immediately; no restart needed. Prefer the terminal? `hold-dictate setup` still works.

## History

Dictations that were pasted are kept in `history.json`, beside `config.toml`: the final text, what was heard, the time and the app. Audio is never kept, and the file never leaves your computer. It is readable only by you on Linux and macOS.

Keep them for 1, 7 (the default), 30, 90 or 365 days, or Off. A new install starts at 7 days. If you used hold-dictate before history existed it stays Off until you turn it on. The newest 2000 entries and 4 MB are kept at most, and the oldest go first.

Off stops saving new dictations and deletes nothing. **Clear all**, or `hold-dictate history clear`, deletes them. Apps with a copy-only or do-nothing rule are never saved, nor is text a hook stops, a paste that failed, or a recording made with an extension's second shortcut. `paste-last` and `copy-last` don't use the file: they work from memory, with history on or off.

## Updates

hold-dictate checks GitHub every 10 minutes and, while you are not dictating, installs a newer release and restarts itself. Turn this off in the settings. When an update is waiting (automatic updates off, or the install failed), the recording pill on Linux shows a small **!** tab; click it to install, or run `hold-dictate update`. Releases are made by pushing a tag that matches `__version__` in `src/hold_dictate/__init__.py`:

```sh
git tag v0.8.3 && git push origin v0.8.3
```

The Release workflow runs the tests and publishes the GitHub release, not yet marked as latest. `gh release edit <tag> --latest` lets installed copies pick it up, within 10 minutes.

## Speech-to-text providers

| Provider | Cost | Notes |
| --- | --- | --- |
| **Groq** (recommended) | free tier | Whisper large-v3, about 1 s. Key: <https://console.groq.com/keys> |
| OpenAI | paid | Key: <https://platform.openai.com/api-keys> |
| Custom | varies | Any server with an OpenAI-style `/audio/transcriptions` endpoint |
| Local | free | Offline via faster-whisper. Install with `hold-dictate offline`, or the button on the settings page |

Getting a Groq key: open the link above, sign in, click **Create API Key**, copy it and paste it into the settings page. The key is stored in your user config file (permissions 600 on Linux and macOS). It leaves your computer only in requests to the provider. Free-tier limits change; see Groq's rate-limit page.

If the cloud request fails and the `local` extra is installed, it falls back to the local model.

## Commands

| Command | Does |
| --- | --- |
| `hold-dictate settings` | Opens the settings page |
| `hold-dictate setup` | Guided setup in the terminal (safe to run again) |
| `hold-dictate doctor` | Checks microphone, keyboard access, paste helpers and API key. `--offline` stays on this computer, `--json` is for scripts |
| `hold-dictate history` | Lists saved dictations. Also `history search WORDS`, `history delete ID`, `history clear` |
| `hold-dictate transcribe FILE.wav` | Transcribes a file |
| `hold-dictate service install\|uninstall\|status` | Start at login |
| `hold-dictate update` | Installs the latest release now |
| `hold-dictate paste-last` | Pastes the last dictation again (bind it to a key in your compositor or desktop) |
| `hold-dictate copy-last` | Copies the last dictation to the clipboard (also in the tray menu) |
| `hold-dictate config` | Prints the config file path |

Settings live in a TOML file: `~/.config/hold-dictate/config.toml` (Linux), `~/Library/Application Support/hold-dictate/config.toml` (macOS), `%APPDATA%\hold-dictate\config.toml` (Windows). Options: `provider`, `api_key`, `base_url`, `model`, `languages` (comma-separated codes such as `"he,en"`; empty means any language), `prompt`, `hotkey`, `hotkey_mode` (`hold`, `auto` or `toggle`), `silence_stop` (seconds, 0 = off), `live_text`, `local_model`, `local_fallback`, `beeps`, `background_audio` (`lower`, `mute` or `off`), `min_hold`, `timeout`, `auto_update`, `desktop_indicator` (the pill on macOS and Windows), `overlay_style`, `overlay_bg`, `overlay_bars`, `overlay_position`, `overlay_size`, `drop_hallucinations`, `remove_fillers`, `filler_words` (comma-separated, empty = built-in), `spoken_punctuation`, `word_overrides` (a table: `{ "get hub" = "GitHub" }`), `history_days` (0 = off), `auto_submit`, `app_rules` (a table: `{ "slack" = "submit", "keepassxc" = "off" }`; values `paste`, `submit`, `shift`, `clipboard`, `off`; a name matches part of the app name), and `post_hook`.

The app is told apart by its window class on X11, Hyprland, Sway and niri, by its name on macOS and its program name on Windows. GNOME and KDE on Wayland do not say which window is focused, so per-app rules do not apply there.

Hotkey format: modifiers (`alt`, `ctrl`, `shift`, `super`) plus one key (`space`, a-z, 0-9, f1-f12), for example `ctrl+alt+d`.

After you let go, the microphone stays on for 200 ms, because the last word often isn't over yet. The stop sound and your music come back at the release itself. A clip with no speech before the release stays silent, whatever comes after it.

## Per-OS notes

- **macOS**: allow Microphone and Accessibility / Input Monitoring when prompted. `alt` is the Option key. The recording pill is on by default: it shows the recording with its live level, the work after it, and problems. It never takes the keyboard, clicks go through it, and it stays out of the Dock and Cmd+Tab. So it can't open the settings as on Linux; a notification says what went wrong. It has no update tab either. Turn it off with **Show the recording pill**, and the beeps stay as you set them.
- **Windows**: Alt+Space normally opens the window menu. hold-dictate hides that press while you dictate. The recording pill is written but has not been tried on real Windows, so it is off: turn on **Show the recording pill** in the settings (`desktop_indicator = true`) to try it. Otherwise you get the beeps.
- **Linux**: the keyboard is read through `/dev/input`, so your user must be in the `input` group. The installer does this; log out and in once. Pasting uses `wl-clipboard` + `wtype` (Wayland) or `xclip` + `xdotool` (X11). GNOME on Wayland does not accept `wtype`; install `ydotool`, or the text is left on the clipboard for you to paste. On Wayland, bind Alt+Space to a no-op in your compositor so a stray space isn't typed (niri: `Alt+Space repeat=false { spawn "true"; }`). A recording pill is shown if [quickshell](https://quickshell.org) is installed; other setups use short beeps. When something goes wrong the pill's bars settle into a flat line and turn red, and a mark appears beside it for a moment: a crossed-out microphone or network sign, or a "!". A click opens the settings, which explain it. Without the pill you get a desktop notification.

## Status

Pre-release. Tested end to end on Linux (Wayland, PipeWire) with a mock provider and the local model. The Windows and macOS code paths are unit-tested and run in CI, but the hotkey hooks, permissions and paste have not yet been tried on real machines of those systems. A live call to Groq or OpenAI has not been tested with this version either. The tray icon (Windows, macOS) and self-update on Windows have not been tried on a real machine yet.

The recording pill on macOS was run on one Intel Mac VM (macOS 26): the app, its hotkey and paste into TextEdit, with the microphone and the speech service faked; the no-microphone error was real. Focus stayed in TextEdit and clicks went through. Apple silicon, several displays, full-screen apps and a real microphone are untested. On Windows the pill has never run on a real machine.

## What is open source

This repository is the app itself, under the MIT license (see [LICENSE](LICENSE)), plus the install scripts in `install/`. It works completely on its own: no account, and nothing is sent anywhere but the speech service you pick. The website is a separate, closed project. Other packages can add to the app through the `hold_dictate.extensions` entry point group; `src/hold_dictate/extensions.py` lists what they can do.

## Develop

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
```
