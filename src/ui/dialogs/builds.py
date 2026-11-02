"""The Chrome engine builds this machine can run (chrome_engine/release.py), to
download ahead of the profiles that need them, like AdsPower's browser list."""

import threading
from collections.abc import Callable

import flet as ft

from ...core.strings import get_string
from ...services.browser import chrome_prefetch
from ..theme.colors import COLORS
from ..theme.styles import OUTLINE_STYLE


def open_builds_dialog(page: ft.Page, on_install: Callable[[str], None]) -> None:
    from chrome_engine import fetch, release

    rows = ft.Column(spacing=0, scroll=ft.ScrollMode.AUTO, height=360)
    stop = threading.Event()
    busy = [False]  # something was installing at the last draw

    def install(version: str) -> None:
        on_install(version)
        draw()

    def action(version: str, installed: bool, pct: int | None) -> ft.Control:
        if installed:
            return ft.Row(
                tight=True,
                controls=[
                    ft.Icon(ft.Icons.CHECK, size=16, color=COLORS["success"]),
                    ft.Text(get_string("build_installed"), color=COLORS["success"]),
                ],
            )
        if pct is not None:
            return ft.OutlinedButton(
                get_string("chrome_installing", pct="…" if pct < 0 else pct),
                disabled=True,
                style=OUTLINE_STYLE,
            )
        return ft.OutlinedButton(
            get_string("build_download"),
            icon=ft.Icons.DOWNLOAD,
            style=OUTLINE_STYLE,
            on_click=lambda _, v=version: install(v),
        )

    def draw() -> None:
        try:
            installed = set(fetch.installed_versions())
        except Exception:  # unreadable cache dir: show them all as not installed
            installed = set()
        installing, errors = chrome_prefetch.installing(), chrome_prefetch.failures()
        busy[0] = bool(installing)
        rows.controls = [
            ft.Container(
                padding=ft.Padding.symmetric(vertical=8),
                border=ft.Border.only(bottom=ft.BorderSide(1, COLORS["border"])),
                content=ft.Row(
                    [
                        ft.Column(
                            [
                                ft.Text(release.label(v), weight=ft.FontWeight.BOLD),
                                ft.Text(
                                    " · ".join(
                                        s
                                        for s in (
                                            v,
                                            v == release.DEFAULT_VERSION
                                            and get_string("build_default"),
                                            errors.get(v),
                                        )
                                        if s
                                    ),
                                    size=12,
                                    color=COLORS["text_dim"],
                                ),
                            ],
                            spacing=2,
                            expand=True,
                        ),
                        action(v, v in installed, installing.get(v)),
                    ]
                ),
            )
            for v in release.available(chrome_prefetch.host())
        ] or [ft.Text(get_string("builds_none"), size=12, color=COLORS["text_dim"])]
        page.update()

    def ticker() -> None:
        # Redraws while a download runs, and once more when it ends.
        while not stop.wait(2):
            if busy[0] or chrome_prefetch.installing():
                try:
                    draw()
                except Exception:
                    return

    def close(_: ft.ControlEvent) -> None:
        stop.set()
        page.pop_dialog()

    page.show_dialog(
        ft.AlertDialog(
            modal=True,
            bgcolor=COLORS["card_bg"],
            title=ft.Text(get_string("builds_title"), size=20, weight=ft.FontWeight.BOLD),
            content=ft.Container(
                width=600,
                content=ft.Column(
                    tight=True,
                    spacing=10,
                    controls=[
                        ft.Text(get_string("builds_hint"), size=12, color=COLORS["text_dim"]),
                        rows,
                    ],
                ),
            ),
            actions=[ft.TextButton(get_string("close_btn"), on_click=close)],
            on_dismiss=lambda _: stop.set(),
        )
    )
    draw()
    threading.Thread(target=ticker, daemon=True).start()
