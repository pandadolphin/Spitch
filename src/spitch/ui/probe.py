"""Synchronous auth probe used by ``spitch-config``.

Supports Doubao and Grok providers. Runs the async client ``.probe`` from a
thread + event loop so the GTK dialog stays responsive. The result is
``(ok, message)`` where ``ok`` is False on connection / auth / protocol
errors and ``message`` is human-readable.

Grok probe success requires ``transcript.done`` (KD-20). Handshake failures
are status-classified via :func:`classify_ws_connect_error`. Non-``wss://``
Grok endpoints are rejected before connect (KD-21).
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping, Tuple

from ..voice.doubao import DoubaoClient, DoubaoCredentials, DoubaoProtocolError
from ..voice.factory import _doubao_creds, _grok_creds
from ..voice.grok_stt import (
    GrokProtocolError,
    GrokSttClient,
    GrokSttCredentials,
    classify_ws_connect_error,
    validate_grok_endpoint,
)


def _sample_rate_from_cfg(cfg: Mapping[str, Any]) -> int:
    audio = cfg.get("audio")
    if not isinstance(audio, Mapping):
        return 16000
    try:
        return int(audio.get("sample_rate") or 16000)
    except (TypeError, ValueError):
        return 16000


def _run_async(coro_factory) -> Tuple[bool, str]:
    """Run an async ``() -> (ok, msg)`` factory under asyncio.run or a fresh loop."""

    try:
        return asyncio.run(coro_factory())
    except RuntimeError:
        # Already-running loop (very unlikely from GTK main thread but
        # be defensive): make a fresh loop in this thread.
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro_factory())
        finally:
            loop.close()


def probe_credentials(creds: DoubaoCredentials, *, timeout: float = 8.0) -> Tuple[bool, str]:
    """Open the Doubao WS, send a zero-length stream, expect a non-error reply.

    Errors are wrapped into a friendly message. The probe deliberately
    does NOT require any audio device — it just round-trips the
    handshake + control frames so we can tell the user "config is OK".
    """

    async def _go() -> Tuple[bool, str]:
        try:
            async with DoubaoClient(creds) as client:
                await client.probe(timeout=timeout)
            return True, "Doubao connection succeeded — credentials accepted."
        except DoubaoProtocolError as exc:
            return False, f"Server rejected the credentials: {exc}"
        except asyncio.TimeoutError:
            return False, "Timed out waiting for the Doubao server."
        except Exception as exc:  # network, DNS, TLS, etc.
            return False, f"Cannot reach Doubao endpoint: {exc!r}"

    return _run_async(_go)


def probe_grok_credentials(
    creds: GrokSttCredentials,
    *,
    timeout: float = 8.0,
    sample_rate: int = 16000,
    allow_insecure_localhost: bool = False,
) -> Tuple[bool, str]:
    """Probe Grok STT: connect + silence + require ``transcript.done`` (KD-20).

    Endpoint scheme is validated first (KD-21). Handshake failures are mapped
    with :func:`classify_ws_connect_error` so 401 is not confused with 429/5xx.
    """
    try:
        validate_grok_endpoint(
            creds.endpoint,
            allow_insecure_localhost=allow_insecure_localhost,
        )
    except GrokProtocolError as exc:
        return False, f"Invalid Grok endpoint: {exc}"

    async def _go() -> Tuple[bool, str]:
        try:
            async with GrokSttClient(
                creds,
                sample_rate=sample_rate,
                allow_insecure_localhost=allow_insecure_localhost,
            ) as client:
                await client.probe(timeout=timeout)
            return True, "Grok STT connection succeeded — credentials accepted."
        except GrokProtocolError as exc:
            return False, f"Grok probe failed: {exc}"
        except asyncio.TimeoutError:
            return False, "Timed out waiting for the Grok STT server."
        except Exception as exc:
            _category, msg = classify_ws_connect_error(exc)
            # Prefer status-aware text; append brief type for operators.
            return False, f"Grok connect failed: {msg} ({type(exc).__name__})"

    return _run_async(_go)


def probe_credentials_for_config(
    cfg: Mapping[str, Any],
    *,
    timeout: float = 8.0,
    allow_insecure_localhost: bool = False,
) -> Tuple[bool, str]:
    """Route probe by ``cfg['provider']`` (default ``doubao``).

    Builds credentials from the matching config section. For Grok, rejects
    non-``wss://`` endpoints before connect. Returns ``(ok, message)``.
    """
    provider = cfg.get("provider") or "doubao"
    sample_rate = _sample_rate_from_cfg(cfg)

    if provider == "doubao":
        try:
            creds = _doubao_creds(cfg)
        except Exception as exc:
            return False, f"Invalid Doubao config: {exc}"
        return probe_credentials(creds, timeout=timeout)

    if provider == "grok":
        # Validate endpoint early for a clear UI message even if factory
        # would also raise; then build full credentials.
        grok_section = cfg.get("grok")
        if grok_section is not None and not isinstance(grok_section, Mapping):
            return False, "Invalid Grok config: section must be a JSON object"
        endpoint = ""
        if isinstance(grok_section, Mapping):
            endpoint = str(grok_section.get("endpoint") or "wss://api.x.ai/v1/stt")
        else:
            endpoint = "wss://api.x.ai/v1/stt"
        try:
            validate_grok_endpoint(
                endpoint,
                allow_insecure_localhost=allow_insecure_localhost,
            )
        except GrokProtocolError as exc:
            return False, f"Invalid Grok endpoint: {exc}"
        try:
            creds = _grok_creds(cfg)
        except GrokProtocolError as exc:
            return False, f"Invalid Grok endpoint: {exc}"
        except Exception as exc:
            return False, f"Invalid Grok config: {exc}"
        return probe_grok_credentials(
            creds,
            timeout=timeout,
            sample_rate=sample_rate,
            allow_insecure_localhost=allow_insecure_localhost,
        )

    return False, f"Unsupported provider: {provider!r}"
