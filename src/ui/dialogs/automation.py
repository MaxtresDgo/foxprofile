from collections.abc import Callable

import flet as ft

from ...core.strings import get_string
from ...services.browser.steps import StepError, parse_script
from ...utils.validation import validate_locale, validate_proxy_format, validate_timezone
from ..theme.colors import COLORS, FONT_BOLD, FONT_DATA
from ..theme.page import build_os_dropdown
from ..theme.styles import ACCENT_STYLE, DLG_FIELD_KWARGS

_DATA = ft.TextStyle(font_family=FONT_DATA, size=13)
_last_script = "navigate https://example.com\n"


def _dialog(page: ft.Page, title: str, hint: str, body: ft.Control, action: ft.Control):
    page.show_dialog(
        ft.AlertDialog(
            modal=True,
            title=ft.Text(title, size=18, font_family=FONT_BOLD),
            content=ft.Container(
                width=560,
                content=ft.Column(
                    tight=True,
                    spacing=10,
                    controls=[ft.Text(hint, size=12.5, color=COLORS["text_sub"]), body],
                ),
            ),
            actions=[
                ft.TextButton(get_string("cancel"), on_click=lambda _: page.pop_dialog()),
                action,
            ],
        )
    )


def open_run_steps_dialog(page: ft.Page, count: int, on_run: Callable[[list[dict]], None]) -> None:
    field = ft.TextField(
        value=_last_script,
        multiline=True,
        min_lines=8,
        max_lines=16,
        text_style=_DATA,
        **DLG_FIELD_KWARGS,
    )

    def run(_: ft.ControlEvent) -> None:
        global _last_script
        try:
            steps = parse_script(field.value or "")
        except StepError as e:
            field.error_text = str(e)
            field.update()
            return
        _last_script = field.value or ""
        page.pop_dialog()
        on_run(steps)

    _dialog(
        page,
        get_string("run_steps_title", count=count),
        get_string("run_steps_hint"),
        field,
        ft.Button(
            get_string("run_btn"),
            icon=ft.Icons.PLAY_ARROW_ROUNDED,
            style=ACCENT_STYLE,
            on_click=run,
        ),
    )


def open_sync_dialog(page: ft.Page, names: list[str], on_start: Callable[[str], None]) -> None:
    leader = ft.Dropdown(
        label=get_string("sync_leader"),
        value=names[0],
        options=[ft.dropdown.Option(n, n) for n in names],
        bgcolor=COLORS["input_bg"],
        color=COLORS["text_main"],
        border_color=COLORS["rule_strong"],
        focused_border_color=COLORS["sidebar"],
        label_style=ft.TextStyle(color=COLORS["text_sub"]),
        border_radius=8,
    )

    def start(_: ft.ControlEvent) -> None:
        page.pop_dialog()
        on_start(leader.value or names[0])

    _dialog(
        page,
        get_string("sync_title"),
        get_string("sync_hint"),
        leader,
        ft.Button(get_string("sync_start"), icon=ft.Icons.SYNC, style=ACCENT_STYLE, on_click=start),
    )


def open_bulk_edit_dialog(
    page: ft.Page, count: int, on_apply: Callable[[dict[str, str]], None]
) -> None:
    """Only the ticked fields change; "" removes the proxy or means automatic."""
    os_dropdown = build_os_dropdown("windows")
    os_dropdown.label = None
    os_dropdown.text_size = 14

    def text(hint: str) -> ft.TextField:
        return ft.TextField(hint_text=hint, text_style=_DATA, **DLG_FIELD_KWARGS)

    fields: dict[str, tuple[str, ft.Control]] = {
        "os_type": (get_string("operating_system"), os_dropdown),
        "proxy": (get_string("proxy_optional"), text("socks5://user:pass@host:port")),
        "timezone": (get_string("timezone_optional"), text("Asia/Ho_Chi_Minh")),
        "locale": (get_string("locale_optional"), text("vi-VN")),
    }
    error = ft.Text("", size=12, color=COLORS["error"], visible=False)
    boxes: dict[str, ft.Checkbox] = {}
    rows: list[ft.Control] = []
    for key, (label, control) in fields.items():
        control.disabled = True

        def toggle(e: ft.ControlEvent, control: ft.Control = control) -> None:
            control.disabled = not e.control.value
            page.update()

        boxes[key] = ft.Checkbox(label=label, on_change=toggle)
        rows += [boxes[key], control]

    def fail(message: str) -> None:
        error.value = message
        error.visible = True
        page.update()

    def apply(_: ft.ControlEvent) -> None:
        changes = {
            key: (fields[key][1].value or "").strip() for key, box in boxes.items() if box.value
        }
        if not changes:
            return fail(get_string("bulk_edit_nothing"))
        for key, validate in (
            ("proxy", validate_proxy_format),
            ("timezone", validate_timezone),
            ("locale", validate_locale),
        ):
            valid, message = validate(changes.get(key, ""))
            if not valid:
                return fail(message)
        page.pop_dialog()
        on_apply(changes)

    _dialog(
        page,
        get_string("bulk_edit_title", count=count),
        get_string("bulk_edit_hint"),
        ft.Column(tight=True, spacing=6, controls=[*rows, error]),
        ft.Button(
            get_string("save_changes"), icon=ft.Icons.SAVE, style=ACCENT_STYLE, on_click=apply
        ),
    )
