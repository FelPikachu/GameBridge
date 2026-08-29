from pathlib import Path

import pytest

from gamebridge import channel_guard


def test_channel_guard_applies_selection_before_exec(monkeypatch):
    events = []

    class Provider:
        def __init__(self, data: Path, compatibility: Path, _spec):
            events.append(("provider", data, compatibility))

        def apply_selected_channel(self, game_id: str):
            events.append(("apply", game_id))

    def execute(program, command):
        events.append(("exec", program, command))
        raise RuntimeError("exec intercepted")

    monkeypatch.setattr(channel_guard, "HoYoPlayProvider", Provider)
    monkeypatch.setattr(channel_guard.os, "execvp", execute)
    monkeypatch.setattr(
        channel_guard.sys,
        "argv",
        ["channel_guard.py", "--game-id", "nap_cn", "--", "proton", "game.exe"],
    )

    with pytest.raises(RuntimeError, match="exec intercepted"):
        channel_guard.main()

    assert events[-2:] == [
        ("apply", "nap_cn"),
        ("exec", "proton", ["proton", "game.exe"]),
    ]


def test_channel_guard_fails_closed_when_selection_cannot_be_applied(monkeypatch):
    class Provider:
        def __init__(self, *_args):
            pass

        def apply_selected_channel(self, _game_id: str):
            raise ValueError("profile unavailable")

    monkeypatch.setattr(channel_guard, "HoYoPlayProvider", Provider)
    monkeypatch.setattr(
        channel_guard.sys,
        "argv",
        ["channel_guard.py", "--game-id", "hkrpg_cn", "--", "proton", "game.exe"],
    )

    assert channel_guard.main() == 6


def test_genshin_direct_shortcut_routes_global_selection_through_gamebridge(
    tmp_path, monkeypatch
):
    root_data = tmp_path / "homebrew/data/GameBridge/data"
    root_data.mkdir(parents=True)
    (root_data / "mihoyo-selection").write_text("global", encoding="utf-8")
    executed = []

    def execute(program, command):
        executed.append((program, command))
        raise RuntimeError("exec intercepted")

    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.setattr(channel_guard.os, "execvp", execute)
    monkeypatch.setattr(
        channel_guard.sys,
        "argv",
        ["channel_guard.py", "--game-id", "hk4e_cn", "--", "proton", "YuanShen.exe"],
    )

    with pytest.raises(RuntimeError, match="exec intercepted"):
        channel_guard.main()

    router = channel_guard.PLUGIN_ROOT / "gamebridge/launcher.py"
    assert executed == [
        (
            channel_guard.sys.executable,
            [
                channel_guard.sys.executable,
                str(router),
                "--provider",
                "mihoyo",
                "--game-id",
                "genshin",
            ],
        )
    ]


def test_genshin_keeps_verified_direct_route_for_cn_selection(tmp_path, monkeypatch):
    root_data = tmp_path / "homebrew/data/GameBridge/data"
    root_data.mkdir(parents=True)
    (root_data / "mihoyo-selection").write_text("official", encoding="utf-8")
    events = []

    class Provider:
        def __init__(self, *_args):
            pass

        def apply_selected_channel(self, game_id):
            events.append(("apply", game_id))

    def execute(program, command):
        events.append(("exec", program, command))
        raise RuntimeError("exec intercepted")

    command = ["YuanShen.exe"]
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.setattr(channel_guard, "HoYoPlayProvider", Provider)
    monkeypatch.setattr(channel_guard.os, "execvp", execute)
    monkeypatch.setattr(
        channel_guard.sys,
        "argv",
        ["channel_guard.py", "--game-id", "hk4e_cn", "--", *command],
    )

    with pytest.raises(RuntimeError, match="exec intercepted"):
        channel_guard.main()

    assert events == [
        ("apply", "hk4e_cn"),
        ("exec", "YuanShen.exe", command),
    ]
