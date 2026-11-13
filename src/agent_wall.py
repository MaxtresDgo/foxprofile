"""The browser wall: every browser this PC runs, live and usable, in one window (Windows).

    python -m src.agent_wall [language]

The agent starts it (tray menu > Browser wall, agent.BrowserWall) and talks to it
in JSON lines. On stdin: {"browsers": {"<profile>": <runner pid>}} whenever its
browsers change, {"show": true} to bring the window back to the front, and
{"reply": id, "ok", "result" | "error"} for each action asked on stdout as
{"id", "name", "action", "params"}. The end of stdin (the agent quit) closes it.

What a tile shows is a DWM thumbnail of the page area of the profile's real
browser window: Windows itself draws it there, scaled, as it does for the taskbar
previews. Nothing is captured, encoded or sent, so it is as live as the window,
video included, and costs next to nothing. What a tile does goes the other way:
a click, a long press, the wheel, typing, the address bar, back, reload and the
tabs become page actions (control.py) that the agent runs on that browser.

Its own process, like src/agent_logs.py: Tk needs the main thread, which the tray owns.
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import sys
import threading
import time
from collections.abc import Callable, Iterable

from src.core.strings import get_string, set_language
from src.ui.theme.colors import COLORS

ICON = pathlib.Path(__file__).resolve().parent / "assets" / "icon.png"
TICK_MS = 1000  # windows looked up again, tiles placed again
POLL_S = 1.5  # a visible tile's address, title, tabs and page size
TILE_SIZES = (220, 280, 340, 420, 520, 640)  # tile widths (px at 96 DPI) for - and +
GAP = 12  # px at 96 DPI
MIN_WINDOW = 120  # px: smaller top-level windows are popups, not the browser
HOLD_MS = 400  # a press held this long is a long press (Facebook's reactions)
DRAG_PX = 8  # a press that moves this far is a drag: it scrolls the page
TYPE_MS = 40  # characters typed within this are sent together
RESHAPE = 0.1  # a window's page area changes shape this much before its tile does

# Tk keysyms -> Playwright key names (press).
KEYS = {
    "Return": "Enter", "KP_Enter": "Enter", "BackSpace": "Backspace", "Tab": "Tab",
    "Escape": "Escape", "Delete": "Delete", "Insert": "Insert", "Home": "Home", "End": "End",
    "Prior": "PageUp", "Next": "PageDown", "Left": "ArrowLeft", "Right": "ArrowRight",
    "Up": "ArrowUp", "Down": "ArrowDown", **{f"F{i}": f"F{i}" for i in range(1, 13)},
}  # fmt: skip
MODIFIER_KEYS = ("Shift", "Control", "Alt", "Meta", "Super", "Caps_Lock", "Num_Lock", "Win")
SHIFT, CONTROL, ALT = 0x1, 0x4, 0x20000  # Tk's event.state on Windows


# --- Pure parts (tested on every OS) ---------------------------------------------


def descendants(roots: Iterable[int], parents: dict[int, int]) -> dict[int, int]:
    """Every process below each root: {pid: root}. `parents` maps pid -> parent pid.

    A runner starts Playwright's driver, which starts the browser and its own
    processes: the window belongs to one of them, a few levels down."""
    children: dict[int, list[int]] = {}
    for pid, parent in parents.items():
        if pid != parent:
            children.setdefault(parent, []).append(pid)
    owner: dict[int, int] = {}
    for root in roots:
        todo = [root]
        while todo:
            pid = todo.pop()
            if pid in owner:
                continue
            owner[pid] = root
            todo.extend(children.get(pid, ()))
    return owner


def match(
    browsers: dict[str, int], parents: dict[int, int], windows: list[tuple[int, int]]
) -> dict[str, int]:
    """The window shown for each profile: {name: hwnd}.

    `windows` are (hwnd, pid) in z-order, front first, so a profile with several
    windows shows the one used last."""
    owner = descendants(browsers.values(), parents)
    by_root: dict[int, int] = {}
    for hwnd, pid in windows:
        root = owner.get(pid)
        if root is not None and root not in by_root:
            by_root[root] = hwnd
    return {name: by_root[pid] for name, pid in browsers.items() if pid in by_root}


def fit(width: float, height: float, box: tuple[float, float, float, float]) -> tuple[int, ...]:
    """The largest `width` x `height` rectangle centred in `box` (left, top, w, h),
    as (left, top, right, bottom)."""
    left, top, bw, bh = box
    if width <= 0 or height <= 0:
        width, height = bw, bh
    scale = min(bw / width, bh / height)
    w, h = width * scale, height * scale
    x, y = left + (bw - w) / 2, top + (bh - h) / 2
    return round(x), round(y), round(x + w), round(y + h)


def clip(
    dest: tuple[int, int, int, int], view: tuple[int, int, int, int], source: tuple[int, ...]
) -> tuple[tuple[int, ...], tuple[int, ...]] | None:
    """The part of `dest` inside `view` and the part of `source` it shows, all
    (left, top, right, bottom); None when none of it is in view. A thumbnail
    is drawn over everything, so a tile scrolled half out must be cut."""
    left, top = max(dest[0], view[0]), max(dest[1], view[1])
    right, bottom = min(dest[2], view[2]), min(dest[3], view[3])
    if right <= left or bottom <= top:
        return None
    dw, dh = dest[2] - dest[0], dest[3] - dest[1]
    sw, sh = source[2] - source[0], source[3] - source[1]

    def sx(x: int) -> int:
        return round(source[0] + (x - dest[0]) * sw / dw)

    def sy(y: int) -> int:
        return round(source[1] + (y - dest[1]) * sh / dh)

    return (left, top, right, bottom), (sx(left), sy(top), sx(right), sy(bottom))


def to_page(
    x: float, y: float, shown: tuple[int, ...], page: tuple[float, float]
) -> tuple[float, float]:
    """A point on the tile's picture (`shown`: left, top, right, bottom) as page
    coordinates in CSS pixels, for a page of `page` (width, height)."""
    w, h = shown[2] - shown[0], shown[3] - shown[1]
    px = min(max((x - shown[0]) / w, 0), 1) * page[0]
    py = min(max((y - shown[1]) / h, 0), 1) * page[1]
    return round(px, 1), round(py, 1)


def key_action(keysym: str, char: str, state: int) -> tuple[str, str] | None:
    """What a key pressed on a tile does: ("type", text), ("press", key),
    ("paste", "") or None (a modifier on its own)."""
    if keysym.startswith(MODIFIER_KEYS):
        return None
    ctrl, alt, shift = bool(state & CONTROL), bool(state & ALT), bool(state & SHIFT)
    # AltGr arrives as Control+Alt with the character it types (@ on Spanish keyboards).
    if char and len(char) == 1 and char.isprintable() and (not ctrl or alt):
        return "type", char
    if keysym in KEYS:
        name = KEYS[keysym]
    elif len(keysym) == 1:
        name = keysym.lower() if ctrl else keysym
    else:
        return None
    if ctrl and name == "v" and not alt and not shift:
        return "paste", ""
    mods = [m for m, on in (("Control", ctrl), ("Alt", alt and not ctrl), ("Shift", shift)) if on]
    if not mods and len(name) == 1:
        return "type", name
    return "press", "+".join([*mods, name])


def common_shape(shape: float | None, shapes: Iterable[float | None]) -> float:
    """The height / width every tile of this kind gets: the tallest phone among
    the phones (each phone persona is another model, its window another shape),
    the tallest desktop among the desktops. Pictures fill the width, top-aligned."""
    if shape is None:
        return 10 / 16
    phone = shape >= 1
    return max(s for s in shapes if s is not None and (s >= 1) == phone)


def columns(width: float, tile: float, gap: float) -> int:
    """How many tiles of `tile` px fit side by side."""
    return max(1, int((width - gap) // (tile + gap)))


# --- Windows ------------------------------------------------------------------------

if os.name == "nt":
    import ctypes
    import ctypes.wintypes as wt

    from src.services.browser.taskbar import _Entry as PROCESSENTRY32W  # Toolhelp32

    # Own instances: the argtypes set below must not change ctypes.windll's
    # functions, which other modules (taskbar.py) call with their own types.
    user32 = ctypes.WinDLL("user32")
    kernel32 = ctypes.WinDLL("kernel32")
    dwmapi = ctypes.WinDLL("dwmapi")

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]  # fmt: skip

    class THUMB_PROPS(ctypes.Structure):
        _fields_ = [
            ("dwFlags", wt.DWORD),
            ("rcDestination", RECT),
            ("rcSource", RECT),
            ("opacity", ctypes.c_ubyte),
            ("fVisible", wt.BOOL),
            ("fSourceClientAreaOnly", wt.BOOL),
        ]

    HTHUMBNAIL = ctypes.c_void_p
    ENUM_PROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
    kernel32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.CloseHandle.argtypes = [wt.HANDLE]
    user32.EnumWindows.argtypes = [ENUM_PROC, wt.LPARAM]
    user32.EnumChildWindows.argtypes = [wt.HWND, ENUM_PROC, wt.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user32.IsWindowVisible.argtypes = [wt.HWND]
    user32.IsWindow.argtypes = [wt.HWND]
    user32.IsIconic.argtypes = [wt.HWND]
    user32.GetWindow.argtypes = [wt.HWND, ctypes.c_uint]
    user32.GetWindow.restype = wt.HWND
    user32.GetParent.argtypes = [wt.HWND]
    user32.GetParent.restype = wt.HWND
    user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    user32.GetWindowTextLengthW.argtypes = [wt.HWND]
    user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
    user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(RECT)]
    user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(RECT)]
    user32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
    user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [wt.HWND]
    user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_uint]  # fmt: skip
    dwmapi.DwmRegisterThumbnail.argtypes = [wt.HWND, wt.HWND, ctypes.POINTER(HTHUMBNAIL)]
    dwmapi.DwmUnregisterThumbnail.argtypes = [HTHUMBNAIL]
    dwmapi.DwmUpdateThumbnailProperties.argtypes = [HTHUMBNAIL, ctypes.POINTER(THUMB_PROPS)]
    dwmapi.DwmGetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]

    GW_OWNER, GWL_EXSTYLE, WS_EX_TOOLWINDOW, DWMWA_CLOAKED = 4, -20, 0x80, 14
    SW_RESTORE, SW_SHOWNOACTIVATE = 9, 4
    HWND_BOTTOM, SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 1, 0x1, 0x2, 0x10
    # rcDestination | rcSource | opacity | visible | client area only
    THUMB_FLAGS = 0x1 | 0x2 | 0x4 | 0x8 | 0x10

    def process_parents() -> dict[int, int]:
        snap = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
        if not snap or snap == wt.HANDLE(-1).value:
            return {}
        parents: dict[int, int] = {}
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                parents[entry.th32ProcessID] = entry.th32ParentProcessID
                ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snap)
        return parents

    def top_windows() -> list[tuple[int, int]]:
        """Visible top-level windows that could be a browser: (hwnd, pid), front first."""
        found: list[tuple[int, int]] = []

        def visit(hwnd, _):
            if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, GW_OWNER):
                return True
            if not user32.GetWindowTextLengthW(hwnd):
                return True
            if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
                return True
            cloaked = wt.DWORD()
            dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), 4)
            if cloaked.value:
                return True
            if not user32.IsIconic(hwnd):
                r = RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(r))
                if r.right - r.left < MIN_WINDOW or r.bottom - r.top < MIN_WINDOW:
                    return True
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            found.append((hwnd, pid.value))
            return True

        user32.EnumWindows(ENUM_PROC(visit), 0)
        return found

    def page_area(hwnd: int, page: tuple[float, float] | None) -> tuple[int, int, int, int]:
        """Where the page is drawn in the window's client area (left, top, right,
        bottom): below the tabs and the address bar. Chromium draws it in a child
        window of its own; otherwise (Camoufox) it is the bottom of the client
        area, as wide as it and as tall as the page's shape gives."""
        client = RECT()
        user32.GetClientRect(hwnd, ctypes.byref(client))
        origin = wt.POINT(0, 0)
        user32.ClientToScreen(hwnd, ctypes.byref(origin))
        best: list[tuple[int, int, int, int]] = []

        def visit(child, _):
            name = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(child, name, 64)
            if name.value == "Chrome_RenderWidgetHostHWND" and user32.IsWindowVisible(child):
                r = RECT()
                user32.GetWindowRect(child, ctypes.byref(r))
                area = (
                    r.left - origin.x,
                    r.top - origin.y,
                    r.right - origin.x,
                    r.bottom - origin.y,
                )
                if area[2] > area[0] and area[3] > area[1]:
                    best.append(area)
            return True

        user32.EnumChildWindows(hwnd, ENUM_PROC(visit), 0)
        # Two of them: the browser's own bars (from the top) and the page, below
        # them. A phone's page is also narrower than its window.
        below = [a for a in best if a[1] > 0] or best
        if below:
            return max(below, key=lambda a: (a[2] - a[0]) * (a[3] - a[1]))
        width, height = client.right, client.bottom
        if page and page[0] > 0:
            tall = min(height, round(width * page[1] / page[0]))
            return 0, height - tall, width, height
        return 0, 0, width, height

    def bring_to_front(hwnd: int) -> None:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        # Allowed: the click on the wall made this process the foreground one.
        user32.SetForegroundWindow(hwnd)

    def restore_behind(hwnd: int) -> None:
        """A minimized browser has no picture: restore it without bringing it
        over the wall."""
        user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
        flags = SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
        user32.SetWindowPos(hwnd, HWND_BOTTOM, 0, 0, 0, 0, flags)

    class Thumbnail:
        """One window drawn into the wall by DWM."""

        def __init__(self, host: int, source: int) -> None:
            self.source = source
            self.handle = HTHUMBNAIL()
            hr = dwmapi.DwmRegisterThumbnail(host, source, ctypes.byref(self.handle))
            if hr != 0:
                raise OSError(f"DwmRegisterThumbnail failed: {hr & 0xFFFFFFFF:#x}")
            self.shown: tuple | None = None

        def place(self, dest: tuple[int, ...] | None, source: tuple[int, ...] | None) -> None:
            if (dest, source) == self.shown:
                return
            props = THUMB_PROPS()
            props.dwFlags = THUMB_FLAGS
            props.opacity = 255
            props.fSourceClientAreaOnly = True
            props.fVisible = dest is not None
            if dest is not None and source is not None:
                props.rcDestination = RECT(*dest)
                props.rcSource = RECT(*source)
            dwmapi.DwmUpdateThumbnailProperties(self.handle, ctypes.byref(props))
            self.shown = (dest, source)

        def close(self) -> None:
            dwmapi.DwmUnregisterThumbnail(self.handle)


