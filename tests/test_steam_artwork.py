from gamebridge.steam_artwork import SteamArtworkResolver


def test_steam_artwork_cache_round_trip(tmp_path, monkeypatch):
    resolver = SteamArtworkResolver(tmp_path / "steam-artwork.json")
    expected = {"app_id": 123, "hero": "https://example.invalid/hero.jpg"}
    monkeypatch.setattr(resolver, "_find_match", lambda _title, _developer: expected)

    assert resolver.resolve("epic", "sample", "Sample", "Studio") == expected
    monkeypatch.setattr(
        resolver,
        "_find_match",
        lambda _title, _developer: (_ for _ in ()).throw(AssertionError("cache missed")),
    )
    assert resolver.resolve("epic", "sample", "Sample", "Studio") == expected


def test_steam_artwork_normalizes_store_punctuation():
    assert SteamArtworkResolver._normalize("LEGO® 2K Drive™") == "lego2kdrive"
    assert SteamArtworkResolver._search_title("LEGO®  2K Drive™") == "LEGO 2K Drive"


def test_steam_match_accepts_epic_publisher_in_developer_field(tmp_path, monkeypatch):
    resolver = SteamArtworkResolver(tmp_path / "steam-artwork.json")
    monkeypatch.setattr(
        resolver,
        "_json",
        lambda url: (
            {"990080": {"success": True, "data": {
                "developers": ["Avalanche Software"],
                "publishers": ["Warner Bros. Games"],
            }}}
            if "appdetails" in url
            else {"items": [{"id": 990080, "name": "Hogwarts Legacy"}]}
        ),
    )
    monkeypatch.setattr(resolver, "_exists", lambda _url: True)

    match = resolver.resolve(
        "epic", "hogwarts", "Hogwarts Legacy", "Warner Bros."
    )
    assert match is not None
    assert match["app_id"] == 990080


def test_delisted_game_uses_pcgw_app_id_fallback(tmp_path, monkeypatch):
    resolver = SteamArtworkResolver(tmp_path / "steam-artwork.json")

    def response(url):
        if "storesearch" in url:
            return {"total": 0, "items": []}
        if "list=search" in url:
            return {"query": {"search": [{"title": "Lego 2K Drive", "pageid": 185998}]}}
        if "cargoquery" in url:
            return {"cargoquery": [{"title": {"Steam AppID": "1451810"}}]}
        return {"1451810": {"success": True, "data": {
            "developers": ["Visual Concepts"], "publishers": ["2K"]
        }}}

    monkeypatch.setattr(resolver, "_json", response)
    monkeypatch.setattr(resolver, "_exists", lambda _url: True)
    match = resolver.resolve("epic", "lego", "LEGO® 2K Drive", "2K Games, Inc.")
    assert match is not None
    assert match["app_id"] == 1451810
