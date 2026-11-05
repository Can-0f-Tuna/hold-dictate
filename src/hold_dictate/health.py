"""The checks behind `hold-dictate doctor` and the Health section of the settings page."""
import json
import os
import subprocess
import sys
import urllib.request

from . import extensions

# Not Input Monitoring: the shortcut's event tap may change events (it hides Option+Space's space), and macOS
# gives that kind of tap to Accessibility alone.
MAC_NEEDS = {
    "Accessibility": "without it the shortcut isn't seen and the text can't be pasted",
    "Microphone": "without it every recording is silent",
}
MAC_AFTER = {"Accessibility": "The shortcut works as soon as it's on.", "Microphone": "Then restart hold-dictate."}
CHILD = "HOLD_DICTATE_HEALTH_CHILD"  # set for the checks the running app makes: they must not ask the app again


def _mac_microphone():
    import ctypes
    objc = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
    media = ctypes.CDLL("/System/Library/Frameworks/AVFoundation.framework/AVFoundation")
    objc.objc_getClass.restype = objc.sel_registerName.restype = ctypes.c_void_p
    objc.objc_getClass.argtypes = objc.sel_registerName.argtypes = [ctypes.c_char_p]
    send = objc.objc_msgSend
    send.restype, send.argtypes = ctypes.c_long, [ctypes.c_void_p] * 3
    status = send(objc.objc_getClass(b"AVCaptureDevice"), objc.sel_registerName(b"authorizationStatusForMediaType:"),
                  ctypes.c_void_p.in_dll(media, "AVMediaTypeAudio"))
    return {0: None, 3: True}.get(status, False)  # 0: macOS hasn't asked yet


def mac_access() -> dict:
    """Permission name -> True, False, or None for not asked yet. It only looks: asking would pop up a dialog."""
    import ctypes
    services = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
    services.AXIsProcessTrusted.restype = ctypes.c_bool
    return {"Accessibility": services.AXIsProcessTrusted(), "Microphone": _mac_microphone()}


def _app_rows():
    """The permission rows of the hold-dictate that is running, from its own checks, or None if it isn't running.
    In a terminal macOS answers for the terminal, which isn't what the app runs as."""
    if os.environ.get(CHILD):
        return None
    try:
        from .settings_ui import runtime_file
        info = json.loads(runtime_file().read_text(encoding="utf-8"))
        request = urllib.request.Request(f"http://127.0.0.1:{info['port']}/api/health", headers={"X-Token": info["token"]})
        with urllib.request.urlopen(request, timeout=APP_WAIT) as response:
            rows = json.load(response)["checks"]
    except Exception:
        return None
    rows = [r for r in rows if r.get("text", "").startswith("macOS ")]
    return rows or None


APP_WAIT = 25  # the app runs the checks in a process of its own, with a 20 s limit


def _mac_rows(line):
    rows = _app_rows()
    if rows is not None:
        for r in rows:
            line(r["ok"], r["text"] + ", in the running hold-dictate", r["fix"])
        return
    try:
        access = mac_access()
    except Exception as e:
        access = {}
        print(f"hold-dictate: can't read the macOS permissions: {e}", file=sys.stderr)
    from .macos import grantee
    name, path = grantee()
    who = f"{name} ({path})" if path else name
    for perm, why in MAC_NEEDS.items():
        if perm in access:
            asked = "" if access[perm] is not None else " (macOS will ask the first time you dictate)"
            line(access[perm] is not False, f"macOS {perm} permission for {name}{asked}",
                 f"It's off, and {why}. Open System Settings > Privacy & Security > {perm} and turn on {who}; "
                 f"if it isn't listed, add it with the + button. {MAC_AFTER[perm]}")


def checks(cfg, online: bool = True) -> list:
    """Rows of {ok, text, fix}; `fix` is in plain words, for failed rows. `online` False stays on this computer:
    the key isn't tried with the service, and extensions, which may call out, are left out."""
    from . import ducking
    from .hotkey import choose_backend, parse_hotkey
    from .paste import missing_tools
    from .providers import LocalTranscriber, check_api
    rows = []

    def line(good, text, fix=""):
        rows.append({"ok": bool(good), "text": text, "fix": "" if good else fix})

    try:
        parse_hotkey(cfg.hotkey)
        line(True, f"hotkey {cfg.hotkey}")
    except ValueError as e:
        line(False, f"hotkey: {e}", "Choose a new shortcut at the top of the settings page, or fix `hotkey` in the config file.")
    backend, problem = choose_backend(quick=True)
    line(backend is not None, f"keyboard access ({backend.name if backend else 'none'})", problem)
    if sys.platform == "darwin":
        _mac_rows(line)
    try:
        import sounddevice as sd
        has_mic = any(d["max_input_channels"] > 0 for d in sd.query_devices())
        line(has_mic, "microphone found", "Plug in or enable a microphone.")
    except Exception as e:
        hint = "Install libportaudio2 (apt) or portaudio (dnf, pacman)." if sys.platform.startswith("linux") else "Install hold-dictate again: its audio part did not load."
        line(False, f"audio: {e}", hint)
    if cfg.background_audio != "off":
        line(ducking.available(), "turning other apps down while you dictate",
             "Install pactl (from the pulseaudio-utils package), or choose Leave as is under Music and videos.")
    tools = missing_tools()
    line(not tools, "paste helpers installed", f"Install these with your package manager: {', '.join(tools)}")
    if online:
        for row in extensions.doctor_lines():
            line(*row)
    if cfg.provider == "local":
        line(LocalTranscriber.available(), "local model library", 'Run this in a terminal: uv tool install --force "hold-dictate[local]"')
    elif online:
        problem = check_api(cfg)
        line(not problem, f"{cfg.preset()['label']} API key", problem or "Run `hold-dictate settings`.")
    else:
        line(bool(cfg.effective_key()), f"{cfg.preset()['label']} API key saved", "Paste your key under Transcription, then press Check and save.")
    return rows


def report() -> dict:
    """The offline checks, in a process of their own: PortAudio only finds a new microphone when it starts, and
    restarting it inside the app would end a recording."""
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    try:
        run = subprocess.run([sys.executable, "-m", "hold_dictate", "doctor", "--offline", "--json"], stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, timeout=20, env=dict(os.environ, **{CHILD: "1"}), **flags)
        return json.loads(run.stdout.strip().splitlines()[-1])
    except Exception as e:
        print(f"hold-dictate: the health checks failed: {e}", file=sys.stderr, flush=True)
        return {"ok": False, "checks": [{"ok": False, "text": "The checks couldn't run", "fix": "Run `hold-dictate doctor` in a terminal to see why."}]}