def _dpi_aware() -> None:
    """Physical pixels for Tk, the same ones DWM places thumbnails in."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per monitor
    except Exception:
        with contextlib.suppress(Exception):
            ctypes.windll.user32.SetProcessDPIAware()


class Agent:
    """The page actions, asked on stdout and answered on stdin (agent.BrowserWall)."""

    def __init__(self, root) -> None:
        self.root = root
        self.seq = 0
        self.waiting: dict[int, Callable[[bool, object], None]] = {}
        self.lock = threading.Lock()

    def ask(
        self, name: str, action: str, params: dict, done: Callable[[bool, object], None]
    ) -> None:
        with self.lock:
            self.seq += 1
            self.waiting[self.seq] = done
            line = json.dumps({"id": self.seq, "name": name, "action": action, "params": params})
            try:
                sys.stdout.write(line + "\n")
                sys.stdout.flush()
            except (OSError, ValueError):
                self.waiting.pop(self.seq, None)

    def answered(self, msg: dict) -> None:
        with self.lock:
            done = self.waiting.pop(msg.get("reply"), None)
        if done is not None:
            ok = bool(msg.get("ok"))
            self.root.after(0, done, ok, msg.get("result") if ok else msg.get("error"))


class Tile:
    """One browser: its name, tabs, address bar and its live page."""

    def __init__(self, wall: Wall, name: str) -> None:
        tk, s = wall.tk, wall.scale
        self.wall, self.name = wall, name
        self.hwnd: int | None = None
        self.thumb: Thumbnail | None = None
        self.page: tuple[float, float] | None = None  # CSS px of the active tab
        self.area: tuple[int, int, int, int] | None = None  # page in the window
        self.shape: float | None = None  # its height / width, as the tile is sized
        self.shown: tuple[int, ...] | None = None  # the picture in the screen frame
        self.queue: list[tuple[str, dict, Callable | None]] = []
        self.busy = False
        self.polling = 0  # poll() answers still to come
        self.polled = 0.0
        self.typed, self.type_timer = "", None
        self.wheel, self.wheel_at, self.wheel_timer = 0.0, (0.0, 0.0), None
        self.down: tuple[float, float, float] | None = None
        self.error_timer = None

        bg, ink, muted = COLORS["cover_deep"], COLORS["cover_ink"], COLORS["cover_muted"]
        self.frame = tk.Frame(
            wall.grid,
            background=bg,
            highlightthickness=max(1, round(2 * s)),
            highlightbackground=COLORS["cover_line"],
            highlightcolor=COLORS["accent"],
        )
        head = tk.Frame(self.frame, background=bg)
        head.pack(fill=tk.X, padx=round(8 * s), pady=(round(6 * s), 0))
        tk.Label(
            head, text=name, font=("Segoe UI Semibold", 10), background=bg, foreground=ink
        ).pack(side=tk.LEFT)
        self.button(head, "✕", self.close_browser).pack(side=tk.RIGHT)
        self.button(head, "⤢", self.open_window).pack(side=tk.RIGHT)
        self.status = tk.Label(
            head, font=("Segoe UI", 9), background=bg, foreground=muted, anchor="e", width=1
        )
        self.status.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=(8, 4))

        # Fixed size: long tab titles must not widen the tile and move the others.
        self.tabs = tk.Frame(self.frame, background=bg, height=round(26 * s))
        self.tabs.pack_propagate(False)
        self.tabs.pack(fill=tk.X, padx=round(8 * s), pady=(round(4 * s), 0))
        self.tab_list: list[dict] = []

        nav = tk.Frame(self.frame, background=bg)
        nav.pack(fill=tk.X, padx=round(8 * s), pady=round(4 * s))
        self.button(nav, "←", lambda: self.act("back")).pack(side=tk.LEFT)
        self.button(nav, "⟳", lambda: self.act("reload")).pack(side=tk.LEFT, padx=(2, 4))
        self.url = tk.Entry(
            nav,
            width=1,
            font=("Consolas", 9),
            background=COLORS["log_bg"],
            foreground=ink,
            insertbackground=ink,
            relief=tk.FLAT,
            highlightthickness=1,
            highlightbackground=COLORS["cover_line"],
            highlightcolor=COLORS["accent"],
        )
        self.url.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=round(3 * s))
        self.url.bind("<Return>", self.go)

        self.screen = tk.Frame(self.frame, background=COLORS["log_bg"], takefocus=1, cursor="arrow")
        self.screen.pack()
        self.note = tk.Label(
            self.screen,
            font=("Segoe UI", 9),
            background=COLORS["log_bg"],
            foreground=muted,
            justify="center",
        )
        self.note.place(relx=0.5, rely=0.5, anchor="center")
        for widget in (self.screen, self.note):
            widget.bind("<ButtonPress-1>", self.press)
            widget.bind("<ButtonRelease-1>", self.release)
            widget.bind("<MouseWheel>", self.on_wheel)
        self.screen.bind("<Key>", self.key)
        self.screen.bind("<FocusOut>", lambda e: self.flush())

    def button(self, parent, text: str, command):
        tk = self.wall.tk
        return tk.Button(
            parent,
            text=text,
            command=command,
            font=("Segoe UI Symbol", 9),
            background=COLORS["cover_deep"],
            foreground=COLORS["cover_ink"],
            activebackground=COLORS["cover_line"],
            activeforeground=COLORS["cover_ink"],
            relief=tk.FLAT,
            borderwidth=0,
            padx=round(6 * self.wall.scale),
            cursor="hand2",
        )

    # --- actions, one at a time in order ------------------------------------------

    def act(self, action: str, params: dict | None = None, done: Callable | None = None) -> None:
        """An action of the operator's, after the ones before it (typing stays in order)."""
        self.queue.append((action, params or {}, done))
        self.next()

    def next(self) -> None:
        if self.busy or not self.queue:
            return
        self.busy = True
        action, params, done = self.queue.pop(0)

        def answered(ok: bool, result) -> None:
            self.busy = False
            if not ok:
                self.error(str(result))
            if done is not None:
                done(ok, result)
            self.polled = 0  # look again soon: the page or the tabs changed
            self.next()

        self.wall.agent.ask(self.name, action, params, answered)

    def poll(self) -> None:
        """The address, page size and tabs, beside the operator's actions, never
        in front of them: reading a background tab's title can take seconds."""
        if self.polling or time.monotonic() - self.polled < POLL_S:
            return
        self.polling = 2
        self.polled = time.monotonic()

        def answered(handler):
            def done(ok: bool, result) -> None:
                self.polling -= 1
                handler(ok, result)

            return done

        self.wall.agent.ask(self.name, "viewport", {}, answered(self.got_viewport))
        self.wall.agent.ask(self.name, "tabs", {}, answered(self.got_tabs))

    def got_viewport(self, ok: bool, result) -> None:
        if not ok or not isinstance(result, dict):
            return
        page = (float(result.get("width") or 0), float(result.get("height") or 0))
        if page[0] > 0 and page[1] > 0:
            self.page = page
        if self.wall.root.focus_get() is not self.url:
            url = result.get("url") or ""
            if self.url.get() != url:
                self.url.delete(0, "end")
                self.url.insert(0, url)

    def got_tabs(self, ok: bool, result) -> None:
        if not ok or not isinstance(result, dict):
            return
        tabs = result.get("tabs") or []
        if tabs == self.tab_list:
            return
        self.tab_list = tabs
        tk, s = self.wall.tk, self.wall.scale
        for child in self.tabs.winfo_children():
            child.destroy()
        for tab in tabs:
            title = (tab.get("title") or tab.get("url") or "")[:18]
            active = bool(tab.get("active"))
            bg = COLORS["cover_line"] if active else COLORS["cover_deep"]
            box = tk.Frame(self.tabs, background=bg)
            box.pack(side=tk.LEFT, padx=(0, round(2 * s)))
            pick = self.button(
                box, title, lambda i=tab["index"]: self.act("tab_select", {"index": i})
            )
            pick.configure(
                font=("Segoe UI Semibold" if active else "Segoe UI", 8), anchor="w", background=bg
            )
            pick.pack(side=tk.LEFT)
            # The last tab stays: closing it would close the profile's browser.
            if len(tabs) > 1:
                close = self.button(
                    box, "×", lambda i=tab["index"]: self.act("tab_close", {"index": i})
                )
                close.configure(font=("Segoe UI", 8), background=bg, padx=round(3 * s))
                close.pack(side=tk.LEFT)
        self.button(self.tabs, "+", lambda: self.act("tab_new", {"url": None})).pack(side=tk.LEFT)

    def error(self, text: str) -> None:
        self.status.configure(text=" ".join(text.split())[:80], foreground=COLORS["accent"])
        if self.error_timer:
            self.wall.root.after_cancel(self.error_timer)
        self.error_timer = self.wall.root.after(
            5000, lambda: self.status.configure(text="", foreground=COLORS["cover_muted"])
        )

    # --- input ------------------------------------------------------------------

    def go(self, _event=None) -> str:
        url = self.url.get().strip()
        if url:
            self.act("navigate", {"url": url, "wait_until": "domcontentloaded"})
            self.screen.focus_set()
        return "break"

    def press(self, event) -> str:
        self.screen.focus_set()
        self.wall.pick(self)
        if self.hwnd and user32.IsIconic(self.hwnd):
            restore_behind(self.hwnd)
            self.wall.root.after(300, self.wall.refresh)
            return "break"
        self.down = (event.x_root, event.y_root, time.monotonic())
        return "break"

    def release(self, event) -> str:
        down, self.down = self.down, None
        if down is None:
            return "break"
        start = self.point_at(down[0], down[1])
        if start is None:
            return "break"
        moved = abs(event.y_root - down[1]) + abs(event.x_root - down[0])
        if moved >= DRAG_PX * self.wall.scale and self.shown and self.page:
            # A drag scrolls, as a finger does: the page follows the pointer.
            scale = self.page[1] / max(1, self.shown[3] - self.shown[1])
            self.act(
                "wheel",
                {"x": start[0], "y": start[1], "dy": round((down[1] - event.y_root) * scale)},
            )
            return "break"
        held = (time.monotonic() - down[2]) * 1000
        params = {"x": start[0], "y": start[1]}
        if held >= HOLD_MS:
            params["hold"] = min(round(held), 3000)
        self.flush()
        self.act("click_at", params)
        return "break"

    def point_at(self, x_root: float, y_root: float) -> tuple[float, float] | None:
        if not self.shown or not self.page:
            return None
        x, y = x_root - self.screen.winfo_rootx(), y_root - self.screen.winfo_rooty()
        return to_page(x, y, self.shown, self.page)

    def on_wheel(self, event) -> str:
        at = self.point_at(event.x_root, event.y_root)
        if at is None:
            return "break"
        self.wheel -= event.delta / 120 * 100
        self.wheel_at = at
        if self.wheel_timer is None:
            self.wheel_timer = self.wall.root.after(60, self.send_wheel)
        return "break"

    def send_wheel(self) -> None:
        self.wheel_timer = None
        dy, self.wheel = round(self.wheel), 0.0
        if dy:
            self.act("wheel", {"x": self.wheel_at[0], "y": self.wheel_at[1], "dy": dy})

    def key(self, event) -> str:
        what = key_action(event.keysym, event.char, event.state)
        if what is None:
            return "break"
        kind, value = what
        if kind == "type":
            self.typed += value
            if self.type_timer is None:
                self.type_timer = self.wall.root.after(TYPE_MS, self.flush)
        elif kind == "paste":
            with contextlib.suppress(Exception):
                self.typed += self.wall.root.clipboard_get()
            self.flush()
        else:
            self.flush()
            self.act("press", {"key": value})
        return "break"

    def flush(self) -> None:
        if self.type_timer is not None:
            self.wall.root.after_cancel(self.type_timer)
            self.type_timer = None
        if self.typed:
            text, self.typed = self.typed, ""
            self.act("keyboard_type", {"text": text})

    def open_window(self) -> None:
        if self.hwnd and user32.IsWindow(self.hwnd):
            bring_to_front(self.hwnd)

    def close_browser(self) -> None:
        """Close the profile's browser, as Stop in the panel does: the agent
        uploads its data to the hub. The tile goes when the browser is gone."""
        from tkinter import messagebox

        if not messagebox.askyesno(
            get_string("agent_wall_close_title"),
            get_string("agent_wall_close_confirm", name=self.name),
            parent=self.wall.root,
        ):
            return
        self.status.configure(
            text=get_string("agent_wall_closing"), foreground=COLORS["cover_muted"]
        )
        self.act("stop")

    # --- picture ------------------------------------------------------------------

    def size(self, width: int, shape: float) -> None:
        """The screen frame: the tile's width, as tall as `shape` (common_shape):
        from the windows' page areas, not the pages' own size, which changes as a
        site shows or hides a bar and would make the tiles jump around."""
        height = min(round(width * shape), round(self.wall.max_height()))
        self.screen.configure(width=width, height=max(60, height))

    def place(self, view: tuple[int, int, int, int]) -> None:
        if self.thumb is None or self.hwnd is None:
            return
        minimized = bool(user32.IsIconic(self.hwnd))
        self.note.configure(text=get_string("agent_wall_minimized") if minimized else "")
        if minimized:
            self.thumb.place(None, None)
            return
        self.area = page_area(self.hwnd, self.page)
        w, h = self.screen.winfo_width(), self.screen.winfo_height()
        aw, ah = self.area[2] - self.area[0], self.area[3] - self.area[1]
        if w <= 1 or aw <= 0 or ah <= 0:
            return
        shape = ah / aw
        if self.shape is None or abs(shape - self.shape) > self.shape * RESHAPE:
            self.shape = shape  # the window was resized: the tile follows, once
            self.wall.root.after_idle(self.wall.relayout)
        left, top, right, bottom = fit(aw, ah, (0, 0, w, h))
        self.shown = (left, 0, right, bottom - top)  # top-aligned
        ox = self.screen.winfo_rootx() - self.wall.root.winfo_rootx()
        oy = self.screen.winfo_rooty() - self.wall.root.winfo_rooty()
        dest = (ox + self.shown[0], oy + self.shown[1], ox + self.shown[2], oy + self.shown[3])
        cut = clip(dest, view, self.area)
        self.thumb.place(*(cut if cut else (None, None)))


