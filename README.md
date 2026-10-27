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

The script installs [uv](https://docs.astral.sh/uv/), the system helpers it needs and hold-dictate, then runs `hold-dictate setup`. Setup asks for a provider, your API key, your language and the hotkey, and starts it at login.

Manual install: `uv tool install git+https://github.com/Can-0f-Tuna/hold-dictate`, then `hold-dictate setup`.

## Speech-to-text providers

| Provider | Cost | Notes |
| --- | --- | --- |
| **Groq** (recommended) | free tier | Whisper large-v3, about 1 s. Key: <https://console.groq.com/keys> |
| OpenAI | paid | Key: <https://platform.openai.com/api-keys> |
| Custom | varies | Any server with an OpenAI-style `/audio/transcriptions` endpoint |
| Local | free | Offline via faster-whisper. Install with `uv tool install "hold-dictate[local]"` |

Getting a Groq key: open the link above, sign in, click **Create API Key**, copy it and paste it into `hold-dictate setup`. The key is stored in your user config file (permissions 600 on Linux and macOS). It leaves your computer only in requests to the provider. Free-tier limits change; see Groq's rate-limit page.

If the cloud request fails and the `local` extra is installed, it falls back to the local model.

## Commands

| Command | Does |
| --- | --- |
| `hold-dictate setup` | Guided setup (safe to run again) |
| `hold-dictate doctor` | Checks microphone, keyboard access, paste helpers and API key |
| `hold-dictate transcribe FILE.wav` | Transcribes a file |
| `hold-dictate service install\|uninstall\|status` | Start at login |
| `hold-dictate config` | Prints the config file path |

Settings live in a TOML file: `~/.config/hold-dictate/config.toml` (Linux), `~/Library/Application Support/hold-dictate/config.toml` (macOS), `%APPDATA%\hold-dictate\config.toml` (Windows). Options: `provider`, `api_key`, `base_url`, `model`, `language`, `prompt`, `hotkey`, `local_model`, `local_fallback`, `sounds`, `min_hold`, `timeout`.

Hotkey format: modifiers (`alt`, `ctrl`, `shift`, `super`) plus one key (`space`, a-z, 0-9, f1-f12), for example `ctrl+alt+d`.

## Per-OS notes

- **macOS**: allow Microphone and Accessibility / Input Monitoring when prompted. `alt` is the Option key.
- **Windows**: Alt+Space normally opens the window menu. hold-dictate hides that press while you dictate.
- **Linux**: the keyboard is read through `/dev/input`, so your user must be in the `input` group. The installer does this; log out and in once. Pasting uses `wl-clipboard` + `wtype` (Wayland) or `xclip` + `xdotool` (X11). GNOME on Wayland does not accept `wtype`; install `ydotool`, or the text is left on the clipboard for you to paste. On Wayland, bind Alt+Space to a no-op in your compositor so a stray space isn't typed (niri: `Alt+Space repeat=false { spawn "true"; }`). A recording pill is shown if [quickshell](https://quickshell.org) is installed; other setups use short beeps.

## Status

Private, pre-release. Tested end to end on Linux (Wayland, PipeWire) with a mock provider and the local model. The Windows and macOS code paths are unit-tested and run in CI, but the hotkey hooks, permissions and paste have not yet been tried on real machines of those systems. A live call to Groq or OpenAI has not been tested with this version either.

## Develop

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
```
