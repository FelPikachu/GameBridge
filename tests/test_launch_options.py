import shlex

import pytest

from gamebridge.launch_options import (
    preset_launch_options,
    repair_launch_options,
    shortcut_profile_launch_options,
)


def repaired(raw: str) -> list[str]:
    return shlex.split(repair_launch_options(raw, "epic", "lego-id"))


def test_empty_options_receive_only_gamebridge_command():
    assert repaired("") == [
        "gamebridge/launcher.py", "--provider", "epic", "--game-id", "lego-id"
    ]


@pytest.mark.parametrize(
    "custom",
    [
        "~/lsfg %command%",
        "~/fgmod/fgmod %command%",
        "~/fgmod/fgmod ~/lsfg %command%",
        "WINEDLLOVERRIDES=dxgi=n,b SteamDeck=0 %command% -dx12",
        "Dx12Upscaler=fsr31 ~/fgmod/fgmod %command%",
    ],
)
def test_user_modifiers_survive_repair(custom: str):
    tokens = repaired(custom)
    for token in shlex.split(custom):
        assert token in tokens
    assert tokens.count("gamebridge/launcher.py") == 1
    if "%command%" in tokens:
        assert tokens[tokens.index("%command%") + 1] == "gamebridge/launcher.py"


def test_old_managed_command_is_replaced_without_losing_wrappers():
    raw = (
        '~/fgmod/fgmod ~/lsfg %command% "gamebridge/launcher.py" '
        '--provider epic --game-id "old" --language "zh-CN" -dx12'
    )
    tokens = repaired(raw)
    assert "old" not in tokens
    assert "--language" not in tokens
    assert tokens[:2] == ["~/lsfg", "%command%"]
    assert tokens[-3:-1] == ["--game-wrapper", "~/fgmod/fgmod"]
    assert tokens[-1] == "-dx12"


def test_repair_is_idempotent():
    once = repair_launch_options("~/lsfg %command%", "epic", "lego-id")
    assert repair_launch_options(once, "epic", "lego-id") == once


def test_malformed_quoting_is_never_silently_rewritten():
    with pytest.raises(ValueError):
        repair_launch_options("~/lsfg 'unterminated", "epic", "lego-id")


def test_existing_decky_handoff_is_not_duplicated():
    raw = 'gamebridge/launcher.py --provider epic --game-id lego-id -- ~/lsfg %command%'
    tokens = repaired(raw)
    assert "--" not in tokens
    assert tokens[:2] == ["~/lsfg", "%command%"]


def test_broken_managed_first_order_is_repaired_for_steam_expansion():
    raw = 'gamebridge/launcher.py --provider epic --game-id lego-id ~/lsfg %command%'
    tokens = repaired(raw)
    assert tokens[:2] == ["~/lsfg", "%command%"]
    assert tokens[2] == "gamebridge/launcher.py"


def test_plugin_snippet_glued_to_game_id_is_separated_before_repair():
    raw = (
        "gamebridge/launcher.py --provider epic --game-id lego-id~/lsfg %command%"
    )
    tokens = repaired(raw)
    assert "lego-id~/lsfg" not in tokens
    assert tokens[:2] == ["~/lsfg", "%command%"]
    assert tokens[tokens.index("--game-id") + 1] == "lego-id"


def test_home_wrapper_is_not_single_quoted_because_tilde_must_expand():
    value = repair_launch_options("~/lsfg %command%", "epic", "lego-id")
    assert value.startswith("~/lsfg %command% ")
    assert "'~/lsfg'" not in value


def test_fgmod_is_deferred_until_gamebridge_knows_the_game_directory():
    tokens = repaired("~/fgmod/fgmod %command%")
    assert tokens[0] == "%command%"
    assert tokens[-2:] == ["--game-wrapper", "~/fgmod/fgmod"]


def test_lsfg_stays_outside_while_fgmod_is_deferred():
    tokens = repaired("~/fgmod/fgmod ~/lsfg %command%")
    assert tokens[:2] == ["~/lsfg", "%command%"]
    assert tokens[-2:] == ["--game-wrapper", "~/fgmod/fgmod"]


def test_deferred_fgmod_survives_repeated_repair():
    once = repair_launch_options(
        "~/fgmod/fgmod ~/lsfg %command%", "epic", "lego-id"
    )
    assert repair_launch_options(once, "epic", "lego-id") == once


def test_framegen_uninstaller_is_deferred_to_the_real_game_command():
    tokens = repaired("~/fgmod/fgmod-uninstaller.sh %command%")
    assert tokens[0] == "%command%"
    assert tokens[-2:] == ["--game-wrapper", "~/fgmod/fgmod-uninstaller.sh"]


