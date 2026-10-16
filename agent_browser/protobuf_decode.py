"""
Blackbox protobuf + gRPC-Web decoding (no .proto schema required).

Decodes the protobuf wire format generically: field numbers, wire types,
varints, UTF-8 strings, and nested messages (heuristic recursion). For
gRPC-Web, splits the flag+length framed stream (data frames and the 0x80
trailer frame). Interpretations are labeled as wire-level facts; without a
schema, field *meanings* are unknown and nested/string guesses are marked.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

MAX_DEPTH = 4
MAX_FIELDS = 200


def _varint(data: bytes, pos: int) -> Tuple[int, int]:
    result = 0
    shift = 0
    while pos < len(data) and shift < 70:
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
    raise ValueError("truncated or overlong varint")


def _looks_text(raw: bytes) -> Optional[str]:
    try:
        text = raw.decode("utf-8")
    except Exception:
        return None
    if all(ch.isprintable() or ch in "\r\n\t" for ch in text):
        return text
    return None


def decode_protobuf(data: bytes, _depth: int = 0) -> Dict[str, Any]:
    """
    Decode one protobuf message. Raises ValueError when the bytes do not
    parse cleanly to the end (callers use that as a plausibility check).
    """
    fields: List[Dict[str, Any]] = []
    pos = 0
    while pos < len(data):
        if len(fields) >= MAX_FIELDS:
            raise ValueError("too many fields")
        tag, pos = _varint(data, pos)
        field_no, wire = tag >> 3, tag & 0x07
        if field_no == 0:
            raise ValueError("field number 0 is invalid")
        entry: Dict[str, Any] = {"field": field_no, "wire_type": wire}
        if wire == 0:  # varint
            value, pos = _varint(data, pos)
            entry["value"] = value
        elif wire == 1:  # 64-bit
            if pos + 8 > len(data):
                raise ValueError("truncated fixed64")
            entry["value"] = int.from_bytes(data[pos:pos + 8], "little")
            pos += 8
        elif wire == 5:  # 32-bit
            if pos + 4 > len(data):
                raise ValueError("truncated fixed32")
            entry["value"] = int.from_bytes(data[pos:pos + 4], "little")
            pos += 4
        elif wire == 2:  # length-delimited
            length, pos = _varint(data, pos)
            if pos + length > len(data):
                raise ValueError("truncated length-delimited field")
            raw = data[pos:pos + length]
            pos += length
            text = _looks_text(raw)
            nested = None
            if _depth < MAX_DEPTH and raw and text is None:
                try:
                    nested = decode_protobuf(raw, _depth + 1)
                except Exception:
                    nested = None
            if nested is not None and nested["fields"]:
                entry["nested"] = nested
            elif text is not None:
                entry["value"] = text
            else:
                entry["value"] = f"[bytes {length}: {raw[:24].hex()}]"
        else:
            raise ValueError(f"unsupported wire type {wire}")
        fields.append(entry)
    return {"fields": fields, "field_count": len(fields)}


def try_decode_protobuf(data: bytes) -> Optional[Dict[str, Any]]:
    """Decode when the bytes plausibly are a whole protobuf message."""
    if not data:
        return None
    try:
        out = decode_protobuf(data)
    except Exception:
        return None
    return out if out["fields"] else None


def decode_grpc_web(data: bytes) -> Dict[str, Any]:
    """
    Split a gRPC-Web body: repeated [flag:1][length:4 BE][payload].
    Flag 0x00 = data (protobuf), 0x80 = trailers (key: value lines).
    """
    frames: List[Dict[str, Any]] = []
    pos = 0
    while pos + 5 <= len(data):
        flag = data[pos]
        length = int.from_bytes(data[pos + 1:pos + 5], "big")
        payload = data[pos + 5:pos + 5 + length]
        if len(payload) < length:
            break
        frame: Dict[str, Any] = {
            "kind": "trailers" if flag & 0x80 else "data",
            "compressed": bool(flag & 0x01),
            "length": length,
        }
        if flag & 0x80:
            frame["trailers"] = payload.decode("utf-8", errors="replace")[:500]
        else:
            frame["protobuf"] = try_decode_protobuf(payload)
        frames.append(frame)
        pos += 5 + length
    return {"frames": frames, "frame_count": len(frames), "trailing_bytes": len(data) - pos}
