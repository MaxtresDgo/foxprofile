"""Browser extensions (services/browser/extensions.py): add them from a file or
the Chrome Web Store, choose which profiles load them, remove them.

The desktop app has no hub groups: an extension applies to every profile of its
engine or to some profiles, and any profile can be left out.
"""

import asyncio
import pathlib
from collections.abc import Callable

import flet as ft

from ...core.strings import get_string
from ...interfaces.protocols import IProfileManager
from ...services.browser import extensions as ext
from ..theme.colors import COLORS
from ..theme.styles import ACCENT_STYLE
from .confirm import open_confirm_dialog


def _engine(engine: str) -> str:
    return "Chrome" if engine == "chrome" else "Firefox · Camoufox"


def _where(x: dict) -> str:
    parts = [
        get_string("ext_all") if x["global"] else "",
        get_string("ext_in_profiles", n=len(x["profiles"])) if x["profiles"] else "",
        get_string("ext_excluded", n=len(x["excluded"])) if x["excluded"] else "",
    ]
    return " · ".join(p for p in parts if p) or get_string("ext_nowhere")


def open_extensions_dialog(
    page: ft.Page,
    file_picker: ft.FilePicker,
    pm: IProfileManager,
    log: Callable[[str], None],
) -> None:
    store = pm.extensions
    status = ft.Text("", size=12, color=COLORS["text_dim"])
    rows = ft.Column(spacing=0, scroll=ft.ScrollMode.AUTO, height=300)
    ref = ft.TextField(label=get_string("ext_store"), expand=True)

    def say(text: str, error: bool = False) -> None:
        status.value = text
        status.color = COLORS["error"] if error else COLORS["text_dim"]

    def added(x: dict) -> None:
        say(get_string("ext_added", name=x["name"], version=x["version"]))
        log(get_string("ext_added", name=x["name"], version=x["version"]))
        show_list()

    async def on_upload(_: ft.ControlEvent) -> None:
        files = await file_picker.pick_files(
            allowed_extensions=["crx", "zip", "xpi"], dialog_title=get_string("ext_upload")
        )
        if not files or not files[0].path:
            return
        path = pathlib.Path(files[0].path)
        try:
            added(await asyncio.to_thread(store.add, path.read_bytes(), filename=path.name))
        except (ext.ExtensionError, OSError) as e:
            say(str(e), error=True)
            page.update()

    async def on_store(_: ft.ControlEvent) -> None:
        if not (ref.value or "").strip():
            return
        from chrome_engine.release import DEFAULT_VERSION

        say(get_string("ext_store_fetching"))
        page.update()
        try:
            data, store_id = await asyncio.to_thread(ext.from_store, ref.value, DEFAULT_VERSION)
            entry = await asyncio.to_thread(
                store.add, data, engine="chrome", source="store", store_id=store_id
            )
        except ext.ExtensionError as e:
            say(str(e), error=True)
            page.update()
            return
        ref.value = ""
        added(entry)

    def delete(x: dict) -> None:
        def go() -> None:
            store.delete(x["id"])
            show_list()

        open_confirm_dialog(
            page,
            x["name"],
            go,
            title=get_string("ext_confirm_delete", name=x["name"]),
        )

    def show_list() -> None:
        items = store.list()
        rows.controls = [
            ft.Container(
                padding=ft.Padding.symmetric(vertical=8),
                border=ft.Border.only(bottom=ft.BorderSide(1, COLORS["border"])),
                content=ft.Row(
                    [
                        ft.Column(
                            [
                                ft.Text(f"{x['name']}  {x['version']}", weight=ft.FontWeight.BOLD),
                                ft.Text(
                                    f"{_engine(x['engine'])} · {_where(x)}",
                                    size=12,
                                    color=COLORS["text_dim"],
                                ),
                            ],
                            spacing=2,
                            expand=True,
                        ),
                        ft.OutlinedButton(
                            get_string("ext_where"),
                            icon=ft.Icons.GROUP_OUTLINED,
                            on_click=lambda _, x=x: show_scope(x),
                        ),
                        ft.IconButton(
                            icon=ft.Icons.DELETE_OUTLINE,
                            icon_color=COLORS["error"],
                            tooltip=get_string("delete"),
                            on_click=lambda _, x=x: delete(x),
                        ),
                    ]
                ),
            )
            for x in items
        ] or [ft.Text(get_string("ext_none"), size=12, color=COLORS["text_dim"])]
        dlg.title = ft.Text(get_string("ext_title"), size=20, weight=ft.FontWeight.BOLD)
        dlg.content = list_view
        dlg.actions = [ft.TextButton(get_string("close_btn"), on_click=lambda _: page.pop_dialog())]
        page.update()

    def picks(
        names: list[str], chosen: list[str], engine: str
    ) -> tuple[ft.Column, Callable[[], list[str]]]:
        boxes = [ft.Checkbox(label=n, value=n in chosen) for n in names]
        column = ft.Column(boxes, spacing=0, scroll=ft.ScrollMode.AUTO, height=130)
        if not names:
            column.controls = [ft.Text(get_string("ext_no_profiles", engine=engine), size=12)]
        return column, lambda: [b.label for b in boxes if b.value]

    def show_scope(x: dict) -> None:
        engine = _engine(x["engine"])
        names = sorted(
            {p.name for p in pm.list_profiles() if p.engine == x["engine"]}
            | set(x["profiles"])
            | set(x["excluded"])
        )
        every = ft.Checkbox(label=get_string("ext_scope_all", engine=engine), value=x["global"])
        own, own_value = picks(names, x["profiles"], engine)
        out, out_value = picks(names, x["excluded"], engine)

        def save(_: ft.ControlEvent) -> None:
            store.set_scope(
                x["id"], **{"global": every.value, "profiles": own_value(), "excluded": out_value()}
            )
            show_list()

        dlg.title = ft.Text(
            f"{x['name']}: {get_string('ext_where').lower()}", size=20, weight=ft.FontWeight.BOLD
        )
        dlg.content = ft.Container(
            width=560,
            content=ft.Column(
                tight=True,
                spacing=8,
                controls=[
                    ft.Text(get_string("ext_scope_hint"), size=12, color=COLORS["text_dim"]),
                    every,
                    ft.Text(get_string("ext_scope_profiles"), weight=ft.FontWeight.BOLD),
                    own,
                    ft.Text(get_string("ext_scope_excluded"), weight=ft.FontWeight.BOLD),
                    out,
                ],
            ),
        )
        dlg.actions = [
            ft.TextButton(get_string("cancel"), on_click=lambda _: show_list()),
            ft.Button(get_string("save_changes"), style=ACCENT_STYLE, on_click=save),
        ]
        page.update()

    list_view = ft.Container(
        width=660,
        content=ft.Column(
            tight=True,
            spacing=10,
            controls=[
                ft.Text(get_string("ext_hint"), size=12, color=COLORS["text_dim"]),
                ft.Row(
                    [
                        ft.Button(
                            get_string("ext_upload"),
                            icon=ft.Icons.UPLOAD_FILE,
                            style=ACCENT_STYLE,
                            on_click=on_upload,
                        ),
                    ]
                ),
                ft.Row(
                    [
                        ref,
                        ft.OutlinedButton(
                            get_string("ext_store_add"), icon=ft.Icons.ADD, on_click=on_store
                        ),
                    ]
                ),
                ft.Text(get_string("ext_store_note"), size=11, color=COLORS["text_dim"]),
                rows,
                status,
            ],
        ),
    )
    dlg = ft.AlertDialog(modal=True, bgcolor=COLORS["card_bg"])
    page.show_dialog(dlg)
    show_list()
