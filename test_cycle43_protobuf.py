"""Cycle 43 tests: blackbox protobuf + gRPC-Web decoding."""

import base64
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.protobuf_decode import decode_grpc_web, decode_protobuf, try_decode_protobuf
from agent_browser.stream_decode import decode_ws_payload


def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _field_varint(no, value):
    return _varint((no << 3) | 0) + _varint(value)


def _field_bytes(no, raw):
    return _varint((no << 3) | 2) + _varint(len(raw)) + raw


MSG = _field_varint(1, 150) + _field_bytes(2, b"hello") + _field_bytes(3, _field_varint(1, 7))


def test_decode_protobuf_fields_and_nesting():
    out = decode_protobuf(MSG)
    by_field = {f["field"]: f for f in out["fields"]}
    assert by_field[1]["value"] == 150
    assert by_field[2]["value"] == "hello"
    assert by_field[3]["nested"]["fields"][0]["value"] == 7
    assert try_decode_protobuf(b"\xff\xff\xff") is None
    assert try_decode_protobuf(b"") is None


def test_decode_grpc_web_frames():
    body = b"\x00" + len(MSG).to_bytes(4, "big") + MSG
    trailers = b"grpc-status: 0\r\n"
    body += b"\x80" + len(trailers).to_bytes(4, "big") + trailers
    out = decode_grpc_web(body)
    assert out["frame_count"] == 2
    assert out["frames"][0]["protobuf"]["field_count"] == 3
    assert "grpc-status: 0" in out["frames"][1]["trailers"]


def test_ws_binary_payload_decodes_as_protobuf():
    payload = base64.b64encode(MSG).decode()
    out = decode_ws_payload(payload, opcode=2)
    assert out["kind"] == "protobuf"
    assert out["protobuf"]["field_count"] == 3
    assert decode_ws_payload(base64.b64encode(b"\x01\x02\x03").decode(), opcode=2)["kind"] in ("binary", "protobuf")
