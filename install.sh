#!/bin/sh
# Installs Xalive Wallpaper for the current user: no root needed, no terminal needed afterwards.
# Usage: ./install.sh            install or update
#        ./install.sh --remove   uninstall (settings are kept)
set -eu

APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/xalive-app"
BIN="$HOME/.local/bin/xalive"
MENU="${XDG_DATA_HOME:-$HOME/.local/share}/applications/xalive.desktop"
AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/xalive.desktop"
OLD_APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/livewallpaper-app"
OLD_BIN="$HOME/.local/bin/livewallpaper"
OLD_MENU="${XDG_DATA_HOME:-$HOME/.local/share}/applications/livewallpaper.desktop"
OLD_AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/livewallpaper.desktop"

if [ "${1:-}" = "--remove" ]; then
    rm -rf "$APP_DIR" "$BIN" "$MENU" "$AUTOSTART" "$OLD_APP_DIR" "$OLD_BIN" "$OLD_MENU" "$OLD_AUTOSTART"
    echo "Xalive Wallpaper removed. Settings in ~/.config/xalive were kept."
    exit 0
fi

SRC="$(cd "$(dirname "$0")" && pwd)"

missing=""
for tool in mpv wmctrl xfconf-query python3; do
    command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"
done
python3 -c 'import gi; gi.require_version("Gtk", "3.0")' 2>/dev/null || missing="$missing python3-gi"
if [ -n "$missing" ]; then
    echo "Missing:$missing"
    echo "On Linux Mint: sudo apt install mpv python3-gi gir1.2-gtk-3.0 wmctrl xfconf"
    exit 1
fi

mkdir -p "$APP_DIR" "$(dirname "$BIN")" "$(dirname "$MENU")"
cp "$SRC/xalive.py" "$SRC/xalive_sources.py" "$SRC/xalive_i18n.py" "$SRC/xalive_x11.py" "$SRC/xalive_workshop.py" "$APP_DIR/"

cat > "$BIN" <<EOF
#!/bin/sh
exec python3 "$APP_DIR/xalive.py" "\$@"
EOF
chmod +x "$BIN"

cat > "$MENU" <<EOF
[Desktop Entry]
Type=Application
Name=Xalive Wallpaper
Comment=Animated video wallpaper
Exec="$BIN"
Icon=video-display
Terminal=false
Categories=Settings;DesktopSettings;GTK;
EOF

# Migrate an existing autostart entry from the old name, pointing at the installed copy.
if [ -f "$OLD_AUTOSTART" ] && [ ! -f "$AUTOSTART" ]; then
    mkdir -p "$(dirname "$AUTOSTART")"
    cp "$OLD_AUTOSTART" "$AUTOSTART"
fi
rm -f "$OLD_AUTOSTART" "$OLD_MENU" "$OLD_BIN"
if [ -f "$AUTOSTART" ]; then
    sed -i "s|^Exec=.*|Exec=\"$BIN\" --background|; s|^Name=.*|Name=Xalive Wallpaper|" "$AUTOSTART"
fi

echo "Installed. Open \"Xalive Wallpaper\" from the applications menu."
echo "Enable \"Iniciar con la sesión\" in its settings to start it with XFCE."
