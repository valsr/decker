"""Plugin lifecycle hooks (init/main) and global template variables: registry, context and helpers."""

import asyncio
import inspect
import re
import threading
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar, overload

from ._context import Sender

MAX_VALUE_LENGTH = 1024
_MAX_ERROR_TEXT = 256
DEFAULT_INTERVAL = 2.0

# The name rules below mirror decker.common.variables (the SDK cannot import the server package); keep in sync.
_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")

_F = TypeVar("_F", bound=Callable[..., Any])

_INIT: list[Callable[..., Any]] = []
_MAIN: list[tuple[Callable[..., Any], float | None]] = []
_GETTERS: dict[str, Callable[..., Any]] = {}
_PATTERNS: list[tuple[str, Callable[..., Any]]] = []
_current: "VariableHost | None" = None


def init(fn: _F) -> _F:
    """Register ``fn(ctx: PluginContext)``; it runs once in the lifecycle thread after the server is ready."""
    _INIT.append(fn)
    return fn


@overload
def main(interval: _F) -> _F: ...


@overload
def main(interval: float | None = DEFAULT_INTERVAL) -> Callable[[_F], _F]: ...


def main(interval: Any = DEFAULT_INTERVAL) -> Any:
    """Register ``fn(ctx)`` to run every ``interval`` seconds (bare: 2 s); ``None`` runs an async main once."""
    if callable(interval):
        _MAIN.append((interval, DEFAULT_INTERVAL))
        return interval

    def decorate(fn: _F) -> _F:
        _MAIN.append((fn, interval))
        return fn

    return decorate


def variable(pattern_or_name: str) -> Callable[[_F], _F]:
    """Register a getter for a variable name or ``name_{placeholder}`` pattern; placeholders arrive as keywords."""

    def decorate(fn: _F) -> _F:
        if _PLACEHOLDER.search(pattern_or_name):
            _PATTERNS.append((pattern_or_name, fn))
        else:
            _GETTERS[pattern_or_name] = fn
        return fn

    return decorate


def reset() -> None:
    """Forget every registered hook and the running host (for tests)."""
    global _current  # pylint: disable=global-statement
    _INIT.clear()
    _MAIN.clear()
    _GETTERS.clear()
    _PATTERNS.clear()
    _current = None


def has_hooks() -> bool:
    """Whether the plugin registered an init or main hook."""
    return bool(_INIT or _MAIN)


def set_current(host: "VariableHost | None") -> None:
    """Make host the target of the module-level helpers."""
    global _current  # pylint: disable=global-statement
    _current = host


def _host() -> "VariableHost":
    if _current is None:
        raise RuntimeError("no Decker plugin runtime is running")
    return _current


def register_variable(name: str) -> None:
    """Announce a variable (with a matching @variable getter) to the server."""
    _host().register_variable(name)


def unregister_variable(name: str) -> None:
    """Withdraw a variable announced with register_variable."""
    _host().unregister_variable(name)


def signal_variable_updated(name: str) -> None:
    """Tell the server the variable's value changed; its getter is evaluated again."""
    _host().signal_variable_updated(name)


def get_variable(name: str) -> str | None:
    """The current text of a variable known to this plugin, or None (also when no runtime is running)."""
    host = _current
    return host.get_variable(name) if host is not None else None


def has_variable(name: str) -> bool:
    """Whether a variable of this name currently has a value (False when no runtime is running)."""
    host = _current
    return host.has_variable(name) if host is not None else False


def value_text(value: Any) -> str | None:
    """Convert a getter's result to variable text (mirrors decker.common.variables.value_text)."""
    if value is None:
        return None
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (str, int, float)):
        text = str(value)
    else:
        raise ValueError(f"unsupported variable value type: {type(value).__name__}")
    if len(text) > MAX_VALUE_LENGTH:
        raise ValueError(f"variable value longer than {MAX_VALUE_LENGTH} characters")
    return text


def _match_pattern(pattern: str, name: str) -> dict[str, str] | None:
    regex = ""
    last = 0
    for found in _PLACEHOLDER.finditer(pattern):
        regex += re.escape(pattern[last : found.start()]) + f"(?P<{found.group(1)}>[a-z0-9_]+)"
        last = found.end()
    matched = re.fullmatch(regex + re.escape(pattern[last:]), name)
    return matched.groupdict() if matched else None


def _find_getter(name: str) -> tuple[Callable[..., Any], dict[str, str]] | None:
    if name in _GETTERS:
        return _GETTERS[name], {}
    for pattern, getter in _PATTERNS:
        values = _match_pattern(pattern, name)
        if values is not None:
            return getter, values
    return None


def _run_coro(coro: Coroutine[Any, Any, Any]) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    box: list[Any] = []
    error: list[BaseException] = []

    def target() -> None:
        try:
            box.append(asyncio.run(coro))
        except BaseException as exc:  # pylint: disable=broad-exception-caught
            error.append(exc)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return box[0] if box else None


def _call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    result = fn(*args, **kwargs)
    return _run_coro(result) if inspect.iscoroutine(result) else result


