"""Finds video wallpapers downloaded from the Steam Workshop of Wallpaper Engine."""

import json
import os
import pty
import re
import select
import shutil
import tarfile
import termios
import io
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
    "~/.local/share/xalive/steam-content",
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


STEAMCMD_URL = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz"
STEAMCMD_DIR = Path("~/.local/share/xalive/steamcmd").expanduser()
DOWNLOAD_DIR = Path("~/.local/share/xalive/steam-content").expanduser()
USER_PATTERN = re.compile(r"[A-Za-z0-9_.-]{2,64}")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\r")
PROMPTS = {
    "password:": "password",
    "guard code:": "guard",
    "two-factor code:": "guard",
    "authentication code:": "guard",
}


class DownloadError(Exception):
    pass


def ensure_steamcmd(progress=lambda _text: None):
    script = STEAMCMD_DIR / "steamcmd.sh"
    if script.is_file():
        return script
    progress("steamcmd")
    request = urllib.request.Request(STEAMCMD_URL, headers={"User-Agent": "Xalive/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read(60_000_000)
    STEAMCMD_DIR.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive.getmembers():
            target = (STEAMCMD_DIR / member.name).resolve()
            if STEAMCMD_DIR.resolve() not in target.parents and target != STEAMCMD_DIR.resolve():
                raise DownloadError("unsafe archive")
            if not (member.isfile() or member.isdir()):
                raise DownloadError("unsafe archive")
        archive.extractall(STEAMCMD_DIR)
    script.chmod(0o755)
    return script


def download_item(item_id, user, ask, progress):
    """Download one Workshop item with steamcmd. Returns the item folder.

    ask("password" | "guard") must return the text typed by the user, or None
    to cancel. The password goes to steamcmd's terminal prompt only: it is never
    written to disk or put in a command line, and steamcmd keeps its own
    session token so later downloads usually need no password.
    """
    if not str(item_id).isdigit():
        raise DownloadError("invalid item id")
    if not USER_PATTERN.fullmatch(user or ""):
        raise DownloadError("invalid user")
    script = ensure_steamcmd(progress)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    master, slave = pty.openpty()
    attrs = termios.tcgetattr(slave)
    attrs[3] &= ~termios.ECHO
    termios.tcsetattr(slave, termios.TCSANOW, attrs)
    process = subprocess.Popen(
        [
            str(script),
            "+force_install_dir", str(DOWNLOAD_DIR),
            "+login", user,
            "+workshop_download_item", WALLPAPER_ENGINE_ID, str(item_id),
            "+quit",
        ],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
    )
    os.close(slave)
    buffer = ""
    transcript = ""
    try:
        while True:
            ready, _, _ = select.select([master], [], [], 1.0)
            if not ready:
                if process.poll() is not None:
                    break
                continue
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            text = ANSI.sub("", chunk.decode("utf-8", "replace"))
            buffer += text
            transcript = (transcript + text)[-6000:]
            lines = [line.strip() for line in text.split("\n") if line.strip()]
            if lines:
                progress(lines[-1])
            tail = buffer.lower().rstrip()
            for prompt, kind in PROMPTS.items():
                if tail.endswith(prompt):
                    answer = ask(kind)
                    if answer is None:
                        raise DownloadError("cancelled")
                    os.write(master, answer.encode() + b"\n")
                    buffer = ""
                    break
            buffer = buffer[-300:]
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        os.close(master)
    folder = DOWNLOAD_DIR / "steamapps" / "workshop" / "content" / WALLPAPER_ENGINE_ID / str(item_id)
    if "Success. Downloaded item" in transcript and folder.is_dir():
        return folder
    reason = next(
        (
            line.strip()
            for line in reversed(transcript.splitlines())
            if re.search(r"ERROR|FAILED|failure|Invalid", line, re.I)
        ),
        "download failed",
    )
    raise DownloadError(reason)
