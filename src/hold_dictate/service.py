"""Start hold-dictate at login: systemd (Linux), a LaunchAgent (macOS), the Run key (Windows)."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .config import config_dir

LABEL = "io.github.hold-dictate"


def _command() -> list[str]:
    """How to launch the daemon: this Python, so it works whichever way the tool was installed."""
    python = sys.executable
    if sys.platform == "win32":  # pythonw has no console window
        pythonw = Path(python).with_name("pythonw.exe")
        python = str(pythonw if pythonw.exists() else python)
    return [python, "-m", "hold_dictate", "run"]


def _linux_unit() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user" / "hold-dictate.service"


def _mac_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _sh(*cmd) -> int:
    return subprocess.run(list(cmd), capture_output=True).returncode


def install() -> str:
    cmd = _command()
    if sys.platform == "win32":
        import winreg
        line = " ".join(f'"{c}"' if " " in c else c for c in cmd)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            winreg.SetValueEx(key, "hold-dictate", 0, winreg.REG_SZ, line)
        flags = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
        subprocess.Popen(cmd, creationflags=flags, close_fds=True)
        return "Will start at every login, and is running now."
    if sys.platform == "darwin":
        log = config_dir() / "hold-dictate.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        args = "".join(f"<string>{c}</string>" for c in cmd)
        plist = _mac_plist()
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            f'<plist version="1.0"><dict><key>Label</key><string>{LABEL}</string>'
            f"<key>ProgramArguments</key><array>{args}</array>"
            "<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>"
            f"<key>StandardOutPath</key><string>{log}</string><key>StandardErrorPath</key><string>{log}</string>"
            "</dict></plist>\n"
        )
        domain = f"gui/{os.getuid()}"
        _sh("launchctl", "bootout", domain, str(plist))
        if _sh("launchctl", "bootstrap", domain, str(plist)) != 0:
            return f"Wrote {plist}. Log out and back in to start it."
        return "Will start at every login, and is running now."
    if not shutil.which("systemctl"):
        return "systemd was not found. Start `hold-dictate run` from your session's autostart instead."
    unit = _linux_unit()
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(
        "[Unit]\nDescription=hold-dictate: hold a hotkey to dictate\n"
        "After=graphical-session.target\nPartOf=graphical-session.target\n\n"
        f"[Service]\nExecStart={' '.join(cmd)}\nRestart=on-failure\nRestartSec=5\n\n"
        "[Install]\nWantedBy=graphical-session.target\n"
    )
    _sh("systemctl", "--user", "daemon-reload")
    if _sh("systemctl", "--user", "enable", "--now", "hold-dictate.service") != 0:
        return f"Wrote {unit}, but could not start it. Try: systemctl --user enable --now hold-dictate"
    return "Will start at every login, and is running now."


def uninstall() -> str:
    if sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, "hold-dictate")
        except FileNotFoundError:
            pass
        subprocess.run(["taskkill", "/F", "/IM", "pythonw.exe", "/FI", "WINDOWTITLE eq hold-dictate*"], capture_output=True)
        return "Autostart removed. A running copy stops at the next logout."
    if sys.platform == "darwin":
        plist = _mac_plist()
        _sh("launchctl", "bootout", f"gui/{os.getuid()}", str(plist))
        plist.unlink(missing_ok=True)
        return "Autostart removed and stopped."
    _sh("systemctl", "--user", "disable", "--now", "hold-dictate.service")
    _linux_unit().unlink(missing_ok=True)
    _sh("systemctl", "--user", "daemon-reload")
    return "Autostart removed and stopped."


def status() -> str:
    if sys.platform == "win32":
        return "Windows: see Task Manager for pythonw.exe; autostart is in HKCU\\...\\Run."
    if sys.platform == "darwin":
        return "running" if _sh("launchctl", "print", f"gui/{os.getuid()}/{LABEL}") == 0 else "not running"
    return "running" if _sh("systemctl", "--user", "is-active", "--quiet", "hold-dictate.service") == 0 else "not running"
