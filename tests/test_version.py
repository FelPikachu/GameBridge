import json
import os
import subprocess
import tomllib
from pathlib import Path

import pytest

from gamebridge import __version__
from gamebridge.application import GameBridgeApplication


def test_all_project_versions_match(tmp_path):
    root = Path(__file__).resolve().parents[1]
    package_version = json.loads((root / "package.json").read_text(encoding="utf-8"))["version"]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project_version = project["project"]["version"]
    assert package_version == project_version == __version__


def test_umu_environment_preserves_game_mode_session(monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")

    environment = GameBridgeApplication._umu_session_environment({"GAMEID": "umu-default"})

    assert environment["DISPLAY"] == ":0"
    assert environment["XDG_RUNTIME_DIR"] == "/run/user/1000"
    assert environment["DBUS_SESSION_BUS_ADDRESS"].endswith("/bus")
    assert environment["HOME"]
    assert environment["GAMEID"] == "umu-default"


def test_umu_environment_has_safe_gamescope_fallbacks(monkeypatch):
    for key in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
        monkeypatch.delenv(key, raising=False)

    environment = GameBridgeApplication._umu_session_environment({})

    assert environment["DISPLAY"] == ":0"
    assert environment["XDG_RUNTIME_DIR"] == f"/run/user/{os.getuid()}"
    assert environment["DBUS_SESSION_BUS_ADDRESS"].startswith("unix:path=/run/user/")


def test_umu_environment_recovers_xauthority_from_user_manager(monkeypatch):
    monkeypatch.delenv("XAUTHORITY", raising=False)
    captured = {}

    def show_environment(argv, **kwargs):
        captured["argv"] = argv
        captured["environment"] = kwargs["env"]
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=(
                "DISPLAY=:0\n"
                "XAUTHORITY=/run/user/1000/xauth_current\n"
                "PATH=/untrusted/path\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(
        subprocess,
        "run",
        show_environment,
    )

    environment = GameBridgeApplication._umu_session_environment({})

    assert environment["XAUTHORITY"] == "/run/user/1000/xauth_current"
    assert environment["PATH"] == "/usr/local/bin:/usr/bin:/bin"
    assert captured["argv"] == ["systemctl", "--user", "show-environment"]
    assert captured["environment"]["XDG_RUNTIME_DIR"] == f"/run/user/{os.getuid()}"
    assert captured["environment"]["DBUS_SESSION_BUS_ADDRESS"].endswith("/bus")


def test_umu_environment_tolerates_unavailable_user_manager(monkeypatch):
    monkeypatch.delenv("XAUTHORITY", raising=False)

    def unavailable(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], timeout=2)

    monkeypatch.setattr(subprocess, "run", unavailable)

    environment = GameBridgeApplication._umu_session_environment({})

    assert "XAUTHORITY" not in environment
    assert environment["DISPLAY"] == ":0"


def test_x11_authorization_is_scoped_to_current_local_user(monkeypatch):
    captured = {}
    monkeypatch.setattr("shutil.which", lambda *args, **kwargs: "/usr/bin/xhost")
    monkeypatch.setattr("pwd.getpwuid", lambda uid: type("User", (), {"pw_name": "deck"})())

    def run(argv, **kwargs):
        captured["argv"] = argv
        captured["environment"] = kwargs["env"]
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", run)

    assert GameBridgeApplication._authorize_x11_local_user(
        {
            "DISPLAY": ":0",
            "XAUTHORITY": "/run/user/1000/xauth_current",
            "HOME": "/home/deck",
            "UNTRUSTED": "not-forwarded",
        }
    )
    assert captured["argv"] == ["/usr/bin/xhost", "+SI:localuser:deck"]
    assert captured["environment"] == {
        "DISPLAY": ":0",
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/deck",
        "XAUTHORITY": "/run/user/1000/xauth_current",
    }


def test_x11_authorization_failure_is_non_fatal(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda *args, **kwargs: "/usr/bin/xhost")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(args[0], timeout=2)
        ),
    )

    assert not GameBridgeApplication._authorize_x11_local_user({"DISPLAY": ":0"})


@pytest.mark.asyncio
async def test_dashboard_reports_package_version(tmp_path):
    app = GameBridgeApplication(tmp_path)
    app.start()
    dashboard = await app.dashboard()
    assert dashboard["version"] == __version__
    assert dashboard["providerCount"] == 3
    assert [provider["id"] for provider in dashboard["providers"]] == [
        "epic",
        "mihoyo_cn",
        "hoyoplay_global",
    ]


def test_launch_modifier_availability_uses_installed_plugin_directories(tmp_path):
    (tmp_path / "decky-lsfg-vk").mkdir()
    (tmp_path / "Decky-Framegen").mkdir()
    assert GameBridgeApplication.launch_modifier_availability(tmp_path) == {
        "lsfg": True,
        "framegen": True,
    }
