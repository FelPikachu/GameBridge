import hashlib
from io import BytesIO

import pytest

from gamebridge.tooling import ToolInstaller, ToolRelease


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
