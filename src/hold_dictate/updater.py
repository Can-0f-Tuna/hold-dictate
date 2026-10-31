"""Self-update from GitHub releases. Installs with uv, the same way the install script did."""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from . import __version__, extensions

REPO = "Can-0f-Tuna/hold-dictate"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
CHECK_EVERY = 10 * 60
FIRST_CHECK_AFTER = 60
# What uv prints while it installs, and how far along each line means it is. Fetching the release comes first.
STAGES = (("Resolved", 30), ("Building", 40), ("Downloading", 40), ("Built", 70), ("Downloaded", 70), ("Prepared", 85), ("Installed", 95))


def parse_version(text: str) -> tuple:
    """'v1.2.3rc1' -> (1, 2, 3): the leading digits of each part."""
    parts = []
    for piece in text.strip().lstrip("vV").split("."):
        digits = re.match(r"\d*", piece).group()
        parts.append(int(digits or 0))
    return tuple(parts)


def newer(latest: str, current: str = __version__) -> bool:
    return parse_version(latest) > parse_version(current)


def find_uv() -> str | None:
    found = shutil.which("uv")
    if found:
        return found
    exe = "uv.exe" if sys.platform == "win32" else "uv"
    for folder in (Path.home() / ".local" / "bin", Path.home() / ".cargo" / "bin"):
        if (folder / exe).exists():
            return str(folder / exe)
    return None


def installed_by_uv() -> bool:
    """True when running from `uv tool install` (a venv in uv's tools folder), not a dev checkout."""
    prefix = Path(sys.prefix)
    return prefix.name == "hold-dictate" and prefix.parent.name == "tools"


def install_spec(tag: str) -> str:
    extras = []
    if importlib.util.find_spec("faster_whisper"):
        extras.append("local")
    if sys.platform.startswith("linux") and importlib.util.find_spec("pynput"):
        extras.append("x11")
    name = f"hold-dictate[{','.join(extras)}]" if extras else "hold-dictate"
    return f"{name} @ git+https://github.com/{REPO}@{tag}"


def run_command() -> list[str]:
    python = sys.executable
    if sys.platform == "win32":
        pythonw = Path(python).with_name("pythonw.exe")
        python = str(pythonw if pythonw.exists() else python)
    return [python, "-m", "hold_dictate", "run"]


class GitHub:
    """Where updates come from unless an extension says otherwise: this repository's latest release."""

    def latest(self) -> str:
        request = urllib.request.Request(LATEST_URL, headers={"User-Agent": "hold-dictate", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)["tag_name"]

    def install_args(self, tag: str) -> list[str]:
        return [install_spec(tag)]


class Updater:
    """Checks every 10 minutes; installs a newer release when dictation is idle, then restarts itself."""

    def __init__(self, cfg, notify=None, is_idle=None):
        self.cfg = cfg
        source = extensions.update_source()
        self.source = GitHub() if source is None else source  # False: no updates, see extensions.update_source
        self.notify = notify or (lambda message: None)
        self.is_idle = is_idle or (lambda: True)
        self.latest = ""
        self.status = ""  # shown on the settings page
        self.next_check = time.monotonic() + FIRST_CHECK_AFTER
        self.busy = threading.Lock()
        self.ready = False  # installed; restart when idle
        self.progress = None  # 0-100 while installing, for the settings page
        self.on_failed = lambda: None  # the pill's update chip shows "!" and goes away
        self.announce = True  # notify after restarting; off when the pill shows it instead

    def can_update(self) -> str:
        """'' when self-update works here, else the reason it does not."""
        if not installed_by_uv():
            return "this copy was not installed by the install script"
        if not find_uv():
            return "uv was not found"
        return ""

    def check(self) -> str:
        if self.source is False:
            self.status = "could not check for updates: an extension failed"
            return ""
        try:
            self.latest = self.source.latest()
        except Exception as e:
            self.status = f"could not check for updates: {e}"
            return ""
        self.status = f"{self.latest} is available" if newer(self.latest) else "up to date"
        return self.latest

    def available(self) -> bool:
        return bool(self.latest) and newer(self.latest)

    def install(self) -> bool:
        """Downloads and installs the latest release. On Windows the running files are locked, so a
        helper waits for this process to exit, installs, and starts the new version."""
        if self.ready:
            return True
        problem = self.can_update()
        if problem:
            self.status = f"cannot update automatically: {problem}"
            self.on_failed()
            return False
        if not self.busy.acquire(blocking=False):
            return False
        try:
            if not self.available():
                self.check()
                if not self.available():
                    self.on_failed()
                    return False
            uv, args = find_uv(), self.source.install_args(self.latest)
            if sys.platform == "win32":
                self.ready = True
                self.status = f"{self.latest} will be installed on restart"
                return True
            self.status = f"installing {self.latest}"
            self.progress = 0
            try:
                output = self._run_install([uv, "tool", "install", "--force", *args])
            except OSError as e:
                output = str(e)
            if output is not None:
                self.progress = None
                self.status = f"update failed: {output.strip()[-300:]}"
                self.on_failed()
                return False
            self.progress = 100
            self.ready = True
            self.status = f"{self.latest} installed, restarting"
            return True
        finally:
            self.busy.release()

    def _run_install(self, cmd) -> str | None:
        """Runs uv, moving `progress` on as it reports each stage. None when it worked, else its output."""
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        lines = []
        for line in proc.stdout:
            lines.append(line)
            for start, percent in STAGES:
                if line.lstrip().startswith(start):
                    self.progress = max(self.progress or 0, percent)
        return None if proc.wait() == 0 else "".join(lines)

    def restart(self):
        """Replaces this process with the newly installed version."""
        if self.announce:
            self.notify(f"Updated to {self.latest}.")
        cmd = run_command()
        if sys.platform == "win32":
            args = " ".join("'" + arg.replace("'", "''") + "'" for arg in self.source.install_args(self.latest))
            script = (
                f"Wait-Process -Id {os.getpid()} -ErrorAction SilentlyContinue; "
                f"& '{find_uv()}' tool install --force {args}; "
                f"Start-Process -FilePath '{cmd[0]}' -ArgumentList '-m','hold_dictate','run'"
            )
            flags = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
            subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script], creationflags=flags, close_fds=True)
            os._exit(0)
        os.environ["HOLD_DICTATE_UPDATED"] = self.latest  # the new version finishes the pill's update chip
        os.execv(cmd[0], cmd)

    def update_now(self):
        """Settings page button: install in the background, restart when idle."""
        threading.Thread(target=self.install, daemon=True).start()

    def tick(self):
        """Called every few seconds from the hotkey loop."""
        if self.ready:
            if self.is_idle():
                self.restart()
            return
        if time.monotonic() < self.next_check:
            return
        self.next_check = time.monotonic() + CHECK_EVERY
        # With automatic updates off (or impossible here) it still looks, so the pill can offer the update.
        job = self.install if self.cfg.auto_update and not self.can_update() else self.check
        threading.Thread(target=job, daemon=True).start()
