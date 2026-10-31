import json
import sys

import pytest

from hold_dictate import cli, config, health
from hold_dictate.config import Config

from test_settings import call, server  # noqa: F401  (the fixture)


def row(rows, text):
    return next(r for r in rows if text in r["text"])


def test_the_page_shows_what_doctor_finds_with_a_plain_fix(server, capsys):  # noqa: F811
    config.save(Config(hotkey="space"))  # no key saved, and a shortcut with no modifier
    code, report = call(server, "/api/health")
    assert code == 200 and report["ok"] is False
    assert all(r["ok"] or r["fix"] for r in report["checks"])
    assert "Transcription" in row(report["checks"], "API key")["fix"]
    assert "settings page" in row(report["checks"], "hotkey")["fix"]
    cli.main(["doctor", "--offline"])
    out = capsys.readouterr().out
    for r in report["checks"]:
        assert r["text"] in out and r["fix"] in out


def test_health_needs_the_token_and_our_own_host(server):  # noqa: F811
    assert call(server, "/api/health", token="nope")[0] == 403
    assert call(server, "/api/health", host="evil.example:1")[0] == 403


def test_the_key_is_not_sent_anywhere_from_the_page_but_doctor_tries_it(api, capsys):
    config.save(Config(provider="custom", base_url=api.url, api_key="k"))
    assert row(health.checks(config.load(), online=False), "API key")["ok"]
    cli.main(["doctor", "--offline", "--json"])
    capsys.readouterr()
    assert api.requests == []
    cli.main(["doctor"])
    assert "API key" in capsys.readouterr().out and len(api.requests) == 1


def test_json_output_is_one_line_a_program_can_read(capsys):
    config.save(Config())
    code = cli.main(["doctor", "--offline", "--json"])
    report = json.loads(capsys.readouterr().out)
    assert (code == 0) == report["ok"] and all(set(r) == {"ok", "text", "fix"} for r in report["checks"])


def test_a_check_that_cannot_run_says_so(server, monkeypatch):  # noqa: F811
    monkeypatch.setattr(health.sys, "executable", "/nonexistent/python")
    code, report = call(server, "/api/health")
    assert code == 200 and report["ok"] is False and "doctor" in report["checks"][0]["fix"]


@pytest.fixture
def mac(monkeypatch):
    def setup(access):
        monkeypatch.setattr(health.sys, "platform", "darwin")
        monkeypatch.setattr("hold_dictate.hotkey.choose_backend", lambda: (type("B", (), {"name": "pynput"})(), ""))
        monkeypatch.setattr(health, "mac_access", lambda: access)
        return health.checks(Config(background_audio="off", provider="local"), online=False)
    return setup


def test_macos_shows_which_permission_is_missing(mac):
    rows = mac({"Input Monitoring": True, "Accessibility": False, "Microphone": True})
    bad = [r for r in rows if "permission" in r["text"] and not r["ok"]]
    assert [r["text"] for r in bad] == ["macOS Accessibility permission"]
    assert "Privacy & Security > Accessibility" in bad[0]["fix"]


def test_macos_not_asked_yet_is_no_failure(mac):
    rows = mac({"Input Monitoring": True, "Accessibility": True, "Microphone": None})
    assert row(rows, "Microphone")["ok"] and "will ask" in row(rows, "Microphone")["text"]


@pytest.mark.skipif(sys.platform != "darwin", reason="asks macOS itself")
def test_macos_answers_for_every_permission():
    assert set(health.mac_access()) == set(health.MAC_NEEDS)
