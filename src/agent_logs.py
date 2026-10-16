"""The agent's log in a window (tray menu > Show logs).

    python -m src.agent_logs <log file> [language]

Its own process: on macOS the tray owns the main thread, and Tk needs it too.
Shows the end of the file and follows what the agent appends.
"""

import os
import pathlib
import sys
import tkinter as tk

from src.core.strings import get_string, set_language
from src.ui.theme.colors import COLORS

ICON = pathlib.Path(__file__).resolve().parent / "assets" / "icon.png"
TAIL = 256 * 1024  # bytes shown on open
POLL_MS = 500


def read_new(path: str, pos: int) -> tuple[str, int]:
    """Text appended to `path` since `pos`, and the new position. A file that
    shrank (rewritten) is read again from the start."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return "", pos
    if size < pos:
        pos = 0
    start = pos
    if pos == 0 and size > TAIL:
        start = size - TAIL
    if size == start:
        return "", start
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read()
    end = start + len(data)
    if start > pos:  # opened mid-file: begin at a whole line
        data = data[data.find(b"\n") + 1 :]
    return data.decode("utf-8", "replace"), end


def main() -> None:
    path = sys.argv[1]
    if len(sys.argv) > 2:
        set_language(sys.argv[2])
    root = tk.Tk()
    root.title(get_string("agent_logs_title"))
    root.geometry("960x560")
    root.configure(background=COLORS["log_bg"])
    if ICON.exists():
        root.iconphoto(True, tk.PhotoImage(file=str(ICON)))

    bar = tk.Scrollbar(root)
    bar.pack(side=tk.RIGHT, fill=tk.Y)
    text = tk.Text(
        root,
        wrap=tk.NONE,
        font="TkFixedFont",
        background=COLORS["log_bg"],
        foreground=COLORS["cover_ink"],
        insertbackground=COLORS["cover_ink"],
        selectbackground=COLORS["cover_line"],
        borderwidth=0,
        highlightthickness=0,
        padx=10,
        pady=8,
        yscrollcommand=bar.set,
    )
    text.pack(fill=tk.BOTH, expand=True)
    bar.configure(command=text.yview)
    # Read-only but selectable and copyable.
    text.bind("<Key>", lambda e: None if (e.state & 0x4 or e.state & 0x8) else "break")

    pos = 0

    def poll() -> None:
        nonlocal pos
        new, pos = read_new(path, pos)
        if new:
            at_end = text.yview()[1] >= 0.999  # keep following unless scrolled up
            text.insert(tk.END, new)
            if at_end:
                text.see(tk.END)
        root.after(POLL_MS, poll)

    poll()
    root.lift()
    root.focus_force()
    root.mainloop()


if __name__ == "__main__":
    main()
