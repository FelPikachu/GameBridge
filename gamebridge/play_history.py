from __future__ import annotations

import json
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


FORMAT = "gamebridge.play-history"
FORMAT_VERSION = 1
SUPPORTED_PROVIDERS = {"epic", "mihoyo_cn"}
_PAIR = re.compile(r'"([^"\\]+)"\s*"([^"\\]*)"')


def read_history_store(path: Path) -> dict[tuple[str, str], dict[str, int]]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if payload.get("format") != "gamebridge.local-play-history" or payload.get("version") != 1:
        return {}
    result: dict[tuple[str, str], dict[str, int]] = {}
    for record in payload.get("games", []):
        if not isinstance(record, dict):
            continue
        provider = str(record.get("providerId", ""))
        external = str(record.get("externalGameId", ""))
        playtime = record.get("playtimeMinutes")
        last_played = record.get("lastPlayed")
        if provider and external and isinstance(playtime, int) and isinstance(last_played, int):
            result[(provider, external)] = {
                "playtimeMinutes": max(0, playtime),
                "lastPlayed": max(0, last_played),
            }
    return result


def merge_history_store(path: Path, records: list[dict[str, object]]) -> int:
    stored = read_history_store(path)
    changed = 0
    for record in records:
        key = (str(record["providerId"]), str(record["externalGameId"]))
        current = stored.get(key, {"playtimeMinutes": 0, "lastPlayed": 0})
        merged = {
            "playtimeMinutes": max(current["playtimeMinutes"], int(record["playtimeMinutes"])),
            "lastPlayed": max(current["lastPlayed"], int(record["lastPlayed"])),
        }
        if merged != current:
            changed += 1
        stored[key] = merged
    payload = {
        "format": "gamebridge.local-play-history",
        "version": 1,
        "games": [
            {"providerId": provider, "externalGameId": external, **values}
            for (provider, external), values in sorted(stored.items())
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".new")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return changed


def _matching_brace(text: str, opening: int) -> int:
    depth = 0
    quoted = escaped = False
    for index in range(opening, len(text)):
        char = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    raise ValueError("steam.localconfig_invalid")


def _named_blocks(text: str, name: str) -> list[tuple[int, int]]:
    blocks = []
    cursor = 0
    pattern = re.compile(rf'"{re.escape(name)}"\s*\{{', re.IGNORECASE)
    while (match := pattern.search(text, cursor)) is not None:
        opening = match.end() - 1
        closing = _matching_brace(text, opening)
        blocks.append((opening + 1, closing))
        cursor = closing + 1
    if not blocks:
        raise ValueError("steam.localconfig_apps_missing")
    return blocks


def read_app_history(text: str) -> dict[int, dict[str, int]]:
    result: dict[int, dict[str, int]] = {}
    for body_start, body_end in _named_blocks(text, "apps"):
        body = text[body_start:body_end]
        cursor = 0
        while cursor < len(body):
            match = re.search(r'"(-?\d+)"\s*\{', body[cursor:])
            if match is None:
                break
            app_id = int(match.group(1)) & 0xFFFFFFFF
            opening = cursor + match.end() - 1
            closing = _matching_brace(body, opening)
            values = {key.casefold(): value for key, value in _PAIR.findall(body[opening + 1:closing])}
            current = result.setdefault(app_id, {"playtimeMinutes": 0, "lastPlayed": 0})
            current["playtimeMinutes"] = max(current["playtimeMinutes"], max(0, int(values.get("playtime", "0") or 0)))
            current["lastPlayed"] = max(current["lastPlayed"], max(0, int(values.get("lastplayed", "0") or 0)))
            cursor = closing + 1
    return result


def merge_app_history(text: str, updates: dict[int, dict[str, int]]) -> tuple[str, int]:
    changed_apps: set[int] = set()
    for app_id, incoming in updates.items():
        for key, json_key in (("Playtime", "playtimeMinutes"), ("LastPlayed", "lastPlayed")):
            signed_id = app_id if app_id < 0x80000000 else app_id - 0x100000000
            occurrences: list[tuple[int, int, str]] = []
            for body_start, body_end in _named_blocks(text, "apps"):
                body = text[body_start:body_end]
                for raw_id in (str(app_id), str(signed_id)):
                    match = re.search(rf'"{re.escape(raw_id)}"\s*\{{', body)
                    if match is not None:
                        opening = body_start + match.end() - 1
                        closing = _matching_brace(text, opening)
                        occurrences.append((opening + 1, closing, text[opening + 1:closing]))
            pattern = re.compile(rf'("{key}"\s*")\d*(")', re.IGNORECASE)
            target = next((item for item in occurrences if pattern.search(item[2])), occurrences[0] if occurrences else None)
            if target is None:
                blocks = _named_blocks(text, "apps")
                marker = "Playtime" if key == "Playtime" else "LastPlayed"
                selected = next(
                    ((start, end) for start, end in blocks if f'"{marker}"' in text[start:end]),
                    blocks[0],
                )
                body_start, body_end = selected
                body = text[body_start:body_end]
                indent_match = re.search(r'\n([ \t]+)"-?\d+"\s*\n\1\{', body)
                entry_indent = indent_match.group(1) if indent_match else "\t\t\t\t\t"
                property_indent = entry_indent + "\t"
                raw_id = signed_id if key == "Playtime" else app_id
                value = int(incoming[json_key])
                addition = (
                    f'\n{entry_indent}"{raw_id}"\n{entry_indent}{{'
                    f'\n{property_indent}"{key}"\t\t"{value}"\n{entry_indent}}}'
                )
                text = text[:body_end] + addition + text[body_end:]
                changed_apps.add(app_id)
                continue
            start, end, block = target
            current_match = pattern.search(block)
            current = int(current_match.group(0).split('"')[-2]) if current_match else 0
            value = max(current, int(incoming[json_key]))
            if current_match:
                new_block = pattern.sub(rf'\g<1>{value}\g<2>', block, count=1)
            else:
                indent_match = re.search(r'\n([ \t]+)"', block)
                indent = indent_match.group(1) if indent_match else "\t\t\t\t\t\t"
                new_block = block + f'\n{indent}"{key}"\t\t"{value}"'
            if new_block != block:
                text = text[:start] + new_block + text[end:]
                changed_apps.add(app_id)
    return text, len(changed_apps)


def locate_localconfig(user_home: Path, app_ids: Iterable[int]) -> Path:
    userdata = user_home / ".local" / "share" / "Steam" / "userdata"
    candidates = sorted(userdata.glob("*/config/localconfig.vdf"))
    if not candidates:
        raise ValueError("steam.localconfig_missing")
    wanted = set(app_ids)
    scored = []
    for candidate in candidates:
        try:
            found = set(read_app_history(candidate.read_text(encoding="utf-8", errors="surrogateescape")))
        except (OSError, ValueError):
            continue
        scored.append((len(found & wanted), candidate.stat().st_mtime_ns, candidate))
    if not scored:
        raise ValueError("steam.localconfig_invalid")
    return max(scored)[2]


def export_history(user_home: Path, games: list[dict[str, object]], runtime: list[dict[str, int]] | None = None, stored: dict[tuple[str, str], dict[str, int]] | None = None) -> dict[str, object]:
    config = locate_localconfig(user_home, (int(game["steamAppId"]) for game in games))
    history = read_app_history(config.read_text(encoding="utf-8", errors="surrogateescape"))
    runtime_by_app = {int(item["steamAppId"]): item for item in (runtime or [])}
    records = []
    for game in games:
        app_id = int(game["steamAppId"])
        values = dict(history.get(app_id, {"playtimeMinutes": 0, "lastPlayed": 0}))
        live = runtime_by_app.get(app_id, {})
        # Steam is the sole source of truth after an import. Never add the old
        # GameBridge baseline again, otherwise repeated exports inflate time.
        values["playtimeMinutes"] = max(values["playtimeMinutes"], int(live.get("playtimeMinutes", 0)))
        local = (stored or {}).get((str(game["providerId"]), str(game["externalGameId"])), {})
        values["lastPlayed"] = max(
            values["lastPlayed"],
            int(live.get("lastPlayed", 0)),
            int(local.get("lastPlayed", 0)),
        )
        records.append({**game, **values})
    desktop = user_home / "Desktop"
    desktop.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = desktop / f"GameBridge-play-history-{timestamp}.json"
    payload = {
        "format": FORMAT,
        "version": FORMAT_VERSION,
        "exportedAt": datetime.now(timezone.utc).isoformat(),
        "games": records,
    }
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"path": os.fspath(destination), "count": len(records)}


