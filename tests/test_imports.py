import importlib
import pytest

MODULES = ["config", "audio", "providers", "hotkey", "paste", "feedback", "service", "app", "cli", "settings_ui", "updater", "tray"]


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name):
    importlib.import_module(f"hold_dictate.{name}")


def test_cli_config(capsys):
    from hold_dictate.cli import main
    assert main(["config"]) == 0
    assert "config.toml" in capsys.readouterr().out
