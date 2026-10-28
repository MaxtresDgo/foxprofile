"""The profile's number on its browser windows' taskbar icon (Windows), like AdsPower:
FoxProfile's icon with the number across the bottom.

The runner finds the windows of its own browser (the processes started under it)
and sets their icon with WM_SETICON, again every few seconds: Chrome may set its
own back, and windows opened later get it too. Pillow comes with the Windows and
macOS installs (the agent's tray icon); without it, or off Windows, nothing happens.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import os
import pathlib
import sys
import tempfile
from ctypes import wintypes

_ASSETS = pathlib.Path(__file__).parents[2] / "assets"
# The panel's "live" violet, like the toolbar badge (label.py).
_PLATE = (91, 63, 168, 255)
_SIZES = (16, 24, 32, 48, 64, 256)
_EVERY = 3.0


def draw(number: str, size: int):
    """The icon at one size: FoxProfile's, with the number on a plate across the bottom."""
    from PIL import Image, ImageDraw, ImageFont

    icon = Image.open(_ASSETS / "icon.png").convert("RGBA").resize((size, size), Image.LANCZOS)
    plate = round(size * 0.48)
    draw = ImageDraw.Draw(icon)
    draw.rounded_rectangle(
        (0, size - plate, size - 1, size - 1), radius=max(1, size // 8), fill=_PLATE
    )
    # The largest bold type that fits the plate.
    font_file = str(_ASSETS / "fonts" / "BeVietnamPro-Bold.ttf")
    points = plate
    while points > 4:
        font = ImageFont.truetype(font_file, points)
        left, top, right, bottom = draw.textbbox((0, 0), number, font=font)
        if right - left <= size * 0.94 and bottom - top <= plate * 0.8:
            break
        points -= 1
    x = (size - (right - left)) / 2 - left
    y = size - plate + (plate - (bottom - top)) / 2 - top
    draw.text((x, y), number, font=font, fill=(255, 255, 255, 255))
    return icon


def icon_file(number: str, name: str) -> str:
    """The .ico with every size, in the temp dir (one per profile, rewritten)."""
    images = [draw(number, size) for size in _SIZES]
    path = pathlib.Path(tempfile.gettempdir(), "foxprofile-label", name, "taskbar.ico")
    path.parent.mkdir(parents=True, exist_ok=True)
    images[-1].save(path, format="ICO", sizes=[(s, s) for s in _SIZES], append_images=images)
    return str(path)


class _Entry(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def _descendants(root: int) -> set[int]:
    """Every process started under `root`: the browser (through Playwright's driver)."""
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    children: dict[int, list[int]] = {}
    entry = _Entry(dwSize=ctypes.sizeof(_Entry))
    try:
        more = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while more:
            children.setdefault(entry.th32ParentProcessID, []).append(entry.th32ProcessID)
            more = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    found, todo = set(), [root]
    while todo:
        for pid in children.get(todo.pop(), ()):
            if pid not in found and pid != root:
                found.add(pid)
                todo.append(pid)
    return found


def _windows(pids: set[int]) -> list[int]:
    """Their visible top-level windows: the ones with a taskbar button."""
    user32 = ctypes.windll.user32
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        tool = user32.GetWindowLongW(hwnd, -20) & 0x80  # WS_EX_TOOLWINDOW
        owned = user32.GetWindow(hwnd, 4)  # GW_OWNER
        if pid.value in pids and user32.IsWindowVisible(hwnd) and not tool and not owned:
            found.append(hwnd)
        return True

    user32.EnumWindows(visit, 0)
    return found


def _stamp(icons: tuple[int, int]) -> None:
    user32 = ctypes.windll.user32
    user32.SendMessageTimeoutW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
    ]  # fmt: skip
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    for hwnd in _windows(_descendants(os.getpid())):
        current = ctypes.c_size_t()
        # WM_GETICON, ICON_BIG; a hung window is skipped this round.
        user32.SendMessageTimeoutW(hwnd, 0x7F, 1, 0, 0x2, 200, ctypes.byref(current))
        if current.value != icons[1]:
            user32.PostMessageW(hwnd, 0x80, 0, icons[0])  # WM_SETICON, ICON_SMALL
            user32.PostMessageW(hwnd, 0x80, 1, icons[1])  # ICON_BIG


def _load(path: str) -> tuple[int, int]:
    user32 = ctypes.windll.user32
    user32.LoadImageW.restype = wintypes.HANDLE
    small = user32.GetSystemMetrics(49)  # SM_CXSMICON
    # The taskbar shows ICON_BIG scaled to its own size: give it more pixels.
    return tuple(user32.LoadImageW(None, path, 1, s, s, 0x10) for s in (small, 64))


async def keep_stamped(number: str, name: str) -> None:
    """Until the runner ends: its browser windows show `number` on the taskbar."""
    try:
        icons = _load(await asyncio.to_thread(icon_file, number, name))
    except Exception:  # no Pillow, no font: the badge in the toolbar still says it
        return
    while True:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(_stamp, icons)
        await asyncio.sleep(_EVERY)


_task: asyncio.Task | None = None


def start(number: str, name: str) -> None:
    """keep_stamped() in the background, on Windows and with a number only."""
    global _task
    if sys.platform == "win32" and number and _task is None:
        _task = asyncio.ensure_future(keep_stamped(number, name))