def import_history(user_home: Path, source: Path, games: list[dict[str, object]]) -> dict[str, object]:
    if not source.is_file() or source.stat().st_size > 5 * 1024 * 1024:
        raise ValueError("play_history.invalid_file")
    payload = json.loads(source.read_text(encoding="utf-8"))
    if payload.get("format") != FORMAT or payload.get("version") != FORMAT_VERSION or not isinstance(payload.get("games"), list):
        raise ValueError("play_history.invalid_format")
    current = {(str(game["providerId"]), str(game["externalGameId"])): game for game in games}
    updates: dict[int, dict[str, int]] = {}
    matched = 0
    for record in payload["games"]:
        if not isinstance(record, dict):
            continue
        game = current.get((str(record.get("providerId", "")), str(record.get("externalGameId", ""))))
        if game is None:
            continue
        playtime = record.get("playtimeMinutes")
        last_played = record.get("lastPlayed")
        if not isinstance(playtime, int) or not isinstance(last_played, int) or not (0 <= playtime <= 100_000_000) or not (0 <= last_played <= 4_102_444_800):
            raise ValueError("play_history.invalid_values")
        updates[int(game["steamAppId"])] = {"playtimeMinutes": playtime, "lastPlayed": last_played}
        matched += 1
    config = locate_localconfig(user_home, updates)
    original = config.read_text(encoding="utf-8", errors="surrogateescape")
    merged, updated = merge_app_history(original, updates)
    backup = config.with_name(f"localconfig.vdf.gamebridge-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(config, backup)
    temporary = config.with_suffix(".vdf.gamebridge-new")
    temporary.write_text(merged, encoding="utf-8", errors="surrogateescape")
    os.replace(temporary, config)
    merged_values = read_app_history(merged)
    records = [{"steamAppId": app_id, **merged_values.get(app_id, values)} for app_id, values in updates.items()]
    return {"matched": matched, "updated": updated, "backupPath": os.fspath(backup), "restartRequired": True, "records": records}


def stage_history_import(
    user_home: Path,
    source: Path,
    games: list[dict[str, object]],
    pending_path: Path,
    steam_pid: int,
    steam_start_time: str,
    store_path: Path | None = None,
    runtime: list[dict[str, int]] | None = None,
) -> dict[str, object]:
    """Validate now, then let the worker write after Steam has exited."""
    if not source.is_file() or source.stat().st_size > 5 * 1024 * 1024:
        raise ValueError("play_history.invalid_file")
    payload = json.loads(source.read_text(encoding="utf-8"))
    if payload.get("format") != FORMAT or payload.get("version") != FORMAT_VERSION or not isinstance(payload.get("games"), list):
        raise ValueError("play_history.invalid_format")
    current = {(str(game["providerId"]), str(game["externalGameId"])): game for game in games}
    runtime_by_app = {int(item["steamAppId"]): item for item in (runtime or [])}
    updates: dict[str, dict[str, int]] = {}
    matched = 0
    for record in payload["games"]:
        if not isinstance(record, dict):
            continue
        game = current.get((str(record.get("providerId", "")), str(record.get("externalGameId", ""))))
        if game is None:
            continue
        playtime = record.get("playtimeMinutes")
        last_played = record.get("lastPlayed")
        if not isinstance(playtime, int) or not isinstance(last_played, int) or not (0 <= playtime <= 100_000_000) or not (0 <= last_played <= 4_102_444_800):
            raise ValueError("play_history.invalid_values")
        app_id = int(game["steamAppId"])
        live = runtime_by_app.get(app_id, {})
        updates[str(app_id)] = {
            "playtimeMinutes": max(playtime, int(live.get("playtimeMinutes", 0))),
            "lastPlayed": max(last_played, int(live.get("lastPlayed", 0))),
        }
        matched += 1
    if not updates:
        raise ValueError("play_history.no_matches")
    plan = {
        "format": "gamebridge.pending-play-history",
        "version": 1,
        "userHome": os.fspath(user_home.resolve()),
        "steamPid": steam_pid,
        "steamStartTime": steam_start_time,
        "updates": updates,
    }
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = pending_path.with_suffix(".new")
    temporary.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, pending_path)
    records = [{"steamAppId": int(app_id), **values} for app_id, values in updates.items()]
    return {
        "matched": matched,
        "updated": len(updates),
        "restartRequired": True,
        "records": records,
        "nonEmpty": sum(1 for values in updates.values() if values["playtimeMinutes"] or values["lastPlayed"]),
    }


