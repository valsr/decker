"""Core: the actions Decker ships, run as a built-in plugin process (stdlib and the SDK only)."""

import http.client
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any

from decker_plugin_sdk import ActionCallContext, ActionError, action, call_action

_HTTP_SCHEMES = ("http://", "https://")
_MAX_STATUS_OK = 400
_MAX_BODY_BYTES = 512 * 1024
_DEFAULT_TIMEOUT_S = 10
_MAX_TIMEOUT_S = 25.0  # a gesture call waits 30 s (the default call timeout); stop requesting before it gives up
_MAX_RESULT_BYTES = 1024 * 1024 - 1024  # the SDK's 1 MiB value limit (JSON-escaped, ASCII) less a margin

_STEP_COUNT = 10
_SEQUENCE_BUDGET_S = 300.0


def _fit(status: int, text: str) -> dict[str, Any]:
    """The result with the longest body prefix whose JSON encoding fits the value limit."""

    def size(count: int) -> int:
        return len(json.dumps({"status": status, "body": text[:count]}).encode("utf-8"))

    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if size(middle) <= _MAX_RESULT_BYTES:
            low = middle
        else:
            high = middle - 1
    return {"status": status, "body": text[:low]}


@action("com.decker.core.httprequest")
def http_request(ctx: ActionCallContext) -> dict[str, Any]:
    """Send one HTTP request; a status below 400 returns its status and body, anything else fails."""
    args = ctx.args
    url = str(args.get("url", "")).strip()
    if not url.lower().startswith(_HTTP_SCHEMES):
        raise ActionError("the URL must start with http:// or https://")
    body = str(args.get("body", ""))
    request = urllib.request.Request(  # noqa: S310  # the scheme was checked above
        url,
        data=body.encode("utf-8") if body else None,
        method=str(args.get("method") or "GET"),
    )
    content_type = str(args.get("content_type", "")).strip()
    if content_type:
        request.add_header("Content-Type", content_type)
    timeout = min(float(args.get("timeout") or _DEFAULT_TIMEOUT_S), _MAX_TIMEOUT_S)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            status = int(response.status)
            data: bytes = response.read(_MAX_BODY_BYTES)
    except urllib.error.HTTPError as exc:
        raise ActionError(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
        raise ActionError(str(getattr(exc, "reason", exc))) from exc
    if status >= _MAX_STATUS_OK:
        raise ActionError(f"HTTP {status}")
    return _fit(status, data.decode("utf-8", errors="replace"))


@action("com.decker.core.delay")
def delay(ctx: ActionCallContext) -> None:
    """Wait the given number of seconds."""
    time.sleep(max(0.0, float(ctx.args.get("seconds", 1.0))))


def _steps(args: dict[str, Any]) -> list[tuple[int, str, dict[str, Any]]]:
    """The configured steps as (number, action id, params); steps with no action are skipped."""
    steps = []
    for number in range(1, _STEP_COUNT + 1):
        action_id = str(args.get(f"step_{number}") or "")
        if action_id:
            params = args.get(f"step_{number}_params")
            steps.append((number, action_id, params if isinstance(params, dict) else {}))
    return steps


@action("com.decker.core.sequence")
def sequence(ctx: ActionCallContext) -> None:
    """Run the configured actions one after another within five minutes of starting."""
    deadline = time.monotonic() + _SEQUENCE_BUDGET_S
    stop_on_error = bool(ctx.args.get("stop_on_error", True))
    for number, action_id, params in _steps(ctx.args):
        left = deadline - time.monotonic()
        if left <= 0:
            raise ActionError(f"step {number}: out of time")
        try:
            call_action(action_id, params, timeout=left)
        except ActionError as exc:
            if stop_on_error:
                raise ActionError(f"step {number} ({action_id}): {exc}") from exc
            print(f"com.decker.core: step {number} ({action_id}) failed: {exc}", file=sys.stderr, flush=True)
