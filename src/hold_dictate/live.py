"""Live typing: the words go into the text box you are in while you speak.

Whisper services take a finished clip, not a stream, so this sends the phrase in progress every few seconds.
Typed text can't be taken back, and the last words of a clip are the ones Whisper still changes, so they wait
for the next request. A phrase that ends in a pause is typed in full and the next one starts, which keeps each
request short."""
import sys
import threading
import time
from collections import deque

from . import audio

SHORTEST = 2 * audio.SAMPLE_RATE  # on less than this Whisper guesses, and a guess that gets typed stays
LONGEST = 20 * audio.SAMPLE_RATE  # no pause for this long: cut anyway, or requests keep growing
UNSURE_WORDS = 2  # held back from the end of a phrase still being spoken


class LiveTyper:
    FAST = 1.5  # seconds between the first requests, or a short sentence shows nothing until you finish
    FAST_STEPS = 6
    GAP = 3.7  # seconds between the rest
    AFTER_ERROR = 10  # seconds to wait after a failed request, such as the limit below
    PER_MINUTE = 18  # Groq's free tier allows 20 a minute; this leaves room for the last pass
    sent = deque()  # when requests went out, across recordings: dictating again must not break the limit

    def __init__(self, recorder, transcribe, clean, type_text, ready=lambda: True, gap: float = GAP, per_minute: int = PER_MINUTE):
        self.recorder = recorder
        self.transcribe = transcribe  # audio -> text
        self.clean = clean  # text -> text, the usual clean-up
        self.type_text = type_text  # text -> '' or a note saying it could not be typed
        self.ready = ready  # False while typing would collide with the keys you hold
        self.gap = gap
        self.per_minute = per_minute  # 0: no limit
        self.steps = 0  # requests in this recording
        self.not_before = 0.0  # no requests until then
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.typed = ""  # everything so far
        self.note = ""  # why typing stopped, if it did
        self.start_at = 0  # where the phrase in progress starts, in samples
        self.phrase = 0  # words of that phrase already typed

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopped.set()

    def run(self):
        while not self.stopped.wait(0.2):
            if not self.may_send() or not self.ready():
                continue
            sound = self.recorder.snapshot()
            phrase = sound[self.start_at:]
            if len(phrase) < SHORTEST:
                continue
            speech = audio.speech_only(phrase)
            if speech is None:
                if len(phrase) >= LONGEST:
                    self.start_at = len(sound)
                continue
            LiveTyper.sent.append(time.monotonic())
            self.steps += 1
            try:
                words = self.clean(self.transcribe(speech)).split()
            except Exception as e:  # the last pass reports real problems and types what is left
                print(f"hold-dictate: live typing paused for {self.AFTER_ERROR} seconds: {e}", file=sys.stderr, flush=True)
                self.not_before = time.monotonic() + self.AFTER_ERROR
                continue
            ended = audio.ends_quiet(phrase) or len(phrase) >= LONGEST
            if self._type(words if ended else words[:-UNSURE_WORDS]) and ended:
                self.start_at, self.phrase = len(sound), 0
            if self.note:
                return

    def may_send(self) -> bool:
        now, sent = time.monotonic(), LiveTyper.sent
        if now < self.not_before:
            return False
        while sent and now - sent[0] >= 60:
            sent.popleft()
        if self.per_minute and len(sent) >= self.per_minute:
            return False
        gap = min(self.gap, self.FAST) if self.steps < self.FAST_STEPS else self.gap
        return not sent or now - sent[-1] >= gap

    def _type(self, words: list) -> bool:
        new = words[self.phrase:]
        if not new:
            return True
        text = " ".join(new)
        self.note = self.type_text(f" {text}" if self.typed else text)
        if self.note:
            return False
        self.typed = f"{self.typed} {text}".strip()
        self.phrase += len(new)
        return True

    def finish(self, recording, transcribe) -> str:
        """Waits for the request in flight, types the rest of the recording and returns all the text."""
        self.stop()
        if self.thread.ident is not None:
            self.thread.join()
        speech = None if self.note else audio.speech_only(recording[self.start_at:])
        if speech is not None:
            self._type(self.clean(transcribe(speech)).split())
        return self.typed