def apply_staged_history(pending_path: Path) -> dict[str, object]:
    plan = json.loads(pending_path.read_text(encoding="utf-8"))
    if plan.get("format") != "gamebridge.pending-play-history" or plan.get("version") != 1:
        raise ValueError("play_history.invalid_pending")
    user_home = Path(str(plan["userHome"])).resolve()
    updates = {
        int(app_id): {
            "playtimeMinutes": int(values["playtimeMinutes"]),
            "lastPlayed": int(values["lastPlayed"]),
        }
        for app_id, values in dict(plan["updates"]).items()
    }
    config = locate_localconfig(user_home, updates)
    original = config.read_text(encoding="utf-8", errors="surrogateescape")
    merged, updated = merge_app_history(original, updates)
    backup = config.with_name(f"localconfig.vdf.gamebridge-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(config, backup)
    temporary = config.with_suffix(".vdf.gamebridge-new")
    temporary.write_text(merged, encoding="utf-8", errors="surrogateescape")
    os.replace(temporary, config)
    pending_path.unlink(missing_ok=True)
    return {"updated": updated, "backupPath": os.fspath(backup)}


def wait_for_process_exit(pid: int, start_time: str, timeout: float = 86_400) -> bool:
    deadline = time.monotonic() + timeout
    stat_path = Path("/proc") / str(pid) / "stat"
    while time.monotonic() < deadline:
        try:
            fields = stat_path.read_text(encoding="utf-8").split()
        except OSError:
            return True
        if len(fields) < 22 or fields[21] != start_time:
            return True
        time.sleep(0.1)
    return False
