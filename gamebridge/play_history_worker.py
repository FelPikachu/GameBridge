from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if os.fspath(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, os.fspath(PLUGIN_ROOT))

from gamebridge.play_history import apply_staged_history, wait_for_process_exit


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    pending = Path(sys.argv[1]).resolve()
    result_path = Path(sys.argv[2]).resolve()
    try:
        plan = json.loads(pending.read_text(encoding="utf-8"))
        if not wait_for_process_exit(int(plan["steamPid"]), str(plan["steamStartTime"])):
            raise TimeoutError("Steam did not exit before the pending import expired")
        result = {"ok": True, **apply_staged_history(pending)}
    except Exception as error:
        result = {"ok": False, "error": f"{type(error).__name__}: {error}"}
    result_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = result_path.with_suffix(".new")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, result_path)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
