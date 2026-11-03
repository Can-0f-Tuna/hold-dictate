import numpy as np
import pytest

from hold_dictate import audio, config
from hold_dictate.providers import TranscribeError, check_api, transcribe_api


def test_config_roundtrip(tmp_path):
    cfg = config.Config(provider="openai", api_key='k"ey', prompt="שלום\nworld", min_hold=0.5, beeps=False)
    path = config.save(cfg, tmp_path / "c.toml")
    assert config.load(path) == cfg


def test_config_defaults_and_unknown_keys(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('provider = "local"\nbogus = 1\n')
    cfg = config.load(p)
    assert cfg.provider == "local" and cfg.hotkey == "alt+space"


def test_env_key_fallback(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "from-env")
    assert config.Config(provider="groq").effective_key() == "from-env"
    assert config.Config(provider="groq", api_key="file").effective_key() == "file"


def tone(seconds, freq=220, amp=0.3):
    t = np.arange(int(audio.SAMPLE_RATE * seconds)) / audio.SAMPLE_RATE
    return (np.sin(2 * np.pi * freq * t) * amp).astype(np.float32)


def test_vad_drops_silence_and_keeps_speech():
    quiet = (np.random.default_rng(0).standard_normal(audio.SAMPLE_RATE * 2) * 0.0005).astype(np.float32)
    assert audio.speech_only(quiet) is None
    clip = np.concatenate([quiet, tone(1.0), quiet])
    speech = audio.speech_only(clip)
    assert speech is not None and 0.9 * audio.SAMPLE_RATE < len(speech) < 2.0 * audio.SAMPLE_RATE


def test_vad_keeps_speech_with_no_pause():
    rng = np.random.default_rng(0)
    # Words at a steady level over a loud room: the quietest frames are not far below the speech.
    t = np.arange(audio.SAMPLE_RATE * 8) / audio.SAMPLE_RATE
    clip = (rng.standard_normal(len(t)) * 0.03 * (1 + 0.35 * np.sin(2 * np.pi * 3 * t))).astype(np.float32)
    speech = audio.speech_only(clip)
    assert speech is not None and len(speech) > 0.9 * len(clip)


def test_vad_keeps_all_of_a_clip_with_few_pauses():
    rng = np.random.default_rng(0)
    gap = (rng.standard_normal(audio.SAMPLE_RATE // 3) * 0.01).astype(np.float32)
    clip = np.concatenate([tone(2.0, amp=0.1), gap, tone(2.0, amp=0.1), tone(2.0, amp=0.04), gap, tone(1.0, amp=0.1)])
    speech = audio.speech_only(clip)
    assert speech is not None and len(speech) > 0.9 * len(clip)


def test_vad_hears_a_quiet_microphone():
    quiet = (np.random.default_rng(0).standard_normal(audio.SAMPLE_RATE) * 0.0002).astype(np.float32)
    clip = np.concatenate([quiet, tone(1.0, amp=0.004), quiet])
    assert audio.speech_only(clip) is not None


def test_wav_roundtrip(tmp_path):
    p = tmp_path / "a.wav"
    p.write_bytes(audio.wav_bytes(tone(0.5)))
    back = audio.read_audio_file(str(p))
    assert len(back) == int(audio.SAMPLE_RATE * 0.5)


def make_cfg(api, **kw):
    return config.Config(provider="custom", base_url=api.url, api_key="secret", model="m1", **kw)


def test_api_request_shape(api):
    api.text = "  שלום world "
    out = transcribe_api(make_cfg(api, languages="fr", prompt="hint"), tone(0.5))
    assert out == "שלום world"
    method, path, auth, body = api.requests[-1]
    assert path == "/v1/audio/transcriptions" and auth == "Bearer secret"
    for needle in (b'name="model"\r\n\r\nm1', b'name="language"\r\n\r\nfr', b'name="prompt"\r\n\r\nhint', b"RIFF"):
        assert needle in body


@pytest.mark.parametrize("status,word", [(401, "key"), (429, "rate limit")])
def test_api_errors_are_explained(api, status, word):
    api.status = status
    with pytest.raises(TranscribeError, match=word):
        transcribe_api(make_cfg(api), tone(0.5))


def test_missing_key_and_unreachable():
    with pytest.raises(TranscribeError, match="API key"):
        transcribe_api(config.Config(provider="groq"), tone(0.5))
    dead = config.Config(provider="custom", base_url="http://127.0.0.1:9/v1", api_key="k", timeout=2)
    with pytest.raises(TranscribeError, match="could not reach"):
        transcribe_api(dead, tone(0.5))


def test_check_api(api):
    assert check_api(make_cfg(api)) == ""
    api.status = 401
    assert "key" in check_api(make_cfg(api))


def test_language_hints():
    from hold_dictate.config import HEBREW_PROMPT
    from hold_dictate.providers import language_hints
    assert language_hints(config.Config(languages="fr")) == ("fr", "")
    assert language_hints(config.Config(languages="he")) == ("he", HEBREW_PROMPT)  # unforced, short Hebrew came out as Cyrillic
    assert language_hints(config.Config(languages="he", provider="local")) == ("he", HEBREW_PROMPT)
    assert language_hints(config.Config(languages="he,en")) == ("", HEBREW_PROMPT)
    force, prompt = language_hints(config.Config(languages="he,en,ru", prompt="Kubernetes"))
    assert force == "" and prompt.startswith(HEBREW_PROMPT) and "Привет" in prompt and prompt.endswith("Kubernetes")
    assert language_hints(config.Config()) == ("", "")


def test_language_outside_the_list_is_transcribed_again(api, monkeypatch):
    from hold_dictate import providers
    answers = iter([("مرحبا", "arabic", -0.3), ("שלום", "", None)])
    sent = []
    monkeypatch.setattr(providers, "_request", lambda cfg, audio, lang, prompt, fmt: sent.append((lang, fmt)) or next(answers))
    cfg = config.Config(provider="groq", api_key="k", languages="he,en")
    assert transcribe_api(cfg, tone(0.5)) == "שלום"
    assert sent == [("", "verbose_json"), ("he", "json")]


def test_a_language_on_the_list_is_left_alone(monkeypatch):
    from hold_dictate import providers
    sent = []
    monkeypatch.setattr(providers, "_request", lambda cfg, audio, lang, prompt, fmt: sent.append(lang) or ("Run it.", "english", -0.8))
    assert transcribe_api(config.Config(provider="groq", api_key="k", languages="he,en"), tone(0.5)) == "Run it."
    assert sent == [""]


def test_settings_an_extension_owns_survive_a_save(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('''provider = "openai"
sounds = true
language = "fr"
some_flag = true
some_text = "a \\"quoted\\" word"
some_table = { "get hub" = "GitHub" }
some_list = [1, "two", { three = 3.5 }]
some_date = 2026-10-31T10:00:00
[some_section]
nested = "yes"
''', encoding="utf-8")
    cfg = config.load(p)
    config.save(cfg, p)
    again = config.load(p)
    assert again == cfg and again.provider == "openai" and again.languages == "fr"
    assert set(again.extra) == {"some_flag", "some_text", "some_table", "some_list", "some_date", "some_section"}
    assert again.extra["some_list"] == [1, "two", {"three": 3.5}] and again.extra["some_section"] == {"nested": "yes"}
    assert "sounds" not in p.read_text(encoding="utf-8")  # read once, then gone


def test_a_setting_in_extra_falls_back_to_its_default():
    cfg = config.Config(extra={"table": "not a table", "flag": True})
    assert cfg.setting("table", {}) == {} and cfg.setting("flag", False) is True and cfg.setting("missing", "x") == "x"
