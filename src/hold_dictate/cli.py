import argparse
import getpass
import importlib.util
import sys

from . import __version__, config as cfgmod, service
from .config import HEBREW_PROMPT, PROVIDERS, Config

REPO = "https://github.com/Can-0f-Tuna/hold-dictate"


def ask(question: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{question}{suffix}: ").strip()
    except EOFError:
        answer = ""
    return answer or default


def yes(question: str, default: bool = True) -> bool:
    answer = ask(f"{question} ({'Y/n' if default else 'y/N'})").lower()
    return default if not answer else answer.startswith("y")


def cmd_setup(args) -> int:
    cfg = cfgmod.load()
    print("\nhold-dictate setup\n")
    if args.provider:
        choice = args.provider
    else:
        print("Which speech-to-text service should it use?")
        print("  1) Groq    - recommended: very fast, accurate Whisper large-v3, has a free tier")
        print("  2) OpenAI  - paid, good accuracy")
        print("  3) Other   - any OpenAI-compatible server (your own, Together, Fireworks, ...)")
        print("  4) Local   - offline on this computer, no account, but slower and a big download")
        choice = {"1": "groq", "2": "openai", "3": "custom", "4": "local"}.get(ask("Choose", "1"), "groq")
    cfg.provider = choice

    if choice == "local":
        if importlib.util.find_spec("faster_whisper") is None:
            print('\nLocal mode needs an extra install. Run:\n    uv tool install --force "hold-dictate[local]"\nthen run `hold-dictate setup` again.')
            return 1
        cfg.local_model = ask("Model (tiny, base, small, medium, large-v3-turbo, or a Hugging Face repo)", cfg.local_model)
    else:
        preset = PROVIDERS[choice]
        if choice == "custom":
            cfg.base_url = args.base_url or ask("Server base URL (for example https://host/v1)", cfg.base_url)
            cfg.model = args.model or ask("Model name", cfg.model or preset["model"])
        key = args.api_key
        if not key:
            if preset["keys_url"]:
                print(f"\nYou need a {preset['label']} API key. To get one:")
                print(f"  1. Open {preset['keys_url']}")
                print("  2. Sign in (or create a free account)")
                print("  3. Click 'Create API Key' and copy it")
            key = getpass.getpass("\nPaste the key here (nothing is shown as you type): ").strip()
        cfg.api_key = key
        from .providers import check_api
        problem = check_api(cfg)
        print("Key works." if not problem else f"Could not verify the key: {problem}")
        if problem and not args.yes and not yes("Save it anyway?", False):
            return 1

    language = args.language if args.language is not None else ask("Main language code (en, he, es, ...) or Enter to auto-detect", cfg.language)
    cfg.language, cfg.prompt = language, ""
    if language == "he":
        cfg.prompt = HEBREW_PROMPT  # keeps English words in Latin letters
        if choice != "local":
            cfg.language = ""  # forcing Hebrew makes Whisper mangle the English words
    cfg.hotkey = args.hotkey or ask("Hold-to-talk hotkey", cfg.hotkey)
    from .hotkey import parse_hotkey
    try:
        parse_hotkey(cfg.hotkey)
    except ValueError as e:
        print(f"Invalid hotkey: {e}")
        return 1
    path = cfgmod.save(cfg)
    print(f"\nSaved {path}")
    if not args.no_service and (args.yes or yes("Start it now and at every login?")):
        print(service.install())
    print(f"\nDone. Hold {cfg.hotkey}, speak, let go.")
    if sys.platform == "darwin":
        print("macOS will ask for Microphone and Accessibility / Input Monitoring access the first time. Allow them.")
    elif sys.platform.startswith("linux"):
        print("Problems? Run `hold-dictate doctor`.")
    return 0


def cmd_doctor(_args) -> int:
    from .hotkey import choose_backend, parse_hotkey
    from .paste import missing_tools
    from .providers import LocalTranscriber, check_api
    ok = True

    def line(good, text, fix=""):
        nonlocal ok
        ok = ok and good
        print(f"  {'OK ' if good else 'FIX'}  {text}" + (f"\n         {fix}" if fix and not good else ""))

    cfg = cfgmod.load()
    print(f"hold-dictate {__version__} on {sys.platform}, config {cfgmod.config_path()}\n")
    try:
        parse_hotkey(cfg.hotkey)
        line(True, f"hotkey {cfg.hotkey}")
    except ValueError as e:
        line(False, f"hotkey: {e}", "Fix `hotkey` in the config.")
    backend, problem = choose_backend()
    line(backend is not None, f"keyboard access ({backend.name if backend else 'none'})", problem)
    try:
        import sounddevice as sd
        has_mic = any(d["max_input_channels"] > 0 for d in sd.query_devices())
        line(has_mic, "microphone found", "Plug in or enable a microphone.")
    except Exception as e:
        hint = "Linux: install libportaudio2 (apt) / portaudio (dnf, pacman)." if sys.platform.startswith("linux") else ""
        line(False, f"audio: {e}", hint)
    if cfg.background_audio != "off":
        from . import ducking
        line(ducking.available(), "turning other apps down while you dictate", "Linux: install pactl (pulseaudio-utils), or set background_audio = \"off\".")
    tools = missing_tools()
    line(not tools, "paste helpers installed", f"Install: {', '.join(tools)}")
    if cfg.provider == "local":
        line(LocalTranscriber.available(), "local model library", 'uv tool install --force "hold-dictate[local]"')
    else:
        problem = check_api(cfg)
        line(not problem, f"{cfg.preset()['label']} API key", problem or "Run `hold-dictate settings`.")
    print("\nAll good." if ok else "\nFix the items marked FIX, then run this again.")
    return 0 if ok else 1


def cmd_file(args) -> int:
    from . import audio
    from .app import transcribe
    from .providers import LocalTranscriber, TranscribeError
    cfg = cfgmod.load()
    try:
        speech = audio.speech_only(audio.read_audio_file(args.path))
        if speech is None:
            print("(no speech found)")
            return 0
        print(transcribe(cfg, LocalTranscriber(cfg), speech)[0])
    except (TranscribeError, ValueError, OSError, EOFError, audio.wave.Error) as e:
        print(f"hold-dictate: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_settings(args) -> int:
    import time
    import webbrowser
    from .settings_ui import SettingsServer, running_url
    url = ""
    for _ in range(int(args.wait * 2) + 1):  # the service may still be starting
        url = running_url()
        if url:
            break
        time.sleep(0.5)
    if url:
        print(f"Opening the settings in your browser:\n  {url}")
        webbrowser.open(url)
        return 0
    server = SettingsServer(cfgmod.load(), daemon=False, port=0).start()
    print("hold-dictate is not running, so this window serves the settings page until you press Ctrl+C:")
    print(f"  {server.url}")
    server.open()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    server.stop()
    return 0


def cmd_config(_args) -> int:
    print(cfgmod.config_path())
    return 0


def cmd_service(args) -> int:
    print({"install": service.install, "uninstall": service.uninstall, "status": service.status}[args.action]())
    return 0


def cmd_run(_args) -> int:
    from .app import run
    return run(cfgmod.load())


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="hold-dictate", description="Hold a hotkey, speak, release: text appears at the cursor.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command")
    sub.add_parser("run", help="start dictating (default)").set_defaults(fn=cmd_run)
    s = sub.add_parser("setup", help="guided setup in the terminal")
    s.add_argument("--provider", choices=["groq", "openai", "custom", "local"])
    s.add_argument("--api-key")
    s.add_argument("--base-url")
    s.add_argument("--model")
    s.add_argument("--language")
    s.add_argument("--hotkey")
    s.add_argument("-y", "--yes", action="store_true", help="no questions; start at login")
    s.add_argument("--no-service", action="store_true", help="do not install start-at-login")
    s.set_defaults(fn=cmd_setup)
    st = sub.add_parser("settings", help="open the settings page in your browser")
    st.add_argument("--wait", type=float, default=0, help="seconds to wait for hold-dictate to start")
    st.set_defaults(fn=cmd_settings)
    sub.add_parser("doctor", help="check that everything is in place").set_defaults(fn=cmd_doctor)
    f = sub.add_parser("transcribe", help="transcribe a WAV file and print it")
    f.add_argument("path")
    f.set_defaults(fn=cmd_file)
    sub.add_parser("config", help="print the config file path").set_defaults(fn=cmd_config)
    sv = sub.add_parser("service", help="manage start-at-login")
    sv.add_argument("action", choices=["install", "uninstall", "status"])
    sv.set_defaults(fn=cmd_service)
    args = p.parse_args(argv)
    if not args.command:
        args = p.parse_args(["run"])
    return args.fn(args)
