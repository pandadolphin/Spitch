"""Voice subsystem: Doubao/Grok clients, audio capture, push-to-talk controller."""

from .audio import AudioCapture, AudioCaptureError, AudioConfig
from .controller import State, TranscriptUpdate, VoiceController
from .doubao import (
    DoubaoClient,
    DoubaoCredentials,
    DoubaoFrameCodec,
    DoubaoProtocolError,
    Frame,
    auth_headers,
    build_request_meta,
    encode_audio,
    encode_full_request,
    extract_full_text,
    extract_text,
)
from .factory import make_client_factory, make_streaming_client
from .grok_stt import (
    GrokProtocolError,
    GrokSttClient,
    GrokSttCredentials,
)
from .types import TranscriptEvent

__all__ = [
    "AudioCapture",
    "AudioCaptureError",
    "AudioConfig",
    "DoubaoClient",
    "DoubaoCredentials",
    "DoubaoFrameCodec",
    "DoubaoProtocolError",
    "Frame",
    "GrokProtocolError",
    "GrokSttClient",
    "GrokSttCredentials",
    "State",
    "TranscriptEvent",
    "TranscriptUpdate",
    "VoiceController",
    "auth_headers",
    "build_request_meta",
    "encode_audio",
    "encode_full_request",
    "extract_full_text",
    "extract_text",
    "make_client_factory",
    "make_streaming_client",
]
