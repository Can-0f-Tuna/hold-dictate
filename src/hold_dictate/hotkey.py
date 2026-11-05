"""Hold-to-talk hotkey detection.

HoldDetector is pure logic (fed key up/down events by name). Backends supply the events:
  - EvdevBackend reads /dev/input directly: works on Wayland and X11, needs the `input` group.
  - PynputBackend uses the OS keyboard hooks on Windows and macOS (and Linux X11).
"""
import os
import queue
import select
import struct
import sys
import threading
import time

MODIFIERS = ("alt", "ctrl", "shift", "super")
ALIASES = {
    "option": "alt", "opt": "alt", "control": "ctrl", "cmd": "super", "command": "super",
    "win": "super", "windows": "super", "meta": "super",
}


class Hotkey:
    def __init__(self, mods: frozenset, key: str):
        self.mods, self.key = mods, key

    def __str__(self):
        return "+".join([m for m in MODIFIERS if m in self.mods] + [self.key])


def parse_hotkey(text: str) -> Hotkey:
    parts = [ALIASES.get(p.strip().lower(), p.strip().lower()) for p in text.split("+") if p.strip()]
    if not parts:
        raise ValueError("empty hotkey")
    *mods, key = parts
    for m in mods:
        if m not in MODIFIERS:
            raise ValueError(f"'{m}' is not a modifier (use alt, ctrl, shift, super)")
    if key in MODIFIERS:
        raise ValueError("the hotkey needs a normal key last, for example alt+space")
    valid = key == "space" or (len(key) == 1 and key.isalnum()) or (key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 12)
    if not valid:
        raise ValueError(f"unsupported key '{key}' (use space, a-z, 0-9 or f1-f12)")
    if not mods and not key.startswith("f"):
        raise ValueError("a plain key would trigger while typing; add a modifier such as alt+")
    return Hotkey(frozenset(mods), key)


class HoldDetector:
    """Turns raw key events into actions. Letting go of any part of the hotkey stops.

    The dictation hotkey gives 'start' and 'stop'; the optional second hotkey gives 'second:start' and
    'second:stop'. When both match the keys held (alt+space inside alt+shift+space), the one with more
    modifiers wins. Only one is active at a time."""

    def __init__(self, hotkey: Hotkey, second: Hotkey | None = None):
        self.hotkey = hotkey
        self.second = second
        self.down: dict[str, set] = {}  # name -> physical key ids currently held
        self.active = False
        self.which = ""  # "" for the dictation hotkey, "second" for the second hotkey, while active

    def hotkeys(self) -> list:
        return [h for h in (self.hotkey, self.second) if h is not None]

    def _stop_if(self, which: str) -> str | None:
        if self.active and self.which == which:
            self.active = False
            return "second:stop" if which else "stop"
        return None

    def set_hotkey(self, hotkey: Hotkey) -> str | None:
        """Switches the hotkey while running. Returns 'stop' if a recording with it was in progress."""
        action, self.hotkey = self._stop_if(""), hotkey
        return action

    def set_second_hotkey(self, hotkey: Hotkey | None) -> str | None:
        """Switches (or with None, turns off) the second hotkey. Returns 'second:stop' if it was held."""
        action, self.second = self._stop_if("second"), hotkey
        return action

    def is_down(self, name: str) -> bool:
        return bool(self.down.get(name))

    def modifiers_held(self) -> bool:
        return any(self.is_down(m) for m in self.hotkey.mods)

    def any_modifier_held(self) -> bool:
        return any(self.is_down(m) for m in MODIFIERS)

    def wait_released(self, seconds: float = 1.0) -> bool:
        """Waits until no modifier is held (the backend thread feeds the events). True if they all are up."""
        deadline = time.monotonic() + seconds
        while self.any_modifier_held():
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)
        return True

    def feed(self, name: str, ident, pressed: bool):
        """Returns 'start', 'stop', 'second:start', 'second:stop' or None."""
        ids = self.down.setdefault(name, set())
        if pressed:
            ids.add(ident)
            if self.active:
                return None
            matches = [(len(h.mods), which) for which, h in (("", self.hotkey), ("second", self.second))
                       if h is not None and name == h.key and all(self.is_down(m) for m in h.mods)]
            if matches:
                self.active, self.which = True, max(matches, key=lambda m: m[0])[1]  # a tie: dictation
                return "second:start" if self.which else "start"
        else:
            ids.discard(ident)
            current = self.second if self.which else self.hotkey
            if self.active and current is not None and (name == current.key or name in current.mods):
                self.active = False
                return "second:stop" if self.which else "stop"
        return None


