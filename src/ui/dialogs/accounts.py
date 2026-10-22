import threading
from collections.abc import Callable

import flet as ft

from ...core.strings import get_string
from ...interfaces.protocols import IBrowserLauncher, IProfileManager
from ...services.browser.login import login
from ...services.profile.accounts import PLATFORM_LABELS, AccountsLocked, totp, valid_totp_secret
from ..theme.colors import COLORS, FONT_BOLD, FONT_DATA, FONT_SEMIBOLD
from ..theme.styles import ACCENT_STYLE, DLG_FIELD_KWARGS, ICON_STYLE, OUTLINE_STYLE
from .confirm import open_confirm_dialog

_DATA = ft.TextStyle(font_family=FONT_DATA, size=13)
_MASK = "••••••••"


def open_accounts_dialog(
    page: ft.Page,
    name: str,
    pm: IProfileManager,
    bl: IBrowserLauncher,
    log: Callable[[str], None],
    on_close: Callable[[], None] = lambda: None,
) -> None:
    """A profile's site accounts: copy them, see the 2FA code, fill a login form.
    `on_close` redraws the list (its name tooltips show the accounts)."""
    store = pm.accounts
    clipboard = next((s for s in page.services if isinstance(s, ft.Clipboard)), None)
    status = ft.Text("", size=12.5, color=COLORS["text_sub"])
    rows = ft.Column(spacing=10, tight=True)
    form = ft.Column(spacing=6, tight=True, visible=False)
    # (secret, code text, seconds text) of each shown 2FA code; a thread ticks them.
    codes: list[tuple[str, ft.Text, ft.Text]] = []
    stop = threading.Event()

    def say(text: str, color: str = COLORS["text_sub"]) -> None:
        status.value, status.color = text, color
        page.update()

    def copier(text: Callable[[], str], what: str):
        """A click handler copying `text()` (Flet awaits async handlers only)."""

        async def handler(_: ft.ControlEvent) -> None:
            if clipboard is not None:
                await clipboard.set(text())
                say(get_string("account_copied", what=what), COLORS["success"])

        return handler

    def icon_button(icon: ft.IconData, tooltip: str, on_click) -> ft.IconButton:
        return ft.IconButton(
            icon=icon,
            icon_size=17,
            icon_color=COLORS["text_sub"],
            tooltip=tooltip,
            style=ICON_STYLE,
            on_click=on_click,
        )

    def line(label: str, value: ft.Control, *extra: ft.Control) -> ft.Row:
        return ft.Row(
            spacing=6,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Text(label, size=12.5, color=COLORS["text_dim"], width=110),
                value,
                *extra,
            ],
        )

    def tick() -> None:
        for secret, code_text, left_text in codes:
            code, remaining = totp(secret)
            code_text.value = code
            left_text.value = get_string("account_left", s=remaining)

    def row(account: dict, full: dict) -> ft.Control:
        label = PLATFORM_LABELS.get(account["platform"], account["platform"])
        lines: list[ft.Control] = [
            line(
                get_string("account_username"),
                ft.Text(full["username"], style=_DATA, selectable=True, expand=True),
                icon_button(
                    ft.Icons.COPY,
                    get_string("account_copy"),
                    copier(lambda: full["username"], get_string("account_username")),
                ),
            )
        ]
        if full["password"]:
            secret_text = ft.Text(_MASK, style=_DATA, expand=True)

            def toggle(_: ft.ControlEvent, t: ft.Text = secret_text) -> None:
                t.value = full["password"] if t.value == _MASK else _MASK
                page.update()

            lines.append(
                line(
                    get_string("account_password"),
                    secret_text,
                    icon_button(ft.Icons.VISIBILITY_OUTLINED, get_string("account_show"), toggle),
                    icon_button(
                        ft.Icons.COPY,
                        get_string("account_copy"),
                        copier(lambda: full["password"], get_string("account_password")),
                    ),
                )
            )
        if full["totp_secret"]:
            code_text = ft.Text("", size=17, font_family=FONT_BOLD, color=COLORS["text_main"])
            left_text = ft.Text("", size=12, color=COLORS["text_dim"], expand=True)
            codes.append((full["totp_secret"], code_text, left_text))
            lines.append(
                line(
                    get_string("account_otp"),
                    code_text,
                    left_text,
                    icon_button(
                        ft.Icons.COPY,
                        get_string("account_copy"),
                        copier(lambda: code_text.value or "", get_string("account_otp")),
                    ),
                )
            )
        running = bl.is_running(name)
        return ft.Container(
            padding=ft.Padding.symmetric(horizontal=14, vertical=8),
            border_radius=9,
            bgcolor=COLORS["bg"],
            border=ft.Border.all(1, COLORS["card_border"]),
            content=ft.Column(
                spacing=2,
                tight=True,
                controls=[
                    ft.Row(
                        spacing=4,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            ft.Text(label, size=14, font_family=FONT_SEMIBOLD, expand=True),
                            ft.Button(
                                get_string("account_login"),
                                height=32,
                                style=ACCENT_STYLE,
                                disabled=not running,
                                tooltip=None if running else get_string("account_need_running"),
                                on_click=lambda _: start_login(full, label),
                            ),
                            icon_button(
                                ft.Icons.EDIT_OUTLINED,
                                get_string("account_edit"),
                                lambda _: open_form(full),
                            ),
                            icon_button(
                                ft.Icons.DELETE_OUTLINE,
                                get_string("delete"),
                                lambda _: confirm_delete(full, label),
                            ),
                        ],
                    ),
                    *lines,
                ],
            ),
        )

    def draw() -> None:
        codes.clear()
        try:
            accounts = store.list(name)
            pairs = [(a, store.get(name, a["id"])) for a in accounts]
        except AccountsLocked:
            rows.controls = []
            say(get_string("err_accounts_locked"), COLORS["error"])
            return
        rows.controls = [row(a, full) for a, full in pairs if full] or [
            ft.Text(get_string("accounts_empty"), size=13, color=COLORS["text_dim"])
        ]
        tick()
        page.update()

    def start_login(full: dict, label: str) -> None:
        say(get_string("account_login_started", platform=label, name=name))

        def work() -> None:
            result = login(bl, name, full)
            if result["ok"]:
                done = (
                    "account_already_signed_in" if result.get("signed_in") else "account_login_done"
                )
                say(get_string(done), COLORS["success"])
            else:
                say(result["error"], COLORS["error"])
                log(get_string("profile_error", name=name, error=result["error"]))

        threading.Thread(target=work, daemon=True).start()

    def confirm_delete(full: dict, label: str) -> None:
        def do_delete() -> None:
            try:
                store.delete(name, full["id"])
            except AccountsLocked:
                say(get_string("err_accounts_locked"), COLORS["error"])
            draw()

        open_confirm_dialog(
            page,
            name,
            do_delete,
            title=get_string("account_confirm_delete", platform=label, username=full["username"]),
        )

    def open_form(full: dict | None) -> None:
        platform = ft.Dropdown(
            value=full["platform"] if full else "facebook",
            options=[ft.dropdown.Option(k, v) for k, v in PLATFORM_LABELS.items()],
            bgcolor=COLORS["input_bg"],
            color=COLORS["text_main"],
            border_color=COLORS["rule_strong"],
            focused_border_color=COLORS["sidebar"],
            border_radius=8,
            text_size=14,
            expand=True,
        )
        user = ft.TextField(
            value=full["username"] if full else "",
            label=get_string("account_username"),
            text_style=_DATA,
            expand=True,
            **DLG_FIELD_KWARGS,
        )
        keep = get_string("account_unchanged") if full else None
        password = ft.TextField(
            label=get_string("account_password"),
            password=True,
            can_reveal_password=True,
            hint_text=keep if full and full["password"] else None,
            expand=True,
            **DLG_FIELD_KWARGS,
        )
        secret = ft.TextField(
            label=get_string("account_totp_secret"),
            hint_text=keep if full and full["totp_secret"] else "JBSW Y3DP EHPK 3PXP",
            helper=get_string("account_totp_hint"),
            text_style=_DATA,
            expand=True,
            **DLG_FIELD_KWARGS,
        )
        error = ft.Text("", size=12, color=COLORS["error"], visible=False)

        def save(_: ft.ControlEvent) -> None:
            username = (user.value or "").strip()
            totp_secret = "".join((secret.value or "").split()).upper()
            problem = None
            if not username:
                problem = get_string("err_account_username")
            elif totp_secret and not valid_totp_secret(totp_secret):
                problem = get_string("err_totp_secret")
            if problem:
                error.value, error.visible = problem, True
                page.update()
                return
            fields = {"platform": platform.value, "username": username}
            # Editing: a blank password or secret keeps the saved one.
            if password.value or not full:
                fields["password"] = password.value or ""
            if totp_secret or not full:
                fields["totp_secret"] = totp_secret
            try:
                if full:
                    store.update(name, full["id"], fields)
                else:
                    store.add(name, fields)
            except AccountsLocked:
                error.value, error.visible = get_string("err_accounts_locked"), True
                page.update()
                return
            form.visible = False
            add_button.visible = True
            say(get_string("account_saved"), COLORS["success"])
            draw()

        def cancel(_: ft.ControlEvent) -> None:
            form.visible = False
            add_button.visible = True
            page.update()

        form.controls = [
            ft.Divider(color=COLORS["card_border"]),
            ft.Text(
                get_string("account_edit" if full else "account_add"),
                size=13,
                font_family=FONT_SEMIBOLD,
            ),
            ft.Row(spacing=10, controls=[platform, user]),
            ft.Row(
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.START,
                controls=[password, secret],
            ),
            error,
            ft.Row(
                spacing=8,
                alignment=ft.MainAxisAlignment.END,
                controls=[
                    ft.TextButton(get_string("cancel"), on_click=cancel),
                    ft.Button(get_string("save_changes"), style=ACCENT_STYLE, on_click=save),
                ],
            ),
        ]
        form.visible = True
        add_button.visible = False
        page.update()

    add_button = ft.OutlinedButton(
        get_string("account_add"),
        icon=ft.Icons.ADD,
        height=36,
        style=OUTLINE_STYLE,
        on_click=lambda _: open_form(None),
    )

    def ticker() -> None:
        while not stop.wait(1):
            if codes:
                tick()
                try:
                    page.update()
                except Exception:
                    return

    def close(_: ft.ControlEvent | None = None) -> None:
        stop.set()
        page.pop_dialog()
        on_close()

    dlg = ft.AlertDialog(
        modal=True,
        bgcolor=COLORS["card_bg"],
        shape=ft.RoundedRectangleBorder(radius=12),
        title=ft.Column(
            spacing=4,
            tight=True,
            controls=[
                ft.Text(get_string("accounts_title", name=name), size=19, font_family=FONT_BOLD),
                ft.Text(get_string("accounts_hint"), size=13, color=COLORS["text_sub"]),
            ],
        ),
        content=ft.Container(
            width=600,
            content=ft.Column(
                tight=True,
                spacing=10,
                scroll=ft.ScrollMode.AUTO,
                controls=[rows, add_button, form, status],
            ),
        ),
        actions=[ft.TextButton(get_string("close_btn"), on_click=close)],
        on_dismiss=lambda _: stop.set(),
    )
    page.show_dialog(dlg)
    draw()
    threading.Thread(target=ticker, daemon=True).start()
