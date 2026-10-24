"""Finds video wallpapers downloaded from the Steam Workshop of Wallpaper Engine."""

import json
import re
from pathlib import Path

WALLPAPER_ENGINE_ID = "431960"
STEAM_ROOTS = (
    "~/.steam/steam",
    "~/.local/share/Steam",
    "~/.var/app/com.valvesoftware.Steam/.local/share/Steam",
)


def steam_libraries():
    roots = []
    for root in STEAM_ROOTS:
        base = Path(root).expanduser()
        if not (base / "steamapps").is_dir():
            continue
        roots.append(base)
        try:
            text = (base / "steamapps" / "libraryfolders.vdf").read_text(errors="replace")
        except OSError:
            continue
        roots.extend(Path(path) for path in re.findall(r'"path"\s+"([^"]+)"', text))
    unique = []
    for root in roots:
        resolved = root.resolve()
        if resolved not in unique:
            unique.append(resolved)
    return unique


def workshop_videos():
    """Return [(title, video_path)] for Workshop items whose type is video."""
    found = []
    for library in steam_libraries():
        content = library / "steamapps" / "workshop" / "content" / WALLPAPER_ENGINE_ID
        try:
            folders = sorted(path for path in content.iterdir() if path.is_dir())
        except OSError:
            continue
        for folder in folders:
            try:
                project = json.loads((folder / "project.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(project, dict) or str(project.get("type", "")).lower() != "video":
                continue
            name = project.get("file")
            if not isinstance(name, str) or not name:
                continue
            video = (folder / name).resolve()
            # Never follow a "file" entry that points outside its own item folder.
            if folder.resolve() not in video.parents or not video.is_file():
                continue
            title = project.get("title")
            found.append((title if isinstance(title, str) and title else folder.name, video))
    return found
