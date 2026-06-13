#!/usr/bin/env python3
"""Restore Cursor extensions and theme settings from repository backups."""

from __future__ import annotations

import argparse
import glob
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
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
        "settings_json": user_data / "settings.json",
    }


INNER_CLI_RELATIVE = Path("usr/share/cursor/bin/cursor")


@dataclass(frozen=True)
class CursorCli:
    argv: list[str]

    @property
    def display(self) -> str:
        return " ".join(self.argv)


def is_shell_script(path: Path) -> bool:
    if not path.is_file():
        return False
    with path.open("rb") as handle:
        return handle.read(2) == b"#!"


def parse_appimage_from_wrapper(wrapper: Path) -> Path | None:
    text = wrapper.read_text(encoding="utf-8", errors="ignore")

    glob_match = re.search(
        r"ls\s+-1\s+([^\s|]+\.AppImage(?:\s+2>/dev/null)?(?:\s*\|\s*sort[^\n|]*)?)",
        text,
    )
    if glob_match:
        glob_pattern = glob_match.group(1).replace("2>/dev/null", "").strip()
        candidates = sorted(glob.glob(glob_pattern), key=os.path.getmtime)
        if candidates:
            return Path(candidates[-1])

    for match in re.finditer(r"([^\s\"']+\.AppImage)", text):
        candidate = Path(match.group(1))
        if candidate.exists():
            return candidate

    return None


def find_mounted_cursor_cli() -> Path | None:
    mount_root = Path("/tmp")
    if not mount_root.is_dir():
        return None

    mounts = sorted(
        mount_root.glob(".mount_Cursor*"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for mount in mounts:
        cli = mount / INNER_CLI_RELATIVE
        if cli.is_file() and os.access(cli, os.X_OK):
            return cli
    return None


def cached_appimage_cli(appimage: Path) -> Path:
    cache_root = Path.home() / ".cache" / "cursor-backup" / "appimage" / appimage.stem
    cli = cache_root / "squashfs-root" / INNER_CLI_RELATIVE
    if cli.is_file() and os.access(cli, os.X_OK):
        if cli.stat().st_mtime >= appimage.stat().st_mtime:
            return cli

    cache_root.mkdir(parents=True, exist_ok=True)
    print(f"Extracting Cursor CLI from AppImage cache: {appimage}")
    subprocess.run(
        [str(appimage), "--appimage-extract"],
        cwd=cache_root,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )

    if not cli.is_file():
        raise FileNotFoundError(
            f"AppImage extraction succeeded but CLI was not found at {cli}"
        )
    return cli


def resolve_cursor_cli(path: Path) -> CursorCli:
    if path.name == "cursor" and INNER_CLI_RELATIVE.as_posix() in path.as_posix():
        return CursorCli([str(path)])

    mounted = find_mounted_cursor_cli()
    if mounted is not None:
        return CursorCli([str(mounted)])

    appimage: Path | None = None
    if path.suffix == ".AppImage" and path.is_file():
        appimage = path
    elif is_shell_script(path):
        appimage = parse_appimage_from_wrapper(path)

    if appimage is not None and appimage.is_file():
        return CursorCli([str(cached_appimage_cli(appimage))])

    return CursorCli([str(path)])


def find_cursor_cli() -> CursorCli:
    candidates: list[Path] = []

    override = os.environ.get("CURSOR_CLI")
    if override:
        path = Path(override).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"CURSOR_CLI points to a missing executable: {path}")
        candidates.append(path)
    else:
        found = shutil.which("cursor")
        if found:
            candidates.append(Path(found))

        if platform.system() == "Darwin":
            bundled = Path("/Applications/Cursor.app/Contents/Resources/app/bin/cursor")
            if bundled.exists():
                candidates.append(bundled)

    mounted = find_mounted_cursor_cli()
    if mounted is not None:
        candidates.insert(0, mounted)

    for candidate in candidates:
        resolved = resolve_cursor_cli(candidate)
        if resolved.argv[0] != str(candidate):
            print(f"Resolved {candidate} -> {resolved.display}")
        return resolved

    raise FileNotFoundError(
        "Could not find the Cursor CLI. Install Cursor, ensure `cursor` is on PATH, "
        "or set CURSOR_CLI to the AppImage or its wrapper script."
    )


def read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def read_jsonc(path: Path) -> Any:
    """Parse JSON with VS Code/Cursor extensions (comments, trailing commas)."""
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"(^|[^:])//.*$", r"\1", text, flags=re.MULTILINE)
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    return json.loads(text)


