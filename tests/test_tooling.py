import hashlib
from io import BytesIO

import pytest

from gamebridge.tooling import (
    DWPROTON_RELEASE,
    GE_PROTON_RELEASE,
    LEGENDARY_RELEASES,
    UMU_RELEASE,
    ToolInstaller,
    ToolRelease,
)


class Response(BytesIO):
    headers = {"Content-Length": "11"}

    def geturl(self):
        return "https://release-assets.githubusercontent.com/asset"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def test_installer_verifies_and_atomically_installs(tmp_path, monkeypatch):
    content = b"hello world"
    release = ToolRelease(
        "test", "https://github.com/legendary-gl/legendary/releases/download/test/tool",
        hashlib.sha256(content).hexdigest(),
    )
    installer = ToolInstaller()
    monkeypatch.setattr(installer, "legendary_release", lambda: release)
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response(content))
    target = tmp_path / "tools" / "legendary"
    result = installer.install_legendary(target)
    assert target.read_bytes() == content
    assert target.stat().st_mode & 0o111
    assert result["version"] == "test"


def test_installer_rejects_untrusted_url(tmp_path, monkeypatch):
    installer = ToolInstaller()
    release = ToolRelease("test", "https://example.com/tool", "0" * 64)
    monkeypatch.setattr(installer, "legendary_release", lambda: release)
    with pytest.raises(ValueError, match="allowlisted"):
        installer.install_legendary(tmp_path / "legendary")


def test_ssl_context_requires_a_real_ca_bundle(monkeypatch):
    monkeypatch.setattr("gamebridge.tooling.SYSTEM_CA_FILES", ("/definitely/missing",))
    with pytest.raises(RuntimeError, match="CA certificate"):
        ToolInstaller._ssl_context()


def test_x86_managed_components_use_the_single_region_gateway():
    releases = (
        UMU_RELEASE,
        GE_PROTON_RELEASE,
        DWPROTON_RELEASE,
        LEGENDARY_RELEASES["x86_64"],
    )
    assert all(
        release.gateway_url
        and release.gateway_url.startswith(
            "https://1300823591-fyjwaget5r.ap-guangzhou.tencentscf.com/download/"
        )
        for release in releases
    )
    assert LEGENDARY_RELEASES["aarch64"].gateway_url is None


def test_download_source_is_derived_after_the_gateway_redirect():
    assert ToolInstaller._download_source("https://d.pcs.baidu.com/file/example") == "china"
    assert ToolInstaller._download_source("https://xafj-ct11.baidupcs.com/file/example") == "china"
    assert ToolInstaller._download_source("https://github.com/example/file") == "official"


def test_real_download_progress_records_component_source_and_bytes(tmp_path, monkeypatch):
    content = b"hello world"
    release = ToolRelease(
        "test",
        "https://github.com/example/official",
        hashlib.sha256(content).hexdigest(),
        "legendary",
        "https://1300823591-fyjwaget5r.ap-guangzhou.tencentscf.com/download/legendary",
    )
    installer = ToolInstaller()
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response(content))

    result = installer._download(release, tmp_path, ".progress-")

    assert result.read_bytes() == content
    status = installer.progress_status()
    assert status["component"] == "legendary"
    assert status["source"] == "official"
    assert status["downloadedBytes"] == len(content)
    assert status["progress"] == 1.0
