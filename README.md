# hold-dictate

Hold **Alt+Space**, speak, let go. The transcribed text is pasted at your cursor.

Built for Linux on Wayland (developed on niri). Hebrew works well, and mixed Hebrew and English too.

## How it works

- Reads the keyboard straight from evdev, so it sees key releases, which most Wayland compositors can't bind.
- Records from PipeWire while the keys are held, and drops silence with a VAD.
- Transcribes with Groq's hosted Whisper large-v3 when `GROQ_API_KEY` is set. If that is missing or fails, it falls back to a local [faster-whisper](https://github.com/SYSTRAN/faster-whisper) model (default: `ivrit-ai/whisper-large-v3-turbo-ct2`).
- Pastes through the clipboard (Ctrl+V, or Ctrl+Shift+V in terminals), then restores your old clipboard.
- Shows a small recording pill via [quickshell](https://quickshell.org) (`overlay.qml`).

## Requirements

`python` 3.10+, `pipewire` (`pw-record`), `wl-clipboard`, `wtype`, `quickshell`, and `niri` (only used to detect whether the focused window is a terminal). Your user must be in the `input` group.

## Install

```sh
sudo usermod -aG input "$USER"   # then log out and back in
mkdir -p ~/.local/share/hold-dictate ~/.config/hold-dictate ~/.config/systemd/user
cp dictate.py overlay.qml ~/.local/share/hold-dictate/
python -m venv ~/.local/share/hold-dictate/.venv
~/.local/share/hold-dictate/.venv/bin/pip install -r requirements.txt
cp env.example ~/.config/hold-dictate/env      # optionally add your GROQ_API_KEY
cp systemd/hold-dictate.service ~/.config/systemd/user/
systemctl --user enable --now hold-dictate.service
```

On niri, add this bind so Alt+Space doesn't also type a space into the focused app:

```kdl
Alt+Space repeat=false hotkey-overlay-title=null { spawn "true"; }
```

## Configuration

Environment variables, set in `~/.config/hold-dictate/env`:

| Variable | Default | Meaning |
| --- | --- | --- |
| `GROQ_API_KEY` | empty | Use Groq's hosted Whisper. Empty means local only. |
| `DICTATE_LANG` | `he` | Language hint for the local model. |
| `DICTATE_MODEL` | `ivrit-ai/whisper-large-v3-turbo-ct2` | Local faster-whisper model. |
| `DICTATE_GROQ_MODEL` | `whisper-large-v3` | Groq model. |
| `DICTATE_GROQ_PROMPT` | Hebrew/English sample | Prompt that nudges Groq's language mix. |
| `DICTATE_IDLE_SECONDS` | `900` | Unload the local model after this long unused. |

To test a file: `dictate.py FILE.wav` prints the transcription.

## Status

Private, pre-release. Not yet tested outside the author's machine.
