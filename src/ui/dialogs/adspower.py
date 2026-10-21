"""Import from AdsPower: the desktop twin of the web panel's dialog
(services/profile/adspower.py does the work, on this PC).

Profiles are listed by AdsPower group, with a count per group, so thousands of
them can be imported and checked group by group. A group's rows are built when
it is opened; each row's state lives in its data until then. Importing closes the
dialog and goes on in the background, with a snack bar when it starts and ends.
"""

import asyncio
import threading
from collections.abc import Callable

import flet as ft

from ...core.config import DATA_DIR
from ...core.strings import get_string
from ...interfaces.protocols import IBrowserLauncher, IProfileManager
from ...services.browser import chrome_prefetch
from ...services.profile import adspower
from ..theme.colors import COLORS, FONT_DATA
from ..theme.styles import ACCENT_STYLE

# One import at a time: AdsPower answers once a second anyway.
_importing = threading.Event()


def _notify(page: ft.Page, text: str, error: bool = False) -> None:
    page.show_dialog(
        ft.SnackBar(
            ft.Text(text, color="#FFFFFF"),
            bgcolor=COLORS["error"] if error else COLORS["ink_button"],
            duration=7000 if error else 4000,
        )
    )


def _is_open(r: dict) -> bool:
    return r["status"] not in ("exists", "created")


def _summary(rows: list[dict]) -> str:
    done = sum(not _is_open(r) for r in rows)
    failed = sum(r["status"] == "failed" for r in rows)
    return get_string(
        "adspower_summary", total=len(rows), done=done, pending=len(rows) - done, failed=failed
    )


