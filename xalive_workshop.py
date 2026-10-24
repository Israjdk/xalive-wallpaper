"""Finds video wallpapers downloaded from the Steam Workshop of Wallpaper Engine."""

import json
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
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


API_URL = "https://api.steampowered.com/IPublishedFileService/QueryFiles/v1/"
API_KEY_URL = "https://steamcommunity.com/dev/apikey"
PAGE_URL = "https://steamcommunity.com/sharedfiles/filedetails/?id={}"
FLATPAK_STEAM = "com.valvesoftware.Steam"
# Wallpaper Engine's own maturity tags; hidden unless the user opts in.
MATURE_TAGS = ("Mature", "Questionable")
RANKED_BY_TREND = 3
RANKED_BY_TEXT = 12


def search(key, text="", cursor="*", per_page=24, allow_mature=False):
    """Query the Workshop (video items only). Returns ([item dicts], next_cursor)."""
    params = {
        "key": key,
        "appid": WALLPAPER_ENGINE_ID,
        "creator_appid": WALLPAPER_ENGINE_ID,
        "page": 0,
        "cursor": cursor or "*",
        "numperpage": per_page,
        "query_type": RANKED_BY_TEXT if text else RANKED_BY_TREND,
        "search_text": text,
        "requiredtags[0]": "Video",
        "match_all_tags": "true",
        "return_previews": "true",
        "return_short_description": "true",
    }
    if text == "":
        params["days"] = 90
    if not allow_mature:
        for index, tag in enumerate(MATURE_TAGS):
            params[f"excludedtags[{index}]"] = tag
    url = API_URL + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": "Xalive/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read(4_000_000))
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise ValueError("invalid API key") from error
        raise
    body = data.get("response", {}) if isinstance(data, dict) else {}
    items = []
    for entry in body.get("publishedfiledetails", []) or []:
        item_id = str(entry.get("publishedfileid", ""))
        title = entry.get("title")
        if not item_id.isdigit() or not isinstance(title, str):
            continue
        preview = entry.get("preview_url")
        if not (isinstance(preview, str) and preview.startswith("https://")):
            preview = ""
        subs = entry.get("subscriptions")
        items.append(
            {
                "id": item_id,
                "title": title,
                "preview": preview,
                "subscriptions": subs if isinstance(subs, int) else 0,
            }
        )
    next_cursor = body.get("next_cursor") or ""
    return items, next_cursor if items else ""


def steam_command():
    """The Steam client to ask for the item page, preferring a native install."""
    if shutil.which("steam"):
        return ["steam"]
    if shutil.which("flatpak"):
        listed = subprocess.run(
            ["flatpak", "info", FLATPAK_STEAM], capture_output=True, timeout=10
        )
        if listed.returncode == 0:
            return ["flatpak", "run", FLATPAK_STEAM]
    return None


def open_in_steam(item_id):
    """Open the item page in the Steam client (native or Flatpak); web page as fallback."""
    if not str(item_id).isdigit():
        raise ValueError("invalid item id")
    uri = f"steam://url/CommunityFilePage/{item_id}"
    command = steam_command()
    if command:
        subprocess.Popen(
            command + [uri],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return "steam"
    webbrowser.open(PAGE_URL.format(item_id))
    return "web"
