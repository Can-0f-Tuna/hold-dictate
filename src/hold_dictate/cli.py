import argparse
import getpass
import importlib.util
import json
import sys
import time
import urllib.request

from . import __version__, config as cfgmod, extensions, service
from .config import OFFLINE_COMMAND, PROVIDERS, Config

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
            print(f"\nLocal mode needs the offline engine. Run:\n    {OFFLINE_COMMAND}\nthen run `hold-dictate setup` again.")
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
        cfg.api_key, cfg.api_key_service = key, choice
        from .providers import check_api
        problem = check_api(cfg)
        print("Key works." if not problem else f"Could not verify the key: {problem}")
        if problem and not args.yes and not yes("Save it anyway?", False):
            return 1

    languages = args.languages if args.languages is not None else ask("Languages you speak, as codes (for example he,en), or Enter for any", cfg.languages)
    try:
        cfg.languages = cfgmod.clean_languages(languages)
    except ValueError as e:
        print(f"Invalid language: {e}")
        return 1
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


def cmd_doctor(args) -> int:
    from . import health
    cfg = cfgmod.load()
    rows = health.checks(cfg, online=not args.offline)
    ok = all(row["ok"] for row in rows)
    if args.json:
        print(json.dumps({"ok": ok, "checks": rows}))
        return 0 if ok else 1
    print(f"hold-dictate {__version__} on {sys.platform}, config {cfgmod.config_path()}\n")
    for row in rows:
        print(f"  {'OK ' if row['ok'] else 'FIX'}  {row['text']}" + (f"\n         {row['fix']}" if row["fix"] else ""))
    print("\nAll good." if ok else "\nFix the items marked FIX, then run this again.")
    return 0 if ok else 1


def _plain(text: str) -> str:
    """Dictated text on one line, with nothing a terminal would obey."""
    return "".join(c if c.isprintable() else " " for c in text)


