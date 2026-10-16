"""
Fast JSON codec for CDP frames.

Uses ``msgspec`` (SIMD-backed, avoids per-message dict overhead on the decode
hot path) when the optional ``[fast]`` extra is installed, and falls back to
the standard library with an identical API. CDP frames arrive as text; the
decode path encodes once to bytes so msgspec can parse without building
intermediate Python strings per field.
"""

from __future__ import annotations

import json as _json
from typing import Any

try:  # optional [fast] extra
    import msgspec as _msgspec
    from msgspec import Raw as _Raw
    from msgspec import Struct as _Struct

    _decoder = _msgspec.json.Decoder()
    _encoder = _msgspec.json.Encoder()
    FAST_CODEC = "msgspec"

    class Envelope(_Struct, gc=False):
        """Cheap routing view of a CDP frame: params/result stay Raw."""

        id: int | None = None
        method: str | None = None
        sessionId: str | None = None
        params: _Raw | None = None
        result: _Raw | None = None
        error: _Raw | None = None

    _envelope_decoder = _msgspec.json.Decoder(Envelope)
except Exception:  # pragma: no cover - exercised only without the extra
    _decoder = None
    _encoder = None
    _envelope_decoder = None
    Envelope = None  # type: ignore
    FAST_CODEC = "stdlib"


def decode_envelope(data: Any):
    """
    Decode only the routing envelope of a frame (id/method/sessionId), with
    params/result/error left as undecoded Raw slices. Returns None when the
    fast codec is unavailable or the frame is not an envelope.
    """
    if _envelope_decoder is None:
        return None
    if isinstance(data, str):
        data = data.encode("utf-8")
    try:
        return _envelope_decoder.decode(data)
    except Exception:
        return None


def decode_raw(raw: Any) -> Any:
    """Decode a Raw slice captured by the envelope (or plain bytes/str)."""
    if raw is None:
        return None
    return loads(bytes(raw) if not isinstance(raw, (bytes, str)) else raw)


def loads(data: Any) -> Any:
    """Parse one CDP frame (str or bytes) into Python objects."""
    if _decoder is not None:
        if isinstance(data, str):
            data = data.encode("utf-8")
        return _decoder.decode(data)
    if isinstance(data, (bytes, bytearray)):
        data = data.decode("utf-8")
    return _json.loads(data)


def dumps(obj: Any) -> str:
    """Serialize one CDP command frame to text (WebSocket text frame)."""
    if _encoder is not None:
        return _encoder.encode(obj).decode("utf-8")
    return _json.dumps(obj)
