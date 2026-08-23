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
