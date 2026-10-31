"""The checks behind `hold-dictate doctor` and the Health section of the settings page."""
import json
import subprocess
import sys

from . import extensions

MAC_NEEDS = {
    "Input Monitoring": "without it the shortcut isn't seen",
    "Accessibility": "without it the text can't be pasted",
    "Microphone": "without it every recording is silent",
}


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
    graphics = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    services = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
    graphics.CGPreflightListenEventAccess.restype = services.AXIsProcessTrusted.restype = ctypes.c_bool
    return {"Input Monitoring": graphics.CGPreflightListenEventAccess(), "Accessibility": services.AXIsProcessTrusted(), "Microphone": _mac_microphone()}


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
    backend, problem = choose_backend()
    line(backend is not None, f"keyboard access ({backend.name if backend else 'none'})", problem)
    if sys.platform == "darwin":
        try:
            access = mac_access()
        except Exception as e:
            access = {}
            print(f"hold-dictate: can't read the macOS permissions: {e}", file=sys.stderr)
        for name, why in MAC_NEEDS.items():
            if name in access:
                asked = "" if access[name] is not None else " (macOS will ask the first time you dictate)"
                line(access[name] is not False, f"macOS {name} permission{asked}",
                     f"It's off, and {why}. Open System Settings > Privacy & Security > {name}, turn on the app that runs hold-dictate (it can be listed as Python), then restart hold-dictate.")
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
                             capture_output=True, text=True, timeout=20, **flags)
        return json.loads(run.stdout.strip().splitlines()[-1])
    except Exception as e:
        print(f"hold-dictate: the health checks failed: {e}", file=sys.stderr, flush=True)
        return {"ok": False, "checks": [{"ok": False, "text": "The checks couldn't run", "fix": "Run `hold-dictate doctor` in a terminal to see why."}]}
