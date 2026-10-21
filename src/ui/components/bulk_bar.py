from collections.abc import Callable

import flet as ft

from ...core.strings import get_string
from ...models.profile import Profile
from ...services.browser.sync import SyncSession
from ..state import AppState
from ..theme.colors import COLORS, FONT_SEMIBOLD
from ..theme.styles import ACCENT_STYLE, OUTLINE_STYLE

BulkCallbacks = dict[str, Callable[[], None]]


def rebuild_bulk_bar(
    bulk_bar: ft.Row,
    state: AppState,
    page_profiles: list[Profile],
    cbs: BulkCallbacks,
    syncs: list[SyncSession] = (),
    on_sync_stop: Callable[[str], None] = lambda _: None,
) -> None:
    count = len(state.selected_names())
    # A running sync stays visible (and stoppable) with nothing selected.
    if count == 0 and not syncs:
        bulk_bar.visible = False
        bulk_bar.controls = []
        return
    bulk_bar.visible = True
    bulk_bar.controls = [_build_container(count, cbs, syncs, on_sync_stop)]


def _sync_chip(session: SyncSession, on_stop: Callable[[str], None]) -> ft.Control:
    return ft.Row(
        spacing=6,
        tight=True,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        controls=[
            ft.Icon(ft.Icons.SYNC, size=16, color=COLORS["text_sub"]),
            ft.Text(
                get_string("syncing", leader=session.leader, count=len(session.followers)),
                size=13,
                color=COLORS["text_main"],
            ),
            ft.OutlinedButton(
                get_string("sync_stop"),
                icon=ft.Icons.STOP_ROUNDED,
                height=32,
                style=OUTLINE_STYLE,
                on_click=lambda _: on_stop(session.leader),
            ),
        ],
    )


def _build_container(
    count: int,
    cbs: BulkCallbacks,
    syncs: list[SyncSession],
    on_sync_stop: Callable[[str], None],
) -> ft.Container:
    def btn(label: str, icon: str, key: str, style: ft.ButtonStyle) -> ft.Control:
        cls = ft.Button if style is ACCENT_STYLE else ft.OutlinedButton
        return cls(label, icon=icon, height=32, style=style, on_click=lambda _: cbs[key]())

    selection = (
        [
            ft.Text(
                get_string("selected_count", count=count),
                size=13.5,
                font_family=FONT_SEMIBOLD,
                color=COLORS["text_main"],
            ),
            ft.Container(width=6),
            btn(get_string("launch"), ft.Icons.PLAY_ARROW_ROUNDED, "launch", ACCENT_STYLE),
            btn(get_string("stop"), ft.Icons.STOP_ROUNDED, "stop", OUTLINE_STYLE),
            btn(get_string("bulk_edit"), ft.Icons.EDIT_OUTLINED, "edit", OUTLINE_STYLE),
            btn(get_string("run_steps"), ft.Icons.PLAYLIST_PLAY, "run_steps", OUTLINE_STYLE),
            btn(get_string("sync"), ft.Icons.SYNC, "sync", OUTLINE_STYLE),
            btn(get_string("delete"), ft.Icons.DELETE_OUTLINE, "delete", OUTLINE_STYLE),
        ]
        if count
        else []
    )
    return ft.Container(
        expand=True,
        bgcolor=COLORS["paper_sunk"],
        padding=ft.Padding.symmetric(horizontal=14, vertical=9),
        border=ft.Border.only(bottom=ft.BorderSide(1, COLORS["card_border"])),
        content=ft.Row(
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                *selection,
                *(_sync_chip(s, on_sync_stop) for s in syncs),
                ft.Container(expand=True),
                *(
                    [
                        ft.TextButton(
                            get_string("clear"),
                            on_click=lambda _: cbs["clear"](),
                            style=ft.ButtonStyle(color=COLORS["text_sub"]),
                        )
                    ]
                    if count
                    else []
                ),
            ],
        ),
    )
