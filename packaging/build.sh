#!/bin/sh
# Builds xalive_<version>_all.deb and xalive-<version>-1-any.pkg.tar.zst into ./dist.
# Needs dpkg-deb, fakeroot, zstd and tar. Usage: packaging/build.sh <version>
set -eu

VERSION="${1:?usage: packaging/build.sh <version>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST="$ROOT/dist"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$DIST"

stage() {
    dest="$1"
    mkdir -p "$dest/usr/lib/xalive" "$dest/usr/bin" "$dest/usr/share/applications" "$dest/usr/share/doc/xalive"
    cp "$ROOT"/xalive.py "$ROOT"/xalive_sources.py "$ROOT"/xalive_i18n.py \
        "$ROOT"/xalive_x11.py "$ROOT"/xalive_workshop.py "$dest/usr/lib/xalive/"
    cp "$ROOT/LICENSE" "$dest/usr/share/doc/xalive/copyright"
    cat > "$dest/usr/bin/xalive" <<'EOF'
#!/bin/sh
exec python3 /usr/lib/xalive/xalive.py "$@"
EOF
    chmod 755 "$dest/usr/bin/xalive"
    cat > "$dest/usr/share/applications/xalive.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=Xalive Wallpaper
Comment=Animated video wallpaper
Exec=xalive
Icon=video-display
Terminal=false
Categories=Settings;DesktopSettings;GTK;
EOF
    chmod 755 "$dest"
    find "$dest/usr" -type d -exec chmod 755 {} +
    find "$dest/usr/lib" "$dest/usr/share" -type f -exec chmod 644 {} +
}

# Debian / Ubuntu / Linux Mint
DEB="$WORK/deb"
stage "$DEB"
mkdir -p "$DEB/DEBIAN"
SIZE="$(du -sk "$DEB/usr" | cut -f1)"
cat > "$DEB/DEBIAN/control" <<EOF
Package: xalive
Version: $VERSION
Architecture: all
Maintainer: Israjdk <noreply@github.com>
Installed-Size: $SIZE
Depends: python3, python3-gi, gir1.2-gtk-3.0, mpv, wmctrl, x11-xserver-utils, xfconf
Recommends: gir1.2-webkit2-4.1, gir1.2-ayatanaappindicator3-0.1
Section: x11
Priority: optional
Homepage: https://github.com/Israjdk/xalive-wallpaper
Description: Animated video wallpaper for X11 (XFCE)
 Plays videos with mpv as the desktop wallpaper, draws its own desktop
 icon layer, pauses with maximized windows and can download wallpapers.
EOF
fakeroot dpkg-deb --build -Zxz "$DEB" "$DIST/xalive_${VERSION}_all.deb" >/dev/null

# Arch Linux (prebuilt package, installable with pacman -U)
ARCH="$WORK/arch"
stage "$ARCH"
SIZE_BYTES="$(du -sb "$ARCH/usr" | cut -f1)"
cat > "$ARCH/.PKGINFO" <<EOF
pkgname = xalive
pkgbase = xalive
pkgver = $VERSION-1
pkgdesc = Animated video wallpaper for X11 (XFCE)
url = https://github.com/Israjdk/xalive-wallpaper
builddate = $(date +%s)
packager = Israjdk
size = $SIZE_BYTES
arch = any
license = custom
depend = python
depend = python-gobject
depend = gtk3
depend = mpv
depend = wmctrl
depend = xorg-xrandr
depend = xfconf
optdepend = webkit2gtk-4.1: built-in wallpaper browser
optdepend = libayatana-appindicator: tray icon
EOF
chmod 644 "$ARCH/.PKGINFO"
(cd "$ARCH" && fakeroot tar --zstd -cf "$DIST/xalive-$VERSION-1-any.pkg.tar.zst" .PKGINFO usr)

echo "Built:"
ls -l "$DIST"
