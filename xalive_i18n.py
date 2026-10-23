"""Minimal translation support: Spanish source texts mapped to English."""

import json
import locale
import os
from pathlib import Path

LANGUAGES = ("auto", "es", "en")

EN = {
    "Explorar": "Browse",
    "Sitios…": "Sites…",
    "Navegador": "Browser",
    "Cargando…": "Loading…",
    "Preparando…": "Preparing…",
    "Completado": "Completed",
    "Cancelado": "Cancelled",
    "Cancelar": "Cancel",
    "Revertir": "Revert",
    "Mantener": "Keep",
    "Quitar": "Remove",
    "Buscar": "Search",
    "Descargar": "Download",
    "Ajustes": "Settings",
    "Biblioteca": "Library",
    " Video ": " Video ",
    " Opciones ": " Options ",
    " Monitores ": " Monitors ",
    "Fondos animados para tu escritorio": "Animated backgrounds for your desktop",
    "Tu escritorio, en movimiento": "Your desktop, in motion",
    "Elige un video y ajusta cómo se verá en tus pantallas.": "Pick a video and choose how it looks on your screens.",
    "Selecciona un archivo de video": "Select a video file",
    "Seleccionar video": "Select video",
    "Archivos de video": "Video files",
    "Todos los archivos": "All files",
    "Elegir video…": "Choose video…",
    "Elegir carpeta…": "Choose folder…",
    "Carpeta de wallpapers": "Wallpaper folder",
    "Ninguna carpeta seleccionada": "No folder selected",
    "Ningún video seleccionado": "No video selected",
    "Cubrir (recorta los bordes)": "Cover (crops the edges)",
    "Contener (puede dejar márgenes)": "Contain (may leave margins)",
    "Ajuste a pantalla": "Fit to screen",
    "Reproducir audio": "Play audio",
    "Pausar con ventanas maximizadas o a pantalla completa": "Pause with maximized or fullscreen windows",
    "Mostrar iconos del escritorio": "Show desktop icons",
    "Tamaño de los iconos": "Icon size",
    "Pequeño (": "Small (",
    "Grande (": "Large (",
    "Recordar el último fondo al abrir": "Remember the last wallpaper on start",
    "Iniciar con la sesión": "Start with the session",
    "Idioma": "Language",
    "Automático": "Automatic",
    "Español": "Spanish",
    "Inglés": "English",
    "El idioma cambia al reiniciar Xalive Wallpaper.": "The language changes after restarting Xalive Wallpaper.",
    " Excepciones (nunca pausan el fondo) ": " Exceptions (never pause the wallpaper) ",
    "Clase de la ventana (WM_CLASS), p. ej. mpv o vlc": "Window class (WM_CLASS), e.g. mpv or vlc",
    "Añadir": "Add",
    "Detener fondo": "Stop wallpaper",
    "Aplicar fondo": "Apply wallpaper",
    "Cambiar fondo": "Change wallpaper",
    "Arrastra los iconos para moverlos por la cuadrícula; tamaño y posiciones se guardan.": "Drag icons to move them around the grid; size and positions are saved.",
    "Buscar wallpapers…": "Search wallpapers…",
    "Dirección o texto a buscar": "Address or text to search",
    "Las descargas se guardan sin modificar en la carpeta de la biblioteca.": "Downloads are saved unmodified in the library folder.",
    "Cargar más": "Load more",
    "Falta WebKitGTK: sudo apt install gir1.2-webkit2-4.1": "WebKitGTK is missing: sudo apt install gir1.2-webkit2-4.1",
    "No hay más resultados.": "No more results.",
    "Elige primero una carpeta en la pestaña Biblioteca.": "First choose a folder in the Library tab.",
    "No se pudo iniciar la descarga": "Could not start the download",
    "Fondo detenido.": "Wallpaper stopped.",
    "Primero elige un video.": "Choose a video first.",
    "Automática (se adapta sola)": "Automatic (adapts by itself)",
    "Usar el general": "Use the general one",
    "Cada monitor puede tener su propio video; sin elegir uno usa el general. Los monitores en espejo comparten un solo video. El fondo se adapta solo a la resolución, y una resolución menor reduce la carga de la GPU.": "Each monitor can have its own video; if you don't pick one it uses the general one. Mirrored monitors share a single video. The wallpaper adapts to the resolution by itself, and a lower resolution reduces the GPU load.",
    "No se encontraron monitores.": "No monitors were found.",
    "No se encontró un enlace de descarga.": "No download link was found.",
    "Este elemento no tiene un video original.": "This item has no original video.",
    "El enlace no devolvió un video.": "The link did not return a video.",
    "No se pudo readaptar el fondo a la nueva resolución: {error}": "Could not adapt the wallpaper to the new resolution: {error}",
    "No se encontró el video: {video}": "Video not found: {video}",
    "mpv terminó inesperadamente (estado {code}).": "mpv ended unexpectedly (status {code}).",
    "Pequeño ({size} px)": "Small ({size} px)",
    "Grande ({size} px)": "Large ({size} px)",
    "No se pudo cargar: {error}": "Could not load: {error}",
    "No se pudo leer la carpeta: {error}": "Could not read the folder: {error}",
    "Video para {name}": "Video for {name}",
    "{name}: usa el video general.": "{name}: uses the general video.",
    "Fondo activo: {name}": "Active wallpaper: {name}",
    "{name}: resolución automática.": "{name}: automatic resolution.",
    "No se pudo cambiar {name} a {choice}: {message}": "Could not change {name} to {choice}: {message}",
    "{name}: se revirtió a {mode}.": "{name}: reverted to {mode}.",
    "¿Mantener {mode} en {name}?": "Keep {mode} on {name}?",
    "Si no confirmas, se revertirá en {seconds} s.": "If you don't confirm, it will revert in {seconds} s.",
    "No se pudo cambiar el inicio automático: {error}": "Could not change autostart: {error}",
}


def detect_language():
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    choice = "auto"
    try:
        data = json.loads((config_home / "xalive" / "settings.json").read_text())
        if data.get("language") in LANGUAGES:
            choice = data["language"]
    except (OSError, ValueError, AttributeError):
        pass
    if choice != "auto":
        return choice
    code = os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG")
    if not code:
        code = (locale.getlocale()[0] or "")
    return "es" if code.lower().startswith("es") else "en"


LANGUAGE = detect_language()


def tr(text, **values):
    result = text if LANGUAGE == "es" else EN.get(text, text)
    return result.format(**values) if values else result