# ---------- Linux evdev ----------

EV_FORMAT = "llHHi"
EV_SIZE = struct.calcsize(EV_FORMAT)
EV_KEY = 1
_LETTER_ROWS = {"q": 16, "a": 30, "z": 44}
EVDEV_CODES = {
    "alt": (56, 100), "ctrl": (29, 97), "shift": (42, 54), "super": (125, 126), "space": (57,),
}
for _row, _start in _LETTER_ROWS.items():
    _letters = {"q": "qwertyuiop", "a": "asdfghjkl", "z": "zxcvbnm"}[_row]
    for _i, _ch in enumerate(_letters):
        EVDEV_CODES[_ch] = (_start + _i,)
for _i, _ch in enumerate("1234567890"):
    EVDEV_CODES[_ch] = (2 + _i,)
for _i in range(1, 11):
    EVDEV_CODES[f"f{_i}"] = (58 + _i,)
EVDEV_CODES["f11"], EVDEV_CODES["f12"] = (87,), (88,)
CODE_TO_NAME = {code: name for name, codes in EVDEV_CODES.items() for code in codes}


def keyboard_devices() -> list[str]:
    paths = []
    try:
        blocks = open("/proc/bus/input/devices").read().split("\n\n")
    except OSError:
        return paths
    for block in blocks:
        for line in block.splitlines():
            if line.startswith("H: Handlers="):
                handlers = line.split("=", 1)[1].split()
                if "kbd" in handlers:
                    paths += ["/dev/input/" + h for h in handlers if h.startswith("event")]
    return paths


class EvdevBackend:
    name = "evdev"

    def __init__(self, devices=None):
        self.devices, self.unreadable = [], []
        if devices is not None:  # tests pass file objects
            self.devices = devices
            return
        for path in keyboard_devices():
            try:
                self.devices.append(open(path, "rb", buffering=0))
            except PermissionError:
                self.unreadable.append(path)

    def usable(self) -> bool:
        return bool(self.devices)

    def run(self, detector: HoldDetector, on_action, on_tick=None, tick_seconds: float = 5.0):
        while self.devices:
            ready, _, _ = select.select(self.devices, [], [], tick_seconds if on_tick else None)
            if not ready:
                on_tick()
                continue
            for dev in ready:
                raw = dev.read(EV_SIZE)
                if len(raw) < EV_SIZE:  # device went away (or a test pipe closed)
                    self.devices.remove(dev)
                    continue
                _, _, ev_type, code, value = struct.unpack(EV_FORMAT, raw)
                name = CODE_TO_NAME.get(code)
                if ev_type != EV_KEY or value == 2 or name is None:
                    continue
                action = detector.feed(name, code, value == 1)
                if action:
                    on_action(action)
            if on_tick:
                on_tick()


# ---------- Windows / macOS / X11 via pynput ----------

_WIN_VK = {"alt": (0x12, 0xA4, 0xA5), "ctrl": (0x11, 0xA2, 0xA3), "shift": (0x10, 0xA0, 0xA1), "super": (0x5B, 0x5C)}
_MAC_FLAGS = {"alt": 0x80000, "ctrl": 0x40000, "shift": 0x20000, "super": 0x100000}


def _pynput_name(key) -> str | None:
    from pynput import keyboard
    special = {
        "alt": "alt", "alt_l": "alt", "alt_r": "alt", "alt_gr": "alt",
        "ctrl": "ctrl", "ctrl_l": "ctrl", "ctrl_r": "ctrl",
        "shift": "shift", "shift_l": "shift", "shift_r": "shift",
        "cmd": "super", "cmd_l": "super", "cmd_r": "super", "space": "space",
    }
    if isinstance(key, keyboard.Key):
        return special.get(key.name) or (key.name if key.name.startswith("f") and key.name[1:].isdigit() else None)
    char = getattr(key, "char", None)
    return char.lower() if char else None


