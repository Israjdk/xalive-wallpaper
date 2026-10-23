import ctypes, ctypes.util

class XProps:
    """Minimal libX11 reader for window manager properties, without spawning xprop."""

    def __init__(self):
        lib = ctypes.util.find_library("X11")
        if lib is None:
            raise OSError("libX11 not found")
        x = self.x = ctypes.CDLL(lib)
        x.XOpenDisplay.restype = ctypes.c_void_p
        x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x.XDefaultRootWindow.restype = ctypes.c_ulong
        x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x.XInternAtom.restype = ctypes.c_ulong
        x.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        x.XGetAtomName.restype = ctypes.c_void_p
        x.XGetAtomName.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        x.XFree.argtypes = [ctypes.c_void_p]
        x.XGetWindowProperty.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_long,
            ctypes.c_long, ctypes.c_int, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        x.XSetErrorHandler.restype = ctypes.c_void_p
        # Windows can vanish between listing and querying; ignore those X errors.
        self._handler = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)(
            lambda *_: 0
        )
        x.XSetErrorHandler(self._handler)
        self.display = x.XOpenDisplay(None)
        if not self.display:
            raise OSError("cannot open X display")
        self.root = x.XDefaultRootWindow(self.display)
        self.atoms = {}
        self.names = {}

    def atom(self, name):
        value = self.atoms.get(name)
        if value is None:
            value = self.atoms[name] = self.x.XInternAtom(self.display, name.encode(), 0)
            self.names[value] = name
        return value

    def get(self, window, name, kind="cardinal"):
        # Returns a list of ints, atom names, or (for kind="text") a list of strings.
        x = self.x
        actual, fmt = ctypes.c_ulong(), ctypes.c_int()
        count, after = ctypes.c_ulong(), ctypes.c_ulong()
        data = ctypes.c_void_p()
        status = x.XGetWindowProperty(
            self.display, window, self.atom(name), 0, 4096, 0, 0,
            ctypes.byref(actual), ctypes.byref(fmt), ctypes.byref(count),
            ctypes.byref(after), ctypes.byref(data),
        )
        if status != 0 or not data.value or fmt.value == 0:
            return []
        try:
            n = count.value
            if fmt.value == 32:
                values = ctypes.cast(data, ctypes.POINTER(ctypes.c_long))[:n]
                values = [v & 0xFFFFFFFF for v in values]
                if kind == "atom":
                    return [self.atom_name(v) for v in values]
                return values
            if fmt.value == 8:
                raw = ctypes.string_at(data.value, n)
                return [s.decode("utf-8", "replace") for s in raw.split(b"\0") if s]
            return []
        finally:
            x.XFree(data)

    def atom_name(self, value):
        name = self.names.get(value)
        if name is None:
            ptr = self.x.XGetAtomName(self.display, value)
            name = ctypes.string_at(ptr).decode() if ptr else ""
            if ptr:
                self.x.XFree(ptr)
            self.names[value] = name
        return name