def test_user_facing_presets_match_the_three_verified_combinations():
    default = shlex.split(preset_launch_options("default", "epic", "lego-id"))
    lsfg = shlex.split(preset_launch_options("lsfg", "epic", "lego-id"))
    framegen = shlex.split(preset_launch_options("framegen", "epic", "lego-id"))
    combined = shlex.split(preset_launch_options("combined", "epic", "lego-id"))
    assert default[0] == "gamebridge/launcher.py"
    assert lsfg[:2] == ["~/lsfg", "%command%"]
    assert framegen[:3] == ["WINEDLLOVERRIDES=dxgi=n,b", "SteamDeck=0", "%command%"]
    assert combined[:2] == ["~/lsfg", "%command%"]
    assert combined[-2:] == ["--game-wrapper", "~/fgmod/fgmod"]


def test_unknown_launch_preset_is_rejected():
    with pytest.raises(ValueError, match="invalid_preset"):
        preset_launch_options("unknown", "epic", "lego-id")


@pytest.mark.parametrize("route_game_id", ["genshin", "zzz", "starrail", "honkai3"])
def test_hoyoplay_router_presets_keep_the_stable_route(route_game_id):
    base = (
        '"gamebridge/launcher.py" --provider mihoyo '
        f'--game-id "{route_game_id}"'
    )
    default = shlex.split(shortcut_profile_launch_options(
        "default", base, "gamebridge_router"
    ))
    lsfg = shlex.split(shortcut_profile_launch_options(
        "lsfg", base, "gamebridge_router"
    ))
    framegen = shlex.split(shortcut_profile_launch_options(
        "framegen", base, "gamebridge_router"
    ))
    combined = shlex.split(shortcut_profile_launch_options(
        "combined", base, "gamebridge_router"
    ))

    for tokens in (default, lsfg, framegen, combined):
        assert tokens[tokens.index("--provider") + 1] == "mihoyo"
        assert tokens[tokens.index("--game-id") + 1] == route_game_id
        assert tokens.count("gamebridge/launcher.py") == 1
    assert lsfg[:2] == ["~/lsfg", "%command%"]
    assert framegen[:3] == [
        "WINEDLLOVERRIDES=dxgi=n,b", "SteamDeck=0", "%command%",
    ]
    assert combined[:2] == ["~/lsfg", "%command%"]
    assert combined[-2:] == ["--game-wrapper", "~/fgmod/fgmod"]


def test_direct_genshin_presets_wrap_the_single_real_game_command():
    base = (
        'WINE_ENABLE_TIMEOUT_FIX=1 "/home/deck/homebrew/plugins/GameBridge/'
        'gamebridge/channel_guard.py" --game-id "hk4e_cn" -- %command%'
    )
    lsfg = shlex.split(shortcut_profile_launch_options(
        "lsfg", base, "direct_executable"
    ))
    framegen = shlex.split(shortcut_profile_launch_options(
        "framegen", base, "direct_executable"
    ))
    combined = shlex.split(shortcut_profile_launch_options(
        "combined", base, "direct_executable"
    ))

    for tokens in (lsfg, framegen, combined):
        assert tokens.count("%command%") == 1
        assert sum(token.endswith("channel_guard.py") for token in tokens) == 1
        assert tokens[tokens.index("--game-id") + 1] == "hk4e_cn"
    assert lsfg[-2:] == ["~/lsfg", "%command%"]
    assert framegen[:2] == ["WINEDLLOVERRIDES=dxgi=n,b", "SteamDeck=0"]
    assert combined[-3:] == ["~/fgmod/fgmod", "~/lsfg", "%command%"]


@pytest.mark.parametrize(
    ("mode", "base"),
    [
        ("direct_executable", "no-placeholder"),
        ("direct_executable", "%command% %command%"),
        ("gamebridge_router", "not-gamebridge"),
        ("unknown", "%command%"),
    ],
)
def test_shortcut_profile_preset_rejects_unsafe_routes(mode, base):
    with pytest.raises(ValueError, match="invalid_shortcut_profile"):
        shortcut_profile_launch_options("combined", base, mode)


@pytest.mark.parametrize(
    "raw",
    [
        "~/fgmod/fgmod ~/lsfg WINEDLLOVERRIDES=dxgi=n,b SteamDeck=0 %command%",
        "%command% ~/lsfg SteamDeck=0 ~/fgmod/fgmod WINEDLLOVERRIDES=dxgi=n,b",
        "gamebridge/launcher.py --provider epic --game-id old "
        "~/fgmod/fgmod %command% SteamDeck=0 ~/lsfg WINEDLLOVERRIDES=dxgi=n,b",
    ],
)
def test_lsfg_and_framegen_are_canonicalized_regardless_of_position(raw: str):
    tokens = repaired(raw)
    assert tokens[:4] == [
        "SteamDeck=0",
        "WINEDLLOVERRIDES=dxgi=n,b",
        "~/lsfg",
        "%command%",
    ] or tokens[:4] == [
        "WINEDLLOVERRIDES=dxgi=n,b",
        "SteamDeck=0",
        "~/lsfg",
        "%command%",
    ]
    assert tokens.count("~/lsfg") == 1
    assert tokens.count("~/fgmod/fgmod") == 1
    assert tokens[-2:] == ["--game-wrapper", "~/fgmod/fgmod"]
