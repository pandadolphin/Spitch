"""Shared voice types used by Doubao and Grok STT clients."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass
class TranscriptEvent:
    """One transcription update from a streaming STT provider.

    ``text`` is the session-facing accumulated text so far (suitable for
    tray preedit / inject). ``is_final`` is the provider finality flag
    (Doubao: utterance ``definite``; Grok: session ``transcript.done`` only).
    ``raw`` is the provider-native event/payload dict — Grok events must
    never carry Doubao-shaped ``result.utterances``.
    """

    text: str
    is_final: bool
    raw: dict | Mapping
