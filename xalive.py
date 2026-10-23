#!/usr/bin/env python3
"""Set a video wallpaper on X11, with a GTK control window."""

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import xalive_sources as sources  # noqa: E402
from xalive_i18n import tr  # noqa: E402


ICON_SIZE_MIN = 24
ICON_SIZE_MAX = 128
THUMB_WIDTH = 144
GALLERY_THUMB_WIDTH = 190
GALLERY_THUMB_HEIGHT = 107
THUMB_HEIGHT = 81
THUMB_CACHE = (
    Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    / "xalive"
    / "thumbs"
)
BROWSER_SITES = [
    ("MoeWalls", "https://moewalls.com/"),
    ("MyLiveWallpapers", "https://mylivewallpapers.com/"),
    ("MotionBGs", "https://motionbgs.com/"),
    ("Pixabay (videos)", "https://pixabay.com/videos/"),
    ("Internet Archive", "https://archive.org/"),
]
VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".ogv", ".wmv", ".flv", ".mpg", ".mpeg",
}


MODE_PATTERN = re.compile(r"\d{3,5}x\d{3,5}(?:_\d+(?:\.\d+)?)?")
OUTPUT_PATTERN = re.compile(
    r"^(\S+) (connected|disconnected)(?: primary)?(?: (\d+)x(\d+)\+(-?\d+)\+(-?\d+))?"
)