def write_json(path: Path, data: Any, dry_run: bool) -> None:
    rendered = json.dumps(data, indent=2) + "\n"
    if dry_run:
        print(f"[dry-run] Would write {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")


def parse_extension_line(line: str) -> tuple[str, str | None]:
    line = line.strip()
    if not line or line.startswith("#"):
        raise ValueError("empty or comment")

    if "@" not in line:
        return line, None

    extension_id, version = line.rsplit("@", 1)
    if not extension_id or not version:
        raise ValueError(f"invalid extension entry: {line!r}")
    return extension_id, version


def load_extensions_txt(path: Path) -> list[tuple[str, str | None]]:
    if not path.exists():
        raise FileNotFoundError(f"Extensions list not found: {path}")

    extensions: list[tuple[str, str | None]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            extensions.append(parse_extension_line(line))
        except ValueError:
            continue
    return extensions


def load_disabled_extension_ids(repo_root: Path) -> list[str]:
    snapshot_path = repo_root / "cursor-setup.json"
    if snapshot_path.exists():
        snapshot = read_json(snapshot_path)
        disabled = snapshot.get("extensions", {}).get("disabled", [])
        return [entry["id"] for entry in disabled if entry.get("id")]

    recommendations_path = repo_root / ".vscode" / "extensions.json"
    if recommendations_path.exists():
        recommendations = read_json(recommendations_path)
        return list(recommendations.get("unwantedRecommendations", []))

    return []


def load_theme_data(repo_root: Path) -> dict[str, Any]:
    theme_path = repo_root / "theme.json"
    if not theme_path.exists():
        raise FileNotFoundError(f"Theme settings not found: {theme_path}")
    return read_json(theme_path)


def theme_settings_to_apply(theme_data: dict[str, Any]) -> dict[str, Any]:
    settings = theme_data.get("settings", {})
    return {key: value for key, value in settings.items() if value is not None}


def run_cursor(
    cli: CursorCli,
    args: list[str],
    *,
    dry_run: bool,
    check: bool = True,
) -> subprocess.CompletedProcess[str] | None:
    command = [*cli.argv, *args]
    printable = " ".join(command)
    if dry_run:
        print(f"[dry-run] Would run: {printable}")
        return None

    return subprocess.run(
        command,
        check=check,
        capture_output=True,
        text=True,
    )


def extension_specs(
    extensions: list[tuple[str, str | None]],
    *,
    latest: bool,
) -> list[str]:
    specs: list[str] = []
    for extension_id, version in extensions:
        spec = extension_id if latest or not version else f"{extension_id}@{version}"
        specs.append(spec)
    return specs


def chunked(items: list[str], size: int) -> list[list[str]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def install_extensions(
    cli: CursorCli,
    extensions: list[tuple[str, str | None]],
    *,
    latest: bool,
    dry_run: bool,
) -> tuple[int, int]:
    specs = extension_specs(extensions, latest=latest)
    if not specs:
        return 0, 0

    installed = 0
    failed = 0
    batches = chunked(specs, 20)

    for batch_number, batch in enumerate(batches, start=1):
        if len(batches) > 1:
            print(f"Installing extensions batch {batch_number}/{len(batches)}...")
        else:
            print(f"Installing {len(batch)} extensions...")

        args = ["--force"]
        for spec in batch:
            args.extend(["--install-extension", spec])

        try:
            result = run_cursor(cli, args, dry_run=dry_run, check=False)
        except OSError as error:
            print(f"  failed: {error}", file=sys.stderr)
            failed += len(batch)
            continue

        if dry_run:
            installed += len(batch)
            continue

        if result is None or result.returncode != 0:
            message = (result.stderr or result.stdout or "unknown error").strip() if result else ""
            print(f"  failed: {message}", file=sys.stderr)
            failed += len(batch)
            continue

        output = (result.stdout or "").strip()
        if output:
            print(output)
        installed += len(batch)

    return installed, failed


def set_extension_disabled_state(
    cli: CursorCli,
    extension_ids: list[str],
    *,
    disabled: bool,
    dry_run: bool,
) -> tuple[int, int]:
    if not extension_ids:
        return 0, 0

    flag = "--disable-extension" if disabled else "--enable-extension"
    action = "Disabling" if disabled else "Enabling"
    print(f"{action} {len(extension_ids)} extensions...")

    args: list[str] = []
    for extension_id in extension_ids:
        args.extend([flag, extension_id])

    try:
        result = run_cursor(cli, args, dry_run=dry_run, check=False)
    except OSError as error:
        print(f"  failed: {error}", file=sys.stderr)
        return 0, len(extension_ids)

    if dry_run:
        return len(extension_ids), 0

    if result is None or result.returncode != 0:
        message = (result.stderr or result.stdout or "unknown error").strip() if result else ""
        print(f"  failed: {message}", file=sys.stderr)
        return 0, len(extension_ids)

    output = (result.stdout or "").strip()
    if output:
        print(output)
    return len(extension_ids), 0


def apply_theme_settings(
    settings_path: Path,
    theme_data: dict[str, Any],
    *,
    dry_run: bool,
) -> list[str]:
    to_apply = theme_settings_to_apply(theme_data)
    if not to_apply:
        print("No theme settings to apply.")
        return []

    existing = read_jsonc(settings_path) if settings_path.exists() else {}
    merged = dict(existing)
    changed_keys: list[str] = []

    for key, value in to_apply.items():
        if merged.get(key) != value:
            merged[key] = value
            changed_keys.append(key)

    if not changed_keys:
        print("Theme settings already match backup.")
        return []

    print(f"Updating theme settings in {settings_path}:")
    for key in changed_keys:
        print(f"  {key} = {merged[key]!r}")

    write_json(settings_path, merged, dry_run)
    return changed_keys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Restore Cursor extensions and theme settings from repository backups.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print actions without changing anything.",
    )
    parser.add_argument(
        "--skip-extensions",
        action="store_true",
        help="Only apply theme settings.",
    )
    parser.add_argument(
        "--skip-theme",
        action="store_true",
        help="Only install/disable extensions.",
    )
    parser.add_argument(
        "--latest",
        action="store_true",
        help="Install latest extension versions instead of pinned versions from extensions.txt.",
    )
    parser.add_argument(
        "--extensions-file",
        type=Path,
        help="Override path to extensions.txt (default: ./extensions.txt).",
    )
    parser.add_argument(
        "--theme-file",
        type=Path,
        help="Override path to theme.json (default: ./theme.json).",
    )
    return parser


def main() -> int:
    repo_root = Path(__file__).resolve().parent
    parser = build_parser()
    args = parser.parse_args()

    if args.skip_extensions and args.skip_theme:
        print("error: --skip-extensions and --skip-theme cannot be used together", file=sys.stderr)
        return 2

    paths = cursor_paths()
    extensions_path = (args.extensions_file or repo_root / "extensions.txt").resolve()
    theme_path = (args.theme_file or repo_root / "theme.json").resolve()

    try:
        cli = find_cursor_cli()
    except FileNotFoundError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print(f"Using Cursor CLI: {cli.display}")

    exit_code = 0

    if not args.skip_extensions:
        try:
            extensions = load_extensions_txt(extensions_path)
        except FileNotFoundError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1

        if not extensions:
            print(f"No extensions found in {extensions_path}")
        else:
            installed, install_failed = install_extensions(
                cli,
                extensions,
                latest=args.latest,
                dry_run=args.dry_run,
            )
            print(f"Installed {installed}/{len(extensions)} extensions")
            if install_failed:
                exit_code = 1

        disabled_ids = load_disabled_extension_ids(repo_root)
        if disabled_ids:
            disabled, disable_failed = set_extension_disabled_state(
                cli,
                disabled_ids,
                disabled=True,
                dry_run=args.dry_run,
            )
            print(f"Disabled {disabled}/{len(disabled_ids)} extensions")
            if disable_failed:
                exit_code = 1

    if not args.skip_theme:
        try:
            theme_data = (
                read_json(theme_path)
                if args.theme_file is not None
                else load_theme_data(repo_root)
            )
        except FileNotFoundError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1

        changed = apply_theme_settings(
            paths["settings_json"],
            theme_data,
            dry_run=args.dry_run,
        )
        if changed:
            print(f"Applied {len(changed)} theme setting(s)")
            if not args.dry_run:
                print("Restart Cursor if it is open so theme changes take effect.")

    if args.dry_run:
        print("Dry run complete; no changes were made.")

    return exit_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except json.JSONDecodeError as error:
        print(f"error: invalid JSON: {error}", file=sys.stderr)
        raise SystemExit(1)