class PluginContext:
    """Plugin-wide handle passed to init/main hooks: variable calls and logging."""

    def __init__(self, host: "VariableHost") -> None:
        self._host = host

    def register_variable(self, name: str) -> None:
        """Announce a variable (with a matching @variable getter) to the server."""
        self._host.register_variable(name)

    def unregister_variable(self, name: str) -> None:
        """Withdraw a variable announced with register_variable."""
        self._host.unregister_variable(name)

    def signal_variable_updated(self, name: str) -> None:
        """Tell the server the variable's value changed; its getter is evaluated again."""
        self._host.signal_variable_updated(name)

    def get_variable(self, name: str) -> str | None:
        """The current text of a variable known to this plugin, or None."""
        return self._host.get_variable(name)

    def has_variable(self, name: str) -> bool:
        """Whether a variable of this name currently has a value."""
        return self._host.has_variable(name)

    def log(self, message: str, level: str = "info") -> None:
        """Send a log line to the server."""
        self._host.send("log", None, {"level": level, "message": message})


def safe_error_text(exc: BaseException) -> str:
    """The exception as `Type: message`, always encodable and at most 256 characters."""
    try:
        text = f"{type(exc).__name__}: {exc}"
    except Exception:  # pylint: disable=broad-exception-caught
        text = f"{type(exc).__name__}: {type(exc).__name__}"
    return text.encode("utf-8", "backslashreplace").decode("utf-8")[:_MAX_ERROR_TEXT]


class VariableHost:
    """Runs the lifecycle thread and serves variable requests for one runtime."""

    def __init__(self, send: Sender, log_error: Callable[[str], None]) -> None:
        self.send = send
        self._log_error = log_error
        self._values: dict[str, str] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: "asyncio.Task[Any] | None" = None
        self._ctx = PluginContext(self)

    def start(self) -> None:
        """Start the lifecycle thread when the plugin has init or main hooks."""
        if has_hooks():
            threading.Thread(target=self._lifecycle, name="decker-sdk-lifecycle", daemon=True).start()

    def stop(self) -> None:
        """Stop the lifecycle loop and cancel a running async main."""
        self._stop.set()
        with self._lock:
            loop, task = self._loop, self._task
        if loop is not None and task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  # loop already closed

    def _lifecycle(self) -> None:
        for fn in list(_INIT):
            try:
                _call(fn, self._ctx)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                self._log_error(f"init failed: {type(exc).__name__}: {exc}")
                return
        for fn, interval in list(_MAIN):
            self._loop_main(fn, interval)

    def _loop_main(self, fn: Callable[..., Any], interval: float | None) -> None:
        while not self._stop.is_set():
            try:
                self._run_main(fn)
            except asyncio.CancelledError:
                return
            except Exception as exc:  # pylint: disable=broad-exception-caught
                self._log_error(f"main failed: {type(exc).__name__}: {exc}")
            if interval is None or self._stop.wait(interval):
                return

    def _run_main(self, fn: Callable[..., Any]) -> None:
        if not inspect.iscoroutinefunction(fn):
            fn(self._ctx)
            return
        loop = asyncio.new_event_loop()

        async def runner() -> None:
            with self._lock:
                self._loop, self._task = loop, asyncio.current_task()
            if self._stop.is_set():
                raise asyncio.CancelledError
            await fn(self._ctx)

        try:
            loop.run_until_complete(runner())
        finally:
            with self._lock:
                self._loop = self._task = None
            loop.close()

    def evaluate(self, name: str) -> str | None:
        """Run the getter for name and return its text; raises when there is none or it fails."""
        found = _find_getter(name)
        if found is None:
            raise LookupError(f"no getter for variable {name!r}")
        getter, values = found
        return value_text(_call(getter, **values))

    def answer(self, payload: dict[str, Any]) -> None:
        """Answer a get_variable request."""
        request = payload.get("request")
        try:
            value = self.evaluate(str(payload.get("name")))
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self.send("variable_value", None, {"request": request, "ok": False, "error": safe_error_text(exc)})
            return
        try:
            self.send("variable_value", None, {"request": request, "ok": True, "value": value})
        except UnicodeEncodeError:  # a lone surrogate in the value: answer anyway, or the server waits out its timeout
            self.send("variable_value", None, {"request": request, "ok": False, "error": "value could not be encoded"})

    def changed(self, payload: dict[str, Any]) -> None:
        """Apply a variables_changed event to the local copy."""
        changes, removed = payload.get("set"), payload.get("removed")
        with self._lock:
            if isinstance(changes, dict):
                self._values.update({str(k): str(v) for k, v in changes.items()})
            if isinstance(removed, list):
                for name in removed:
                    self._values.pop(str(name), None)

    def register_variable(self, name: str) -> None:
        """Send register_variable when a getter matches name."""
        if _find_getter(name) is None:
            self._ctx.log(f"register_variable({name!r}): no matching @variable getter", "warning")
            return
        self.send("register_variable", None, {"name": name})

    def unregister_variable(self, name: str) -> None:
        """Send unregister_variable."""
        self.send("unregister_variable", None, {"name": name})

    def signal_variable_updated(self, name: str) -> None:
        """Evaluate the getter, refresh the local copy and send variable_updated."""
        try:
            value = self.evaluate(name)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self._log_error(f"variable {name} failed: {type(exc).__name__}: {exc}")
            return
        with self._lock:
            if value is None:
                self._values.pop(name, None)
            else:
                self._values[name] = value
        self.send("variable_updated", None, {"name": name})

    def get_variable(self, name: str) -> str | None:
        """The local copy of a variable's text."""
        with self._lock:
            return self._values.get(name)

    def has_variable(self, name: str) -> bool:
        """Whether the local copy holds name."""
        with self._lock:
            return name in self._values