def open_adspower_dialog(
    page: ft.Page,
    pm: IProfileManager,
    bl: IBrowserLauncher,
    log: Callable[[str], None],
    refresh: Callable[[], None],
) -> None:
    port_field = ft.TextField(
        label=get_string("adspower_port"),
        value=str(adspower.DEFAULT_PORT),
        width=160,
        keyboard_type=ft.KeyboardType.NUMBER,
    )
    key_field = ft.TextField(
        label=get_string("adspower_key"), password=True, can_reveal_password=True, expand=True
    )
    status = ft.Text("", size=12, color=COLORS["text_dim"])
    total = ft.Text("", size=12, color=COLORS["text_dim"])
    groups: list[dict] = []
    group_column = ft.Column(spacing=0, scroll=ft.ScrollMode.AUTO, height=360, visible=False)
    select_all = ft.Checkbox(label=get_string("select_all"), value=True)
    all_row = ft.Row([select_all, ft.Container(expand=True), total], visible=False)
    import_btn = ft.Button(
        get_string("adspower_import_n", n=0), icon=ft.Icons.DOWNLOAD, style=ACCENT_STYLE
    )
    import_btn.disabled = True

    def rows() -> list[dict]:
        return [r for g in groups for r in g["rows"]]

    def client() -> adspower.AdsPower:
        port = int(port_field.value) if (port_field.value or "").isdigit() else 0
        return adspower.AdsPower(port, key_field.value or "")

    def draw_row(r: dict) -> None:
        if "box" not in r:
            return
        r["box"].value, r["box"].disabled = r["picked"], _importing.is_set() or not _is_open(r)
        texts = {
            "exists": get_string("adspower_exists"),
            "created": get_string("adspower_created", n=r.get("cookies", 0)),
            "failed": r.get("error", ""),
            "working": "…",
        }
        r["state"].value = texts.get(r["status"], "")
        r["state"].color = {"failed": COLORS["error"], "created": COLORS["success"]}.get(
            r["status"], COLORS["text_dim"]
        )

    def sync() -> None:
        for g in groups:
            open_rows = [r for r in g["rows"] if _is_open(r)]
            g["sum"].value = _summary(g["rows"])
            g["box"].value = bool(open_rows) and all(r["picked"] for r in open_rows)
            g["box"].disabled = _importing.is_set() or not open_rows
            for r in g["rows"]:
                draw_row(r)
        picked = [r for r in rows() if r["picked"] and _is_open(r)]
        open_rows = [r for r in rows() if _is_open(r)]
        select_all.value = bool(open_rows) and all(r["picked"] for r in open_rows)
        select_all.disabled = _importing.is_set()
        total.value = _summary(rows())
        import_btn.content = get_string("adspower_import_n", n=len(picked))
        import_btn.disabled = _importing.is_set() or not picked
        page.update()

    def pick(rs: list[dict], value: bool) -> None:
        for r in rs:
            if _is_open(r):
                r["picked"] = value
        sync()

    select_all.on_change = lambda e: pick(rows(), bool(e.control.value))

    def build_row(r: dict) -> ft.Control:
        r["box"] = ft.Checkbox()

        def on_pick(e: ft.ControlEvent) -> None:
            r["picked"] = bool(e.control.value)
            sync()

        r["box"].on_change = on_pick
        r["state"] = ft.Text("", size=12)
        draw_row(r)
        sub = r["name"] if r["name"] != r["target"] else ""
        return ft.Container(
            padding=ft.Padding.symmetric(vertical=4),
            border=ft.Border.only(bottom=ft.BorderSide(1, COLORS["rule_strong"])),
            content=ft.Row(
                [
                    r["box"],
                    ft.Column(
                        [ft.Text(r["target"], size=13)]
                        + ([ft.Text(sub, size=11, color=COLORS["text_dim"])] if sub else []),
                        spacing=0,
                        expand=3,
                    ),
                    ft.Text(
                        r["proxy"] or get_string("adspower_no_proxy"),
                        size=11,
                        font_family=FONT_DATA,
                        color=COLORS["text_dim"],
                        expand=3,
                        no_wrap=True,
                        overflow=ft.TextOverflow.ELLIPSIS,
                    ),
                    ft.Container(r["state"], expand=2, alignment=ft.Alignment.CENTER_RIGHT),
                ]
            ),
        )

    def build_group(g: dict) -> ft.Control:
        g["box"] = ft.Checkbox(on_change=lambda e: pick(g["rows"], bool(e.control.value)))
        g["sum"] = ft.Text("", size=12, color=COLORS["text_dim"])
        tile = ft.ExpansionTile(
            title=ft.Text(g["label"], size=14, weight=ft.FontWeight.W_600),
            subtitle=g["sum"],
            leading=g["box"],
            controls=[],
            maintain_state=True,
            dense=True,
        )

        def on_open(_: ft.ControlEvent) -> None:
            if not tile.controls:  # rows built on first open
                tile.controls = [build_row(r) for r in g["rows"]]
                sync()

        tile.on_change = on_open
        return tile

    def say(text: str, error: bool = False) -> None:
        status.value = text
        status.color = COLORS["error"] if error else COLORS["text_dim"]
        page.update()

    async def on_find(_: ft.ControlEvent) -> None:
        say("…")
        try:
            found = await asyncio.to_thread(lambda: client().profiles())
        except adspower.AdsPowerError as e:
            say(str(e), error=True)
            return
        by_group: dict[str, list[dict]] = {}
        for p in found:
            exists = p["target"] in pm.profiles
            row = {**p, "status": "exists" if exists else "pending", "picked": not exists}
            by_group.setdefault(p["group"], []).append(row)
        # By name, "no group" last.
        names = sorted(by_group, key=lambda n: (not n, n.lower()))
        groups[:] = [
            {"label": n or get_string("adspower_no_group"), "rows": by_group[n]} for n in names
        ]
        group_column.controls = [build_group(g) for g in groups]
        group_column.visible = all_row.visible = bool(groups)
        say("" if groups else get_string("adspower_none"))
        sync()

    async def on_import(_: ft.ControlEvent) -> None:
        if _importing.is_set():
            say(get_string("adspower_busy"), error=True)
            return
        todo = [r for r in rows() if r["picked"] and _is_open(r)]
        ads = client()
        page.pop_dialog()
        _importing.set()
        _notify(page, get_string("adspower_started", n=len(todo)))
        counts = {"created": 0, "skipped": 0, "failed": 0}
        errors: list[str] = []
        try:
            for r in todo:
                try:
                    result = await asyncio.to_thread(
                        lambda r=r: adspower.import_here(
                            pm, bl, ads.fetch(r["id"]), r["target"], DATA_DIR
                        )
                    )
                except adspower.AdsPowerError as e:
                    counts["failed"] += 1
                    errors.append(f"{r['target']}: {e}")
                    log(get_string("profile_error", name=r["target"], error=e))
                    continue
                counts["created" if result["status"] == "created" else "skipped"] += 1
                if result["status"] == "created":
                    log(get_string("adspower_logged", name=r["target"], n=result["cookies"]))
                    for warning in result["warnings"]:
                        log(f"[{r['target']}] {warning}")
                    refresh()
        finally:
            _importing.clear()
        if counts["created"]:
            chrome_prefetch.start(log)
        summary = get_string("adspower_done", **counts)
        log(summary)
        _notify(page, " · ".join([summary, *errors[:2]]), error=bool(counts["failed"]))

    import_btn.on_click = on_import

    dlg = ft.AlertDialog(
        modal=True,
        bgcolor=COLORS["card_bg"],
        title=ft.Text(get_string("adspower_title"), size=20, weight=ft.FontWeight.BOLD),
        content=ft.Container(
            width=660,
            content=ft.Column(
                tight=True,
                spacing=10,
                controls=[
                    ft.Text(get_string("adspower_sub"), size=12, color=COLORS["text_dim"]),
                    ft.Text(get_string("adspower_hint"), size=12, color=COLORS["text_dim"]),
                    ft.Row(
                        [
                            port_field,
                            key_field,
                            ft.OutlinedButton(
                                get_string("adspower_find"), icon=ft.Icons.SEARCH, on_click=on_find
                            ),
                        ]
                    ),
                    status,
                    all_row,
                    group_column,
                ],
            ),
        ),
        actions=[
            ft.TextButton(get_string("close_btn"), on_click=lambda _: page.pop_dialog()),
            import_btn,
        ],
    )
    page.show_dialog(dlg)
