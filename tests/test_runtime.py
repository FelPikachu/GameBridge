import pytest

from gamebridge.models import RuntimeProfile
from gamebridge.runtime import UmuRuntime


def test_umu_command_is_argv_and_allowlisted(tmp_path):
    executable = tmp_path / "game.exe"
    executable.touch()
    profile = RuntimeProfile(
        "demo:sample", str(tmp_path / "prefix"), str(executable),
        launch_arguments=("--safe", "argument with spaces"), environment={"UMU_LOG": "1"},
    )
    command = UmuRuntime().build(profile)
    assert command.argv == ("umu-run", str(executable), "--safe", "argument with spaces")
    assert command.environment["WINEPREFIX"] == str(tmp_path / "prefix")


def test_umu_rejects_arbitrary_environment(tmp_path):
    executable = tmp_path / "game.exe"
    executable.touch()
    profile = RuntimeProfile(
        "x", str(tmp_path / "prefix"), str(executable), environment={"LD_PRELOAD": "bad"}
    )
    with pytest.raises(ValueError, match="unsupported environment"):
        UmuRuntime().build(profile)
