"""Provider-aware streaming client factory.

Pure helpers importable without starting the daemon — unit-tested for
provider branching and KD-22 Mapping guards (M4).
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from spitch.config import ConfigError

from .doubao import DoubaoClient, DoubaoCredentials
from .grok_stt import GrokSttClient, GrokSttCredentials, validate_grok_endpoint


def _require_mapping_section(cfg: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """Return section if Mapping; raise :class:`ConfigError` if present but wrong type.

    Missing / null sections yield ``{}`` so callers can report incomplete
    credentials. A present non-mapping (string, list) must not surface as
    ``AttributeError`` on ``.get`` (M4 / KD-22).
    """
    raw = cfg.get(key)
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ConfigError(
            f"config section {key!r} must be a JSON object, "
            f"got {type(raw).__name__}"
        )
    return raw


def _doubao_creds(cfg: Mapping[str, Any]) -> DoubaoCredentials:
    d = _require_mapping_section(cfg, "doubao")
    return DoubaoCredentials(
        app_key=str(d.get("app_key") or ""),
        access_key=str(d.get("access_key") or ""),
        resource_id=str(
            d.get("resource_id") or "volc.bigasr.sauc.duration"
        ),
        endpoint=str(
            d.get("endpoint")
            or "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel"
        ),
    )


def _grok_creds(
    cfg: Mapping[str, Any],
    *,
    allow_insecure_localhost: bool = False,
) -> GrokSttCredentials:
    """Build Grok credentials from config.

    Production factory keeps ``allow_insecure_localhost=False`` (default).
    Probe / local mocks may pass True so ``ws://127.0.0.1`` is accepted (KD-21).
    """
    g = _require_mapping_section(cfg, "grok")
    endpoint = str(g.get("endpoint") or "wss://api.x.ai/v1/stt")
    # Validate early so bad schemes fail at factory/probe time, not mid-session.
    validate_grok_endpoint(
        endpoint, allow_insecure_localhost=allow_insecure_localhost
    )
    endpointing = g.get("endpointing_ms")
    if endpointing is not None:
        try:
            endpointing = int(endpointing)
        except (TypeError, ValueError):
            endpointing = None
    return GrokSttCredentials(
        api_key=str(g.get("api_key") or ""),
        endpoint=endpoint,
        language=str(g.get("language") or ""),
        interim_results=bool(g.get("interim_results", True)),
        endpointing_ms=endpointing,
        filler_words=bool(g.get("filler_words", False)),
        send_finalize_on_eos=bool(g.get("send_finalize_on_eos", True)),
    )


def make_streaming_client(
    cfg: Mapping[str, Any], *, sample_rate: int
) -> DoubaoClient | GrokSttClient:
    """Construct a :class:`StreamingClient` for ``cfg['provider']``."""
    provider = cfg.get("provider") or "doubao"
    if provider == "doubao":
        return DoubaoClient(_doubao_creds(cfg), sample_rate=sample_rate)
    if provider == "grok":
        return GrokSttClient(_grok_creds(cfg), sample_rate=sample_rate)
    raise RuntimeError(f"unsupported provider: {provider!r}")


def make_client_factory(
    cfg: Mapping[str, Any], *, sample_rate: int
) -> Callable[[], DoubaoClient | GrokSttClient]:
    """Return a zero-arg factory suitable for :class:`VoiceController`."""

    def _factory() -> DoubaoClient | GrokSttClient:
        return make_streaming_client(cfg, sample_rate=sample_rate)

    return _factory


__all__ = [
    "make_client_factory",
    "make_streaming_client",
    "_doubao_creds",
    "_grok_creds",
]