def cmd_history(args) -> int:
    from . import history
    cfg = cfgmod.load()
    if args.action == "clear":
        if args.yes or yes("Delete every saved dictation?", False):
            history.clear()
            print("History cleared.")
        return 0
    if args.action == "delete":
        found = bool(args.value) and history.delete(args.value)
        print("Deleted." if found else "No dictation has that id.")
        return 0 if found else 1
    if args.action == "search" and not args.value:
        print("Say what to search for: hold-dictate history search WORDS")
        return 1
    rows = history.entries(cfg, args.value or "" if args.action == "search" else "")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # a console that can't show Hebrew
    for row in rows[:args.limit]:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["time"]))
        print(f"{row['id']}  {when}  {_plain(row['app']) or '-'}\n  {_plain(row['text'])}")
    if not rows:
        print("Nothing found." if args.action == "search" else "The history is empty." if history.days(cfg) else "The history is empty, and off. Turn it on in the settings.")
    elif len(rows) > args.limit:
        print(f"... and {len(rows) - args.limit} older (--limit shows more)")
    return 0


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
        from .textproc import clean
        print(clean(transcribe(cfg, LocalTranscriber(cfg), speech)[0], cfg) or "(nothing to paste)")
    except (TranscribeError, ValueError, OSError, EOFError, audio.wave.Error) as e:
        print(f"hold-dictate: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_settings(args, anchor: str = "", alongside=None) -> int:
    """`anchor` goes on the page's address. `alongside` runs next to opening it: opening a browser that isn't
    running yet can take as long as the browser runs."""
    import threading
    import webbrowser
    from .settings_ui import SettingsServer, running_url

    def show(address):
        if alongside:
            threading.Thread(target=alongside).start()
        webbrowser.open(address + anchor)

    url = ""
    for _ in range(int(args.wait * 2) + 1):  # the service may still be starting
        url = running_url()
        if url:
            break
        time.sleep(0.5)
    if url:
        print(f"Opening the settings in your browser:\n  {url}")
        show(url)
        return 0
    server = SettingsServer(cfgmod.load(), daemon=False, port=0).start()
    print("hold-dictate is not running, so this window serves the settings page until you press Ctrl+C:")
    print(f"  {server.url}")
    show(server.url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    server.stop()
    return 0


def _daemon(path: str, get: bool = False) -> dict | None:
    """POSTs to (or GETs from) the running hold-dictate's local API. None when it is not running."""
    from .settings_ui import running_url, runtime_file
    if not running_url():
        return None
    info = json.loads(runtime_file().read_text(encoding="utf-8"))
    request = urllib.request.Request(
        f"http://127.0.0.1:{info['port']}{path}", data=None if get else b"{}", method="GET" if get else "POST",
        headers={"X-Token": info["token"], "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def cmd_update(_args) -> int:
    """Installs the latest release in the running hold-dictate (the pill's "!" tab runs this)."""
    import webbrowser
    from .settings_ui import running_url
    state = _daemon("/api/update/install")
    if state is None:
        print("hold-dictate is not running. Start it, or run the install script again to update.")
        return 1
    problem = state["update"]["can_update"]
    if problem:  # cannot install itself here: the settings page explains what to do
        print(f"Cannot update automatically: {problem}. Opening the settings.")
        webbrowser.open(running_url())
        return 1
    print(f"Installing {state['update']['latest'] or 'the latest version'}; hold-dictate restarts when it is done.")
    return 0


def cmd_offline(_args) -> int:
    """Installs the offline engine the way an update installs: the latest release again, with the `local` extra,
    on the Python this copy runs on. A running hold-dictate does it and restarts; else this command does."""
    if importlib.util.find_spec("faster_whisper"):
        print("The offline engine is already installed. Choose Offline in the settings to use it.")
        return 0
    try:
        state = _daemon("/api/offline/install")
    except OSError:
        state = None
    if state is not None:
        return _follow_offline(state)
    from .updater import Updater
    updater = Updater(cfgmod.load())
    problem = updater.can_update()
    if problem:
        print(f"This copy can't install it by itself: {problem}.\n"
              f"Install faster-whisper into the Python that runs it:\n    {sys.executable} -m pip install 'faster-whisper>=1.0'")
        return 1
    print("Installing the offline engine. It is a large download, so it takes a few minutes.")
    updater.echo = True
    if not updater.install_offline():
        print(f"\n{updater.offline_status or 'It did not install.'}")
        return 1
    if sys.platform == "win32":  # this command's own files are locked while it runs: a helper installs after it exits
        import subprocess
        subprocess.Popen(["powershell", "-NoProfile", "-Command", updater.windows_helper(start=False) + "; Write-Host 'Done. Choose Offline in the settings.'; pause"],
                         creationflags=0x00000010, close_fds=True)  # CREATE_NEW_CONSOLE: you see it finish
        print("It installs in a new window once this one is done. hold-dictate can start after that.")
        return 0
    print("\nThe offline engine is installed. Choose Offline in the settings to use it.")
    return 0


def _follow_offline(state: dict) -> int:
    """Shows how the running hold-dictate's install goes, until it restarts with the engine."""
    from .settings_ui import running_url
    if state["offline"]["can_install"]:
        print(f"hold-dictate can't install it by itself: {state['offline']['can_install']}.")
        return 1
    print("hold-dictate is installing the offline engine, then restarts. It is a large download, so it takes a few minutes.")
    shown, gone = "", None
    deadline = time.monotonic() + 30 * 60
    while time.monotonic() < deadline:
        time.sleep(1)
        try:
            state = _daemon("/api/state", get=True) if running_url() else None
        except OSError:
            state = None
        if state is None:  # restarting
            gone = gone or time.monotonic()
            if time.monotonic() - gone > 120:
                print("hold-dictate didn't come back. Start it again, then check with: hold-dictate doctor")
                return 1
            continue
        if gone or state["local_installed"]:
            if state["local_installed"]:
                print("The offline engine is installed. Choose Offline in the settings to use it.")
                return 0
            print(f"It didn't install: {state['offline']['status'] or 'hold-dictate restarted without it.'}")
            return 1
        offline = state["offline"]
        line = f"{offline['progress']}%" if offline["progress"] is not None else offline["status"]
        if line and line != shown:
            print(f"  {line}", flush=True)
            shown = line
        if offline["progress"] is None and not offline["ready"] and offline["status"].startswith(("offline install failed", "an update")):
            print(f"It didn't install: {offline['status']}")
            return 1
    print("It is taking too long. Check the settings page.")
    return 1


def cmd_last(args) -> int:
    """Pastes or copies the last dictation again. Bind `hold-dictate paste-last` to a key to use it anywhere."""
    result = _daemon(f"/api/last/{args.action}")
    if result is None:
        print("hold-dictate is not running.")
        return 1
    if result.get("message"):
        print(f"hold-dictate: {result['message']}", file=sys.stderr)
        return 1
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
    s.add_argument("--languages", "--language", dest="languages", help="comma-separated codes, for example he,en")
    s.add_argument("--hotkey")
    s.add_argument("-y", "--yes", action="store_true", help="no questions; start at login")
    s.add_argument("--no-service", action="store_true", help="do not install start-at-login")
    s.set_defaults(fn=cmd_setup)
    st = sub.add_parser("settings", help="open the settings page in your browser")
    st.add_argument("--wait", type=float, default=0, help="seconds to wait for hold-dictate to start")
    st.set_defaults(fn=cmd_settings)
    d = sub.add_parser("doctor", help="check that everything is in place")
    d.add_argument("--offline", action="store_true", help="stay on this computer: the key is not tried with the service")
    d.add_argument("--json", action="store_true", help="print the results as JSON")
    d.set_defaults(fn=cmd_doctor)
    f = sub.add_parser("transcribe", help="transcribe a WAV file and print it")
    f.add_argument("path")
    f.set_defaults(fn=cmd_file)
    sub.add_parser("update", help="install the latest release now").set_defaults(fn=cmd_update)
    sub.add_parser("offline", help="install the offline engine, to dictate without internet").set_defaults(fn=cmd_offline)
    for action in ("paste", "copy"):
        sub.add_parser(f"{action}-last", help=f"{action} the last dictation again").set_defaults(fn=cmd_last, action=action)
    h = sub.add_parser("history", help="list, search, delete or clear saved dictations")
    h.add_argument("action", nargs="?", default="list", choices=["list", "search", "delete", "clear"])
    h.add_argument("value", nargs="?", help="what to search for, or the id to delete")
    h.add_argument("--limit", type=int, default=20, help="how many to show")
    h.add_argument("-y", "--yes", action="store_true", help="clear without asking")
    h.set_defaults(fn=cmd_history)
    sub.add_parser("config", help="print the config file path").set_defaults(fn=cmd_config)
    sv = sub.add_parser("service", help="manage start-at-login")
    sv.add_argument("action", choices=["install", "uninstall", "status"])
    sv.set_defaults(fn=cmd_service)
    extensions.extend_cli(sub)
    args = p.parse_args(argv)
    if not args.command:
        args = p.parse_args(["run"])
    return args.fn(args)
