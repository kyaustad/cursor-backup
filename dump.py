#!/usr/bin/env python3
"""Dump installed/enabled Cursor extensions and active theme settings."""

from __future__ import annotations

import json
import os
import platform
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def cursor_paths() -> dict[str, Path]:
    home = Path.home()
    system = platform.system()

    if system == "Darwin":
        user_data = home / "Library" / "Application Support" / "Cursor" / "User"
    elif system == "Windows":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise RuntimeError("APPDATA is not set; cannot locate Cursor config on Windows")
        user_data = Path(appdata) / "Cursor" / "User"
    else:
        user_data = home / ".config" / "Cursor" / "User"

    return {
        "extensions_json": home / ".cursor" / "extensions" / "extensions.json",
        "settings_json": user_data / "settings.json",
        "state_db": user_data / "globalStorage" / "state.vscdb",
    }


def read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def read_state_value(db_path: Path, key: str) -> Any | None:
    if not db_path.exists():
        return None

    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT value FROM ItemTable WHERE key = ?",
            (key,),
        ).fetchone()
    finally:
        connection.close()

    if row is None:
        return None
    return json.loads(row[0])


def extension_id(entry: dict[str, Any]) -> str:
    return entry["identifier"]["id"]


def extension_version(entry: dict[str, Any]) -> str:
    return entry.get("version", "unknown")


def load_installed_extensions(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"Installed extensions file not found: {path}")

    installed = read_json(path)
    return [
        {
            "id": extension_id(entry),
            "version": extension_version(entry),
        }
        for entry in installed
    ]


def load_disabled_extension_ids(db_path: Path) -> set[str]:
    disabled = read_state_value(db_path, "extensionsIdentifiers/disabled")
    if not disabled:
        return set()

    ids: set[str] = set()
    for item in disabled:
        if isinstance(item, str):
            ids.add(item)
            continue
        if isinstance(item, dict):
            identifier = item.get("id")
            if identifier:
                ids.add(identifier)
    return ids


def theme_summary(theme_data: dict[str, Any] | None) -> dict[str, Any] | None:
    if not theme_data:
        return None

    extension_data = theme_data.get("extensionData") or {}
    return {
        "label": theme_data.get("label"),
        "settingsId": theme_data.get("settingsId"),
        "extensionId": extension_data.get("_extensionId"),
    }


def pick_setting(settings: dict[str, Any], *keys: str) -> Any | None:
    for key in keys:
        if key in settings:
            return settings[key]
    return None


def main() -> int:
    repo_root = Path(__file__).resolve().parent
    paths = cursor_paths()

    installed = load_installed_extensions(paths["extensions_json"])
    disabled_ids = load_disabled_extension_ids(paths["state_db"])

    enabled = [ext for ext in installed if ext["id"] not in disabled_ids]
    disabled = [ext for ext in installed if ext["id"] in disabled_ids]

    settings = read_json(paths["settings_json"]) if paths["settings_json"].exists() else {}
    color_theme = theme_summary(read_state_value(paths["state_db"], "colorThemeData"))
    icon_theme = theme_summary(read_state_value(paths["state_db"], "iconThemeData"))

    snapshot = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "extensions": {
            "installed": installed,
            "enabled": enabled,
            "disabled": disabled,
        },
        "theme": {
            "activeColorTheme": color_theme,
            "activeIconTheme": icon_theme,
            "settings": {
                "workbench.colorTheme": pick_setting(settings, "workbench.colorTheme"),
                "workbench.iconTheme": pick_setting(settings, "workbench.iconTheme"),
                "workbench.preferredDarkColorTheme": pick_setting(
                    settings, "workbench.preferredDarkColorTheme"
                ),
                "workbench.preferredLightColorTheme": pick_setting(
                    settings, "workbench.preferredLightColorTheme"
                ),
                "window.autoDetectColorScheme": pick_setting(
                    settings, "window.autoDetectColorScheme"
                ),
            },
        },
    }

    extensions_txt = repo_root / "extensions.txt"
    extensions_json = repo_root / ".vscode" / "extensions.json"
    theme_json = repo_root / "theme.json"
    snapshot_json = repo_root / "cursor-setup.json"

    extensions_txt.write_text(
        "\n".join(f"{ext['id']}@{ext['version']}" for ext in enabled) + ("\n" if enabled else ""),
        encoding="utf-8",
    )

    extensions_json.parent.mkdir(parents=True, exist_ok=True)
    extensions_json.write_text(
        json.dumps(
            {
                "recommendations": [ext["id"] for ext in enabled],
                "unwantedRecommendations": [ext["id"] for ext in disabled],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    theme_json.write_text(json.dumps(snapshot["theme"], indent=2) + "\n", encoding="utf-8")
    snapshot_json.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")

    print(f"Wrote {len(enabled)} enabled extensions to {extensions_txt}")
    if disabled:
        print(f"Skipped {len(disabled)} disabled extensions")
    print(f"Wrote recommendations to {extensions_json}")
    print(f"Wrote theme settings to {theme_json}")
    print(f"Wrote full snapshot to {snapshot_json}")

    if color_theme:
        print(f"Color theme: {color_theme.get('label')} ({color_theme.get('settingsId')})")
    if icon_theme:
        print(f"Icon theme: {icon_theme.get('label')} ({icon_theme.get('settingsId')})")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FileNotFoundError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
