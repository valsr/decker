"""Core: the actions Decker ships, run as a built-in plugin process (stdlib and the SDK only)."""

import urllib.error
import urllib.request
from typing import Any

from decker_plugin_sdk import ActionCallContext, ActionError, action

_HTTP_SCHEMES = ("http://", "https://")
_MAX_STATUS_OK = 400
_MAX_BODY_BYTES = 512 * 1024
_DEFAULT_TIMEOUT_S = 10


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
    timeout = float(args.get("timeout") or _DEFAULT_TIMEOUT_S)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            status = int(response.status)
            data: bytes = response.read(_MAX_BODY_BYTES)
    except urllib.error.HTTPError as exc:
        raise ActionError(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ActionError(str(getattr(exc, "reason", exc))) from exc
    if status >= _MAX_STATUS_OK:
        raise ActionError(f"HTTP {status}")
    return {"status": status, "body": data.decode("utf-8", errors="replace")}
