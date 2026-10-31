import json
import os
import stat
import sys
import threading
import time
from html.parser import HTMLParser
from pathlib import Path

import pytest

from hold_dictate import cli, config, history, settings_ui
from hold_dictate.config import Config
from hold_dictate.settings_ui import SettingsServer

from test_app import FakeRecorder, cfg_for, dictation, settle, tap
from test_settings import call, server  # noqa: F401  (the fixture)

HOSTILE = '</script><script>alert(1)</script><!-- <script> <img src=x onerror=alert(2)>'


on = Config


def seed(*texts, age_days=0.0):
    cfg = on()
    for text in texts:
        history.add(cfg, text, text.upper(), "slack")
    if age_days:
        data = json.loads(history.path().read_text(encoding="utf-8"))
        for row in data["entries"]:
            row["time"] -= age_days * 86400
        history.path().write_text(json.dumps(data), encoding="utf-8")


# ---------- what gets saved ----------

def test_a_pasted_dictation_is_saved_with_text_raw_time_and_app(api):
    api.text = "Um, push it."
    d, out, _ = dictation(cfg_for(api, remove_fillers=True), app="slack")
    before = time.time()
    tap(d, 0.3)
    settle(d)
    (row,) = history.entries(d.cfg)
    assert (row["text"], row["raw"], row["app"]) == ("Push it.", "Um, push it.", "slack")
    assert before <= row["time"] <= time.time()
    assert set(row) == {"id", "time", "app", "text", "raw"}  # no audio, or anything else


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell commands")
def test_a_text_hook_decides_the_final_text_and_can_keep_it_out(api):
    d, out, _ = dictation(cfg_for(api, post_hook="tr a-z A-Z"), app="kitty")
    tap(d, 0.3)
    settle(d)
    (row,) = history.entries(d.cfg)
    assert (row["text"], row["raw"]) == ("HELLO WORLD", "hello world")
    d, out, _ = dictation(cfg_for(api, post_hook="exit 77"))
    history.clear()
    tap(d, 0.3)
    settle(d)
    assert history.entries(d.cfg) == []


@pytest.mark.parametrize("rule", ["off", "clipboard"])
def test_apps_kept_out_of_pasting_stay_out_of_history(api, rule):
    d, out, _ = dictation(cfg_for(api, app_rules={"keepass": rule}), app="KeePassXC")
    tap(d, 0.3)
    settle(d)
    assert history.entries(d.cfg) == []


def test_a_failed_paste_is_not_saved(api):
    d, out, _ = dictation(cfg_for(api))
    d.paster = lambda text, **kw: "text copied to the clipboard; press Ctrl+V"
    tap(d, 0.3)
    settle(d)
    assert history.entries(d.cfg) == []


def test_text_typed_live_is_saved_once_it_is_all_out(api):
    from hold_dictate.live import LiveTyper
    d, out, _ = dictation(cfg_for(api), app="slack")
    live = LiveTyper(None, None, str, lambda text: "")
    d.process_live(FakeRecorder(None).stop(), live, 32000, "slack")
    (row,) = history.entries(d.cfg)
    assert (row["text"], row["raw"], row["app"]) == ("hello world", "hello world", "slack")


def test_a_broken_history_does_not_stop_a_dictation(api, monkeypatch):
    def fail(*a):
        raise OSError("disk full")
    monkeypatch.setattr(history, "add", fail)
    d, out, fb = dictation(cfg_for(api))
    tap(d, 0.3)
    settle(d)
    assert len(out.calls) == 1 and fb.log[-1] == "done"


def test_paste_last_and_copy_last_work_with_history_off(api):
    d, out, _ = dictation(cfg_for(api, history_days=0))
    tap(d, 0.3)
    settle(d)
    assert not history.path().exists()
    assert d.paste_last() == "" and out.calls[-1]["text"] == "hello world"
    assert d.copy_last() == "" and out.copied == ["hello world"]


# ---------- keeping it ----------

def test_old_entries_go_even_on_days_without_dictating():
    seed("old", age_days=8)
    seed("new")
    cfg = on(history_days=7)
    assert [r["text"] for r in history.entries(cfg)] == ["new"]
    assert "old" not in history.path().read_text(encoding="utf-8")


def test_the_running_app_clears_old_entries_on_its_own(api):
    seed("old", age_days=8)
    d, _, _ = dictation(cfg_for(api))
    d.tick()
    assert "old" not in history.path().read_text(encoding="utf-8")


def test_turning_it_off_stops_adding_and_deletes_nothing():
    seed("kept", age_days=30)
    off = on(history_days=0)
    history.add(off, "new", "new", "app")
    assert [r["text"] for r in history.entries(off)] == ["kept"]
    assert "kept" in history.path().read_text(encoding="utf-8")
    history.clear()
    assert history.entries(off) == [] and not history.path().exists()


