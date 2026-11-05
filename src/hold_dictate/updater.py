"""Self-update from GitHub releases. Installs with uv, the same way the install script did. The offline
engine is installed the same way: the whole install again, with the `local` extra."""
import importlib.util
import inspect
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
# The release's source as an archive, so installing needs no git. HOLD_DICTATE_CORE_URL tries a build elsewhere.
CORE_URL = os.environ.get("HOLD_DICTATE_CORE_URL") or f"https://github.com/{REPO}/archive/refs/tags/{{tag}}.tar.gz"
# The newest Python that every library of the offline engine has wheels for on macOS (Intel and Apple), Windows
# and Linux: onnxruntime, which faster-whisper needs, stops at 3.13 on Intel Macs. The install scripts use it too.
OFFLINE_PYTHON = (3, 13)
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
    return (prefix / "uv-receipt.toml").exists() or (prefix.name == "hold-dictate" and prefix.parent.name == "tools")


def installed_extras() -> list[str]:
    """The extras this copy has, so an update keeps them."""
    extras = []
    if importlib.util.find_spec("faster_whisper"):
        extras.append("local")
    if sys.platform.startswith("linux") and importlib.util.find_spec("pynput"):
        extras.append("x11")
    return extras


def install_spec(tag: str, extras=None) -> str:
    """The app at `tag`, with `extras` (default: the ones installed now)."""
    extras = installed_extras() if extras is None else sorted(set(extras))
    name = f"hold-dictate[{','.join(extras)}]" if extras else "hold-dictate"
    return f"{name} @ {CORE_URL.format(tag=tag)}"


def install_python(extras) -> str:
    """The Python to install onto: this one. Without it uv picks the newest Python it can get, which the
    libraries may have no wheels for yet, and the install fails. The offline engine on a newer Python than its
    libraries support moves to OFFLINE_PYTHON, which uv downloads if it isn't here."""
    if "local" in extras and sys.version_info[:2] > OFFLINE_PYTHON:
        return ".".join(map(str, OFFLINE_PYTHON))
    return getattr(sys, "_base_executable", "") or sys.executable  # the venv's own Python goes with the old venv


def install_args(source, tag: str, extras: list[str]) -> list[str]:
    """What follows `uv tool install --force`, from the update source. Extensions written before extras get
    only the tag; they keep the extras installed now."""
    try:
        takes_extras = len(inspect.signature(source.install_args).parameters) >= 2
    except (TypeError, ValueError):
        takes_extras = False
    return source.install_args(tag, extras) if takes_extras else source.install_args(tag)


def install_command(uv: str, source, tag: str, extras: list[str]) -> list[str]:
    return [uv, "tool", "install", "--force", "--python", install_python(extras), *install_args(source, tag, extras)]


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

    def install_args(self, tag: str, extras=None) -> list[str]:
        return [install_spec(tag, extras)]


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
        self.job = ""  # "update" or "offline": what is installing, or installed and waiting to restart
        self.offline_status = ""  # how installing the offline engine went, for the settings page
        self.command = []  # the uv command of the install under way
        self.echo = False  # print what uv says (the `offline` command in a terminal)

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
        return self._install(())

    def install_offline(self) -> bool:
        """Installs the offline engine: the latest release again, with the `local` extra, then restarts."""
        return self._install(("local",))

    def _install(self, add: tuple) -> bool:
        offline = bool(add)
        if self.ready:
            return True
        problem = self.can_update()
        if problem:
            self._failed(offline, f"cannot install automatically: {problem}" if offline else f"cannot update automatically: {problem}")
            return False
        if not self.busy.acquire(blocking=False):
            if offline:
                self.offline_status = "an update is installing; try again when it is done"
            return False
        try:
            if offline:
                self.offline_status = "checking the latest version"
            if offline or not self.available():
                self.check()  # the offline engine comes with the latest release; the check also tells the source its files
            if not self.latest or not (offline or self.available()):
                self._failed(offline, f"offline install failed: {self.status}" if offline else "")
                return False
            extras = sorted({*installed_extras(), *add})
            self.command = install_command(find_uv(), self.source, self.latest, extras)
            self.job = "offline" if offline else "update"
            what = "the offline engine" if offline else self.latest
            if sys.platform == "win32":
                self.ready = True
                self._say(offline, f"{what} will be installed on restart")
                return True
            self._say(offline, f"installing {what}")
            self.progress = 0
            try:
                output = self._run_install(self.command)
            except OSError as e:
                output = str(e)
            if output is not None:
                self.progress = None
                self.job = ""
                self._failed(offline, f"{'offline install' if offline else 'update'} failed: {output.strip()[-300:]}")
                return False
            self.progress = 100
            self.ready = True
            self._say(offline, f"{what} installed, restarting")
            return True
        finally:
            self.busy.release()

    def _say(self, offline: bool, text: str):
        if offline:
            self.offline_status = text
        else:
            self.status = text

    def _failed(self, offline: bool, text: str):
        if text:
            self._say(offline, text)
        if not offline:
            self.on_failed()

    def _run_install(self, cmd) -> str | None:
        """Runs uv, moving `progress` on as it reports each stage. None when it worked, else its output."""
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        lines = []
        for line in proc.stdout:
            lines.append(line)
            if self.echo:
                print(line, end="", flush=True)
            for start, percent in STAGES:
                if line.lstrip().startswith(start):
                    self.progress = max(self.progress or 0, percent)
        return None if proc.wait() == 0 else "".join(lines)

    def windows_helper(self, start: bool = True) -> str:
        """The PowerShell that installs once this process has exited (Windows locks running files), then starts
        the new version. uv is tried a few times: the venv's launcher can outlive this process for a moment.
        What uv says goes to update.log next to the config."""
        from .config import config_dir

        def quote(text: str) -> str:
            return "'" + str(text).replace("'", "''") + "'"
        uv, *rest = self.command
        log = quote(config_dir() / "update.log")
        script = (
            f"Wait-Process -Id {os.getpid()} -ErrorAction SilentlyContinue; "
            f"foreach ($i in 1..5) {{ & {quote(uv)} {' '.join(map(quote, rest))} *> {log}; "
            "if ($LASTEXITCODE -eq 0) { break }; Start-Sleep -Seconds 3 }; "
        )
        if start:
            cmd = run_command()
            script += f"Start-Process -FilePath {quote(cmd[0])} -ArgumentList '-m','hold_dictate','run'"
        return script

    def restart(self):
        """Replaces this process with the newly installed version."""
        changed = newer(self.latest)
        if self.announce or self.job == "offline":
            self.notify(f"Updated to {self.latest}." if self.job == "update" else
                        "The offline engine is installed." + (f" Updated to {self.latest}." if changed else ""))
        cmd = run_command()
        if sys.platform == "win32":
            flags = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
            subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", self.windows_helper()], creationflags=flags, close_fds=True)
            os._exit(0)
        if changed:
            os.environ["HOLD_DICTATE_UPDATED"] = self.latest  # the new version finishes the pill's update chip
        os.execv(cmd[0], cmd)

    def update_now(self):
        """Settings page button: install in the background, restart when idle."""
        threading.Thread(target=self.install, daemon=True).start()

    def offline_now(self):
        """Settings page button: install the offline engine in the background, restart when idle."""
        threading.Thread(target=self.install_offline, daemon=True).start()

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
