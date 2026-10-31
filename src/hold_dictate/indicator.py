"""The recording pill on macOS and Windows: a helper process that shows what overlay.qml shows on Linux.

A helper, not a window in this process: macOS wants windows on the main thread and the tray holds ours, and a
helper that hangs or crashes can't take dictation with it. It starts with the app, so the pill opens at once, and
sleeps between recordings. It gets overlay.qml's event lines (C, S, L, T, E, Q) on stdin and leaves when the pipe closes.

Stdlib only: the helper imports this file, and it should start fast.
"""
import math
import queue
import subprocess
import sys
import threading
from collections import deque

MAX_STARTS = 3  # a helper that keeps dying is left dead: the beeps and notifications still work
BACKLOG = 256  # lines waiting for the helper; more are dropped, the audio callback must never wait


def supported() -> bool:
    return sys.platform in ("darwin", "win32")


class Indicator:
    """The app's end of the pipe. Nothing here blocks or raises, whatever the helper does."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.proc = None
        self.starts = 0
        self.lines = queue.Queue(BACKLOG)
        threading.Thread(target=self._pump, daemon=True).start()
        self.send("Q")  # starts the helper now, so the first recording doesn't wait for it

    def send(self, line: str):
        if self.starts >= MAX_STARTS and not self._alive():
            return
        # Turned off in the settings: nothing new opens, but a pill that is showing still gets to close.
        if not self.cfg.desktop_indicator and (self.proc is None or line[0] in "SE"):
            return
        try:
            self.lines.put_nowait(line)
        except queue.Full:
            pass

    def _alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _start(self):
        self.starts += 1
        try:
            self.proc = subprocess.Popen(
                [sys.executable, "-m", "hold_dictate.indicator"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                text=True, bufsize=1, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as e:
            print(f"hold-dictate: the recording pill didn't start: {e}", file=sys.stderr, flush=True)

    def _pump(self):
        """Its own thread: starting the helper, and writing to one that is stuck, can both take a while."""
        while True:
            line = self.lines.get()
            if not self._alive() and self.cfg.desktop_indicator and self.starts < MAX_STARTS:
                self._start()
            if self._alive():
                try:
                    self.proc.stdin.write(line + "\n")
                except (OSError, ValueError):
                    pass  # it died; the next line finds out and starts another


def _rgb(text: str) -> tuple:
    return tuple(int(text[i:i + 2], 16) / 255 for i in (1, 3, 5))


class Pill:
    """What the pill looks like at each moment, in logical pixels at size 1, as in overlay.qml. The window around it
    is up to indicator_mac and indicator_win. `feed` is for the thread reading stdin; everything else is for the
    thread that draws, so a look change can't land in the middle of a frame."""

    WIDTH, HEIGHT = 224, 56
    MARK, MARK_GAP = 48, 10  # the round problem mark beside the pill
    FIELD_W, FIELD_H = 192, 40
    CELL, DOT, PITCH = 2.5, 2.1, 4  # the dots: a bar is 3 columns of dots, then a gap of 1
    DOT_BARS, LINE_BARS = 19, 27
    PROBLEM = (0xe5 / 255, 0x34 / 255, 0x3c / 255)
    BORDER = (0x2a / 255, 0x2a / 255, 0x2e / 255)
    WARM_UP = 0.45  # a problem can come the moment the pill opens: the bars move a little first
    STAY = 2.6  # how long a problem shows

    def __init__(self):
        self.waiting = deque(maxlen=512)
        self.bg, self.bars = _rgb("#0a0a0b"), _rgb("#6b80ff")
        self.top, self.scale, self.lines = False, 1.0, False
        self.seed = [0.7 + 0.3 * abs(math.fmod(math.sin(i * 12.9898) * 43758.5453, 1)) for i in range(self.LINE_BARS)]
        self.active = False
        self.t = 0.0
        self.icon = ""  # alert | mic | net while a problem shows
        self._reset()

    def _reset(self):
        n = self.count()
        self.heights = [0.035] * n
        self.history = [0.0] * ((n + 1) // 2 + 1)
        self.shown = 0.0
        self.level = self.envelope = 0.0
        self.transcribing = self.flat = False
        self.red = self.mark = 0.0
        self.opened = self.problem_at = 0.0
        self.icon = ""

    def count(self) -> int:
        return self.LINE_BARS if self.lines else self.DOT_BARS

    def feed(self, line: str):
        self.waiting.append(line)

    def _handle(self, line: str):
        parts = line.split()
        kind = parts[0] if parts else ""
        if kind == "C" and len(parts) >= 5:
            try:
                bg, bars, scale = _rgb(parts[1]), _rgb(parts[2]), float(parts[4])
            except ValueError:
                return
            lines = parts[5:6] == ["lines"]
            self.bg, self.bars, self.top, self.scale = bg, bars, parts[3] == "top", min(1.5, max(0.75, scale))
            if lines != self.lines:
                self.lines = lines
                self._reset()
        elif kind == "L" and len(parts) > 1:
            try:
                self.level = float(parts[1])
            except ValueError:
                pass
        elif kind == "S":
            shown = self.shown
            self._reset()
            self.shown = shown  # already showing: don't blink
            self.active, self.opened = True, self.t
        elif kind == "T":
            self.transcribing, self.level = True, 0.0
        elif kind == "E":
            if not self.active:
                self.active, self.opened = True, self.t
            self.icon = parts[1] if len(parts) > 1 else "alert"
            self.problem_at = max(self.t, self.opened + self.WARM_UP)
        elif kind == "Q" and not self.icon:
            self.active = False

    def read_stdin(self, wake, leave):
        """The helper's reader thread. Levels wait for the next frame; the rest may open or close the pill."""
        for line in sys.stdin:
            self.feed(line)
            if not line.startswith("L"):
                wake()
        leave()  # the app is gone

    def wants_frames(self) -> bool:
        return self.active or self.shown > 0

    def step(self, dt: float):
        """Takes in what arrived and moves everything on by dt seconds."""
        while self.waiting:
            self._handle(self.waiting.popleft())
        if not self.wants_frames():
            return
        self.t += dt
        problem_on = self.icon and self.t >= self.problem_at
        warming = self.icon and not problem_on
        if problem_on:
            self.flat = True
            self.red += (1 - self.red) * min(1, 12 * dt)
            self.mark += (1 - self.mark) * min(1, 14 * dt)
            if self.t >= self.problem_at + self.STAY:
                self.active = False
        # opens quickly, closes a touch slower
        self.shown += ((1 if self.active else 0) - self.shown) * min(1, (22 if self.active else 16) * dt)
        if self.active and self.shown > 0.99:
            self.shown = 1.0
        elif not self.active and self.shown < 0.02:
            self._reset()
            return

        # The same motion as overlay.qml: a follower of the level, whose history spreads out from the middle.
        level = max(self.level, 0.28 + 0.2 * math.sin(self.t * 11)) if warming else self.level
        self.envelope += (level - self.envelope) * min(1, (30 if level > self.envelope else 9) * dt)
        hist, half = self.history, len(self.history) - 1
        for k in range(half, 0, -1):
            hist[k] += (hist[k - 1] - hist[k]) * min(1, 16 * dt)
        hist[0] = self.envelope
        n, mid, t = len(self.heights), (len(self.heights) - 1) / 2, self.t
        sweep = ((t * 1.1) % 1.6 - 0.3) * n  # a soft pulse going left to right while the text is worked out
        for i in range(n):
            d = abs(i - mid)
            old = hist[min(half, round(d * half / mid))]
            shimmer = 0.78 + 0.22 * math.sin(t * 9 + i * 1.7)
            if self.lines:
                if self.flat:
                    target = 0.0
                elif self.transcribing:
                    target = 0.3 * math.exp(-(((i - sweep) / 2.6) ** 2))
                else:
                    target = old * (1 - (d / (mid + 1)) ** 2 * 0.55) * shimmer
            else:
                taper = 1 - (d / mid) ** 2 * 0.7
                syllable = 0.5 + 0.5 * math.sin(t * 1.3 - d * 0.29) * math.sin(t * 0.47 + d * 0.11 + 1.3)
                rest = 0.08 * taper * (0.3 + 0.7 * syllable) * self.seed[i] * (0.8 + 0.2 * math.sin(t * 5.1 + i * 0.9))
                if self.flat:
                    target = 0.035
                elif self.transcribing:
                    target = 0.035 + max(rest * 0.45, 0.42 * math.exp(-(((i - sweep) / (n / 18 + 0.6)) ** 2)))
                else:
                    target = 0.035 + max(rest, min(1, 1.8 * old) * taper * self.seed[i] * shimmer * 0.97)
            self.heights[i] += (target - self.heights[i]) * min(1, 20 * dt)

    def bar_color(self) -> tuple:
        return tuple(a + (b - a) * self.red for a, b in zip(self.bars, self.PROBLEM))

    def shapes(self) -> list:
        """The bars as (x, y, width, height, alpha) inside the field. Dots are squares, lines are rounded."""
        out = []
        if self.lines:
            x0 = (self.FIELD_W - self.LINE_BARS * 6 + 3) / 2
            for i, v in enumerate(self.heights):
                h = 3 + v * 30
                out.append((x0 + i * 6, (self.FIELD_H - h) / 2, 3, h, 0.45 + 0.55 * min(1, v * 1.6)))
            return out
        cell, rows = self.CELL, int(self.FIELD_H / self.CELL)
        x0 = (self.FIELD_W - self.DOT_BARS * self.PITCH * cell + cell) / 2
        y0 = (self.FIELD_H - rows * cell) / 2
        for i, v in enumerate(self.heights):
            h = max(1, v * (rows / 2 - 1))
            cx = x0 + i * self.PITCH * cell
            for r in range(rows):
                y = abs(r + 0.5 - rows / 2)
                if y > h + 0.5:
                    continue
                size = self.DOT * (0.45 + 0.55 * (1 - (y / (h + 0.5)) ** 1.4 * 0.85))  # dense core, finer tips
                off = (cell - size) / 2
                for k in range(3):
                    out.append((cx + k * cell + off, y0 + r * cell + off, size, size, 1.0))
        return out


def main():
    if sys.platform == "darwin":
        from .indicator_mac import run
    elif sys.platform == "win32":
        from .indicator_win import run
    else:
        raise SystemExit("the helper is for macOS and Windows; Linux has overlay.qml")
    run(Pill())


if __name__ == "__main__":
    main()