class Wall:
    def __init__(self, root) -> None:
        import tkinter as tk

        self.tk, self.root = tk, root
        self.scale = root.winfo_fpixels("1i") / 96
        self.agent = Agent(root)
        self.browsers: dict[str, int] = {}
        self.tiles: dict[str, Tile] = {}
        self.lock = threading.Lock()
        self.size_at = 2
        self.picked: Tile | None = None
        self.cols = 0

        root.title(get_string("agent_wall_title"))
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{int(sw * 0.8)}x{int(sh * 0.8)}+{int(sw * 0.1)}+{int(sh * 0.08)}")
        root.state("zoomed")
        root.configure(background=COLORS["log_bg"])
        if ICON.exists():
            root.iconphoto(True, tk.PhotoImage(file=str(ICON)))

        bg, ink, muted = COLORS["cover_deep"], COLORS["cover_ink"], COLORS["cover_muted"]
        bar = tk.Frame(root, background=bg)
        bar.pack(fill=tk.X)
        pad = round(10 * self.scale)
        tk.Label(
            bar,
            text=get_string("agent_wall_title"),
            font=("Segoe UI Semibold", 12),
            background=bg,
            foreground=ink,
        ).pack(side=tk.LEFT, padx=(round(16 * self.scale), 8), pady=pad)
        self.count = tk.Label(bar, font=("Segoe UI", 10), background=bg, foreground=muted)
        self.count.pack(side=tk.LEFT)
        sizes = tk.Frame(bar, background=bg)
        sizes.pack(side=tk.RIGHT, padx=round(16 * self.scale))
        tk.Label(
            sizes,
            text=get_string("agent_wall_size"),
            font=("Segoe UI", 10),
            background=bg,
            foreground=muted,
        ).pack(side=tk.LEFT, padx=(0, 6))
        for text, step in (("−", -1), ("+", 1)):
            tk.Button(
                sizes,
                text=text,
                command=lambda d=step: self.resize(d),
                font=("Segoe UI", 11),
                width=3,
                background=bg,
                foreground=ink,
                activebackground=COLORS["cover_line"],
                activeforeground=ink,
                relief=tk.FLAT,
                borderwidth=1,
                highlightthickness=0,
                cursor="hand2",
            ).pack(side=tk.LEFT, padx=2)
        tk.Label(
            bar,
            text=get_string("agent_wall_hint"),
            font=("Segoe UI", 10),
            background=bg,
            foreground=muted,
        ).pack(side=tk.RIGHT, padx=round(16 * self.scale))

        self.canvas = tk.Canvas(
            root, background=COLORS["log_bg"], highlightthickness=0, borderwidth=0
        )
        scroll = tk.Scrollbar(root, command=self.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.configure(yscrollcommand=scroll.set)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.grid = tk.Frame(self.canvas, background=COLORS["log_bg"])
        self.window = self.canvas.create_window(0, 0, window=self.grid, anchor="nw")
        self.empty = tk.Label(
            self.canvas,
            text=get_string("agent_wall_empty"),
            font=("Segoe UI", 11),
            background=COLORS["log_bg"],
            foreground=muted,
        )
        self.grid.bind("<Configure>", lambda e: self.after_layout())
        self.canvas.bind("<Configure>", lambda e: self.relayout())
        root.bind("<MouseWheel>", self.scroll_wall)
        root.bind("<Configure>", lambda e: self.place_all())
        root.update_idletasks()
        self.host = user32.GetParent(root.winfo_id()) or root.winfo_id()

    # --- stdin from the agent ---------------------------------------------------------

    def listen(self) -> None:
        for line in sys.stdin:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if "reply" in msg:
                self.agent.answered(msg)
            if isinstance(msg.get("browsers"), dict):
                with self.lock:
                    self.browsers = {str(k): int(v) for k, v in msg["browsers"].items()}
                self.root.after(0, self.refresh)
            if msg.get("show"):
                self.root.after(0, self.show)
        self.root.after(0, self.root.destroy)  # the agent is gone

    def show(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    # --- tiles ------------------------------------------------------------------------

    def refresh(self) -> None:
        with self.lock:
            browsers = dict(self.browsers)
        found = match(browsers, process_parents(), top_windows()) if browsers else {}
        for name in [n for n in self.tiles if n not in browsers]:
            tile = self.tiles.pop(name)
            if tile.thumb is not None:
                tile.thumb.close()
            tile.frame.destroy()
            if self.picked is tile:
                self.picked = None
        added = False
        for name in sorted(browsers, key=str.casefold):
            if name not in self.tiles:
                self.tiles[name] = Tile(self, name)
                added = True
        for name, tile in self.tiles.items():
            hwnd = found.get(name)
            if tile.thumb is not None and tile.thumb.source != hwnd:
                tile.thumb.close()
                tile.thumb = None
            tile.hwnd = hwnd
            if hwnd is not None and tile.thumb is None:
                with contextlib.suppress(OSError):
                    tile.thumb = Thumbnail(self.host, hwnd)
            if hwnd is None:
                tile.note.configure(text=get_string("agent_wall_no_window"))
        self.count.configure(text=get_string("agent_wall_count", n=len(self.tiles)))
        if added or not self.tiles:
            self.relayout()
        self.place_all()

    def tile_width(self) -> int:
        return round(TILE_SIZES[self.size_at] * self.scale)

    def max_height(self) -> float:
        return max(200, self.canvas.winfo_height() - 140 * self.scale)

    def resize(self, step: int) -> None:
        self.size_at = max(0, min(len(TILE_SIZES) - 1, self.size_at + step))
        self.relayout()

    def relayout(self) -> None:
        width = self.tile_width()
        gap = round(GAP * self.scale)
        cols = columns(self.canvas.winfo_width(), width + 4, gap)
        names = sorted(self.tiles, key=str.casefold)
        shapes = [t.shape for t in self.tiles.values()]
        for i, name in enumerate(names):
            tile = self.tiles[name]
            tile.size(width, common_shape(tile.shape, shapes))
            tile.frame.grid(
                row=i // cols, column=i % cols, padx=(gap, 0), pady=(gap, 0), sticky="n"
            )
        if names:
            self.empty.place_forget()
        else:
            self.empty.place(relx=0.5, rely=0.5, anchor="center")
        self.after_layout()

    def after_layout(self) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all") or (0, 0, 0, 0))
        self.root.after_idle(self.place_all)

    def yview(self, *args) -> None:
        self.canvas.yview(*args)
        self.place_all()

    def scroll_wall(self, event) -> None:
        self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        self.place_all()

    def place_all(self) -> None:
        left = self.canvas.winfo_rootx() - self.root.winfo_rootx()
        top = self.canvas.winfo_rooty() - self.root.winfo_rooty()
        view = (left, top, left + self.canvas.winfo_width(), top + self.canvas.winfo_height())
        for tile in self.tiles.values():
            tile.place(view)

    def pick(self, tile: Tile) -> None:
        self.picked = tile

    def tick(self) -> None:
        self.refresh()
        for tile in self.tiles.values():
            if tile.hwnd is not None:
                tile.poll()
        self.root.after(TICK_MS, self.tick)


def main() -> None:
    if os.name != "nt":
        sys.exit("The browser wall needs Windows (DWM thumbnails).")
    if len(sys.argv) > 1:
        set_language(sys.argv[1])
    _dpi_aware()
    import tkinter as tk

    root = tk.Tk()
    wall = Wall(root)
    threading.Thread(target=wall.listen, daemon=True).start()
    wall.tick()
    wall.show()
    root.mainloop()
    for tile in wall.tiles.values():
        if tile.thumb is not None:
            tile.thumb.close()


if __name__ == "__main__":
    main()
