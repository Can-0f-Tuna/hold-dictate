#!/usr/bin/env python3
"""Hold Alt+Space to dictate: record while Space is held, transcribe on release, type at the cursor.

Reads the keyboard straight from evdev (niri has no key-release bindings), so the
user must be in the `input` group. Usage:
  dictate.py            run the daemon
  dictate.py FILE.wav   transcribe a file and print it (no typing)
"""
import gc
import io
import json
import os
import select
import signal
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import wave

import ctranslate2
import numpy as np
from faster_whisper import WhisperModel
from faster_whisper.tokenizer import Tokenizer
from faster_whisper.vad import VadOptions, collect_chunks, get_speech_timestamps

# Groq's hosted Whisper large-v3 answers in about a second (free tier: ~2,000 requests a day).
# Without a key, or when it fails, the local model below is used instead.
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("DICTATE_GROQ_MODEL", "whisper-large-v3")
GROQ_TIMEOUT = 8  # seconds before falling back to the local model
# Groq gets no language hint: forcing "he" makes Whisper drop or mangle English words. Instead a
# mixed Hebrew/English prompt nudges it to keep Hebrew in Hebrew and English words in Latin letters.
GROQ_PROMPT = os.environ.get(
    "DICTATE_GROQ_PROMPT",
    "שלום, אני כותב בעברית ומשלב מילים באנגלית כמו pull request, GitHub ו-deploy.",
)
# Whisper turbo fine-tuned on Hebrew by ivrit.ai; far fewer misheard words than the stock model.
MODEL = os.environ.get("DICTATE_MODEL", "ivrit-ai/whisper-large-v3-turbo-ct2")
LANG = os.environ.get("DICTATE_LANG", "he")
MIN_HOLD = 0.3  # seconds; shorter presses are ignored as accidental taps
IDLE_UNLOAD = float(os.environ.get("DICTATE_IDLE_SECONDS", "900"))  # free the model after this long unused

OVERLAY_QML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "overlay.qml")
EVENTS_FILE = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "hold-dictate.events")
CHUNK_BYTES = 640  # 20 ms of 16 kHz s16 mono, one level sample for the overlay
SAMPLE_RATE = 16000
LAST_WAV = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "hold-dictate-last.wav")
SHORT_CLIP = 28.0  # seconds of speech; shorter clips get a trimmed encoder window (much faster on CPU)

EV_FORMAT = "llHHi"  # struct input_event on 64-bit: timeval sec, usec, type, code, value
EV_SIZE = struct.calcsize(EV_FORMAT)
EV_KEY = 1
KEY_SPACE = 57
KEY_LEFTALT, KEY_RIGHTALT = 56, 100
KEY_RELEASE, KEY_PRESS = 0, 1
AUTOREPEAT = 2


def keyboard_devices():
    """Event nodes of every device the kernel reports as a keyboard."""
    paths = []
    for block in open("/proc/bus/input/devices").read().split("\n\n"):
        for line in block.splitlines():
            if line.startswith("H: Handlers="):
                handlers = line.split("=", 1)[1].split()
                if "kbd" in handlers:
                    paths += ["/dev/input/" + h for h in handlers if h.startswith("event")]
    return paths