def query_outputs():
    """Connected monitors from xrandr: name, current mode, position and available modes."""
    try:
        result = subprocess.run(
            ["xrandr", "--query"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    outputs = []
    current = None
    for line in result.stdout.splitlines():
        if line[:1].isspace():
            if current is None:
                continue
            parts = line.split()
            if parts and MODE_PATTERN.fullmatch(parts[0]):
                if parts[0] not in current["modes"]:
                    current["modes"].append(parts[0])
                if any("*" in rate for rate in parts[1:]):
                    current["mode"] = parts[0]
                if any("+" in rate for rate in parts[1:]):
                    current["preferred"] = parts[0]
            continue
        match = OUTPUT_PATTERN.match(line)
        if match is None or match.group(2) != "connected":
            current = None
            continue
        current = {
            "name": match.group(1),
            "mode": None,
            "preferred": None,
            "modes": [],
            "geometry": tuple(int(v) for v in match.group(3, 4, 5, 6))
            if match.group(3)
            else None,
        }
        outputs.append(current)
    for output in outputs:
        if output["mode"] is None and output["geometry"] is not None:
            output["mode"] = f"{output['geometry'][0]}x{output['geometry'][1]}"
    return outputs


def set_output_mode(name, mode):
    try:
        result = subprocess.run(
            ["xrandr", "--output", name, "--mode", mode],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, str(error)
    return result.returncode == 0, result.stderr.strip()


def apply_saved_modes(preferences):
    for output in query_outputs():
        mode = preferences.data["monitor_modes"].get(output["name"])
        if mode and mode in output["modes"] and mode != output["mode"]:
            ok, message = set_output_mode(output["name"], mode)
            if not ok:
                print(f"Could not set {output['name']} to {mode}: {message}", file=sys.stderr)


class WallpaperPreferences:
    def __init__(self):
        config_home = Path(
            os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
        )
        self.path = config_home / "xalive" / "settings.json"
        legacy_path = config_home / "livewallpaper" / "settings.json"
        if not self.path.exists() and legacy_path.is_file():
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(legacy_path, self.path)
            except OSError as error:
                print(f"Could not migrate old settings: {error}", file=sys.stderr)
        self.data = {
            "icon_size": 48,
            "show_icons": True,
            "language": "auto",
            "icon_positions": {},
            "pause_on_activity": True,
            "pause_exceptions": [],
            "library_folder": "",
            "restore_last": True,
            "last_video": "",
            "last_fit": "cover",
            "last_audio": False,
            "monitor_modes": {},
            "monitor_videos": {},
        }
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as error:
            print(f"Could not load wallpaper preferences: {error}", file=sys.stderr)
            return

        if isinstance(saved, dict):
            size = saved.get("icon_size", 48)
            positions = saved.get("icon_positions", {})
            if isinstance(saved.get("library_folder"), str):
                self.data["library_folder"] = saved["library_folder"]
            exceptions = saved.get("pause_exceptions")
            if isinstance(exceptions, list):
                self.data["pause_exceptions"] = [
                    name for name in exceptions if isinstance(name, str) and name
                ]
            if isinstance(saved.get("last_video"), str):
                self.data["last_video"] = saved["last_video"]
            if saved.get("last_fit") in ("cover", "contain"):
                self.data["last_fit"] = saved["last_fit"]
            if saved.get("language") in ("auto", "es", "en"):
                self.data["language"] = saved["language"]
            for key in ("restore_last", "last_audio", "show_icons"):
                if isinstance(saved.get(key), bool):
                    self.data[key] = saved[key]
            modes = saved.get("monitor_modes")
            if isinstance(modes, dict):
                self.data["monitor_modes"] = {
                    name: mode
                    for name, mode in modes.items()
                    if isinstance(name, str)
                    and isinstance(mode, str)
                    and MODE_PATTERN.fullmatch(mode)
                }
            videos = saved.get("monitor_videos")
            if isinstance(videos, dict):
                self.data["monitor_videos"] = {
                    name: path
                    for name, path in videos.items()
                    if isinstance(name, str) and isinstance(path, str) and path
                }
            if isinstance(saved.get("pause_on_activity"), bool):
                self.data["pause_on_activity"] = saved["pause_on_activity"]
            if isinstance(size, int) and ICON_SIZE_MIN <= size <= ICON_SIZE_MAX:
                self.data["icon_size"] = size
            if isinstance(positions, dict):
                self.data["icon_positions"] = {
                    path: position
                    for path, position in positions.items()
                    if isinstance(path, str)
                    and isinstance(position, list)
                    and len(position) == 2
                    and all(isinstance(value, int) for value in position)
                }

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as error:
            print(f"Could not save wallpaper preferences: {error}", file=sys.stderr)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Set a video as the live wallpaper on an X11 desktop."
    )
    parser.add_argument(
        "video",
        type=Path,
        nargs="?",
        help="video file to play directly instead of opening the graphical interface",
    )
    parser.add_argument(
        "--fit",
        choices=("contain", "cover"),
        default="cover",
        help="fit the video inside each monitor or fill it by cropping (default: cover)",
    )
    parser.add_argument(
        "--audio",
        action="store_true",
        help="enable the video's audio (muted by default)",
    )
    parser.add_argument(
        "--mpv",
        default="mpv",
        help="mpv executable to use (default: mpv)",
    )
    parser.add_argument(
        "--background",
        action="store_true",
        help="start without showing the window (used by session autostart)",
    )
    return parser.parse_args()


AUTOSTART_FILE = (
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    / "autostart"
    / "xalive.desktop"
)
LEGACY_AUTOSTART_FILE = AUTOSTART_FILE.with_name("livewallpaper.desktop")


def desktop_quote(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$") + '"'


def autostart_enabled():
    return AUTOSTART_FILE.is_file()


def set_autostart(enabled):
    LEGACY_AUTOSTART_FILE.unlink(missing_ok=True)
    if not enabled:
        AUTOSTART_FILE.unlink(missing_ok=True)
        return
    script = Path(__file__).resolve()
    command = f"{desktop_quote(sys.executable)} {desktop_quote(str(script))} --background"
    AUTOSTART_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTOSTART_FILE.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Xalive Wallpaper\n"
        "Comment=Animated video wallpaper\n"
        f"Exec={command}\n"
        "Icon=video-display\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
        "X-GNOME-Autostart-Delay=3\n",
        encoding="utf-8",
    )


class XfceIconLayer:
    style_property = "/desktop-icons/style"

    def __init__(
        self,
        Gtk,
        Gdk,
        Gio,
        GLib,
        GdkPixbuf,
        Pango,
        wallpaper_windows,
        on_stop=None,
        icon_size=48,
        icon_positions=None,
        on_positions_changed=None,
    ):
        self.Gtk = Gtk
        self.Gdk = Gdk
        self.Gio = Gio
        self.GLib = GLib
        self.GdkPixbuf = GdkPixbuf
        self.Pango = Pango
        self.icon_provider = self.Gtk.CssProvider()
        self.icon_provider.load_from_data(
            b"window.live-wallpaper-icons {"
            b" background-color: transparent; background-image: none;"
            b"}"
            b"eventbox.live-wallpaper-icon {"
            b" background-color: transparent; background-image: none;"
            b" border: none;"
            b"}"
            b"label.live-wallpaper-icon-label { font-size: 8pt; }"
        )
        self.wallpaper_windows = wallpaper_windows
        self.desktop_path = Path(
            GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DESKTOP)
            or Path.home() / "Desktop"
        )
        self.original_style = None
        self.style_was_missing = False
        self.window = None
        self.fixed = None
        self.paths = []
        self.on_stop = on_stop
        self.icon_size = icon_size
        self.icon_positions = icon_positions if icon_positions is not None else {}
        self.on_positions_changed = on_positions_changed
        self.icon_widgets = {}
        self.screen_width = 0
        self.screen_height = 0
        self.grid_cell_width = self.cell_width_for_size(self.icon_size)
        self.grid_cell_height = self.icon_size + 16
        self.drag = None
        self.refresh_timer_id = None
        self.debounce_id = None
        self.monitor = None

    @staticmethod
    def xfdesktop_is_running():
        proc = Path("/proc")
        try:
            processes = proc.iterdir()
        except OSError:
            return False

        for process in processes:
            if not process.name.isdigit():
                continue
            try:
                if (process / "comm").read_text().strip() == "xfdesktop":
                    return True
            except (FileNotFoundError, PermissionError):
                continue
        return False

    @staticmethod
    def ensure_xfdesktop():
        """Relaunch xfdesktop if it crashed (it can segfault when the screen layout changes)."""
        if XfceIconLayer.xfdesktop_is_running():
            return
        executable = shutil.which("xfdesktop")
        if executable is None:
            return
        try:
            subprocess.Popen(
                [executable],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as error:
            print(f"Could not restart xfdesktop: {error}", file=sys.stderr)

    @classmethod
    def available(cls):
        return shutil.which("xfconf-query") is not None and cls.xfdesktop_is_running()

    @staticmethod
    def find_xfdesktop_window(display, GdkX11):
        wmctrl = shutil.which("wmctrl")
        if wmctrl is None:
            raise RuntimeError(
                "wmctrl is required for XFCE desktop stacking "
                "(on Linux Mint: sudo apt install wmctrl)."
            )

        result = subprocess.run(
            [wmctrl, "-l", "-x"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            message = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"Could not inspect XFCE windows with wmctrl: {message}")

        for line in result.stdout.splitlines():
            fields = line.split(maxsplit=3)
            if len(fields) < 3 or fields[2].casefold() != "xfdesktop.xfdesktop":
                continue
            try:
                xid = int(fields[0], 16)
            except ValueError as error:
                raise RuntimeError("wmctrl returned an invalid XFCE window ID.") from error
            window = GdkX11.X11Window.foreign_new_for_display(display, xid)
            if window is None:
                raise RuntimeError("Could not access XFCE's desktop window.")
            return window

        raise RuntimeError("Could not find XFCE's desktop window for video stacking.")

    def query_style(self):
        result = subprocess.run(
            [
                "xfconf-query",
                "-c",
                "xfce4-desktop",
                "-p",
                self.style_property,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            try:
                return int(result.stdout.strip())
            except ValueError as error:
                raise RuntimeError("XFCE returned an invalid desktop icon style.") from error

        message = (result.stderr + result.stdout).lower()
        if "does not exist" in message or "no existe" in message:
            return None
        raise RuntimeError(f"Could not read XFCE desktop settings: {message.strip()}")

    def set_style(self, value, create=False):
        command = [
            "xfconf-query",
            "-c",
            "xfce4-desktop",
            "-p",
            self.style_property,
        ]
        if create:
            command.extend(["-n", "-t", "int"])
        command.extend(["-s", str(value)])
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            message = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"Could not change XFCE desktop icon style: {message}")

    def hide_native_icons(self):
        self.original_style = self.query_style()
        self.style_was_missing = self.original_style is None
        if self.original_style != 0:
            self.set_style(0, create=self.style_was_missing)

    def restore_native_icons(self):
        try:
            current_style = self.query_style()
        except RuntimeError as error:
            print(f"Could not restore XFCE desktop icons: {error}", file=sys.stderr)
            return
        if current_style != 0:
            return

        if self.original_style is None:
            if not self.style_was_missing:
                return
            result = subprocess.run(
                [
                    "xfconf-query",
                    "-c",
                    "xfce4-desktop",
                    "-p",
                    self.style_property,
                    "-r",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        elif self.original_style != 0:
            result = subprocess.run(
                [
                    "xfconf-query",
                    "-c",
                    "xfce4-desktop",
                    "-p",
                    self.style_property,
                    "-s",
                    str(self.original_style),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        else:
            return

        if result.returncode != 0:
            message = (result.stderr or result.stdout).strip()
            print(f"Could not restore XFCE desktop icons: {message}", file=sys.stderr)

    def desktop_app_for_path(self, path):
        if path.suffix.lower() != ".desktop":
            return None
        try:
            return self.Gio.DesktopAppInfo.new_from_filename(str(path))
        except self.GLib.Error:
            return None

    def icon_for_path(self, path, size, desktop_app=None):
        try:
            icon = desktop_app.get_icon() if desktop_app is not None else None
            if icon is None:
                info = self.Gio.File.new_for_path(str(path)).query_info(
                    "standard::icon",
                    self.Gio.FileQueryInfoFlags.NONE,
                    None,
                )
                icon = info.get_icon()
            if icon is not None:
                icon_info = self.Gtk.IconTheme.get_default().lookup_by_gicon(
                    icon,
                    size,
                    self.Gtk.IconLookupFlags.FORCE_SIZE,
                )
                if icon_info is not None:
                    return icon_info.load_icon()
        except self.GLib.Error:
            pass

        fallback = "folder" if path.is_dir() else "text-x-generic"
        try:
            return self.Gtk.IconTheme.get_default().load_icon(
                fallback,
                size,
                self.Gtk.IconLookupFlags.FORCE_SIZE,
            )
        except self.GLib.Error:
            return None

    def refresh(self):
        try:
            current_paths = sorted(
                (path for path in self.desktop_path.iterdir() if not path.name.startswith(".")),
                key=lambda path: path.name.casefold(),
            )
        except FileNotFoundError:
            current_paths = []
        except OSError as error:
            print(f"Could not read {self.desktop_path}: {error}", file=sys.stderr)
            return self.GLib.SOURCE_CONTINUE

        if current_paths == self.paths and self.icon_widgets:
            return self.GLib.SOURCE_CONTINUE

        self.paths = current_paths
        current_path_strings = {str(path) for path in current_paths}
        saved_positions = {
            path: position
            for path, position in self.icon_positions.items()
            if path in current_path_strings
        }
        self.icon_positions.clear()
        self.icon_positions.update(saved_positions)
        for widget in self.icon_widgets.values():
            self.fixed.remove(widget)
        self.icon_widgets.clear()
        widgets = []
        for path in current_paths:
            desktop_app = self.desktop_app_for_path(path)
            pixbuf = self.icon_for_path(path, self.icon_size, desktop_app)
            label = desktop_app.get_name() if desktop_app is not None else path.name
            widget = self.create_icon_widget(path, label, pixbuf)
            self.icon_widgets[str(path)] = widget
            widgets.append((path, widget))
            self.fixed.put(widget, 0, 0)
            widget.show_all()

        if widgets:
            self.grid_cell_width = max(
                self.cell_width_for_size(self.icon_size),
                *(widget.get_preferred_size()[1].width for _, widget in widgets),
            )
            self.grid_cell_height = max(
                self.icon_size + 16,
                *(widget.get_preferred_size()[1].height for _, widget in widgets),
            )

        for index, (path, widget) in enumerate(widgets):
            x, y = self.position_for_path(path, index)
            self.fixed.move(widget, x, y)
        if self.on_positions_changed is not None:
            self.on_positions_changed()
        self.window.show_all()
        return self.GLib.SOURCE_CONTINUE

    def launch_path(self, path):
        try:
            if path.suffix.lower() == ".desktop":
                desktop_app = self.desktop_app_for_path(path)
                if desktop_app is not None:
                    desktop_app.launch([], None)
                    return
            self.Gio.AppInfo.launch_default_for_uri(path.as_uri(), None)
        except (self.GLib.Error, OSError) as error:
            print(f"Could not open {path}: {error}", file=sys.stderr)

    def show_error_dialog(self, text):
        dialog = self.Gtk.MessageDialog(
            message_type=self.Gtk.MessageType.ERROR,
            buttons=self.Gtk.ButtonsType.CLOSE,
            text=text,
        )
        dialog.set_keep_above(True)
        dialog.run()
        dialog.destroy()

    def rename_path(self, path):
        Gtk = self.Gtk
        dialog = Gtk.Dialog(title="Rename")
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Rename", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        dialog.set_keep_above(True)
        dialog.set_position(Gtk.WindowPosition.CENTER)
        entry = Gtk.Entry(text=path.name)
        entry.set_activates_default(True)
        entry.set_width_chars(36)
        box = dialog.get_content_area()
        box.set_border_width(12)
        box.add(entry)
        stem_end = len(path.stem) if path.is_file() else len(path.name)
        dialog.show_all()
        entry.select_region(0, stem_end)
        response = dialog.run()
        new_name = entry.get_text().strip()
        dialog.destroy()
        if response != Gtk.ResponseType.OK or not new_name or new_name == path.name:
            return
        if "/" in new_name:
            self.show_error_dialog("The name cannot contain '/'.")
            return
        try:
            self.Gio.File.new_for_path(str(path)).set_display_name(new_name, None)
        except self.GLib.Error as error:
            self.show_error_dialog(f"Could not rename: {error.message}")
            return
        self.refresh()

    def trash_path(self, path):
        dialog = self.Gtk.MessageDialog(
            message_type=self.Gtk.MessageType.QUESTION,
            buttons=self.Gtk.ButtonsType.NONE,
            text=f"Move \u201c{path.name}\u201d to the Trash?",
        )
        dialog.add_button("Cancel", self.Gtk.ResponseType.CANCEL)
        dialog.add_button("Move to Trash", self.Gtk.ResponseType.OK)
        dialog.set_keep_above(True)
        response = dialog.run()
        dialog.destroy()
        if response != self.Gtk.ResponseType.OK:
            return
        try:
            self.Gio.File.new_for_path(str(path)).trash(None)
        except self.GLib.Error as error:
            self.show_error_dialog(f"Could not move to the Trash: {error.message}")
            return
        self.refresh()

    def show_context_menu(self, path, event):
        selected = path
        menu = self.Gtk.Menu()
        open_item = self.Gtk.MenuItem(label="Open")
        open_item.connect("activate", lambda *_: self.launch_path(selected))
        menu.append(open_item)
        folder_item = self.Gtk.MenuItem(label="Open containing folder")
        folder_item.connect(
            "activate",
            lambda *_: self.launch_path(selected.parent),
        )
        menu.append(folder_item)
        menu.append(self.Gtk.SeparatorMenuItem())
        rename_item = self.Gtk.MenuItem(label="Rename…")
        rename_item.connect("activate", lambda *_: self.rename_path(selected))
        menu.append(rename_item)
        trash_item = self.Gtk.MenuItem(label="Move to Trash")
        trash_item.connect("activate", lambda *_: self.trash_path(selected))
        menu.append(trash_item)
        menu.append(self.Gtk.SeparatorMenuItem())

        refresh_item = self.Gtk.MenuItem(label="Refresh icons")
        refresh_item.connect("activate", lambda *_: self.refresh())
        menu.append(refresh_item)
        folder_item = self.Gtk.MenuItem(label="Open Desktop folder")
        folder_item.connect(
            "activate",
            lambda *_: self.launch_path(self.desktop_path),
        )
        menu.append(folder_item)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def on_background_button_press(self, _widget, event):
        if event.button != 3 or event.type != self.Gdk.EventType.BUTTON_PRESS:
            return False
        menu = self.Gtk.Menu()

        def add(label, callback):
            item = self.Gtk.MenuItem(label=tr(label))
            item.connect("activate", lambda *_: callback())
            menu.append(item)

        add("Crear carpeta", lambda: self.create_entry(True))
        add("Crear documento vacío", lambda: self.create_entry(False))
        menu.append(self.Gtk.SeparatorMenuItem())
        add("Abrir terminal", self.open_terminal)
        add("Configuración de pantalla", lambda: self.spawn_tool(["xfce4-display-settings"]))
        add("Ajustes del escritorio", lambda: self.spawn_tool(["xfdesktop-settings"]))
        menu.append(self.Gtk.SeparatorMenuItem())
        add("Actualizar iconos", self.refresh)
        add("Abrir carpeta Escritorio", lambda: self.launch_path(self.desktop_path))
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def spawn_tool(self, command, cwd=None):
        try:
            subprocess.Popen(
                command,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as error:
            self.show_error_dialog(f"{command[0]}: {error}")

    def open_terminal(self):
        for terminal in ("xfce4-terminal", "x-terminal-emulator", "gnome-terminal"):
            if shutil.which(terminal):
                self.spawn_tool([terminal], cwd=str(Path.home()))
                return
        self.show_error_dialog(tr("No se encontró un emulador de terminal."))

    def create_entry(self, folder):
        Gtk = self.Gtk
        base = tr("Carpeta nueva") if folder else tr("Documento nuevo")
        dialog = Gtk.Dialog(title=tr("Crear carpeta") if folder else tr("Crear documento vacío"))
        dialog.add_button(tr("Cancelar"), Gtk.ResponseType.CANCEL)
        dialog.add_button(tr("Crear"), Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        dialog.set_keep_above(True)
        dialog.set_position(Gtk.WindowPosition.CENTER)
        entry = Gtk.Entry(text=base if folder else base + ".txt")
        entry.set_activates_default(True)
        entry.set_width_chars(36)
        box = dialog.get_content_area()
        box.set_border_width(12)
        box.add(entry)
        dialog.show_all()
        entry.select_region(0, len(base))
        response = dialog.run()
        name = entry.get_text().strip()
        dialog.destroy()
        if response != Gtk.ResponseType.OK or not name:
            return
        if "/" in name:
            self.show_error_dialog(tr("El nombre no puede contener '/'."))
            return
        target = self.desktop_path / name
        try:
            if folder:
                target.mkdir()
            else:
                target.touch(exist_ok=False)
        except OSError as error:
            self.show_error_dialog(str(error))
            return
        self.refresh()

    def position_for_path(self, path, index):
        saved = self.icon_positions.get(str(path))
        if saved is not None:
            position = self.snap_to_grid(*saved, exclude_path=str(path))
        else:
            columns, _rows = self.grid_dimensions()
            x = self.grid_origin + (index % columns) * self.grid_step_x
            y = self.grid_origin + (index // columns) * self.grid_step_y
            position = self.snap_to_grid(x, y, exclude_path=str(path))
        self.icon_positions[str(path)] = list(position)
        return position

    @property
    def grid_origin(self):
        return 12

    @property
    def grid_step_x(self):
        return self.grid_cell_width + 4

    @property
    def grid_step_y(self):
        return self.grid_cell_height + 4

    def grid_dimensions(self, width=None, height=None):
        width = width or self.grid_cell_width
        height = height or self.grid_cell_height
        columns = max(
            1,
            (self.screen_width - self.grid_origin - width) // self.grid_step_x + 1,
        )
        rows = max(
            1,
            (self.screen_height - self.grid_origin - height) // self.grid_step_y + 1,
        )
        return columns, rows

    def snap_to_grid(self, x, y, width=None, height=None, exclude_path=None):
        width = width or self.icon_size + 48
        height = height or self.icon_size + 64
        columns, rows = self.grid_dimensions(width, height)
        target_column = max(
            0,
            min(
                round((x - self.grid_origin) / self.grid_step_x),
                columns - 1,
            ),
        )
        target_row = max(
            0,
            min(
                round((y - self.grid_origin) / self.grid_step_y),
                rows - 1,
            ),
        )

        occupied = set()
        for path, position in self.icon_positions.items():
            if path == exclude_path:
                continue
            column = round((position[0] - self.grid_origin) / self.grid_step_x)
            row = round((position[1] - self.grid_origin) / self.grid_step_y)
            occupied.add((column, row))

        available = (
            (column, row)
            for row in range(rows)
            for column in range(columns)
            if (column, row) not in occupied
        )
        cell = min(
            available,
            key=lambda candidate: (
                (candidate[0] - target_column) ** 2
                + (candidate[1] - target_row) ** 2
            ),
            default=(target_column, target_row),
        )
        return (
            self.grid_origin + cell[0] * self.grid_step_x,
            self.grid_origin + cell[1] * self.grid_step_y,
        )

    @staticmethod
    def cell_width_for_size(icon_size):
        return max(icon_size + 16, 84)

    def create_icon_widget(self, path, label, pixbuf):
        item = self.Gtk.EventBox()
        item.set_visible_window(True)
        item.get_style_context().add_class("live-wallpaper-icon")
        item.get_style_context().add_provider(
            self.icon_provider,
            self.Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        item.add_events(
            self.Gdk.EventMask.BUTTON_PRESS_MASK
            | self.Gdk.EventMask.BUTTON_RELEASE_MASK
            | self.Gdk.EventMask.POINTER_MOTION_MASK
        )
        item.connect("button-press-event", self.on_icon_button_press, path)
        item.connect("button-release-event", self.on_icon_button_release, path)
        item.connect("motion-notify-event", self.on_icon_motion, path)

        content = self.Gtk.Box(
            orientation=self.Gtk.Orientation.VERTICAL,
            spacing=2,
        )
        content.set_border_width(2)
        image = self.Gtk.Image.new_from_pixbuf(pixbuf) if pixbuf else self.Gtk.Image()
        image.set_halign(self.Gtk.Align.CENTER)
        content.pack_start(image, False, False, 0)
        text = self.Gtk.Label(label=label)
        cell_width = self.cell_width_for_size(self.icon_size)
        text.get_style_context().add_class("live-wallpaper-icon-label")
        text.set_size_request(cell_width - 8, -1)
        text.set_max_width_chars(max(4, cell_width // 7))
        text.set_line_wrap(True)
        text.set_line_wrap_mode(self.Pango.WrapMode.WORD_CHAR)
        text.set_lines(-1)
        text.set_justify(self.Gtk.Justification.CENTER)
        text.set_tooltip_text(label)
        content.pack_start(text, False, False, 0)
        item.add(content)
        item.set_size_request(cell_width, -1)
        return item

    def on_icon_button_press(self, widget, event, path):
        if event.button == 3:
            return self.show_context_menu(path, event)
        if event.button == 1:
            if event.type == self.Gdk.EventType.DOUBLE_BUTTON_PRESS:
                self.launch_path(path)
                return True
            allocation = widget.get_allocation()
            x = self.fixed.child_get_property(widget, "x")
            y = self.fixed.child_get_property(widget, "y")
            self.drag = {
                "widget": widget,
                "path": str(path),
                "offset_x": int(event.x),
                "offset_y": int(event.y),
                "x": x,
                "y": y,
                "moved": False,
                "width": allocation.width,
                "height": allocation.height,
            }
        return False

    def on_icon_motion(self, _widget, event, _path):
        if self.drag is None or not event.state & self.Gdk.ModifierType.BUTTON1_MASK:
            return False
        x, y = self.snap_to_grid(
            event.x_root - self.drag["offset_x"],
            event.y_root - self.drag["offset_y"],
            self.drag["width"],
            self.drag["height"],
            exclude_path=self.drag["path"],
        )
        self.fixed.move(self.drag["widget"], x, y)
        self.drag["moved"] = True
        return True

    def on_icon_button_release(self, _widget, _event, _path):
        if self.drag is None:
            return False
        if self.drag["moved"]:
            x = self.fixed.child_get_property(self.drag["widget"], "x")
            y = self.fixed.child_get_property(self.drag["widget"], "y")
            self.icon_positions[self.drag["path"]] = [int(x), int(y)]
            if self.on_positions_changed is not None:
                self.on_positions_changed()
        self.drag = None
        return False

    def set_icon_size(self, size):
        self.icon_size = max(ICON_SIZE_MIN, min(int(size), ICON_SIZE_MAX))
        self.grid_cell_width = self.cell_width_for_size(self.icon_size)
        self.grid_cell_height = self.icon_size + 16
        for widget in self.icon_widgets.values():
            self.fixed.remove(widget)
        self.icon_widgets.clear()
        self.paths = []
        self.refresh()

    def create_window(self):
        screen = self.Gdk.Screen.get_default()
        visual = screen.get_rgba_visual()
        if visual is None or not screen.is_composited():
            raise RuntimeError(
                "XFCE transparency is unavailable; enable the desktop compositor "
                "to show icons above the live wallpaper."
            )

        self.window = self.Gtk.Window(type=self.Gtk.WindowType.TOPLEVEL)
        self.window.set_title("Xalive Desktop Icons")
        self.window.set_visual(visual)
        self.window.set_app_paintable(True)
        self.window.set_decorated(False)
        self.window.set_type_hint(self.Gdk.WindowTypeHint.NORMAL)
        self.window.stick()
        self.window.set_skip_taskbar_hint(True)
        self.window.set_skip_pager_hint(True)
        self.window.set_accept_focus(False)
        self.window.set_focus_on_map(False)
        self.window.set_keep_below(True)
        self.screen_width = screen.get_width()
        self.screen_height = screen.get_height()
        self.window.set_default_size(self.screen_width, self.screen_height)
        self.window.move(0, 0)
        self.window.connect("delete-event", self.on_delete)
        self.window.add_events(self.Gdk.EventMask.BUTTON_PRESS_MASK)
        self.window.connect("button-press-event", self.on_background_button_press)

        self.window.get_style_context().add_class("live-wallpaper-icons")
        self.window.get_style_context().add_provider(
            self.icon_provider,
            self.Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        self.fixed = self.Gtk.Fixed()
        self.window.add(self.fixed)
        self.refresh()
        self.window.show_all()
        self.window.get_window().restack(
            self.wallpaper_windows[-1].get_window(),
            True,
        )
        self.start_watching()

    def start_watching(self):
        # inotify wakes us only when the Desktop folder changes; the slow timer is a safety net.
        try:
            self.monitor = self.Gio.File.new_for_path(str(self.desktop_path)).monitor_directory(
                self.Gio.FileMonitorFlags.NONE, None
            )
            self.monitor.connect("changed", self.on_desktop_changed)
        except self.GLib.Error as error:
            print(f"Could not watch {self.desktop_path}: {error.message}", file=sys.stderr)
            self.monitor = None
        interval = 30 if self.monitor is not None else 2
        self.refresh_timer_id = self.GLib.timeout_add_seconds(interval, self.refresh)

    def on_desktop_changed(self, *_args):
        if self.debounce_id is None:
            self.debounce_id = self.GLib.timeout_add(250, self.refresh_debounced)

    def refresh_debounced(self):
        self.debounce_id = None
        self.refresh()
        return False

    def stop_refresh(self):
        for name in ("refresh_timer_id", "debounce_id"):
            source = getattr(self, name)
            if source is not None:
                self.GLib.source_remove(source)
                setattr(self, name, None)
        if self.monitor is not None:
            self.monitor.cancel()
            self.monitor = None

    def on_delete(self, *_args):
        if self.on_stop is not None:
            self.on_stop()
        else:
            self.stop_refresh()
            self.Gtk.main_quit()
        return True

class WallpaperController:
    def __init__(self, Gtk, Gdk, GdkX11, Gio, GLib, GdkPixbuf, Pango, display, mpv):
        self.Pango = Pango
        self.Gtk = Gtk
        self.Gdk = Gdk
        self.GdkX11 = GdkX11
        self.Gio = Gio
        self.GLib = GLib
        self.GdkPixbuf = GdkPixbuf
        self.display = display
        self.mpv = mpv
        self.windows = []
        self.players = []
        self.icon_layer = None
        self.timer_id = None
        self.activity_timer_id = None
        self.pause_on_activity = True
        self.paused = False
        self.sockets = []
        self.socket_dir = None
        self.exceptions = []
        self.ignored_windows = set()
        self.active_video = None
        self.icon_size = 48
        self.on_stopped = None
        self.on_error = None
        self.on_layout_changed = None
        self.last_start = None
        self.layout = None
        self.layout_timer_id = None
        self.restarting = False
        self.restart_timer_id = None
        screen = Gdk.Screen.get_default()
        screen.connect("size-changed", self.on_screen_changed)
        screen.connect("monitors-changed", self.on_screen_changed)

    def layout_signature(self):
        monitors = []
        for index in range(self.display.get_n_monitors()):
            geometry = self.display.get_monitor(index).get_geometry()
            monitors.append((geometry.x, geometry.y, geometry.width, geometry.height))
        return tuple(sorted(set(monitors)))

    def on_screen_changed(self, *_args):
        if self.layout_timer_id is not None:
            self.GLib.source_remove(self.layout_timer_id)
        # xrandr emits several signals per change; wait for them to settle.
        self.layout_timer_id = self.GLib.timeout_add(800, self.apply_layout_change)

    def apply_layout_change(self):
        self.layout_timer_id = None
        if self.on_layout_changed is not None:
            self.on_layout_changed()
        if self.running and self.last_start is not None:
            if self.layout_signature() != self.layout:
                self.restart()
        return self.GLib.SOURCE_REMOVE

    def restart(self, attempts=12):
        self.restarting = True
        self.stop()
        self.try_restart(attempts)

    def try_restart(self, attempts):
        video, fit, audio, preferences = self.last_start
        try:
            self.start(video, fit, audio, self.icon_size, preferences)
        except (OSError, RuntimeError) as error:
            # xfdesktop rebuilds its window a moment after a resolution change.
            XfceIconLayer.ensure_xfdesktop()
            if attempts > 1:
                self.restart_timer_id = self.GLib.timeout_add_seconds(
                    1, self.retry_restart, attempts - 1
                )
                return
            self.restarting = False
            message = tr("No se pudo readaptar el fondo a la nueva resolución: {error}", error=error)
            print(message, file=sys.stderr)
            if self.on_error is not None:
                self.on_error(message)
            return
        self.restarting = False

    def retry_restart(self, attempts):
        self.restart_timer_id = None
        self.try_restart(attempts)
        return self.GLib.SOURCE_REMOVE

    @property
    def running(self):
        return bool(self.players)

    def start(self, video, fit, audio, icon_size=48, preferences=None):
        if self.restart_timer_id is not None:
            # A manual start supersedes a pending automatic restart.
            self.GLib.source_remove(self.restart_timer_id)
            self.restart_timer_id = None
            self.restarting = False
        video = video.expanduser().resolve()
        if not video.is_file():
            raise RuntimeError(tr("No se encontró el video: {video}", video=video))
        if self.running:
            self.stop()
        self.icon_size = icon_size
        if preferences is None:
            preferences = WallpaperPreferences()

        self.socket_dir = Path(tempfile.mkdtemp(prefix="xalive-"))
        self.pause_on_activity = preferences.data["pause_on_activity"]
        self.exceptions = preferences.data["pause_exceptions"]
        try:
            # Mirrored monitors share one area, so they share one player.
            areas = {}
            for monitor_index in range(self.display.get_n_monitors()):
                monitor = self.display.get_monitor(monitor_index)
                geometry = monitor.get_geometry()
                area = (geometry.x, geometry.y, geometry.width, geometry.height)
                areas.setdefault(area, []).append(monitor.get_model())
            monitor_videos = preferences.data["monitor_videos"]
            for area, names in areas.items():
                geometry = self.Gdk.Rectangle()
                geometry.x, geometry.y, geometry.width, geometry.height = area
                area_video = video
                for name in names:
                    custom = Path(monitor_videos.get(name, "")).expanduser()
                    if name in monitor_videos and custom.is_file():
                        area_video = custom.resolve()
                        break
                index = len(self.windows)
                window = self.Gtk.Window(type=self.Gtk.WindowType.TOPLEVEL)
                window.set_title("Xalive Wallpaper Video")
                window.set_decorated(False)
                window.set_app_paintable(True)
                window.set_type_hint(self.Gdk.WindowTypeHint.NORMAL)
                window.stick()
                window.set_skip_taskbar_hint(True)
                window.set_skip_pager_hint(True)
                window.set_accept_focus(False)
                window.set_focus_on_map(False)
                window.set_keep_below(True)
                window.set_default_size(geometry.width, geometry.height)
                window.move(geometry.x, geometry.y)
                window.connect("delete-event", self.on_wallpaper_delete)
                window.show_all()
                window.get_window().raise_()
                self.windows.append(window)

                xwindow = window.get_window()
                if not isinstance(xwindow, self.GdkX11.X11Window):
                    raise RuntimeError("GTK did not create an X11 window.")

                command = [
                    self.mpv,
                    "--no-terminal",
                    "--force-window=yes",
                    "--no-osc",
                    # Hardware decoding is the difference between ~15% and >300% CPU on 4K;
                    # "fast" trades scaler quality for lower GPU load.
                    "--hwdec=auto-safe",
                    "--profile=fast",
                    "--demuxer-max-bytes=16MiB",
                    "--demuxer-max-back-bytes=0",
                    "--load-scripts=no",
                    "--ytdl=no",
                    "--sub-auto=no",
                    "--loop-file=inf",
                    "--no-input-default-bindings",
                    "--no-input-cursor",
                    "--input-ipc-server=" + str(self.socket_path(index)),
                    "--wid=" + str(xwindow.get_xid()),
                ]
                # Only the first player makes sound so videos don't talk over each other.
                if not audio or index > 0:
                    command.append("--no-audio")
                if fit == "cover":
                    command.append("--panscan=1.0")
                command.append(str(area_video))
                self.players.append(subprocess.Popen(command))

            if not self.windows:
                raise RuntimeError(tr("No se encontraron monitores."))

            if preferences.data["show_icons"] and XfceIconLayer.available():
                desktop_window = XfceIconLayer.find_xfdesktop_window(
                    self.display,
                    self.GdkX11,
                )
                self.icon_layer = XfceIconLayer(
                    self.Gtk,
                    self.Gdk,
                    self.Gio,
                    self.GLib,
                    self.GdkPixbuf,
                    self.Pango,
                    self.windows,
                    on_stop=self.stop,
                    icon_size=self.icon_size,
                    icon_positions=preferences.data["icon_positions"],
                    on_positions_changed=preferences.save,
                )
                self.icon_layer.hide_native_icons()
                for window in self.windows:
                    window.get_window().restack(desktop_window, True)
                self.icon_layer.create_window()
            else:
                for window in self.windows:
                    window.get_window().raise_()

            self.active_video = video
            self.last_start = (video, fit, audio, preferences)
            self.layout = self.layout_signature()
            self.timer_id = self.GLib.timeout_add_seconds(2, self.check_players)
            self.activity_timer_id = self.GLib.timeout_add_seconds(1, self.check_activity)
        except (OSError, RuntimeError):
            self.stop()
            raise

    def socket_path(self, index):
        return self.socket_dir / f"mpv-{index}.sock"

    def set_paused(self, paused):
        if paused == self.paused:
            return
        self.paused = paused
        payload = (
            json.dumps({"command": ["set_property", "pause", paused]}) + "\n"
        ).encode()
        for index in range(len(self.players)):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(0.5)
            try:
                client.connect(str(self.socket_path(index)))
                client.sendall(payload)
            except OSError as error:
                print(f"Could not control mpv: {error}", file=sys.stderr)
            finally:
                client.close()

    def set_pause_on_activity(self, enabled):
        self.pause_on_activity = enabled
        if not enabled:
            self.set_paused(False)
        else:
            self.check_activity()

    @staticmethod
    def xprop(*args):
        try:
            result = subprocess.run(
                ["xprop", *args], capture_output=True, text=True, timeout=2
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return result.stdout if result.returncode == 0 else ""

    def window_blocks_wallpaper(self, xid, desktop):
        # A visible fullscreen or maximized window of a non-excepted application blocks the video.
        if xid in self.ignored_windows:
            return False
        output = self.xprop(
            "-id",
            xid,
            "_NET_WM_STATE",
            "_NET_WM_DESKTOP",
            "_NET_WM_PID",
            "WM_CLASS",
            "_NET_WM_WINDOW_TYPE",
        )
        if not output:
            return False
        # Panels, the desktop and our own windows can never cover the video: skip them for good.
        match = re.search(r"_NET_WM_PID\(CARDINAL\) = (\d+)", output)
        if (
            (match and int(match.group(1)) == os.getpid())
            or "_NET_WM_WINDOW_TYPE_DOCK" in output
            or "_NET_WM_WINDOW_TYPE_DESKTOP" in output
        ):
            self.ignored_windows.add(xid)
            return False
        covers_screen = "_NET_WM_STATE_FULLSCREEN" in output or (
            "_NET_WM_STATE_MAXIMIZED_HORZ" in output
            and "_NET_WM_STATE_MAXIMIZED_VERT" in output
        )
        if not covers_screen or "_NET_WM_STATE_HIDDEN" in output:
            return False
        match = re.search(r"_NET_WM_DESKTOP\(CARDINAL\) = (\d+)", output)
        if match and desktop is not None and int(match.group(1)) not in (desktop, 0xFFFFFFFF):
            return False
        class_line = next(
            (line for line in output.splitlines() if line.startswith("WM_CLASS")), ""
        )
        classes = {name.casefold() for name in re.findall(r'"([^"]*)"', class_line)}
        excepted = {name.casefold() for name in self.exceptions}
        return not (classes & excepted)

    def check_activity(self):
        if not self.running:
            return self.GLib.SOURCE_REMOVE
        if not self.pause_on_activity:
            return self.GLib.SOURCE_CONTINUE
        root = self.xprop("-root", "_NET_CLIENT_LIST_STACKING", "_NET_CURRENT_DESKTOP")
        stacking = next(
            (line for line in root.splitlines() if line.startswith("_NET_CLIENT_LIST_STACKING")),
            "",
        )
        match = re.search(r"_NET_CURRENT_DESKTOP\(CARDINAL\) = (\d+)", root)
        desktop = int(match.group(1)) if match else None
        windows = re.findall(r"0x[0-9a-fA-F]+", stacking)
        self.ignored_windows.intersection_update(windows)
        blocked = False
        # Inspect only the few topmost windows to keep this check cheap.
        for xid in reversed(windows[-6:]):
            if self.window_blocks_wallpaper(xid, desktop):
                blocked = True
                break
        self.set_paused(blocked)
        return self.GLib.SOURCE_CONTINUE

    def on_wallpaper_delete(self, *_args):
        self.stop()
        return True

    def set_icon_size(self, size):
        self.icon_size = max(ICON_SIZE_MIN, min(int(size), ICON_SIZE_MAX))
        if self.icon_layer is not None:
            self.icon_layer.set_icon_size(self.icon_size)

    def check_players(self):
        for player in self.players:
            returncode = player.poll()
            if returncode is not None:
                self.timer_id = None
                message = tr("mpv terminó inesperadamente (estado {code}).", code=returncode)
                print(message, file=sys.stderr)
                self.stop()
                if self.on_error is not None:
                    self.on_error(message)
                return self.GLib.SOURCE_REMOVE
        self.watch_desktop()
        return self.GLib.SOURCE_CONTINUE

    def watch_desktop(self):
        if self.restarting or self.last_start is None or shutil.which("xfconf-query") is None:
            return
        if not self.last_start[3].data["show_icons"]:
            return
        alive = XfceIconLayer.xfdesktop_is_running()
        if not alive:
            XfceIconLayer.ensure_xfdesktop()
            return
        if self.icon_layer is None:
            # xfdesktop came back (or started late): rebuild so the icon layer appears.
            self.restart()

    def stop(self):
        if self.restart_timer_id is not None and not self.restarting:
            self.GLib.source_remove(self.restart_timer_id)
            self.restart_timer_id = None
        if self.timer_id is not None:
            self.GLib.source_remove(self.timer_id)
            self.timer_id = None
        if self.activity_timer_id is not None:
            self.GLib.source_remove(self.activity_timer_id)
            self.activity_timer_id = None
        self.paused = False

        was_running = self.running or self.icon_layer is not None
        for player in self.players:
            if player.poll() is None:
                player.terminate()
        for player in self.players:
            try:
                player.wait(timeout=3)
            except subprocess.TimeoutExpired:
                player.kill()
                player.wait()
        self.players.clear()
        if self.socket_dir is not None:
            shutil.rmtree(self.socket_dir, ignore_errors=True)
            self.socket_dir = None

        for window in self.windows:
            window.destroy()
        self.windows.clear()
        if self.icon_layer is not None:
            self.icon_layer.stop_refresh()
            if self.icon_layer.window is not None:
                self.icon_layer.window.destroy()
            self.icon_layer.restore_native_icons()
            self.icon_layer = None
            if not self.restarting:
                XfceIconLayer.ensure_xfdesktop()
        self.active_video = None

        if was_running and self.on_stopped is not None and not self.restarting:
            self.on_stopped()


class WallpaperApp:
    def __init__(self, Gtk, Pango, controller, preferences, initial_video=None, background=False):
        self.GLib = controller.GLib
        self.Gio = controller.Gio
        self.GdkPixbuf = controller.GdkPixbuf
        self.Gtk = Gtk
        self.Pango = Pango
        self.controller = controller
        self.preferences = preferences
        self.quiet_start = False
        self.video = initial_video
        if self.video is None and preferences.data["last_video"]:
            last = Path(preferences.data["last_video"])
            self.video = last if last.is_file() else None
        self.icon_size = preferences.data["icon_size"]
        self.window = Gtk.Window(title="Xalive Wallpaper")
        self.window.set_default_size(680, 600)
        self.window.set_border_width(0)
        self.window.set_position(Gtk.WindowPosition.CENTER)
        self.window.connect("delete-event", self.on_close)
        self.controller.on_stopped = self.on_stopped
        self.controller.on_error = self.show_error

        header = Gtk.HeaderBar()
        header.set_title("Xalive Wallpaper")
        header.set_subtitle(tr("Fondos animados para tu escritorio"))
        header.set_show_close_button(True)
        self.window.set_titlebar(header)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        root.set_border_width(24)
        self.window.add(root)

        title = Gtk.Label()
        title.set_markup(f"<big><b>{tr('Tu escritorio, en movimiento')}</b></big>")
        title.set_xalign(0)
        root.pack_start(title, False, False, 0)

        description = Gtk.Label(
            label=tr("Elige un video y ajusta cómo se verá en tus pantallas.")
        )
        description.set_xalign(0)
        description.set_line_wrap(True)
        root.pack_start(description, False, False, 0)

        file_frame = Gtk.Frame(label=tr(" Video "))
        file_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        file_box.set_border_width(14)
        file_frame.add(file_box)
        self.file_label = Gtk.Label()
        self.file_label.set_xalign(0)
        self.file_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.file_label.set_hexpand(True)
        self.file_label.set_tooltip_text(tr("Selecciona un archivo de video"))
        file_box.pack_start(self.file_label, True, True, 0)
        choose_button = Gtk.Button(label=tr("Elegir video…"))
        choose_button.connect("clicked", self.choose_video)
        file_box.pack_start(choose_button, False, False, 0)
        root.pack_start(file_frame, False, False, 0)

        self.notebook = Gtk.Notebook()
        root.pack_start(self.notebook, True, True, 0)
        settings_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        settings_box.set_border_width(12)
        settings_scroll = Gtk.ScrolledWindow()
        settings_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        settings_scroll.add(settings_box)
        self.build_library_tab()
        self.notebook.append_page(settings_scroll, Gtk.Label(label=tr("Ajustes")))
        self.build_browser_tab()

        options_frame = Gtk.Frame(label=tr(" Opciones "))
        options = Gtk.Grid(column_spacing=18, row_spacing=14)
        options.set_border_width(14)
        options_frame.add(options)
        fit_label = Gtk.Label(label=tr("Ajuste a pantalla"))
        fit_label.set_xalign(0)
        options.attach(fit_label, 0, 0, 1, 1)
        self.fit_combo = Gtk.ComboBoxText()
        self.fit_combo.append("cover", tr("Cubrir (recorta los bordes)"))
        self.fit_combo.append("contain", tr("Contener (puede dejar márgenes)"))
        self.fit_combo.set_active_id(preferences.data["last_fit"])
        options.attach(self.fit_combo, 1, 0, 1, 1)
        self.audio_switch = Gtk.Switch()
        self.audio_switch.set_halign(Gtk.Align.START)
        self.audio_switch.set_active(preferences.data["last_audio"])
        options.attach(Gtk.Label(label=tr("Reproducir audio")), 0, 1, 1, 1)
        options.attach(self.audio_switch, 1, 1, 1, 1)
        self.pause_switch = Gtk.Switch()
        self.pause_switch.set_halign(Gtk.Align.START)
        self.pause_switch.set_active(preferences.data["pause_on_activity"])
        self.pause_switch.connect("notify::active", self.on_pause_switch_changed)
        pause_label = Gtk.Label(label=tr("Pausar con ventanas maximizadas o a pantalla completa"))
        pause_label.set_xalign(0)
        options.attach(pause_label, 0, 2, 1, 1)
        options.attach(self.pause_switch, 1, 2, 1, 1)
        self.icons_switch = Gtk.Switch()
        self.icons_switch.set_halign(Gtk.Align.START)
        self.icons_switch.set_active(preferences.data["show_icons"])
        self.icons_switch.connect("notify::active", self.on_icons_switch_changed)
        options.attach(Gtk.Label(label=tr("Mostrar iconos del escritorio"), xalign=0), 0, 3, 1, 1)
        options.attach(self.icons_switch, 1, 3, 1, 1)
        options.attach(Gtk.Label(label=tr("Tamaño de los iconos")), 0, 4, 1, 1)
        size_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.icon_size_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL,
            ICON_SIZE_MIN,
            ICON_SIZE_MAX,
            8,
        )
        self.icon_size_scale.set_digits(0)
        self.icon_size_scale.set_hexpand(True)
        self.icon_size_scale.set_value(self.icon_size)
        self.icon_size_scale.connect("value-changed", self.on_icon_size_changed)
        size_box.pack_start(self.icon_size_scale, True, True, 0)
        range_labels = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        range_labels.pack_start(
            Gtk.Label(label=tr("Pequeño ({size} px)", size=ICON_SIZE_MIN)),
            False,
            False,
            0,
        )
        range_labels.pack_end(
            Gtk.Label(label=tr("Grande ({size} px)", size=ICON_SIZE_MAX)),
            False,
            False,
            0,
        )
        size_box.pack_start(range_labels, False, False, 0)
        options.attach(size_box, 1, 4, 1, 1)
        size_box.set_sensitive(preferences.data["show_icons"])
        self.icon_size_box = size_box
        self.restore_switch = Gtk.Switch()
        self.restore_switch.set_halign(Gtk.Align.START)
        self.restore_switch.set_active(preferences.data["restore_last"])
        self.restore_switch.connect("notify::active", self.on_restore_switch_changed)
        options.attach(Gtk.Label(label=tr("Recordar el último fondo al abrir"), xalign=0), 0, 5, 1, 1)
        options.attach(self.restore_switch, 1, 5, 1, 1)
        self.autostart_switch = Gtk.Switch()
        self.autostart_switch.set_halign(Gtk.Align.START)
        self.autostart_switch.set_active(autostart_enabled())
        self.autostart_switch.connect("notify::active", self.on_autostart_switch_changed)
        options.attach(Gtk.Label(label=tr("Iniciar con la sesión"), xalign=0), 0, 6, 1, 1)
        options.attach(self.autostart_switch, 1, 6, 1, 1)
        self.language_combo = Gtk.ComboBoxText()
        self.language_combo.append("auto", tr("Automático"))
        self.language_combo.append("es", tr("Español"))
        self.language_combo.append("en", tr("Inglés"))
        self.language_combo.set_active_id(preferences.data["language"])
        self.language_combo.set_tooltip_text(tr("El idioma cambia al reiniciar Xalive Wallpaper."))
        self.language_combo.connect("changed", self.on_language_changed)
        options.attach(Gtk.Label(label=tr("Idioma"), xalign=0), 0, 7, 1, 1)
        options.attach(self.language_combo, 1, 7, 1, 1)
        settings_box.pack_start(options_frame, False, False, 0)

        monitors_frame = Gtk.Frame(label=tr(" Monitores "))
        self.monitors_grid = Gtk.Grid(column_spacing=18, row_spacing=10)
        self.monitors_grid.set_border_width(14)
        monitors_frame.add(self.monitors_grid)
        settings_box.pack_start(monitors_frame, False, False, 0)
        self.controller.on_layout_changed = self.reload_monitors
        self.reload_monitors()

        exceptions_frame = Gtk.Frame(label=tr(" Excepciones (nunca pausan el fondo) "))
        exceptions_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        exceptions_box.set_border_width(14)
        exceptions_frame.add(exceptions_box)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_min_content_height(80)
        scrolled.set_shadow_type(Gtk.ShadowType.IN)
        self.exceptions_list = Gtk.ListBox()
        scrolled.add(self.exceptions_list)
        exceptions_box.pack_start(scrolled, True, True, 0)
        add_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.exception_combo = Gtk.ComboBoxText.new_with_entry()
        self.exception_combo.set_hexpand(True)
        self.exception_combo.set_tooltip_text(
            tr("Clase de la ventana (WM_CLASS), p. ej. mpv o vlc")
        )
        add_row.pack_start(self.exception_combo, True, True, 0)
        add_button = Gtk.Button(label=tr("Añadir"))
        add_button.connect("clicked", self.add_exception)
        add_row.pack_start(add_button, False, False, 0)
        remove_button = Gtk.Button(label=tr("Quitar"))
        remove_button.connect("clicked", self.remove_exception)
        add_row.pack_start(remove_button, False, False, 0)
        exceptions_box.pack_start(add_row, False, False, 0)
        settings_box.pack_start(exceptions_frame, False, False, 0)
        self.reload_exceptions()

        self.status = Gtk.Label()
        self.status.set_xalign(0)
        self.status.set_line_wrap(True)
        self.status.set_text(
            tr("Arrastra los iconos para moverlos por la cuadrícula; "
            "tamaño y posiciones se guardan.")
        )
        root.pack_start(self.status, True, True, 0)

        actions = Gtk.ButtonBox(orientation=Gtk.Orientation.HORIZONTAL)
        actions.set_layout(Gtk.ButtonBoxStyle.END)
        actions.set_spacing(10)
        self.stop_button = Gtk.Button(label=tr("Detener fondo"))
        self.stop_button.connect("clicked", self.stop_wallpaper)
        actions.add(self.stop_button)
        self.apply_button = Gtk.Button(label=tr("Aplicar fondo"))
        self.apply_button.get_style_context().add_class("suggested-action")
        self.apply_button.connect("clicked", self.apply_wallpaper)
        actions.add(self.apply_button)
        root.pack_end(actions, False, False, 0)

        self.update_selected_video()
        self.update_running_state()
        if not background:
            self.window.show_all()

    def build_browser_tab(self):
        Gtk = self.Gtk
        self.webview = None
        self.web_context = None
        self.web_filter_manager = None
        self.web_modules = None
        self.web_error = None
        self.guard = None
        self.helpers = {}
        self.gallery_loaded = False
        self.gallery_generation = 0
        self.gallery_page = 1
        self.gallery_seen = set()
        self.thumb_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)

        self.browser_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.browser_box.set_border_width(8)
        self.stack = Gtk.Stack()
        switcher = Gtk.StackSwitcher()
        switcher.set_stack(self.stack)
        switcher.set_halign(Gtk.Align.CENTER)
        self.browser_box.pack_start(switcher, False, False, 0)
        self.browser_box.pack_start(self.stack, True, True, 0)

        self.build_gallery_page()
        self.build_web_page()

        self.browser_hint = Gtk.Label(
            label=tr("Las descargas se guardan sin modificar en la carpeta de la biblioteca.")
        )
        self.browser_hint.set_xalign(0)
        self.browser_hint.set_line_wrap(True)
        self.browser_box.pack_start(self.browser_hint, False, False, 0)
        self.downloads_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.browser_box.pack_start(self.downloads_box, False, False, 0)

        self.stack.connect("notify::visible-child", self.on_stack_page)
        self.notebook.insert_page(self.browser_box, Gtk.Label(label=tr("Descargar")), 1)
        self.notebook.connect("switch-page", self.on_notebook_page)

    def build_gallery_page(self):
        Gtk = self.Gtk
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.source_combo = Gtk.ComboBoxText()
        for source in sources.SOURCES:
            self.source_combo.append_text(source.name)
        self.source_combo.set_active(0)
        self.source_combo.connect("changed", lambda *_: self.reload_gallery())
        bar.pack_start(self.source_combo, False, False, 0)
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text(tr("Buscar wallpapers…"))
        self.search_entry.connect("activate", lambda *_: self.reload_gallery())
        bar.pack_start(self.search_entry, True, True, 0)
        search_button = Gtk.Button(label=tr("Buscar"))
        search_button.connect("clicked", lambda *_: self.reload_gallery())
        bar.pack_start(search_button, False, False, 0)
        page.pack_start(bar, False, False, 0)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(190)
        self.gallery_flow = Gtk.FlowBox()
        self.gallery_flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self.gallery_flow.set_homogeneous(True)
        self.gallery_flow.set_min_children_per_line(2)
        self.gallery_flow.set_max_children_per_line(4)
        self.gallery_flow.set_row_spacing(10)
        self.gallery_flow.set_column_spacing(10)
        gallery_inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        gallery_inner.pack_start(self.gallery_flow, False, False, 0)
        self.more_button = Gtk.Button(label=tr("Cargar más"))
        self.more_button.connect("clicked", lambda *_: self.load_gallery_page())
        gallery_inner.pack_start(self.more_button, False, False, 0)
        scroll.add(gallery_inner)
        page.pack_start(scroll, True, True, 0)
        self.gallery_status = Gtk.Label()
        self.gallery_status.set_xalign(0)
        self.gallery_status.set_line_wrap(True)
        page.pack_start(self.gallery_status, False, False, 0)
        self.stack.add_titled(page, "gallery", tr("Explorar"))

    def build_web_page(self):
        Gtk = self.Gtk
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.back_button = Gtk.Button.new_from_icon_name("go-previous-symbolic", Gtk.IconSize.BUTTON)
        self.forward_button = Gtk.Button.new_from_icon_name("go-next-symbolic", Gtk.IconSize.BUTTON)
        self.reload_button = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
        self.back_button.connect("clicked", lambda *_: self.webview and self.webview.go_back())
        self.forward_button.connect("clicked", lambda *_: self.webview and self.webview.go_forward())
        self.reload_button.connect("clicked", lambda *_: self.webview and self.webview.reload())
        for button in (self.back_button, self.forward_button, self.reload_button):
            bar.pack_start(button, False, False, 0)
        self.sites_combo = Gtk.ComboBoxText()
        self.sites_combo.append_text(tr("Sitios…"))
        for name, _url in BROWSER_SITES:
            self.sites_combo.append_text(name)
        self.sites_combo.set_active(0)
        self.sites_combo.connect("changed", self.on_site_selected)
        bar.pack_start(self.sites_combo, False, False, 0)
        self.url_entry = Gtk.Entry()
        self.url_entry.set_placeholder_text(tr("Dirección o texto a buscar"))
        self.url_entry.connect("activate", self.on_url_activate)
        bar.pack_start(self.url_entry, True, True, 0)
        page.pack_start(bar, False, False, 0)
        self.web_holder = Gtk.Box()
        self.web_holder.set_size_request(-1, 190)
        page.pack_start(self.web_holder, True, True, 0)
        self.stack.add_titled(page, "browser", tr("Navegador"))

    def on_notebook_page(self, _notebook, page, _number):
        if page is self.browser_box and not self.gallery_loaded:
            self.gallery_loaded = True
            self.reload_gallery()

    def on_stack_page(self, stack, _param):
        if stack.get_visible_child_name() == "browser" and self.webview is None:
            self.create_webview()

    # --- shared WebKit state -------------------------------------------------

    def ensure_web(self):
        if self.web_context is not None:
            return True
        if self.web_error:
            return False
        try:
            import gi

            gi.require_version("WebKit2", "4.1")
            from gi.repository import WebKit2
        except (ImportError, ValueError):
            self.web_error = tr("Falta WebKitGTK: sudo apt install gir1.2-webkit2-4.1")
            self.browser_hint.set_text(self.web_error)
            return False
        self.web_modules = WebKit2
        data_dir = Path(
            os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
        ) / "xalive" / "web"
        manager = WebKit2.WebsiteDataManager(
            base_data_directory=str(data_dir),
            base_cache_directory=str(THUMB_CACHE.parent / "web"),
        )
        self.web_context = WebKit2.WebContext.new_with_website_data_manager(manager)
        self.web_context.set_cache_model(WebKit2.CacheModel.DOCUMENT_VIEWER)
        self.web_context.connect("download-started", self.on_download_started)
        self.web_content = WebKit2.UserContentManager()
        store = WebKit2.UserContentFilterStore.new(str(THUMB_CACHE.parent / "filters"))
        store.save(
            "ads",
            self.GLib.Bytes.new(sources.adblock_rules_json().encode()),
            None,
            self.on_filter_saved,
            store,
        )
        return True

    def on_filter_saved(self, store, result, _data=None):
        try:
            self.web_content.add_filter(store.save_finish(result))
        except self.GLib.Error as error:
            print(f"Could not load ad-blocking rules: {error}", file=sys.stderr)

    def new_web_view(self):
        view = self.web_modules.WebView(
            web_context=self.web_context, user_content_manager=self.web_content
        )
        guard = sources.WebGuard(self.web_modules, self.GLib)
        guard.attach(view)
        return view, guard

    def create_webview(self):
        if not self.ensure_web():
            return
        self.webview, self.guard = self.new_web_view()
        self.webview.connect("notify::uri", self.on_web_uri_changed)
        self.webview.connect("load-changed", self.on_web_load_changed)
        self.web_holder.pack_start(self.webview, True, True, 0)
        self.webview.show()
        self.guard.navigate(self.webview, BROWSER_SITES[0][1])

    def on_site_selected(self, combo):
        index = combo.get_active() - 1
        if index >= 0 and self.webview is not None:
            self.guard.navigate(self.webview, BROWSER_SITES[index][1])
        combo.handler_block_by_func(self.on_site_selected)
        combo.set_active(0)
        combo.handler_unblock_by_func(self.on_site_selected)

    def on_url_activate(self, entry):
        text = entry.get_text().strip()
        if not text or self.webview is None:
            return
        if " " in text or "." not in text:
            uri = "https://duckduckgo.com/?q=" + urllib.parse.quote_plus(text)
        elif "://" not in text:
            uri = "https://" + text
        else:
            uri = text
        self.guard.navigate(self.webview, uri)

    def on_web_uri_changed(self, webview, _param):
        uri = webview.get_uri()
        if uri and not self.url_entry.has_focus():
            self.url_entry.set_text(uri)

    def on_web_load_changed(self, webview, _event):
        self.back_button.set_sensitive(webview.can_go_back())
        self.forward_button.set_sensitive(webview.can_go_forward())

    # --- gallery -------------------------------------------------------------

    def current_source(self):
        return sources.SOURCES[self.source_combo.get_active()]

    def reload_gallery(self):
        for child in self.gallery_flow.get_children():
            self.gallery_flow.remove(child)
        self.gallery_generation += 1
        self.gallery_page = 1
        self.gallery_seen = set()
        self.load_gallery_page()

    def load_gallery_page(self):
        source = self.current_source()
        query = self.search_entry.get_text().strip()
        page = self.gallery_page
        generation = self.gallery_generation
        self.more_button.set_sensitive(False)
        self.gallery_status.set_text(tr("Cargando…"))

        def work():
            try:
                items = source.search(query, page)
                error = None
            except Exception as exc:  # network and parsing errors are shown to the user
                items, error = [], str(exc)
            self.GLib.idle_add(self.show_gallery_items, items, error, generation)

        threading.Thread(target=work, daemon=True).start()

    def show_gallery_items(self, items, error, generation):
        if generation != self.gallery_generation:
            return False
        self.more_button.set_sensitive(True)
        if error:
            self.gallery_status.set_text(tr("No se pudo cargar: {error}", error=error))
            return False
        fresh = [item for item in items if item.page not in self.gallery_seen]
        for item in fresh:
            self.gallery_seen.add(item.page)
            self.gallery_flow.add(self.create_card(item, generation))
        self.gallery_flow.show_all()
        self.gallery_page += 1
        self.more_button.set_visible(bool(fresh))
        self.gallery_status.set_text("" if fresh else tr("No hay más resultados."))
        return False

    def create_card(self, item, generation):
        Gtk = self.Gtk
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        image = Gtk.Image.new_from_icon_name("video-x-generic", Gtk.IconSize.DIALOG)
        image.set_size_request(GALLERY_THUMB_WIDTH, GALLERY_THUMB_HEIGHT)
        card.pack_start(image, False, False, 0)
        title = Gtk.Label(label=item.title)
        title.set_max_width_chars(22)
        title.set_ellipsize(self.Pango.EllipsizeMode.END)
        title.set_tooltip_text(item.title)
        card.pack_start(title, False, False, 0)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        row.set_halign(Gtk.Align.CENTER)
        if item.detail:
            detail = Gtk.Label(label=item.detail)
            detail.get_style_context().add_class("dim-label")
            row.pack_start(detail, False, False, 0)
        button = Gtk.Button(label=tr("Descargar"))
        button.connect("clicked", self.on_card_download, item)
        row.pack_start(button, False, False, 0)
        card.pack_start(row, False, False, 0)

        def fetch_thumb():
            if generation != self.gallery_generation:
                return
            try:
                data, _ = sources.fetch(item.thumb)
            except Exception:
                return
            self.GLib.idle_add(self.set_card_image, image, data, generation)

        self.thumb_pool.submit(fetch_thumb)
        return card

    def set_card_image(self, image, data, generation):
        if generation != self.gallery_generation:
            return False
        try:
            stream = self.Gio.MemoryInputStream.new_from_bytes(self.GLib.Bytes.new(data))
            pixbuf = self.GdkPixbuf.Pixbuf.new_from_stream_at_scale(
                stream, GALLERY_THUMB_WIDTH, GALLERY_THUMB_HEIGHT, True, None
            )
            image.set_from_pixbuf(pixbuf)
        except self.GLib.Error:
            pass
        return False

    # --- downloads -----------------------------------------------------------

    def download_folder(self):
        folder = self.preferences.data["library_folder"]
        if folder and Path(folder).is_dir():
            return Path(folder)
        return None

    def on_card_download(self, _button, item):
        folder = self.download_folder()
        if folder is None:
            self.gallery_status.set_text(
                tr("Elige primero una carpeta en la pestaña Biblioteca.")
            )
            return
        source = next(s for s in sources.SOURCES if s.name == item.source)
        if source.needs_browser:
            self.start_helper_download(item)
        else:
            self.start_direct_download(source, item, folder)

    def new_download_row(self, name, on_cancel):
        row = self.Gtk.Box(orientation=self.Gtk.Orientation.HORIZONTAL, spacing=8)
        label = self.Gtk.Label(label=name)
        label.set_xalign(0)
        label.set_ellipsize(self.Pango.EllipsizeMode.MIDDLE)
        label.set_max_width_chars(28)
        bar = self.Gtk.ProgressBar()
        bar.set_hexpand(True)
        bar.set_show_text(True)
        bar.set_text(tr("Preparando…"))
        cancel = self.Gtk.Button(label=tr("Cancelar"))
        cancel.connect("clicked", lambda *_: on_cancel())
        row.pack_start(label, False, False, 0)
        row.pack_start(bar, True, True, 0)
        row.pack_start(cancel, False, False, 0)
        self.downloads_box.pack_start(row, False, False, 0)
        row.show_all()
        return row, bar, cancel

    def end_download_row(self, entry, text, ok):
        row, bar, cancel = entry
        if ok:
            bar.set_fraction(1.0)
        bar.set_text(text)
        cancel.hide()
        self.GLib.timeout_add_seconds(8, self.remove_download_row, row)

    def remove_download_row(self, row):
        row.destroy()
        return False

    def start_direct_download(self, source, item, folder):
        cancel_flag = threading.Event()
        entry = self.new_download_row(item.title, cancel_flag.set)
        bar = entry[1]

        def progress(done, total):
            fraction = done / total if total else 0
            text = f"{int(fraction * 100)} %" if total else f"{done // 1048576} MB"
            self.GLib.idle_add(self.update_bar, bar, fraction, text)

        def work():
            try:
                url = source.resolve(item)
                target = sources.download_file(
                    url, folder, progress, cancel_flag.is_set, referer=item.page
                )
                self.GLib.idle_add(self.direct_download_done, entry, target.name, None)
            except Exception as exc:
                self.GLib.idle_add(self.direct_download_done, entry, None, str(exc))

        threading.Thread(target=work, daemon=True).start()

    def update_bar(self, bar, fraction, text):
        bar.set_fraction(fraction)
        bar.set_text(text)
        return False

    def direct_download_done(self, entry, name, error):
        if error:
            self.end_download_row(entry, error, False)
        else:
            self.end_download_row(entry, tr("Completado"), True)
            self.reload_library()
        return False

    def start_helper_download(self, item):
        # The site only reveals its file link through its own download button,
        # so a hidden, ad-blocked browser view presses it for the user.
        if not self.ensure_web():
            self.gallery_status.set_text(self.web_error or "")
            return
        view, guard = self.new_web_view()
        window = self.Gtk.OffscreenWindow()
        window.set_default_size(1000, 700)
        window.add(view)
        window.show_all()
        state = {
            "view": view,
            "window": window,
            "item": item,
            "clicks": 0,
            "started": False,
            "timer": None,
            "entry": self.new_download_row(item.title, lambda: self.cancel_helper(state)),
        }
        self.helpers[view] = state
        view.connect("load-changed", self.on_helper_loaded, state)
        guard.navigate(view, item.page)
        state["timer"] = self.GLib.timeout_add_seconds(4, self.helper_tick, state)
        state["deadline"] = self.GLib.timeout_add_seconds(
            45, self.helper_timeout, state
        )

    def on_helper_loaded(self, view, event, state):
        if event == self.web_modules.LoadEvent.FINISHED:
            state["loaded"] = True

    def helper_tick(self, state):
        if state["view"] not in self.helpers or state["started"]:
            state["timer"] = None
            return False
        if state.get("loaded") and state["clicks"] < 4:
            state["clicks"] += 1
            state["view"].run_javascript(
                "var b=document.getElementById('moe-download'); if(b){b.click();}",
                None,
                None,
                None,
            )
        return True

    def helper_timeout(self, state):
        state["deadline"] = None
        if not state["started"]:
            self.close_helper(state, tr("No se pudo iniciar la descarga"))
        return False

    def close_helper(self, state, text=None):
        view = state["view"]
        if self.helpers.pop(view, None) is None:
            return
        for key in ("timer", "deadline"):
            if state.get(key):
                self.GLib.source_remove(state[key])
                state[key] = None
        if text:
            self.end_download_row(state["entry"], text, False)
        state["window"].destroy()

    def cancel_helper(self, state):
        download = state.get("download")
        if download is not None:
            download.cancel()
        else:
            self.close_helper(state, tr("Cancelado"))

    def on_download_started(self, _context, download):
        state = self.helpers.get(download.get_web_view())
        ctx = {"entry": None, "helper": state}
        if state is not None:
            state["started"] = True
            state["download"] = download
            ctx["entry"] = state["entry"]
        download.connect("decide-destination", self.on_decide_destination, ctx)
        download.connect("received-data", self.on_download_progress, ctx)
        download.connect("finished", self.on_download_finished, ctx)
        download.connect("failed", self.on_download_failed, ctx)

    def on_decide_destination(self, download, suggested, ctx):
        folder = self.download_folder()
        if folder is None:
            download.cancel()
            self.gallery_status.set_text(
                tr("Elige primero una carpeta en la pestaña Biblioteca.")
            )
            return True
        target = sources.unique_target(folder, suggested or "wallpaper.mp4")
        download.set_destination(target.as_uri())
        if ctx["entry"] is None:
            ctx["entry"] = self.new_download_row(target.name, download.cancel)
        return True

    def on_download_progress(self, download, _length, ctx):
        if ctx["entry"] is None:
            return
        fraction = download.get_estimated_progress()
        ctx["entry"][1].set_fraction(fraction)
        ctx["entry"][1].set_text(f"{int(fraction * 100)} %")

    def finish_web_download(self, ctx, text, ok):
        if ctx["entry"] is not None:
            self.end_download_row(ctx["entry"], text, ok)
        if ctx["helper"] is not None:
            self.close_helper(ctx["helper"])

    def on_download_finished(self, _download, ctx):
        self.finish_web_download(ctx, tr("Completado"), True)
        self.reload_library()

    def on_download_failed(self, download, _error, ctx):
        self.finish_web_download(ctx, "Cancelado o fallido", False)
        destination = download.get_destination()
        if destination:
            try:
                Path(self.GLib.filename_from_uri(destination)[0]).unlink(missing_ok=True)
            except (OSError, self.GLib.Error):
                pass

    def build_library_tab(self):
        Gtk = self.Gtk
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_border_width(12)
        folder_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.folder_label = Gtk.Label()
        self.folder_label.set_xalign(0)
        self.folder_label.set_hexpand(True)
        self.folder_label.set_ellipsize(self.Pango.EllipsizeMode.MIDDLE)
        folder_row.pack_start(self.folder_label, True, True, 0)
        folder_button = Gtk.Button(label=tr("Elegir carpeta…"))
        folder_button.connect("clicked", self.choose_folder)
        folder_row.pack_start(folder_button, False, False, 0)
        box.pack_start(folder_row, False, False, 0)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(190)
        self.library_flow = Gtk.FlowBox()
        self.library_flow.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.library_flow.set_homogeneous(True)
        self.library_flow.set_max_children_per_line(4)
        self.library_flow.set_min_children_per_line(2)
        self.library_flow.set_row_spacing(8)
        self.library_flow.set_column_spacing(8)
        self.library_flow.connect("child-activated", self.on_library_selected)
        self.library_flow.connect("selected-children-changed", self.on_library_selected)
        scroll.add(self.library_flow)
        box.pack_start(scroll, True, True, 0)
        self.notebook.insert_page(box, Gtk.Label(label=tr("Biblioteca")), 0)
        self.library_generation = 0
        self.reload_library()

    def choose_folder(self, *_args):
        dialog = self.Gtk.FileChooserDialog(
            title=tr("Carpeta de wallpapers"),
            parent=self.window,
            action=self.Gtk.FileChooserAction.SELECT_FOLDER,
        )
        dialog.add_buttons(
            self.Gtk.STOCK_CANCEL,
            self.Gtk.ResponseType.CANCEL,
            self.Gtk.STOCK_OPEN,
            self.Gtk.ResponseType.ACCEPT,
        )
        current = self.preferences.data["library_folder"]
        if current:
            dialog.set_filename(current)
        if dialog.run() == self.Gtk.ResponseType.ACCEPT:
            self.preferences.data["library_folder"] = dialog.get_filename()
            self.preferences.save()
            self.reload_library()
        dialog.destroy()

    def reload_library(self):
        for child in self.library_flow.get_children():
            self.library_flow.remove(child)
        self.library_generation += 1
        folder = self.preferences.data["library_folder"]
        if not folder or not Path(folder).is_dir():
            self.folder_label.set_text(tr("Ninguna carpeta seleccionada"))
            return
        self.folder_label.set_text(folder)
        try:
            videos = sorted(
                (
                    path
                    for path in Path(folder).iterdir()
                    if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
                ),
                key=lambda path: path.name.casefold(),
            )
        except OSError as error:
            self.folder_label.set_text(tr("No se pudo leer la carpeta: {error}", error=error))
            return
        items = []
        for video in videos:
            card = self.Gtk.Box(orientation=self.Gtk.Orientation.VERTICAL, spacing=4)
            card.set_border_width(4)
            image = self.Gtk.Image.new_from_icon_name("video-x-generic", self.Gtk.IconSize.DIALOG)
            image.set_size_request(THUMB_WIDTH, THUMB_HEIGHT)
            card.pack_start(image, False, False, 0)
            label = self.Gtk.Label(label=video.stem)
            label.set_max_width_chars(18)
            label.set_ellipsize(self.Pango.EllipsizeMode.END)
            card.pack_start(label, False, False, 0)
            card.set_tooltip_text(video.name)
            card.video_path = video
            self.library_flow.add(card)
            if video == self.video:
                self.library_flow.select_child(card.get_parent())
            items.append((video, image))
        self.library_flow.show_all()
        generation = self.library_generation
        threading.Thread(
            target=self.generate_thumbnails, args=(items, generation), daemon=True
        ).start()

    def thumbnail_path(self, video):
        stat = video.stat()
        key = hashlib.sha1(f"{video}:{stat.st_mtime_ns}:{stat.st_size}".encode()).hexdigest()
        return THUMB_CACHE / f"{key}.jpg"

    def generate_thumbnails(self, items, generation):
        THUMB_CACHE.mkdir(parents=True, exist_ok=True)
        for video, image in items:
            if generation != self.library_generation:
                return
            try:
                target = self.thumbnail_path(video)
                if not target.exists():
                    with tempfile.TemporaryDirectory() as tmp:
                        subprocess.run(
                            [
                                self.controller.mpv,
                                "--no-config",
                                "--no-audio",
                                "--vo=image",
                                "--vo-image-format=jpg",
                                f"--vo-image-outdir={tmp}",
                                "--frames=1",
                                "--start=10%",
                                "--vf=scale=480:-2",
                                str(video),
                            ],
                            capture_output=True,
                            timeout=30,
                        )
                        frames = sorted(Path(tmp).glob("*.jpg"))
                        if not frames:
                            continue
                        shutil.copyfile(frames[0], target)
            except (OSError, subprocess.SubprocessError):
                continue
            self.GLib.idle_add(self.set_thumbnail, image, target, generation)

    def set_thumbnail(self, image, path, generation):
        if generation != self.library_generation:
            return False
        try:
            pixbuf = self.GdkPixbuf.Pixbuf.new_from_file_at_scale(
                str(path), THUMB_WIDTH, THUMB_HEIGHT, True
            )
        except self.GLib.Error:
            return False
        image.set_from_pixbuf(pixbuf)
        return False

    def on_library_selected(self, flow, child=None):
        if child is None:
            selected = flow.get_selected_children()
            child = selected[0] if selected else None
        if child is None:
            return
        self.video = child.get_child().video_path
        self.update_selected_video()

    def update_selected_video(self):
        if self.video is None:
            self.file_label.set_text(tr("Ningún video seleccionado"))
            self.file_label.set_tooltip_text(tr("Selecciona un archivo de video"))
            return
        self.file_label.set_text(self.video.name)
        self.file_label.set_tooltip_text(str(self.video))

    def pick_video_file(self, title, current=None):
        dialog = self.Gtk.FileChooserDialog(
            title=title,
            parent=self.window,
            action=self.Gtk.FileChooserAction.OPEN,
        )
        dialog.add_buttons(
            self.Gtk.STOCK_CANCEL,
            self.Gtk.ResponseType.CANCEL,
            self.Gtk.STOCK_OPEN,
            self.Gtk.ResponseType.ACCEPT,
        )
        video_filter = self.Gtk.FileFilter()
        video_filter.set_name(tr("Archivos de video"))
        video_filter.add_mime_type("video/*")
        for pattern in (
            "*.mp4",
            "*.mkv",
            "*.webm",
            "*.mov",
            "*.avi",
            "*.m4v",
            "*.ogv",
            "*.wmv",
            "*.flv",
            "*.mpg",
            "*.mpeg",
        ):
            video_filter.add_pattern(pattern)
        dialog.add_filter(video_filter)
        dialog.set_filter(video_filter)
        all_filter = self.Gtk.FileFilter()
        all_filter.set_name(tr("Todos los archivos"))
        all_filter.add_pattern("*")
        dialog.add_filter(all_filter)
        if current is not None:
            dialog.set_filename(str(current))
        chosen = None
        if dialog.run() == self.Gtk.ResponseType.ACCEPT:
            chosen = Path(dialog.get_filename())
        dialog.destroy()
        return chosen

    def choose_video(self, *_args):
        chosen = self.pick_video_file(tr("Seleccionar video"), self.video)
        if chosen is not None:
            self.video = chosen
            self.update_selected_video()
            self.status.set_text("")

    def choose_monitor_video(self, _button, name):
        videos = self.preferences.data["monitor_videos"]
        current = videos.get(name)
        chosen = self.pick_video_file(
            tr("Video para {name}", name=name), Path(current) if current else self.video
        )
        if chosen is None:
            return
        videos[name] = str(chosen)
        self.after_monitor_video_change(f"{name}: {chosen.name}")

    def clear_monitor_video(self, _button, name):
        if self.preferences.data["monitor_videos"].pop(name, None) is None:
            return
        self.after_monitor_video_change(tr("{name}: usa el video general.", name=name))

    def after_monitor_video_change(self, message):
        self.preferences.save()
        self.status.set_text(message)
        self.GLib.idle_add(self.reload_monitors)
        if self.controller.running:
            self.GLib.idle_add(self.controller.restart)

    def apply_wallpaper(self, *_args, quiet=False):
        if self.video is None:
            self.status.set_text(tr("Primero elige un video."))
            return
        self.quiet_start = quiet
        try:
            self.controller.start(
                self.video,
                self.fit_combo.get_active_id(),
                self.audio_switch.get_active(),
                self.icon_size,
                self.preferences,
            )
        except (OSError, RuntimeError) as error:
            if quiet:
                self.status.set_text(str(error))
            else:
                self.show_error(str(error))
            return
        finally:
            self.quiet_start = False
        data = self.preferences.data
        data["last_video"] = str(self.video)
        data["last_fit"] = self.fit_combo.get_active_id()
        data["last_audio"] = self.audio_switch.get_active()
        self.preferences.save()
        self.status.set_text(tr("Fondo activo: {name}", name=self.video.name))
        self.update_running_state()

    def stop_wallpaper(self, *_args):
        self.controller.stop()
        self.status.set_text(tr("Fondo detenido."))
        self.update_running_state()

    def on_stopped(self):
        self.status.set_text(tr("Fondo detenido."))
        self.update_running_state()
        if (
            not self.quiet_start
            and not self.window.get_visible()
            and self.Gtk.main_level() > 0
        ):
            self.Gtk.main_quit()

    def show_error(self, message):
        self.status.set_text(message)
        self.update_running_state()
        if not self.window.get_visible():
            self.present()

    def running_window_classes(self):
        result = subprocess.run(["wmctrl", "-lx"], capture_output=True, text=True)
        classes = set()
        for line in result.stdout.splitlines():
            parts = line.split(None, 4)
            if len(parts) >= 3 and "." in parts[2]:
                classes.add(parts[2].split(".", 1)[1])
        for own_class in ("Live Wallpaper", "Xalive Wallpaper", "xalive.py"):
            classes.discard(own_class)
        return sorted(classes, key=str.casefold)

    def reload_exceptions(self):
        for row in self.exceptions_list.get_children():
            self.exceptions_list.remove(row)
        for name in self.preferences.data["pause_exceptions"]:
            row = self.Gtk.ListBoxRow()
            label = self.Gtk.Label(label=name)
            label.set_xalign(0)
            label.set_margin_start(6)
            row.add(label)
            row.exception_name = name
            self.exceptions_list.add(row)
        self.exceptions_list.show_all()
        self.exception_combo.remove_all()
        for name in self.running_window_classes():
            self.exception_combo.append_text(name)

    def add_exception(self, *_args):
        name = self.exception_combo.get_child().get_text().strip()
        names = self.preferences.data["pause_exceptions"]
        if name and name.casefold() not in {item.casefold() for item in names}:
            names.append(name)
            self.preferences.save()
            self.reload_exceptions()
        self.exception_combo.get_child().set_text("")

    def remove_exception(self, *_args):
        row = self.exceptions_list.get_selected_row()
        if row is not None:
            self.preferences.data["pause_exceptions"].remove(row.exception_name)
            self.preferences.save()
            self.reload_exceptions()

    def reload_monitors(self):
        Gtk = self.Gtk
        for child in self.monitors_grid.get_children():
            self.monitors_grid.remove(child)
        saved = self.preferences.data["monitor_modes"]
        outputs = query_outputs()
        videos = self.preferences.data["monitor_videos"]
        for number, output in enumerate(outputs):
            row = number * 2
            name = output["name"]
            current = (output["mode"] or "?").replace("x", "×", 1)
            label = Gtk.Label(label=f"{name}  (ahora {current})", xalign=0)
            label.set_hexpand(True)
            self.monitors_grid.attach(label, 0, row, 1, 1)
            combo = Gtk.ComboBoxText()
            combo.append("auto", tr("Automática (se adapta sola)"))
            for mode in sorted(
                output["modes"],
                key=lambda m: tuple(int(v) for v in re.findall(r"\d+", m)[:2]),
                reverse=True,
            ):
                text = mode.replace("x", "×", 1)
                if mode == output["preferred"]:
                    text += " (recomendada)"
                combo.append(mode, text)
            wanted = saved.get(name)
            combo.set_active_id(wanted if wanted in output["modes"] else "auto")
            combo.connect("changed", self.on_monitor_mode_changed, name, output["mode"])
            self.monitors_grid.attach(combo, 1, row, 1, 1)

            custom = videos.get(name)
            video_label = Gtk.Label(
                label=f"Video: {Path(custom).name}" if custom else "Video: el general",
                xalign=0,
            )
            video_label.set_ellipsize(self.Pango.EllipsizeMode.MIDDLE)
            if custom:
                video_label.set_tooltip_text(custom)
            self.monitors_grid.attach(video_label, 0, row + 1, 1, 1)
            buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            buttons.set_halign(Gtk.Align.END)
            choose = Gtk.Button(label=tr("Elegir video…"))
            choose.connect("clicked", self.choose_monitor_video, name)
            buttons.pack_start(choose, False, False, 0)
            if custom:
                clear = Gtk.Button(label=tr("Usar el general"))
                clear.connect("clicked", self.clear_monitor_video, name)
                buttons.pack_start(clear, False, False, 0)
            self.monitors_grid.attach(buttons, 1, row + 1, 1, 1)
        note = Gtk.Label(
            label=tr("Cada monitor puede tener su propio video; sin elegir uno usa el "
            "general. Los monitores en espejo comparten un solo video. El fondo se "
            "adapta solo a la resolución, y una resolución menor reduce la carga "
            "de la GPU."),
            xalign=0,
        )
        note.set_line_wrap(True)
        self.monitors_grid.attach(note, 0, len(outputs) * 2, 2, 1)
        self.monitors_grid.show_all()

    def on_monitor_mode_changed(self, combo, name, previous_mode):
        choice = combo.get_active_id()
        saved = self.preferences.data["monitor_modes"]
        if choice is None:
            return
        if choice == "auto":
            saved.pop(name, None)
            self.preferences.save()
            self.status.set_text(tr("{name}: resolución automática.", name=name))
            return
        if choice != previous_mode:
            ok, message = set_output_mode(name, choice)
            if not ok:
                self.status.set_text(tr("No se pudo cambiar {name} a {choice}: {message}", name=name, choice=choice, message=message))
                self.GLib.idle_add(self.reload_monitors)
                return
            if not self.confirm_mode(name, choice):
                if previous_mode:
                    set_output_mode(name, previous_mode)
                self.status.set_text(tr("{name}: se revirtió a {mode}.", name=name, mode=previous_mode))
                self.GLib.idle_add(self.reload_monitors)
                return
        saved[name] = choice
        self.preferences.save()
        self.status.set_text(f"{name}: {choice.replace('x', '×')}.")
        self.GLib.idle_add(self.reload_monitors)

    def confirm_mode(self, name, mode, seconds=10):
        Gtk = self.Gtk
        dialog = Gtk.MessageDialog(
            transient_for=self.window,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text=tr("¿Mantener {mode} en {name}?", mode=mode.replace("x", "×"), name=name),
        )
        dialog.add_button(tr("Revertir"), Gtk.ResponseType.CANCEL)
        dialog.add_button(tr("Mantener"), Gtk.ResponseType.OK)
        dialog.set_keep_above(True)
        state = {"left": seconds, "timer": None}

        def tick():
            state["left"] -= 1
            if state["left"] <= 0:
                state["timer"] = None
                dialog.response(Gtk.ResponseType.CANCEL)
                return False
            dialog.format_secondary_text(
                tr("Si no confirmas, se revertirá en {seconds} s.", seconds=state["left"])
            )
            return True

        dialog.format_secondary_text(tr("Si no confirmas, se revertirá en {seconds} s.", seconds=seconds))
        state["timer"] = self.GLib.timeout_add_seconds(1, tick)
        response = dialog.run()
        if state["timer"] is not None:
            self.GLib.source_remove(state["timer"])
        dialog.destroy()
        return response == Gtk.ResponseType.OK

    def on_pause_switch_changed(self, switch, _param):
        enabled = switch.get_active()
        self.preferences.data["pause_on_activity"] = enabled
        self.preferences.save()
        self.controller.set_pause_on_activity(enabled)

    def on_icons_switch_changed(self, switch, _param):
        enabled = switch.get_active()
        self.preferences.data["show_icons"] = enabled
        self.preferences.save()
        self.icon_size_box.set_sensitive(enabled)
        if self.controller.running:
            self.controller.restart()

    def on_language_changed(self, combo):
        self.preferences.data["language"] = combo.get_active_id()
        self.preferences.save()
        self.status.set_text(tr("El idioma cambia al reiniciar Xalive Wallpaper."))

    def on_restore_switch_changed(self, switch, _param):
        self.preferences.data["restore_last"] = switch.get_active()
        self.preferences.save()

    def on_autostart_switch_changed(self, switch, _param):
        try:
            set_autostart(switch.get_active())
        except OSError as error:
            self.status.set_text(tr("No se pudo cambiar el inicio automático: {error}", error=error))
            switch.handler_block_by_func(self.on_autostart_switch_changed)
            switch.set_active(autostart_enabled())
            switch.handler_unblock_by_func(self.on_autostart_switch_changed)

    def on_icon_size_changed(self, scale):
        self.icon_size = round(scale.get_value() / 8) * 8
        self.icon_size = max(ICON_SIZE_MIN, min(self.icon_size, ICON_SIZE_MAX))
        self.preferences.data["icon_size"] = self.icon_size
        self.preferences.save()
        self.controller.set_icon_size(self.icon_size)

    def update_running_state(self):
        running = self.controller.running
        self.stop_button.set_sensitive(running)
        self.apply_button.set_label(tr("Cambiar fondo") if running else tr("Aplicar fondo"))

    def on_close(self, *_args):
        # With an active wallpaper the program keeps running in the background.
        if self.controller.running:
            self.window.hide()
            return True
        if self.Gtk.main_level() > 0:
            self.Gtk.main_quit()
        return True

    def present(self):
        self.window.show_all()
        self.window.present()


def load_gtk():
    try:
        import gi

        gi.require_version("Gdk", "3.0")
        gi.require_version("Gio", "2.0")
        gi.require_version("Gtk", "3.0")
        gi.require_version("GdkX11", "3.0")
        gi.require_version("Pango", "1.0")
        from gi.repository import Gdk, GdkPixbuf, GdkX11, Gio, GLib, Gtk, Pango
    except (ImportError, ValueError) as error:
        print(
            "GTK 3 with its Python GObject bindings is required "
            "(on Linux Mint: sudo apt install python3-gi gir1.2-gtk-3.0).",
            file=sys.stderr,
        )
        print(error, file=sys.stderr)
        return None
    return Gdk, GdkPixbuf, GdkX11, Gio, GLib, Gtk, Pango


def restore_last_wallpaper(app, GLib, attempts=40):
    # At session start xfdesktop or the compositor may not be ready yet, so retry for a while.
    state = {"left": attempts}

    def attempt():
        if app.controller.running:
            return False
        app.apply_wallpaper(quiet=True)
        if app.controller.running:
            return False
        state["left"] -= 1
        if state["left"] > 0:
            return True
        app.present()
        return False

    if attempt():
        GLib.timeout_add_seconds(1, attempt)


def main():
    args = parse_args()
    mpv = shutil.which(args.mpv)
    if mpv is None:
        print(f"Could not find mpv executable: {args.mpv}", file=sys.stderr)
        return 2

    gtk = load_gtk()
    if gtk is None:
        return 2
    Gdk, GdkPixbuf, GdkX11, Gio, GLib, Gtk, Pango = gtk

    initialized, _ = Gtk.init_check([])
    if not initialized:
        print("Could not connect to the graphical display.", file=sys.stderr)
        return 2

    display = Gdk.Display.get_default()
    if not isinstance(display, GdkX11.X11Display):
        print("This prototype currently requires an X11 session.", file=sys.stderr)
        return 2
    if display.get_n_monitors() < 1:
        print("No monitors were found on the current display.", file=sys.stderr)
        return 2

    gtk_app = Gtk.Application(application_id="io.github.xalive")
    try:
        gtk_app.register(None)
    except GLib.Error as error:
        print(f"Single-instance check unavailable: {error.message}", file=sys.stderr)
    else:
        if gtk_app.get_is_remote():
            if not args.background:
                gtk_app.activate()
            return 0

    controller = WallpaperController(
        Gtk,
        Gdk,
        GdkX11,
        Gio,
        GLib,
        GdkPixbuf,
        Pango,
        display,
        mpv,
    )
    preferences = WallpaperPreferences()
    apply_saved_modes(preferences)
    signal.signal(signal.SIGINT, lambda *_: Gtk.main_quit())
    signal.signal(signal.SIGTERM, lambda *_: Gtk.main_quit())
    signal.signal(signal.SIGHUP, lambda *_: Gtk.main_quit())

    if args.video is None:
        last = Path(preferences.data["last_video"]) if preferences.data["last_video"] else None
        restore = preferences.data["restore_last"] and last is not None and last.is_file()
        background = args.background and restore
        app = WallpaperApp(Gtk, Pango, controller, preferences, background=background)
        if not background:
            app.window.present()
        gtk_app.connect("activate", lambda *_: app.present())
        if restore:
            restore_last_wallpaper(app, GLib)
    else:
        try:
            controller.start(
                args.video,
                args.fit,
                args.audio,
                preferences.data["icon_size"],
                preferences,
            )
        except (OSError, RuntimeError) as error:
            print(f"Could not start Xalive Wallpaper: {error}", file=sys.stderr)
            return 1

    try:
        Gtk.main()
    finally:
        controller.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
