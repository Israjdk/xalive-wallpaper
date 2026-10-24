# How Xalive draws desktop icons over an animated wallpaper

On XFCE the desktop is owned by `xfdesktop`. It paints the wallpaper and the
icons in the same window, so anything drawn under it (like a video) is hidden,
and there is no setting to make it transparent. A video wallpaper therefore
normally means losing the desktop icons. Xalive works around this by replacing
the icon part of `xfdesktop` with its own layer. Everything lives in the
`XfceIconLayer` class of [`xalive.py`](../xalive.py).

## Window stack

From bottom to top:

```
xfdesktop window        (still there, but with no icons; it only paints the plain background)
Xalive video window(s)  one borderless GTK window per monitor/area, mpv embedded with --wid
Xalive icon layer       one transparent, full-screen GTK window
normal application windows
panel
```

- The video windows are restacked right above the `xfdesktop` window
  (`Gdk.Window.restack`, using the window id found with `wmctrl -l -x`).
- The icon layer is restacked right above the last video window. It is marked
  `keep_below`, sticky, skip-taskbar and skip-pager, so it behaves like part of
  the desktop: it appears on every workspace, never in the task list, and
  application windows always cover it.

## Step by step

1. **Hide the native icons.** The icon style of `xfdesktop` is stored in the
   xfconf property `xfce4-desktop:/desktop-icons/style` (`0` means "no icons").
   Xalive remembers the original value, sets it to `0`, and restores (or
   removes) it when the wallpaper stops. If the property did not exist, it is
   created and later deleted again.
2. **Create a transparent window.** The window needs an RGBA visual and a
   running compositor (`screen.is_composited()`; the XFCE window manager
   compositor is enough). CSS makes the window and every icon widget
   `background-color: transparent`, so the video below shows through. Without a
   compositor the layer cannot be created and Xalive reports an error.
3. **Read the Desktop folder.** The folder comes from the XDG user directory
   (`~/Desktop`, `~/Escritorio`, ...). Hidden files are skipped and the rest is
   sorted by name.
4. **Pick an icon and a label for each entry.**
   - `.desktop` launchers use `Gio.DesktopAppInfo`: its name is the label and
     its icon is the icon.
   - Everything else asks GIO for `standard::icon` (file type or thumbnail icon).
   - The icon is loaded from the current GTK icon theme at the chosen size
     (`FORCE_SIZE`). If nothing is found, `folder` or `text-x-generic` is used.
5. **Place the icons on a grid.** Every icon is a small `Gtk.EventBox` (image
   plus label) put in a `Gtk.Fixed` that covers the screen. The cell size
   depends on the icon size and the widest label, and positions snap to the
   grid. New icons fill the grid column by column; dragged icons keep their
   cell, which is saved by full path in
   `~/.config/xalive/settings.json` (`icon_positions`).
6. **Handle input.**
   - Double click launches the entry (`.desktop` files through
     `Gio.DesktopAppInfo.launch`, everything else with the default handler).
   - Dragging moves the icon and snaps it to a free cell.
   - Right click on an icon: Open, Open containing folder, Rename, Move to
     Trash. Right click on empty space: create folder or document, open a
     terminal, display settings, desktop settings, refresh.
   - The layer normally refuses keyboard focus. When you click it, it takes
     focus briefly so that panel menus (for example the Whisker menu) close, as
     they do on a normal desktop. Menus opened by the layer itself are closed
     manually on the next click, because without focus GTK cannot grab the
     pointer.
7. **Stay up to date.** A `Gio.FileMonitor` (inotify) on the Desktop folder
   triggers a refresh 250 ms after changes, so creating or deleting files is
   reflected almost instantly. A slow 30 second timer is only a safety net; it
   lists the folder (about 0.01 ms) and does nothing if the list is unchanged.
8. **Survive `xfdesktop` crashes.** Every 2 seconds the controller checks that
   `xfdesktop` is still running. If it crashed (it can segfault when the screen
   layout changes) it is started again and the icon layer is rebuilt.

## Why this works (and its limits)

- The trick is the separation: the video is a normal window above `xfdesktop`,
  and the icons are another normal window above the video. Nothing is injected
  into `xfdesktop` or patched; the only thing changed is one xfconf property.
- Because the layer is a full-screen window it covers the whole screen from the
  stacking point of view, but it is transparent, so it does not hide anything
  visually, and it never takes focus except on a click.
- Not included, compared with the real `xfdesktop`: window-minimize icons,
  removable devices, the Home/Trash/File System special icons and the complete
  desktop context menu.
- Other X11 desktops are untested. The same idea should work wherever the
  desktop is a separate window that can be told not to draw icons, and a
  compositor is available.
