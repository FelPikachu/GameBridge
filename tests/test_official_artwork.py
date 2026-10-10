from gamebridge.official_artwork import OfficialLauncherArtworkResolver


def test_mihoyo_official_artwork_resolves_exact_biz_and_caches_catalog(tmp_path, monkeypatch):
    resolver = OfficialLauncherArtworkResolver(tmp_path / "official-artwork.json")
    official = "https://launcher-webstatic.mihoyo.com/launcher-public/genshin.webp"
    icon = "https://launcher-webstatic.mihoyo.com/launcher-public/genshin.png"
    monkeypatch.setattr(
        resolver,
        "_json",
        lambda _url: {
            "retcode": 0,
            "data": {
                "game_info_list": [
                    {
                        "game": {"biz": "hk4e_cn"},
                        "backgrounds": [
                            {"background": {"url": official}, "icon": {"url": icon}}
                        ],
                    },
                    {
                        "game": {"biz": "nap_cn"},
                        "backgrounds": [
                            {"background": {"url": official}, "icon": {"url": icon}}
                        ],
                    },
                ]
            },
        },
    )

    assert resolver.resolve("mihoyo_cn", "hk4e_cn") == {
        "capsule": official,
        "hero": official,
        "header": official,
        "logo": icon,
    }
    monkeypatch.setattr(
        resolver,
        "_json",
        lambda _url: (_ for _ in ()).throw(AssertionError("cache missed")),
    )
    assert resolver.resolve("mihoyo_cn", "nap_cn")["capsule"] == official


def test_official_artwork_rejects_untrusted_or_malformed_urls(tmp_path, monkeypatch):
    resolver = OfficialLauncherArtworkResolver(tmp_path / "official-artwork.json")
    monkeypatch.setattr(
        resolver,
        "_json",
        lambda _url: {
            "retcode": 0,
            "data": {
                "game_info_list": [
                    {
                        "game": {"biz": "hk4e_cn"},
                        "backgrounds": [
                            {
                                "background": {"url": "https://example.invalid/fake.webp"},
                                "icon": {"url": "javascript:alert(1)"},
                            }
                        ],
                    }
                ]
            },
        },
    )

    assert resolver.resolve("mihoyo_cn", "hk4e_cn") is None
    assert resolver.resolve("hoyoplay_global", "hk4e_global") is None


def test_official_artwork_fails_closed_on_bad_payload(tmp_path, monkeypatch):
    resolver = OfficialLauncherArtworkResolver(tmp_path / "official-artwork.json")
    monkeypatch.setattr(resolver, "_json", lambda _url: {"retcode": -1, "data": []})

    assert resolver.resolve("mihoyo_cn", "hk4e_cn") is None


# ---------------------------------------------------------------------------
# NTE (Neverness to Everness): TapTap-hosted app icon is hardcoded so the
# Steam library and detail views always have a fallback before touching the
# cache file or the miHoYo HYP API.
# ---------------------------------------------------------------------------

NTE_CN_TAPTAP_ICON = (
    "https://img-tc.tapimg.com/market/images/"
    "ec0bd50c4a0bafa60cde8d0f3775ee84.png/_tap_appicon.jpg"
)


def test_nte_cn_artwork_returns_taptap_icon_without_touching_cache(tmp_path, monkeypatch):
    resolver = OfficialLauncherArtworkResolver(tmp_path / "official-artwork.json")
    # Block any network call; the TapTap URL is hardcoded and must never be
    # re-fetched at runtime.
    def _fail(_url):
        raise AssertionError(f"unexpected network call: {_url}")
    monkeypatch.setattr(resolver, "_json", _fail)

    artwork = resolver.cached("nte_cn", "nte_cn")
    assert artwork == {
        "capsule": NTE_CN_TAPTAP_ICON,
        "hero": NTE_CN_TAPTAP_ICON,
        "header": NTE_CN_TAPTAP_ICON,
        "logo": NTE_CN_TAPTAP_ICON,
        "icon": NTE_CN_TAPTAP_ICON,
    }

    # Same result via resolve(); it must not reach the network either.
    assert resolver.resolve("nte_cn", "nte_cn") == artwork


def test_nte_cn_artwork_does_not_persist_into_cache_file(tmp_path):
    resolver = OfficialLauncherArtworkResolver(tmp_path / "official-artwork.json")
    resolver.cached("nte_cn", "nte_cn")
    resolver.resolve("nte_cn", "nte_cn")
    # The cache file must stay absent: NTE artwork is hardcoded, no need to
    # touch the persistent cache.
    assert not (tmp_path / "official-artwork.json").exists()


def test_nte_cn_artwork_ignores_other_external_game_ids(tmp_path):
    """The hardcoded fallback only applies to ``nte_cn``/``nte_cn``. Any other
    external_game_id (e.g. if a future NTE build adds more titles) must fall
    through to the persistent cache rather than silently reuse the icon."""
    resolver = OfficialLauncherArtworkResolver(tmp_path / "official-artwork.json")
    # No cache entry, no miHoYo API; resolve() must return None.
    assert resolver.resolve("nte_cn", "nte_steam") is None
