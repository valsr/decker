"""Dynamic choice options: the @options registry and the host that answers get_options."""

import threading
from collections.abc import Callable
from typing import Any, TypeVar

from ._context import Sender
from ._variables import _call

# The limits and cleaning rules mirror decker.common.options (the SDK cannot import the server package); keep in sync.
MAX_OPTIONS = 256
MAX_TEXT = 256

_F = TypeVar("_F", bound=Callable[..., Any])

_SOURCES: dict[str, Callable[..., Any]] = {}
_LOCK = threading.Lock()


def options(source: str) -> Callable[[_F], _F]:
    """Register ``fn()`` (sync or async) as the supplier of the options for a manifest ``options_source``."""

    def decorate(fn: _F) -> _F:
        with _LOCK:
            _SOURCES[source] = fn
        return fn

    return decorate


def reset() -> None:
    """Forget every registered options source (for tests)."""
    with _LOCK:
        _SOURCES.clear()


def _find(source: Any) -> Callable[..., Any] | None:
    if not isinstance(source, str):
        return None
    with _LOCK:
        return _SOURCES.get(source)


def _entry(item: Any) -> dict[str, Any] | None:
    if isinstance(item, str):
        return {"label": item, "value": item}
    if isinstance(item, dict):
        return item
    if isinstance(item, (tuple, list)) and len(item) == 2:
        return {"label": item[0], "value": item[1]}
    return None


def clean(raw: Any) -> list[dict[str, str]]:
    """Keep well-formed, unique-valued ``{label, value}`` string entries, cut and capped to the limits."""
    if not isinstance(raw, list):
        return []
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict) or not isinstance(entry.get("label"), str):
            continue
        if not isinstance(entry.get("value"), str):
            continue
        value = entry["value"][:MAX_TEXT]
        if value in seen:
            continue
        seen.add(value)
        result.append({"label": entry["label"][:MAX_TEXT], "value": value})
        if len(result) >= MAX_OPTIONS:
            break
    return result


def normalise(result: list[Any] | tuple[Any, ...]) -> list[dict[str, str]]:
    """Turn a supplier's list of dicts, pairs or strings into cleaned options."""
    entries = (_entry(item) for item in result)
    return clean([entry for entry in entries if entry is not None])


class OptionsHost:
    """Answers get_options requests for one runtime."""

    def __init__(self, send: Sender, log_error: Callable[[str], None]) -> None:
        self._send = send
        self._log_error = log_error

    def answer(self, payload: dict[str, Any]) -> None:
        """Answer a get_options request with options_result."""
        request = payload.get("request")
        if not isinstance(request, str):
            return
        fn = _find(payload.get("source"))
        if fn is None:
            self._reply(request, False, error="unknown options source")
            return
        try:
            raw = _call(fn)
            if not isinstance(raw, (list, tuple)):
                self._reply(request, False, error="options must be a list")
                return
            options_list = normalise(raw)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self._log_error(f"options source {payload.get('source')} failed: {type(exc).__name__}: {exc}")
            self._reply(request, False, error=f"{type(exc).__name__}: {exc}"[:MAX_TEXT])
            return
        self._reply(request, True, options=options_list)

    def _reply(self, request: str, ok: bool, **extra: Any) -> None:
        self._send("options_result", None, {"request": request, "ok": ok, **extra})
