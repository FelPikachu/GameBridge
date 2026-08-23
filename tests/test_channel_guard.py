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