def test_entries_over_the_cap_go_oldest_first(monkeypatch):
    monkeypatch.setattr(history, "MAX_ENTRIES", 3)
    seed("1", "2", "3", "4", "5")
    assert [r["text"] for r in history.entries(on())] == ["5", "4", "3"]


def test_the_file_stays_under_the_size_cap_oldest_first(monkeypatch):
    monkeypatch.setattr(history, "MAX_BYTES", 1500)
    seed("a" * 300, "ב" * 150, "c" * 300, "d" * 300)
    assert history.path().stat().st_size <= 1500
    assert [r["text"][0] for r in history.entries(on())][:2] == ["d", "c"]


def test_a_single_huge_entry_cannot_break_the_cap(monkeypatch):
    monkeypatch.setattr(history, "MAX_BYTES", 500)
    seed("short", "x" * 900)
    assert history.path().stat().st_size <= 500


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_only_the_owner_can_read_it_even_with_an_open_umask():
    old = os.umask(0)
    try:
        seed("first")
        assert stat.S_IMODE(history.path().stat().st_mode) == 0o600
        history.path().chmod(0o644)  # a backup restored it this way
        spare = history.path().with_name("history.json.tmp")
        spare.write_text("")  # and a crash left this one
        spare.chmod(0o644)
        seed("second")
    finally:
        os.umask(old)
    assert stat.S_IMODE(history.path().stat().st_mode) == 0o600
    assert stat.S_IMODE(history.path().with_name("history.lock").stat().st_mode) == 0o600


def test_a_crash_while_saving_leaves_the_last_good_file(monkeypatch):
    seed("safe")
    def crash(*a):
        raise OSError("power cut")
    with monkeypatch.context() as during, pytest.raises(OSError):
        during.setattr(history.os, "replace", crash)
        seed("lost")
    assert [r["text"] for r in history.entries(on())] == ["safe"]


def test_clear_leaves_no_text_in_a_file_left_by_a_crash():
    seed("secret")
    history.path().with_name("history.json.tmp").write_text("secret", encoding="utf-8")
    history.clear()
    assert not any("secret" in p.read_text(encoding="utf-8") for p in history.path().parent.iterdir())


@pytest.mark.parametrize("damage", ["{not json", "[1, 2]", '{"entries": 5}', '{"entries": [{"id": 1}, 7]}'])
def test_a_damaged_file_does_not_stop_anything(damage):
    history.path().parent.mkdir(parents=True)
    history.path().write_text(damage, encoding="utf-8")
    assert history.entries(on()) == []
    seed("after")
    assert [r["text"] for r in history.entries(on())] == ["after"]


def test_two_writers_lose_nothing():
    writers = [threading.Thread(target=seed, args=(str(i),)) for i in range(12)]
    for w in writers:
        w.start()
    for w in writers:
        w.join()
    assert sorted(int(r["text"]) for r in history.entries(on())) == list(range(12))


def test_text_with_a_lone_surrogate_is_saved_and_served(server):  # noqa: F811
    history.add(server.cfg, "bad \ud83d end", "raw", "app")
    code, body = call(server, "/api/history", {"q": ""})
    assert code == 200 and "end" in body["entries"][0]["text"]


def test_a_setting_that_makes_no_sense_counts_as_off():
    history.add(on(history_days="soon"), "text", "text", "app")
    assert not history.path().exists()


# ---------- the first run ----------

def test_new_installs_keep_seven_days(tmp_path):
    assert config.load(tmp_path / "none.toml").history_days == 7
    p = config.save(Config(), tmp_path / "c.toml")
    assert config.load(p).history_days == 7


