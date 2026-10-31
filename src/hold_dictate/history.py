"""The history: past dictations in one file beside config.toml. Text only, never audio, and it never leaves this
computer. Every change replaces the whole file, so a crash leaves the old one or the new one, not half of either."""
import json
import os
import secrets
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from . import config as cfgmod

MAX_ENTRIES = 2000
MAX_BYTES = 4_000_000  # of the file


def path() -> Path:
    return cfgmod.config_dir() / "history.json"


def _spare() -> Path:
    return path().with_name("history.json.tmp")


def days(cfg) -> float:
    """How long to keep text; 0 is Off. A value that makes no sense counts as Off."""
    try:
        return max(0.0, float(cfg.history_days))
    except (TypeError, ValueError):
        return 0.0


@contextmanager
def _locked():
    """One at a time, across processes: `hold-dictate history` and the settings page run beside the app."""
    path().parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path().with_name("history.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
    finally:
        os.close(fd)


def _good(row) -> bool:
    return (isinstance(row, dict) and isinstance(row.get("time"), (int, float)) and not isinstance(row["time"], bool)
            and all(isinstance(row.get(name), str) for name in ("id", "text", "raw", "app")))


def _read() -> list:
    """Oldest first. A damaged file reads as empty: it is a convenience, and it must not stop dictation."""
    try:
        rows = json.loads(path().read_text(encoding="utf-8")).get("entries")
    except FileNotFoundError:
        return []
    except (ValueError, AttributeError) as e:
        print(f"hold-dictate: the history file can't be read, starting a new one: {e}", file=sys.stderr, flush=True)
        return []
    return [row for row in rows if _good(row)] if isinstance(rows, list) else []


def _sync_folder():
    if sys.platform != "win32":  # Windows can't open a folder like this
        fd = os.open(path().parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _write(rows: list):
    spare = _spare()
    fd = os.open(spare, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)  # owner-only from the first byte
    with os.fdopen(fd, "wb") as f:
        if sys.platform != "win32":
            os.fchmod(f.fileno(), 0o600)  # a spare left by a crash could have another mode
        f.write(json.dumps({"entries": rows}, ensure_ascii=False).encode("utf-8"))
        f.flush()
        os.fsync(f.fileno())
    os.replace(spare, path())
    _sync_folder()


def _trim(rows: list, keep_days: float, now: float) -> list:
    """Drops what is past its age, then the oldest until the caps hold. With history Off only the caps apply."""
    rows = sorted(rows, key=lambda row: row["time"])
    if keep_days:
        rows = [row for row in rows if row["time"] >= now - keep_days * 86400]
    room, kept = MAX_BYTES - 16, []
    for row in reversed(rows[-MAX_ENTRIES:]):
        room -= len(json.dumps(row, ensure_ascii=False).encode("utf-8")) + 2
        if room < 0:
            break
        kept.append(row)
    return kept[::-1]


def _utf8(text: str) -> str:
    """A lone surrogate, which a server's JSON can hold, can't be written to the file or sent to the page."""
    return text.encode("utf-8", "replace").decode("utf-8")


def add(cfg, text: str, raw: str, app: str):
    keep_days = days(cfg)
    if not keep_days:
        return
    now = time.time()
    row = {"id": secrets.token_hex(6), "time": now, "app": _utf8(app), "text": _utf8(text), "raw": _utf8(raw)}
    with _locked():
        _write(_trim(_read() + [row], keep_days, now))


def prune(cfg) -> list:
    """Applies the age and the caps to the file. Returns what is left, oldest first."""
    if not path().exists():
        return []
    with _locked():
        rows = _read()
        kept = _trim(rows, days(cfg), time.time())
        if kept != rows:
            _write(kept)
    return kept


def entries(cfg, query: str = "") -> list:
    """Newest first. `query` is any part of the text, what was heard or the app, in any case."""
    rows = prune(cfg)
    needle = query.strip().casefold()
    if needle:
        rows = [row for row in rows if needle in f"{row['text']}\n{row['raw']}\n{row['app']}".casefold()]
    return rows[::-1]


def delete(entry_id: str) -> bool:
    if not path().exists():
        return False
    with _locked():
        rows = _read()
        kept = [row for row in rows if row["id"] != entry_id]
        if len(kept) == len(rows):
            return False
        _write(kept)
    return True


def clear():
    with _locked():
        path().unlink(missing_ok=True)
        _spare().unlink(missing_ok=True)
        _sync_folder()
