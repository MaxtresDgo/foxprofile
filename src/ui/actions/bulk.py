import threading
from collections.abc import Callable

import flet as ft

from ...core.strings import get_string
from ...interfaces.protocols import IBrowserLauncher, IProfileManager
from ...services.browser.launcher import BrowserControlError
from ...services.browser.steps import run_steps
from ...services.browser.sync import SyncError, SyncManager
from ..dialogs import open_confirm_dialog
from ..state import AppState
from .browser import launch_or_stop


def bulk_delete_profiles(
    page: ft.Page,
    names: list[str],
    pm: IProfileManager,
    log: Callable[[str], None],
    refresh: Callable[[], None],
    on_done: Callable[[], None],
) -> None:
    if not names:
        return

    def do_bulk_delete() -> None:
        for name in names:
            pm.delete_profile(name)
            log(get_string("deleted_profile", name=name))
        on_done()
        refresh()

    count = len(names)
    open_confirm_dialog(
        page,
        "",
        do_bulk_delete,
        title=get_string("confirm_bulk_delete", count=count),
        body=get_string("cannot_undo"),
    )


def bulk_launch_profiles(
    names: list[str],
    pm: IProfileManager,
    bl: IBrowserLauncher,
    state: AppState,
    log: Callable[[str], None],
) -> None:
    for name in names:
        if not bl.is_running(name) and not state.is_loading(name):
            launch_or_stop(name, pm, bl, state, log)


def bulk_stop_profiles(
    names: list[str],
    pm: IProfileManager,
    bl: IBrowserLauncher,
    state: AppState,
    log: Callable[[str], None],
) -> None:
    for name in names:
        if bl.is_running(name):
            launch_or_stop(name, pm, bl, state, log)


def bulk_run_steps(
    names: list[str],
    steps: list[dict],
    bl: IBrowserLauncher,
    log: Callable[[str], None],
) -> None:
    log(get_string("steps_running", steps=len(steps), count=len(names)))

    def work() -> None:
        results = run_steps(bl, names, steps)
        for name, result in results.items():
            if result["error"]:
                log(get_string("profile_error", name=name, error=result["error"]))
        failed = sum(1 for r in results.values() if not r["ok"])
        log(get_string("steps_done", ok=len(results) - failed, failed=failed))

    threading.Thread(target=work, daemon=True).start()


def start_sync(
    sync: SyncManager,
    leader: str,
    followers: list[str],
    log: Callable[[str], None],
) -> None:
    def work() -> None:
        try:
            session = sync.start(leader, followers)
        except (SyncError, BrowserControlError) as e:
            log(get_string("profile_error", name=leader, error=e))
            return
        log(get_string("sync_started", leader=leader, count=len(session.followers)))

    threading.Thread(target=work, daemon=True).start()


def stop_sync(sync: SyncManager, leader: str, log: Callable[[str], None]) -> None:
    def work() -> None:
        if sync.stop(leader):
            log(get_string("sync_stopped", leader=leader))

    threading.Thread(target=work, daemon=True).start()
