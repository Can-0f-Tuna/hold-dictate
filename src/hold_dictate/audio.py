"""Microphone capture and the small signal helpers around it."""
import io
import wave

import numpy as np

SAMPLE_RATE = 16000
BLOCK = 320  # 20 ms


class MicError(RuntimeError):
    pass


class Recorder:
    """Captures 16 kHz mono audio until stop(), reporting loudness (0..1) to on_level as it goes."""

    def __init__(self, on_level=None):
        try:
            import sounddevice as sd
        except OSError as e:  # PortAudio missing (Linux: install libportaudio2)
            raise MicError(f"audio library not found: {e}") from e
        self.on_level = on_level
        self.chunks: list[np.ndarray] = []
        try:
            self.stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=BLOCK, callback=self._callback
            )
            self.stream.start()
        except Exception as e:
            raise MicError(f"cannot open the microphone: {e}") from e

    def _callback(self, indata, frames, time_info, status):
        samples = indata[:, 0].copy()
        self.chunks.append(samples)
        if self.on_level:
            self.on_level(level_of(samples))

    def stop(self) -> np.ndarray:
        self.stream.stop()
        self.stream.close()
        if not self.chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(self.chunks).astype(np.float32) / 32768


def level_of(samples: np.ndarray) -> float:
    """-55 dBFS (room noise) maps to 0, -15 dBFS (loud speech) to 1."""
    if not samples.size:
        return 0.0
    rms = float(np.sqrt(np.mean((samples.astype(np.float32) / 32768) ** 2))) + 1e-9
    return min(1.0, max(0.0, (20 * np.log10(rms) + 55) / 40))


class VoiceMeter:
    """Tells speech from room noise in the stream of levels, to stop a recording once you go quiet.

    The noise floor follows the quietest recent level: it drops at once and creeps up slowly, so a noisy
    room is learned within seconds while the gaps between words keep pulling it back down."""

    MARGIN = 0.25  # about 10 dB above the floor counts as speech

    def __init__(self):
        self.smooth = None
        self.floor = 0.0
        self.heard = False  # any speech yet
        self.last = 0.0  # monotonic time of the last speech

    def feed(self, level: float, now: float):
        if self.smooth is None:  # a low start, so talking right away is not taken for room noise
            self.smooth, self.floor = level, min(level, 0.2)
        self.smooth += (level - self.smooth) * 0.3
        self.floor = min(self.floor + 0.001, self.smooth)
        if self.smooth > self.floor + self.MARGIN:
            self.heard, self.last = True, now


def speech_only(audio: np.ndarray, pad_ms=200, gap_ms=500, frame_ms=30):
    """Drops silence so it costs nothing and cannot turn into hallucinated words. None if no speech.

    A frame counts as speech when it is clearly above the quietest part of the clip."""
    frame = SAMPLE_RATE * frame_ms // 1000
    n = len(audio) // frame
    if n == 0:
        return None
    rms = np.sqrt(np.mean(audio[: n * frame].reshape(n, frame) ** 2, axis=1)) + 1e-9
    floor = np.percentile(rms, 10)
    threshold = max(floor * 3, 10 ** (-48 / 20))
    voiced = rms > threshold
    if voiced.sum() < 3:
        return None
    # Close gaps shorter than gap_ms, then pad each run.
    gap, pad = max(1, gap_ms // frame_ms), max(0, pad_ms // frame_ms)
    keep = np.zeros(n, dtype=bool)
    last = -10**9
    idx = np.flatnonzero(voiced)
    for i in idx:
        if i - last <= gap:
            keep[last:i + 1] = True
        keep[i] = True
        last = i
    padded = keep.copy()
    for shift in range(1, pad + 1):
        padded[shift:] |= keep[:-shift]
        padded[:-shift] |= keep[shift:]
    mask = np.repeat(padded, frame)
    return audio[: n * frame][mask]


def wav_bytes(audio: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
    return buf.getvalue()


def read_audio_file(path: str) -> np.ndarray:
    """Reads a 16 kHz-or-other mono/stereo WAV as float32 at 16 kHz (WAV only, no extra dependencies)."""
    with wave.open(path, "rb") as w:
        rate, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        if width != 2:
            raise ValueError("only 16-bit WAV files are supported")
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    if rate != SAMPLE_RATE:
        target = int(len(data) * SAMPLE_RATE / rate)
        data = np.interp(np.linspace(0, len(data) - 1, target), np.arange(len(data)), data).astype(np.float32)
    return data