class PynputBackend:
    name = "pynput"

    def usable(self) -> bool:
        try:
            import pynput.keyboard  # noqa: F401
            return True
        except Exception:
            return False

    def run(self, detector: HoldDetector, on_action, on_tick=None, tick_seconds: float = 5.0):
        from pynput import keyboard

        win_state: set = set()
        actions: queue.Queue = queue.Queue()

        def deliver():
            # The OS calls the hooks below with the keyboard waiting on them: Windows drops a slow low-level hook,
            # and macOS turns off a tap that takes too long (a first recording waits for the Microphone prompt).
            # So they only note the key; what it starts runs here.
            while True:
                action = actions.get()
                try:
                    on_action(action)
                except Exception as e:
                    print(f"hold-dictate: {action} failed: {e!r}", file=sys.stderr, flush=True)

        threading.Thread(target=deliver, daemon=True, name="hotkey actions").start()

        def handle(key, pressed):
            name = _pynput_name(key)
            if name is None:
                return
            action = detector.feed(name, name, pressed)
            if action:
                actions.put(action)

        def win_filter(msg, data):
            # Alt+Space opens the window menu on Windows; hide the Space press while dictating.
            vk, down = data.vkCode, msg in (0x100, 0x104)
            for mod, codes in _WIN_VK.items():
                if vk in codes:
                    (win_state.add if down else win_state.discard)(mod)
            # read each time: the settings page can change the hotkeys while running
            if vk == 0x20 and any(h.key == "space" and h.mods <= win_state for h in detector.hotkeys()):
                listener.suppress_event()
            return True

        def mac_intercept(event_type, event):
            # Option+Space types a non-breaking space on macOS; swallow Space while dictating.
            try:
                import Quartz
                if event_type in _MAC_TAP_OFF:
                    _mac_enable_tap(listener, "it was turned off" if event_type == _MAC_TAP_OFF[1] else "it was too slow")
                    return event
                code = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
                flags = Quartz.CGEventGetFlags(event)
                if code == 49 and any(h.key == "space" and all(flags & _MAC_FLAGS[m] for m in h.mods) for h in detector.hotkeys()):
                    return None
            except Exception:
                pass
            return event

        extra, listener_class = {}, keyboard.Listener
        if sys.platform == "win32":
            extra["win32_event_filter"] = win_filter
        elif sys.platform == "darwin":
            extra["darwin_intercept"] = mac_intercept
            listener_class = _mac_listener_class(keyboard.Listener)
        listener = listener_class(
            on_press=lambda k: handle(k, True), on_release=lambda k: handle(k, False), **extra
        )
        listener.start()
        while listener.is_alive():
            time.sleep(min(tick_seconds, 1.0))
            if sys.platform == "darwin":
                _mac_check_tap(listener)
            if on_tick:
                on_tick()


# kCGEventTapDisabledByTimeout, kCGEventTapDisabledByUserInput: macOS tells the tap it was switched off this way
_MAC_TAP_OFF = (0xFFFFFFFE, 0xFFFFFFFF)


def _mac_listener_class(base):
    """pynput's listener, keeping its event tap where we can switch it back on: pynput never does."""
    class Listener(base):
        tap = None

        def _create_event_tap(self):
            self.tap = super()._create_event_tap()
            return self.tap
    return Listener


def _mac_enable_tap(listener, why: str):
    import Quartz
    if listener.tap is not None:
        Quartz.CGEventTapEnable(listener.tap, True)
        print(f"hold-dictate: macOS turned the keyboard tap off ({why}); it is back on", file=sys.stderr, flush=True)


def _mac_check_tap(listener):
    """The tap's own notice can be missed; once a second, a tap that is off is switched back on."""
    try:
        import Quartz
        if listener.tap is not None and not Quartz.CGEventTapIsEnabled(listener.tap):
            _mac_enable_tap(listener, "found off")
    except Exception:
        pass


def choose_backend():
    """Returns (backend or None, problem text)."""
    if sys.platform.startswith("linux"):
        evdev = EvdevBackend()
        if evdev.usable():
            return evdev, ""
        pynput_ok = bool(os.environ.get("DISPLAY")) and not os.environ.get("WAYLAND_DISPLAY")
        if pynput_ok and PynputBackend().usable():
            return PynputBackend(), ""
        return None, (
            "cannot read the keyboard. Add your user to the 'input' group, then log out and back in:\n"
            "    sudo usermod -aG input $USER"
        )
    backend = PynputBackend()
    if backend.usable():
        return backend, ""
    return None, "the 'pynput' package is not available"
