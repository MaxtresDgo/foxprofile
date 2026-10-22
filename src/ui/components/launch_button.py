from collections.abc import Callable

import flet as ft

from ...core.strings import get_string
from ...services.profile.accounts import PLATFORM_LABELS
from ..theme.colors import COLORS, FONT_SEMIBOLD
from ..theme.styles import ACCENT_STYLE, INK_STYLE

_WIDTH, _HEIGHT = 112, 36
_CARET = 28


def build_launch_button(
    name: str,
    is_loading: bool,
    is_running: bool,
    on_launch: Callable,
    accounts: list[dict] | None = None,
    on_open_with: Callable[[str, dict], None] | None = None,
) -> ft.Control:
    """Create the context-aware Launch / Stop / Loading button. A stopped profile
    with accounts gets a caret: "Open with" one of them (launch, then log in)."""
    if is_loading:
        return ft.Button(
            content=ft.ProgressRing(width=16, height=16, stroke_width=2, color=COLORS["text_sub"]),
            width=_WIDTH,
            height=_HEIGHT,
            disabled=True,
            tooltip=get_string("loading"),
            style=ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=8),
                bgcolor=COLORS["paper_sunk"],
            ),
        )
    if is_running:
        return ft.Button(
            get_string("stop"),
            icon=ft.Icons.STOP_ROUNDED,
            width=_WIDTH,
            height=_HEIGHT,
            style=INK_STYLE,
            on_click=lambda _, n=name: on_launch(n),
        )
    if not accounts or on_open_with is None:
        return ft.Button(
            get_string("launch"),
            icon=ft.Icons.PLAY_ARROW_ROUNDED,
            width=_WIDTH,
            height=_HEIGHT,
            style=ACCENT_STYLE,
            on_click=lambda _, n=name: on_launch(n),
        )
    left = ft.RoundedRectangleBorder(radius=ft.BorderRadius.only(top_left=8, bottom_left=8))
    return ft.Row(
        spacing=1,
        tight=True,
        controls=[
            ft.Button(
                get_string("launch"),
                icon=ft.Icons.PLAY_ARROW_ROUNDED,
                width=_WIDTH - _CARET - 1,
                height=_HEIGHT,
                style=ft.ButtonStyle(
                    shape=left,
                    bgcolor=ACCENT_STYLE.bgcolor,
                    color=ACCENT_STYLE.color,
                    text_style=ACCENT_STYLE.text_style,
                    padding=ft.Padding.symmetric(horizontal=8),
                    elevation=0,
                ),
                on_click=lambda _, n=name: on_launch(n),
            ),
            ft.PopupMenuButton(
                tooltip=get_string("open_with"),
                content=ft.Container(
                    width=_CARET,
                    height=_HEIGHT,
                    bgcolor=COLORS["accent"],
                    border_radius=ft.BorderRadius.only(top_right=8, bottom_right=8),
                    alignment=ft.Alignment.CENTER,
                    content=ft.Icon(ft.Icons.ARROW_DROP_DOWN, color="#FFFFFF", size=20),
                ),
                items=[
                    ft.PopupMenuItem(
                        content=ft.Text(
                            get_string("open_with"),
                            size=12,
                            color=COLORS["text_dim"],
                        ),
                        disabled=True,
                    ),
                    *(
                        ft.PopupMenuItem(
                            content=ft.Row(
                                spacing=10,
                                controls=[
                                    ft.Text(
                                        PLATFORM_LABELS.get(a["platform"], a["platform"]),
                                        font_family=FONT_SEMIBOLD,
                                        size=13,
                                    ),
                                    ft.Text(a["username"], size=13, color=COLORS["text_sub"]),
                                ],
                            ),
                            on_click=lambda _, n=name, a=a: on_open_with(n, a),
                        )
                        for a in accounts
                    ),
                ],
            ),
        ],
    )
