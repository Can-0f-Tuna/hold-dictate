"""The uv commands the updater and the offline engine's install run."""
import sys

import pytest

from hold_dictate import updater
from hold_dictate.config import Config


class Source:
    """An update source like the official channel: it names the release and adds its own wheel."""

    def latest(self):
        return "v99.0.0"

    def install_args(self, tag, extras=None):
        return ["--with", "pro.whl", updater.install_spec(tag, extras)]


class OldSource(Source):
    """An extension written before extras were passed on."""

    def install_args(self, tag):
        return ["--with", "pro.whl", f"core@{tag}"]


def has(monkeypatch, *modules):
    """Pretends these modules are installed, and no others the updater asks about."""
    monkeypatch.setattr(updater.importlib.util, "find_spec", lambda name: object() if name in modules else None)


@pytest.fixture
def installs(monkeypatch):
    """An Updater that would install from `Source`, and the commands it ran."""
    ran = []
    monkeypatch.setattr(updater, "installed_by_uv", lambda: True)
    monkeypatch.setattr(updater, "find_uv", lambda: "uv")
    monkeypatch.setattr(updater.sys, "platform", "linux")
    u = updater.Updater(Config())
    u.source = Source()
    monkeypatch.setattr(u, "_run_install", lambda cmd: ran.append(cmd))
    return u, ran


def test_the_app_comes_from_a_release_archive_so_git_is_not_needed():
    assert updater.install_spec("v1.2.3", ["x11", "local", "local"]) == (
        "hold-dictate[local,x11] @ https://github.com/Can-0f-Tuna/hold-dictate/archive/refs/tags/v1.2.3.tar.gz")


def test_installs_stay_on_this_python(monkeypatch):
    """Without --python uv takes the newest Python it can get, which onnxruntime may have no wheels for."""
    monkeypatch.setattr(sys, "_base_executable", "/uv/python/cpython-3.12/bin/python3.12", raising=False)
    assert updater.install_python(["local"]) == "/uv/python/cpython-3.12/bin/python3.12"


def test_the_offline_engine_moves_off_a_python_too_new_for_it(monkeypatch):
    monkeypatch.setattr(sys, "_base_executable", "/uv/python/cpython-3.15/bin/python3.15", raising=False)
    monkeypatch.setattr(sys, "version_info", (3, 15, 0, "final", 0))
    assert updater.install_python(["local"]) == "3.13"
    assert updater.install_python([]) == "/uv/python/cpython-3.15/bin/python3.15"  # nothing there needs the move


def test_an_update_keeps_the_offline_engine_and_the_python(installs, monkeypatch):
    u, ran = installs
    has(monkeypatch, "faster_whisper")
    assert u.install() is True and u.job == "update"
    assert ran == [["uv", "tool", "install", "--force", "--python", updater.install_python(["local"]), "--with", "pro.whl",
                    updater.install_spec("v99.0.0", ["local"])]]
    assert ran[0][-1].startswith("hold-dictate[local] @ ")


def test_installing_the_offline_engine_adds_the_extra_to_this_version(installs, monkeypatch):
    u, ran = installs
    has(monkeypatch)
    this = f"v{updater.__version__}"
    u.source.latest = lambda: this  # up to date: an update would do nothing
    assert u.install() is False and ran == []
    assert u.install_offline() is True and u.job == "offline" and u.ready
    assert ran[0][-1] == updater.install_spec(this, ["local"])
    assert u.status == "up to date" and "installed" in u.offline_status  # the update section of the page stays as it was


def test_an_extension_from_before_extras_still_updates(installs, monkeypatch):
    u, ran = installs
    u.source = OldSource()
    has(monkeypatch, "faster_whisper")
    assert u.install() is True and ran[0][-3:] == ["--with", "pro.whl", "core@v99.0.0"]


def test_a_failed_offline_install_says_so_on_its_own(installs, monkeypatch):
    u, _ = installs
    has(monkeypatch)
    monkeypatch.setattr(u, "_run_install", lambda cmd: "error: no wheels for onnxruntime\n")
    failed = []
    u.on_failed = lambda: failed.append(1)
    assert u.install_offline() is False and not u.ready and u.job == ""
    assert u.offline_status == "offline install failed: error: no wheels for onnxruntime" and failed == []  # not the pill's update chip


def test_windows_installs_after_exit_with_the_same_command(installs, monkeypatch):
    u, ran = installs
    has(monkeypatch)
    monkeypatch.setattr(updater.sys, "platform", "win32")
    assert u.install_offline() is True and u.ready and ran == []  # the running files are locked
    script = u.windows_helper()
    assert script.startswith(f"Wait-Process -Id {updater.os.getpid()} ")
    assert "& 'uv' 'tool' 'install' '--force' '--python' " in script and "'hold-dictate[local] @ " in script
    assert "Start-Process" in script and "Start-Process" not in u.windows_helper(start=False)