def test_existing_users_stay_off_until_they_choose(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('provider = "groq"\n')
    assert config.load(p).history_days == 0
    config.save(config.load(p), p)
    assert config.load(p).history_days == 0  # also after any later save


def test_an_existing_user_can_turn_it_on_and_it_stays_on(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('history_days = 30.0\n')
    assert config.load(p).history_days == 30


# ---------- the page ----------

def test_list_search_copy_delete_clear_through_the_page(server, monkeypatch):  # noqa: F811
    seed("buy milk", "call Dana")
    copied = []
    monkeypatch.setattr("hold_dictate.paste.copy_text", lambda text: copied.append(text) or "")
    _, listed = call(server, "/api/history", {})
    assert [e["text"] for e in listed["entries"]] == ["call Dana", "buy milk"] and listed["total"] == 2
    _, found = call(server, "/api/history", {"q": "MILK"})
    assert [e["text"] for e in found["entries"]] == ["buy milk"]
    assert call(server, "/api/history", {"q": "slack"})[1]["total"] == 2  # the app counts too
    target = found["entries"][0]["id"]
    assert call(server, "/api/history/copy", {"id": target}) == (200, {"message": ""}) and copied == ["buy milk"]
    assert call(server, "/api/history/delete", {"id": target}) == (200, {"deleted": True})
    assert call(server, "/api/history/copy", {"id": target})[0] == 400
    assert call(server, "/api/history", {})[1]["total"] == 1
    assert call(server, "/api/history/clear", {})[0] == 200
    assert call(server, "/api/history", {})[1]["total"] == 0


def test_the_page_gets_only_so_many_rows_and_search_reaches_the_rest(server, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings_ui, "HISTORY_ROWS", 2)
    seed("needle", "b", "c")
    _, listed = call(server, "/api/history", {})
    assert len(listed["entries"]) == 2 and listed["total"] == 3
    assert call(server, "/api/history", {"q": "needle"})[1]["entries"][0]["text"] == "needle"


@pytest.mark.parametrize("path, body", [
    ("/api/history", {}), ("/api/history/copy", {"id": "x"}), ("/api/history/delete", {"id": "x"}), ("/api/history/clear", {}),
])
def test_history_routes_need_the_token_and_our_own_host(server, path, body):  # noqa: F811
    seed("private")
    assert call(server, path, body, token="wrong")[0] == 403
    assert call(server, path, body, token="")[0] == 403
    assert call(server, path, body, host="evil.example:80")[0] == 403
    assert len(history.entries(on())) == 1


def test_choosing_a_shorter_time_applies_at_once(server):  # noqa: F811
    seed("old", age_days=10)
    code, state = call(server, "/api/config", {"history_days": 7})
    assert code == 200 and state["config"]["history_days"] == 7
    assert "old" not in history.path().read_text(encoding="utf-8")
    assert config.load(config.config_path()).history_days == 7


def test_off_can_be_saved_and_nonsense_cannot(server):  # noqa: F811
    assert call(server, "/api/config", {"history_days": 0})[1]["config"]["history_days"] == 0
    assert call(server, "/api/config", {"history_days": 12})[0] == 400


def test_a_days_value_set_by_hand_does_not_block_other_saves(server):  # noqa: F811
    server.cfg.history_days = 14.0
    assert call(server, "/api/config", {"beeps": False})[0] == 200


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.count = 0

    def handle_starttag(self, tag, attrs):
        self.count += tag == "script"


def scripts_in(server):
    import urllib.request
    with urllib.request.urlopen(server.url, timeout=5) as r:
        page = r.read().decode("utf-8")
    parser = Scripts()
    parser.feed(page)
    return page, parser.count


def test_dictated_text_cannot_break_out_of_the_page(server):  # noqa: F811
    _, clean = scripts_in(server)
    server.session = type("Session", (), {"last_text": HOSTILE, "last_app": HOSTILE})()
    page, hostile = scripts_in(server)
    assert hostile == clean and HOSTILE not in page and "<!-- <script>" not in page


def test_history_comes_back_as_json_the_browser_cannot_take_for_a_page(server):  # noqa: F811
    import urllib.request
    seed(HOSTILE)
    request = urllib.request.Request(f"http://127.0.0.1:{server.port}/api/history", data=b"{}", headers={"X-Token": server.token})
    with urllib.request.urlopen(request, timeout=5) as r:
        assert r.headers["Content-Type"] == "application/json" and r.headers["X-Content-Type-Options"] == "nosniff"
        assert json.load(r)["entries"][0]["text"] == HOSTILE


def test_the_page_never_builds_history_rows_from_markup():
    page = (Path(settings_ui.__file__).with_name("settings.html")).read_text(encoding="utf-8")
    start = page.index("// ---- history")
    assert "innerHTML" not in page[start:page.index("// ---- health", start)]


# ---------- the command ----------

def test_the_command_lists_searches_deletes_and_clears(capsys):
    seed("buy milk", "call Dana")
    assert cli.main(["history"]) == 0
    out = capsys.readouterr().out
    assert out.index("call Dana") < out.index("buy milk")
    assert cli.main(["history", "search", "milk"]) == 0
    out = capsys.readouterr().out
    assert "buy milk" in out and "call Dana" not in out
    target = history.entries(on(), "milk")[0]["id"]
    assert cli.main(["history", "delete", target]) == 0
    assert cli.main(["history", "delete", target]) == 1
    capsys.readouterr()
    assert cli.main(["history", "clear", "--yes"]) == 0
    assert not history.path().exists()


def test_clear_asks_first(monkeypatch):
    seed("kept")
    monkeypatch.setattr("builtins.input", lambda q: "n")
    cli.main(["history", "clear"])
    assert history.path().exists()


def test_the_command_keeps_dictated_text_from_steering_the_terminal(capsys):
    seed("safe\x1b[2Jtext\nsecond line")
    cli.main(["history"])
    out = capsys.readouterr().out
    assert "\x1b" not in out and "safe [2Jtext second line" in out


def test_the_command_says_when_more_are_left(capsys):
    seed("1", "2", "3")
    cli.main(["history", "--limit", "2"])
    assert "1 older" in capsys.readouterr().out