class Overlay:
    """The quickshell pill at the bottom of the screen. It runs for the whole session (so it appears
    instantly) and is driven through a small events file."""

    def __init__(self):
        self.events = open(EVENTS_FILE, "w", buffering=1)
        self.proc = None
        self.ensure_running()

    def ensure_running(self):
        if self.proc and self.proc.poll() is None:
            return
        try:
            self.proc = subprocess.Popen(
                ["quickshell", "-p", OVERLAY_QML],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as e:
            print(f"overlay unavailable: {e}", flush=True)

    def send(self, line):
        self.events.write(line + "\n")

    def show(self):
        self.ensure_running()
        self.send("S")

    def hide(self):
        self.send("Q")


class Recorder:
    """Streams 16 kHz mono PCM from pw-record, keeping it in memory and reporting loudness as it goes."""

    def __init__(self, on_level):
        self.on_level = on_level
        self.chunks = []
        self.proc = subprocess.Popen(
            ["pw-record", "--raw", "--rate", "16000", "--channels", "1", "--format", "s16",
             "--latency", "20ms", "-"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        while True:
            chunk = self.proc.stdout.read(CHUNK_BYTES)
            if not chunk:
                return
            self.chunks.append(chunk)
            samples = np.frombuffer(chunk[: len(chunk) // 2 * 2], dtype=np.int16)
            if samples.size:
                rms = np.sqrt(np.mean((samples / 32768) ** 2)) + 1e-9
                # -55 dBFS (room noise) maps to 0, -15 dBFS (loud speech) to 1.
                self.on_level(min(1.0, max(0.0, (20 * np.log10(rms) + 55) / 40)))

    def stop(self):
        """Ends the recording and returns it as float32 samples in -1..1."""
        self.proc.send_signal(signal.SIGINT)
        self.thread.join()
        self.proc.wait()
        raw = b"".join(self.chunks)
        return np.frombuffer(raw[: len(raw) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768


def speech_only(audio):
    """Drops the silences, so they cost nothing and cannot turn into hallucinated words. None if no speech."""
    vad = VadOptions(min_silence_duration_ms=500, speech_pad_ms=200)
    chunks = get_speech_timestamps(audio, vad)
    if not chunks:
        return None
    return np.concatenate(collect_chunks(audio, chunks)[0])


def wav_bytes(audio):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
    return buf.getvalue()


def transcribe_groq(speech):
    boundary = uuid.uuid4().hex
    fields = {"model": GROQ_MODEL, "prompt": GROQ_PROMPT, "response_format": "json", "temperature": "0"}
    body = b"".join(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        for k, v in fields.items()
    )
    body += (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="speech.wav"\r\n'
        "Content-Type: audio/wav\r\n\r\n"
    ).encode() + wav_bytes(speech) + f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(
        "https://api.groq.com/openai/v1/audio/transcriptions",
        data=body,
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "hold-dictate",
        },
    )
    with urllib.request.urlopen(request, timeout=GROQ_TIMEOUT) as response:
        return json.load(response)["text"].strip()


def transcribe_local(model, speech):
    if len(speech) / SAMPLE_RATE > SHORT_CLIP:
        segments, _ = model.transcribe(speech, language=LANG, beam_size=5, vad_filter=False)
        return " ".join(s.text.strip() for s in segments).strip()

    # Whisper normally pads every clip to 30 s, and on this CPU the encoder spends ~8 s on that
    # window. Encoding only the clip plus a second of margin is ~6x faster for a short dictation.
    features = model.feature_extractor(speech)
    frames = features.shape[1] + 200
    frames += frames % 2
    features = np.pad(features, ((0, 0), (0, frames - features.shape[1])))
    encoded = model.model.encode(
        ctranslate2.StorageView.from_array(np.ascontiguousarray(features[None])), to_cpu=False
    )
    tokenizer = Tokenizer(model.hf_tokenizer, True, task="transcribe", language=LANG)
    result = model.model.generate(
        encoded,
        [list(tokenizer.sot_sequence) + [tokenizer.no_timestamps]],
        beam_size=5,
        # A trimmed window makes Whisper prone to looping on one sentence; cap the length to what
        # this much speech can hold and forbid repeating any 4-token run.
        max_length=min(224, 20 + int(len(speech) / SAMPLE_RATE * 30)),
        no_repeat_ngram_size=4,
        repetition_penalty=1.1,
        suppress_blank=True,
        suppress_tokens=[-1],
    )
    return tokenizer.decode([t for t in result[0].sequences_ids[0] if t < tokenizer.eot]).strip()


def save_wav(audio):
    """Keeps the last recording (in RAM-backed $XDG_RUNTIME_DIR) for checking transcription quality."""
    with open(LAST_WAV, "wb") as f:
        f.write(wav_bytes(audio))


# Terminals paste with Ctrl+Shift+V; everything else with Ctrl+V.
TERMINALS = ("alacritty", "konsole", "kitty", "foot", "ghostty", "wezterm", "ptyxis", "terminal")


def focused_app():
    try:
        out = subprocess.run(["niri", "msg", "-j", "focused-window"], capture_output=True, text=True, timeout=1)
        return (json.loads(out.stdout) or {}).get("app_id") or ""
    except Exception:
        return ""


def type_text(text):
    """Pastes the text into the focused field through the clipboard, then puts the old clipboard back.
    (Typing it with wtype dropped Hebrew letters such as ב, א and מ.)"""
    old = subprocess.run(["wl-paste", "-n", "-t", "text"], capture_output=True, timeout=1)
    subprocess.run(["wl-copy", "--", text])
    app = focused_app().lower()
    if any(t in app for t in TERMINALS):
        subprocess.run(["wtype", "-M", "ctrl", "-M", "shift", "v", "-m", "shift", "-m", "ctrl"])
    else:
        subprocess.run(["wtype", "-M", "ctrl", "v", "-m", "ctrl"])
    time.sleep(0.3)  # let the app read the clipboard before restoring it
    if old.returncode == 0 and old.stdout:
        subprocess.run(["wl-copy"], input=old.stdout)
    else:
        subprocess.run(["wl-copy", "--clear"])


def load_model():
    print(f"loading {MODEL}", flush=True)
    return WhisperModel(MODEL, device="cpu", compute_type="int8", cpu_threads=os.cpu_count() // 2)


def main():
    if len(sys.argv) > 1:
        from faster_whisper import decode_audio
        speech = speech_only(decode_audio(sys.argv[1]))
        if speech is not None:
            print(transcribe_groq(speech) if GROQ_API_KEY else transcribe_local(load_model(), speech))
        return

    devices = []
    for path in keyboard_devices():
        try:
            devices.append(open(path, "rb", buffering=0))
        except PermissionError:
            print(f"cannot read {path} (add this user to the 'input' group)", flush=True)
    if not devices:
        sys.exit("no readable keyboard devices")
    print(f"listening on {len(devices)} keyboard device(s): hold Alt+Space", flush=True)

    # The model is loaded in the background on the first press and freed after IDLE_UNLOAD seconds unused.
    model = {}
    loader = None
    overlay = Overlay()
    recorder = None
    alt_down = False
    pressed_at = 0.0
    last_used = 0.0
    while True:
        idle = recorder is None and "m" in model
        timeout = max(0.0, last_used + IDLE_UNLOAD - time.monotonic()) if idle else None
        ready, _, _ = select.select(devices, [], [], timeout)
        if not ready:
            if idle and time.monotonic() - last_used >= IDLE_UNLOAD:
                model.clear()
                gc.collect()
                print("model unloaded after idle", flush=True)
            continue
        for dev in ready:
            _, _, ev_type, code, value = struct.unpack(EV_FORMAT, dev.read(EV_SIZE))
            if ev_type != EV_KEY or value == AUTOREPEAT:
                continue
            is_alt = code in (KEY_LEFTALT, KEY_RIGHTALT)
            if is_alt:
                alt_down = value == KEY_PRESS
            if value == KEY_PRESS and code == KEY_SPACE and recorder is None and alt_down:
                overlay.show()
                recorder = Recorder(lambda level: overlay.send(f"L {level:.2f}"))
                pressed_at = last_used = time.monotonic()
                if not GROQ_API_KEY and "m" not in model and (loader is None or not loader.is_alive()):
                    # Loads while the user is still speaking.
                    loader = threading.Thread(target=lambda: model.setdefault("m", load_model()), daemon=True)
                    loader.start()
            elif value == KEY_RELEASE and recorder is not None and (code == KEY_SPACE or is_alt):
                # Letting go of either key of the shortcut ends the recording.
                audio = recorder.stop()
                recorder = None
                if time.monotonic() - pressed_at < MIN_HOLD:
                    overlay.hide()
                    continue
                overlay.send("T")
                save_wav(audio)
                started = time.monotonic()
                speech = speech_only(audio)
                text, source = "", "-"
                if speech is not None and GROQ_API_KEY:
                    try:
                        text, source = transcribe_groq(speech), "groq"
                    except Exception as e:  # offline, rate-limited, bad key: fall back to local
                        print(f"groq failed ({e}), using local model", flush=True)
                if speech is not None and source == "-":
                    if loader:
                        loader.join()
                    if "m" not in model:
                        model["m"] = load_model()
                    text, source = transcribe_local(model["m"], speech), "local"
                last_used = time.monotonic()
                print(f"transcribed ({source}) in {last_used - started:.1f}s: {text!r}", flush=True)
                overlay.hide()
                if text:
                    type_text(text)

if __name__ == "__main__":
    main()
