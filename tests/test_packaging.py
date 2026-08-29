from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path


def test_plugin_zip_preserves_channel_guard_executable_permission(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parent.parent
    output = tmp_path / "GameBridge.zip"

    subprocess.run(  # noqa: S603 - fixed project-local packaging command
        [sys.executable, str(root / "scripts/build_plugin_zip.py"), "--output", str(output)],
        cwd=root,
        check=True,
    )

    with zipfile.ZipFile(output) as archive:
        guard = archive.getinfo("GameBridge/gamebridge/channel_guard.py")
        assert (guard.external_attr >> 16) & 0o777 == 0o755
        assert archive.read("GameBridge/gamebridge/application.py") == (
            root / "gamebridge/application.py"
        ).read_bytes()
        assert archive.read("GameBridge/dist/index.js") == (root / "dist/index.js").read_bytes()
