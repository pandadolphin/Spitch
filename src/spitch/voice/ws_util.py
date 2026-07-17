"""WebSocket connect helpers shared by Doubao and Grok clients.

KD-16: ``websockets>=12,<16`` changed the top-level ``connect`` header
kwarg name between major lines:

* 12.x / 13.x — ``extra_headers``
* 14.x / 15.x — ``additional_headers``

Calling ``additional_headers=`` unconditionally is a latent bug on 12–13.
Prefer ``inspect.signature`` over version parsing.
"""

from __future__ import annotations

import inspect
from typing import Any, Mapping, Sequence


def _normalize_headers(
    headers: Sequence[tuple[str, str]] | Mapping[str, str],
) -> list[tuple[str, str]]:
    if isinstance(headers, Mapping):
        return [(str(k), str(v)) for k, v in headers.items()]
    return [(str(k), str(v)) for k, v in headers]


def _header_kwarg_name(connect_fn: Any) -> str:
    """Return the header kwarg accepted by ``connect_fn``."""
    try:
        sig = inspect.signature(connect_fn)
        params = sig.parameters
    except (TypeError, ValueError):
        params = {}
    if "additional_headers" in params:
        return "additional_headers"
    if "extra_headers" in params:
        return "extra_headers"
    # Fallback: some wrappers accept **kwargs only — prefer the modern name.
    for name in ("additional_headers", "extra_headers"):
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return "additional_headers"
    raise RuntimeError(
        "websockets.connect accepts neither additional_headers nor extra_headers; "
        "upgrade or pin a supported websockets version (12–15)"
    )


async def ws_connect(
    url: str,
    *,
    headers: Sequence[tuple[str, str]] | Mapping[str, str],
    **kwargs: Any,
):
    """Connect with the correct header kwarg for the installed websockets.

    ``close_timeout`` and other kwargs are passed through unchanged when
    accepted by the active ``connect`` implementation.
    """
    import websockets  # lazy — optional dep at package level

    connect_fn = websockets.connect
    hdr_name = _header_kwarg_name(connect_fn)
    hdr_list = _normalize_headers(headers)
    connect_kwargs = dict(kwargs)
    connect_kwargs[hdr_name] = hdr_list
    return await connect_fn(url, **connect_kwargs)
