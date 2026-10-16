"""Steps: page actions run in order on many running profiles at once.

A step is `{"action": ..., "params": {...}}`, the same actions as the page
API. Scripts are one step per line; two-part arguments split on " | ":

    navigate https://example.com
    click text=Sign in
    type #email | user@example.com
    press Enter
    wait #dashboard
    sleep 2000

The sync leader's recorder produces steps too, so followers replay through
`run_steps` like any script.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from ...api.schemas.page import (
    ClickAtRequest,
    ClickRequest,
    EvaluateRequest,
    KeyboardTypeRequest,
    NavigateRequest,
    PressRequest,
    SelectRequest,
    TabNewRequest,
    TypeRequest,
    WaitRequest,
)

MAX_STEPS = 100
MAX_SLEEP_MS = 60_000
MAX_PARALLEL = 16


class StepError(ValueError):
    pass


class _NoParams(BaseModel):
    pass


class _TabIndex(BaseModel):
    index: int = Field(ge=0)


class _Sleep(BaseModel):
    ms: int = Field(ge=0, le=MAX_SLEEP_MS)


STEP_SCHEMAS: dict[str, type[BaseModel]] = {
    "navigate": NavigateRequest,
    "back": _NoParams,
    "click": ClickRequest,
    "type": TypeRequest,
    "select": SelectRequest,
    "press": PressRequest,
    "click_at": ClickAtRequest,
    "keyboard_type": KeyboardTypeRequest,
    "wait_for": WaitRequest,
    "evaluate": EvaluateRequest,
    "tab_new": TabNewRequest,
    "tab_select": _TabIndex,
    "tab_close": _TabIndex,
    "sleep": _Sleep,  # waits in the caller; never reaches the browser
}

_ALIASES = {"wait": "wait_for", "keyboard": "keyboard_type"}


def _two(arg: str) -> tuple[str, str | None]:
    first, sep, second = arg.partition(" | ")
    return first.strip(), second if sep else None


def _number(arg: str) -> float:
    try:
        return float(arg)
    except ValueError:
        raise StepError(f"'{arg}' is not a number") from None


def _press(arg: str) -> dict[str, Any]:
    key, selector = _two(arg)
    return {"key": key, "selector": selector}


def _click_at(arg: str) -> dict[str, Any]:
    parts = arg.split()
    if len(parts) != 2:
        raise StepError("expected 'click_at X Y'")
    return {"x": _number(parts[0]), "y": _number(parts[1])}


def _pair(first: str, second: str) -> Callable[[str], dict[str, Any]]:
    def parse(arg: str) -> dict[str, Any]:
        a, b = _two(arg)
        if b is None:
            raise StepError(f"expected '<{first}> | <{second}>'")
        return {first: a, second: b}

    return parse


_PARSERS: dict[str, Callable[[str], dict[str, Any]]] = {
    "navigate": lambda a: {"url": a},
    "back": lambda a: {},
    "click": lambda a: {"selector": a},
    "type": _pair("selector", "text"),
    "select": _pair("selector", "value"),
    "press": _press,
    "click_at": _click_at,
    "keyboard_type": lambda a: {"text": a},
    "wait_for": lambda a: {"selector": a},
    "evaluate": lambda a: {"script": a},
    "tab_new": lambda a: {"url": a or None},
    "tab_select": lambda a: {"index": int(_number(a))},
    "tab_close": lambda a: {"index": int(_number(a))},
    "sleep": lambda a: {"ms": int(_number(a))},
}
_OPTIONAL_ARG = {"back", "tab_new"}


def _validate(action: Any, params: Any, where: str) -> dict[str, Any]:
    schema = STEP_SCHEMAS.get(action) if isinstance(action, str) else None
    if schema is None:
        raise StepError(f"{where}: unknown action '{action}'")
    try:
        model = schema.model_validate(params or {})
    except ValidationError as e:
        err = e.errors()[0]
        field = ".".join(str(p) for p in err["loc"])
        raise StepError(f"{where} ({action}): {field} {err['msg']}".replace(" :", ":")) from None
    return {"action": action, "params": model.model_dump()}


def _check_count(steps: list) -> None:
    if not steps:
        raise StepError("No steps")
    if len(steps) > MAX_STEPS:
        raise StepError(f"At most {MAX_STEPS} steps")


def validate_steps(raw: list[Any]) -> list[dict[str, Any]]:
    """Check JSON steps against the page API's schemas."""
    _check_count(raw)
    out = []
    for i, step in enumerate(raw, 1):
        if not isinstance(step, dict):
            raise StepError(f"Step {i}: expected an object with 'action' and 'params'")
        out.append(_validate(step.get("action"), step.get("params"), f"Step {i}"))
    return out


def parse_script(text: str) -> list[dict[str, Any]]:
    """Parse the one-step-per-line format; lines starting with // are comments."""
    steps = []
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        word, _, arg = line.partition(" ")
        action, arg = _ALIASES.get(word.lower(), word.lower()), arg.strip()
        where = f"Line {number}"
        parser = _PARSERS.get(action)
        if parser is None:
            raise StepError(f"{where}: unknown action '{word}'")
        if not arg and action not in _OPTIONAL_ARG:
            raise StepError(f"{where} ({action}): missing argument")
        try:
            params = parser(arg)
        except StepError as e:
            raise StepError(f"{where} ({action}): {e}") from None
        steps.append(_validate(action, params, where))
    _check_count(steps)
    return steps


def _run_one(bl: Any, name: str, steps: list[dict[str, Any]], stop_on_error: bool) -> dict:
    done, errors = 0, []
    for i, step in enumerate(steps, 1):
        try:
            if step["action"] == "sleep":
                time.sleep(step["params"]["ms"] / 1000)
            else:
                bl.control(name, step["action"], step["params"])
            done += 1
        except Exception as e:  # one profile's failure must not end the others' run
            errors.append(f"step {i} ({step['action']}): {e}")
            if stop_on_error:
                break
    return {"ok": not errors, "done": done, "error": "; ".join(errors) or None}


def run_steps(
    bl: Any,
    names: list[str],
    steps: list[dict[str, Any]],
    stop_on_error: bool = True,
) -> dict[str, dict[str, Any]]:
    """Run `steps` on each profile: in order within one, profiles in parallel.

    Returns {name: {"ok", "done", "error"}}."""
    # ponytail: blocks until every profile finishes (the API request waits too).
    # Upgrade path: a background job with a status endpoint if runs grow past minutes.
    if not names:
        return {}
    with ThreadPoolExecutor(max_workers=min(len(names), MAX_PARALLEL)) as pool:
        futures = {n: pool.submit(_run_one, bl, n, steps, stop_on_error) for n in names}
        return {n: f.result() for n, f in futures.items()}
