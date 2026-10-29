#!/bin/sh
# hold-dictate installer for macOS and Linux.  Usage: curl -fsSL <site>/install.sh | sh
# Running it again updates to the latest release.
set -eu

REPO="Can-0f-Tuna/hold-dictate"
say() { printf '\n==> %s\n' "$*"; }

# 1. uv: installs Python and the tool without touching your system Python.
if ! command -v uv >/dev/null 2>&1; then
  say "Installing uv (Python tool installer)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"; export PATH

# 2. Linux system packages (audio library, clipboard and paste helpers).
if [ "$(uname -s)" = "Linux" ] && [ -z "${HOLD_DICTATE_SKIP_SYSTEM:-}" ]; then
  if [ -n "${WAYLAND_DISPLAY:-}" ]; then helpers="wl-clipboard wtype"; else helpers="xclip xdotool"; fi
  if command -v apt-get >/dev/null 2>&1; then
    mgr="apt-get install -y"; portaudio="libportaudio2"
  elif command -v dnf >/dev/null 2>&1; then
    mgr="dnf install -y"; portaudio="portaudio"
  elif command -v pacman >/dev/null 2>&1; then
    mgr="pacman -S --needed --noconfirm"; portaudio="portaudio"
  elif command -v zypper >/dev/null 2>&1; then
    mgr="zypper install -y"; portaudio="libportaudio2"
  else
    mgr=""
  fi
  if [ -n "$mgr" ]; then
    say "Installing system packages (needs sudo): $portaudio $helpers"
    for pkg in $portaudio $helpers; do
      sudo $mgr "$pkg" >/dev/null 2>&1 || echo "    could not install $pkg; run 'hold-dictate doctor' later for help"
    done
  else
    echo "Install these with your package manager: PortAudio, $helpers"
  fi
  if ! id -nG | tr ' ' '\n' | grep -qx input; then
    say "Allowing keyboard access (adds you to the 'input' group)"
    sudo usermod -aG input "$USER" && echo "    Log out and back in once so the hotkey works."
  fi
fi

# 3. The tool itself: the latest release, or the main branch if there is none yet.
if [ -n "${HOLD_DICTATE_SOURCE:-}" ]; then
  source="$HOLD_DICTATE_SOURCE"
else
  tag=$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" 2>/dev/null | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n 1)
  source="git+https://github.com/$REPO${tag:+@$tag}"
fi
say "Installing hold-dictate ${tag:-}"
uv tool install --force "$source"

# 4. Start now and at every login, then open the settings page for the API key.
say "Starting hold-dictate"
hold-dictate service install
if [ -z "${HOLD_DICTATE_NO_SETUP:-}" ]; then
  say "Opening the settings in your browser. Add your API key there."
  hold-dictate settings --wait 10 || echo "Open the settings any time with: hold-dictate settings"
fi
