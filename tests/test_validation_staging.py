import json
from pathlib import Path

import pytest

from gamebridge.validation_staging import (
    StagingError,
    restore_game_directory,
    stage_game_directory,
)


def make_game(tmp_path: Path) -> tuple[Path, Path]:
    game = tmp_path / "Example Game"
    executable = game / "bin" / "Game.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"game")
    (game / "data.bin").write_bytes(b"data")
    return game, executable


def test_stage_and_restore_preserve_game_and_manifest_until_verified(tmp_path):
    game, executable = make_game(tmp_path)
    manifest = tmp_path / "recovery" / "example.json"

    entry = stage_game_directory(
        game_id="example",
        game_path=game,
        key_executable=executable,
        manifest_path=manifest,
        stage="clean-official-first-use",
        is_busy=lambda _path: False,
    )

    hidden = game.with_name("Example Game.gamebridge-test-hidden")
    assert not game.exists()
    assert hidden.is_dir()
    assert manifest.is_file()
    assert json.loads(manifest.read_text(encoding="utf-8"))["size_bytes"] == 8

    restored = restore_game_directory(manifest, is_busy=lambda _path: False)

    assert restored == entry
    assert (game / "bin/Game.exe").read_bytes() == b"game"
    assert not hidden.exists()
    assert not manifest.exists()


def test_stage_refuses_busy_game(tmp_path):
    game, executable = make_game(tmp_path)

    with pytest.raises(StagingError, match="game_busy"):
        stage_game_directory(
            game_id="example",
            game_path=game,
            key_executable=executable,
            manifest_path=tmp_path / "manifest.json",
            stage="clean",
            is_busy=lambda _path: True,
        )

    assert game.is_dir()


def test_stage_refuses_existing_hidden_target(tmp_path):
    game, executable = make_game(tmp_path)
    game.with_name("Example Game.gamebridge-test-hidden").mkdir()

    with pytest.raises(StagingError, match="staged_path_exists"):
        stage_game_directory(
            game_id="example",
            game_path=game,
            key_executable=executable,
            manifest_path=tmp_path / "manifest.json",
            stage="clean",
            is_busy=lambda _path: False,
        )


def test_stage_refuses_key_executable_outside_game(tmp_path):
    game, _executable = make_game(tmp_path)
    outside = tmp_path / "Other.exe"
    outside.write_bytes(b"other")

    with pytest.raises(StagingError, match="executable_outside_game"):
        stage_game_directory(
            game_id="example",
            game_path=game,
            key_executable=outside,
            manifest_path=tmp_path / "manifest.json",
            stage="clean",
            is_busy=lambda _path: False,
        )


def test_restore_stops_when_original_and_hidden_both_exist(tmp_path):
    game, executable = make_game(tmp_path)
    manifest = tmp_path / "manifest.json"
    stage_game_directory(
        game_id="example",
        game_path=game,
        key_executable=executable,
        manifest_path=manifest,
        stage="clean",
        is_busy=lambda _path: False,
    )
    game.mkdir()

    with pytest.raises(StagingError, match="original_path_exists"):
        restore_game_directory(manifest, is_busy=lambda _path: False)

    assert manifest.exists()
    assert game.with_name("Example Game.gamebridge-test-hidden").exists()


def test_restore_keeps_manifest_when_key_executable_is_missing(tmp_path):
    game, executable = make_game(tmp_path)
    manifest = tmp_path / "manifest.json"
    stage_game_directory(
        game_id="example",
        game_path=game,
        key_executable=executable,
        manifest_path=manifest,
        stage="clean",
        is_busy=lambda _path: False,
    )
    hidden = game.with_name("Example Game.gamebridge-test-hidden")
    (hidden / "bin/Game.exe").unlink()

    with pytest.raises(StagingError, match="executable_missing"):
        restore_game_directory(manifest, is_busy=lambda _path: False)

    assert manifest.exists()
