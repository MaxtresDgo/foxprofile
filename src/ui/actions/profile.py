from collections.abc import Callable

import flet as ft

from ...core.strings import get_string
from ...interfaces.protocols import IBrowserLauncher, IProfileManager, IProxyService
from ...services.browser import chrome_prefetch
from ..dialogs import open_confirm_dialog, open_profile_dialog


def delete_profile(
    page: ft.Page,
    name: str,
    pm: IProfileManager,
    log: Callable[[str], None],
    refresh: Callable[[], None],
) -> None:
    def do_delete() -> None:
        pm.delete_profile(name)
        log(get_string("deleted_profile", name=name))
        refresh()

    open_confirm_dialog(page, name, do_delete)


def edit_profile(
    page: ft.Page,
    name: str,
    pm: IProfileManager,
    bl: IBrowserLauncher,
    ps: IProxyService,
    log: Callable[[str], None],
    refresh: Callable[[], None],
) -> None:
    profile = pm.profiles.get(name)
    if not profile:
        return
    original = profile.name

    def on_save(
        new_name: str,
        new_proxy: str,
        new_os: str,
        timezone: str,
        locale: str,
        _engine: str,
        number: str,
        remark: str,
        build: str,
    ) -> str | None:
        if new_name != original and bl.is_running(original):
            return get_string("stop_before_rename")
        new_build = build or profile.chrome_version
        if new_build != profile.chrome_version:
            problem = chrome_prefetch.upgrade_problem(profile, new_build, bl.is_running(original))
            if problem:
                return problem[1]
        if pm.update_profile(original, new_name, new_proxy, new_os, timezone, locale):
            pm.set_label(new_name, number, remark)
            if new_build != pm.profiles[new_name].chrome_version:
                chrome_prefetch.move_to(pm, new_name, new_build)
            log(get_string("updated_profile", old=original, new=new_name))
            refresh()
            return None
        return get_string("update_failed")

    open_profile_dialog(page, ps, on_save, profile)


def add_profile(
    page: ft.Page,
    pm: IProfileManager,
    ps: IProxyService,
    log: Callable[[str], None],
    refresh: Callable[[], None],
) -> None:
    def on_save(
        name: str,
        proxy: str,
        os_type: str,
        timezone: str,
        locale: str,
        engine: str,
        number: str,
        remark: str,
        build: str,
    ) -> str | None:
        if engine == "chrome" and (reason := chrome_prefetch.unsupported_reason()):
            return get_string("chrome_engine_unsupported", reason=reason)
        if engine == "chrome" and build and (problem := chrome_prefetch.version_problem(build)):
            return problem
        if pm.add_profile(
            name, proxy, os_type, timezone, locale, engine, chrome_version=build or None
        ):
            if number or remark:
                pm.set_label(name, number, remark)
            log(get_string("created_profile", name=name))
            if engine == "chrome":
                chrome_prefetch.start(pm.profiles[name].chrome_version, log)
            refresh()
            return None
        return get_string("profile_exists")

    open_profile_dialog(page, ps, on_save)
