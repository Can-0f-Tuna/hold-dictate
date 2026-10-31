import sys

import pytest

from hold_dictate import config, textproc
from hold_dictate.config import Config
from hold_dictate.textproc import apply_overrides, clean, is_hallucination, remove_fillers, run_hook, spoken_punctuation

FILLERS = list(textproc.FILLERS)


@pytest.mark.parametrize("text", [
    "Thanks for watching!", "  thank you for watching.  ", "[BLANK_AUDIO]", "(silence)", "you", "You.",
    "♪ la la ♪", "Subtitles by the Amara.org community", "תודה שצפיתם!", "תורגם על ידי יוסי", "כתוביות: דנה", "...",
])
def test_invented_phrases_are_dropped(text):
    assert is_hallucination(text)
    assert clean(text, Config()) == ""


@pytest.mark.parametrize("text", [
    "Thank you.", "תודה רבה", "Thanks for watching the kids tonight", "you should go", "תרגום של המסמך מוכן",
    "I like music videos",
])
def test_real_speech_is_kept(text):
    assert not is_hallucination(text)
    assert clean(text, Config()) == text


def test_hallucination_filter_can_be_turned_off():
    assert clean("Thanks for watching!", Config(drop_hallucinations=False)) == "Thanks for watching!"


@pytest.mark.parametrize("text, expected", [
    ("Um, I think, uh, we should go.", "I think we should go."),
    ("So um we go now", "So we go now"),
    ("Hmm... okay. Uh, let's start.", "Okay. Let's start."),
    ("Um.", ""),
    ("אממ, אני חושב, אה, שכדאי ללכת.", "אני חושב שכדאי ללכת."),
    ("Umbrella and hummus", "Umbrella and hummus"),  # only whole words
    ("I said ahead of time", "I said ahead of time"),
])
def test_fillers(text, expected):
    assert remove_fillers(text, FILLERS) == expected


def test_fillers_off_by_default_and_custom_list():
    assert clean("Um, hi.", Config()) == "Um, hi."
    assert clean("Um, hi.", Config(remove_fillers=True)) == "Hi."
    assert clean("Like, hi um.", Config(remove_fillers=True, filler_words="like")) == "Hi um."


@pytest.mark.parametrize("text, overrides, expected", [
    ("I use hyper whisper daily", {"hyper whisper": "hyprwhspr"}, "I use hyprwhspr daily"),
    ("Hyper  Whisper rocks", {"hyper whisper": "hyprwhspr"}, "hyprwhspr rocks"),
    ("write c++ code", {"c++": "C++"}, "write C++ code"),
    ("delete the word basically here", {"basically": ""}, "delete the word here"),
    ("cat category", {"cat": "dog"}, "dog category"),  # whole words only
    ("אני עושה דיפלוי לורסל", {"ורסל": "Vercel", "דיפלוי": "deploy"}, "אני עושה deploy ל-Vercel"),
    ("ורסל עובד", {"ורסל": "Vercel"}, "Vercel עובד"),
    ("שלום עולם", {"עולם": "חברים"}, "שלום חברים"),
])
def test_word_overrides(text, overrides, expected):
    assert apply_overrides(text, overrides) == expected


@pytest.mark.parametrize("text, expected", [
    ("hello comma how are you question mark", "hello, how are you?"),
    ("Hello, comma, world.", "Hello, world."),
    ("Um, hello comma how are you? Question mark new line I pushed it period.", "Um, hello, how are you?\nI pushed it."),  # real Whisper output
    ("first line new line second line", "first line\nSecond line"),
    ("done period next one", "done. Next one"),
    ("שלום פסיק מה שלומך סימן שאלה", "שלום, מה שלומך?"),
    ("שורה ראשונה שורה חדשה שורה שנייה", "שורה ראשונה\nשורה שנייה"),
    ("call it open paren soon close paren now", "call it (soon) now"),
    ("no punctuation words here", "no punctuation words here"),
])
def test_spoken_punctuation(text, expected):
    assert spoken_punctuation(text) == expected


def test_clean_runs_every_step_in_order():
    cfg = Config(remove_fillers=True, spoken_punctuation=True, word_overrides={"get hub": "GitHub"})
    assert clean("Um, push it to get hub comma please.", cfg) == "Push it to GitHub, please."


sh = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell commands")


@sh
def test_hook_replaces_text_and_sees_env():
    assert run_hook("tr a-z A-Z", "hello") == "HELLO"
    assert run_hook('printf "%s:" "$HOLD_DICTATE_APP"; cat', "hi", {"HOLD_DICTATE_APP": "kitty"}) == "kitty:hi"


@sh
def test_hook_empty_output_keeps_text_and_77_drops_it():
    assert run_hook("cat > /dev/null", "keep me") == "keep me"
    assert run_hook("exit 77", "drop me") is None


@sh
def test_hook_failure_and_timeout_keep_text(monkeypatch, capsys):
    assert run_hook("echo broken >&2; exit 3", "text") == "text"
    assert "exit 3" in capsys.readouterr().err
    monkeypatch.setattr(textproc, "HOOK_TIMEOUT", 0.3)
    assert run_hook("sleep 2", "slow") == "slow"


def test_dict_settings_round_trip(tmp_path):
    path = tmp_path / "config.toml"
    cfg = Config(word_overrides={'say "x"': "X", "ורסל": "Vercel", "gone": ""}, app_rules={"slack": "submit"})
    config.save(cfg, path)
    loaded = config.load(path)
    assert loaded.word_overrides == cfg.word_overrides and loaded.app_rules == {"slack": "submit"}
    assert config.load(tmp_path / "missing.toml").word_overrides == {}


def test_coerce_checks_dicts():
    assert config.coerce("word_overrides", {" a ": " b ", " ": "x"}) == {"a": "b"}
    with pytest.raises(ValueError):
        config.coerce("app_rules", ["slack"])
    with pytest.raises(ValueError):
        config.coerce("app_rules", {"slack": 1})
