"""Cycle 42 tests: WASM section/import/export parsing."""

import base64
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agent_browser.source_inspector import SourceInspector
from agent_browser.wasm_inspect import parse_wasm

# Minimal module: type () -> (), one function exported as "f", empty body.
WASM = bytes.fromhex(
    "0061736d01000000"  # magic + version
    "010401600000"      # type section: 1 type func() -> ()
    "03020100"          # function section: 1 function, type 0
    "07050101660000"    # export section: "f" func 0
    "0a040102000b"      # code section: 1 body (size 2: locals 0, end)
)


def test_parse_wasm_sections_and_exports():
    info = parse_wasm(WASM)
    assert info["version"] == 1
    assert info["function_count"] == 1
    assert info["exports"] == [{"name": "f", "kind": "func", "index": 0}]
    names = [s["name"] for s in info["sections"]]
    assert names == ["type", "function", "export", "code"]


def test_parse_wasm_rejects_bad_magic():
    with pytest.raises(ValueError):
        parse_wasm(b"not wasm at all")


class _FakeCDP:
    async def send(self, method, params=None, session_id=None, timeout=15):
        if method == "Debugger.getScriptSource":
            return {"bytecode": base64.b64encode(WASM).decode()}
        if method == "Debugger.disassembleWasmModule":
            return {"streamId": "s1", "totalNumberOfLines": 3,
                    "chunk": {"lines": ["func $f", "  nop"], "bytecodeOffsets": [0, 1]}}
        if method == "Debugger.nextWasmDisassemblyChunk":
            return {"chunk": {"lines": ["  end"], "bytecodeOffsets": [2]}}
        return {}


@pytest.mark.asyncio
async def test_inspector_wasm_paths():
    insp = SourceInspector(_FakeCDP(), lambda: "sid")
    insp.scripts["7"] = {"url": "https://x/mod.wasm", "is_wasm": True, "language": "WebAssembly", "length": len(WASM)}
    assert insp.list_wasm_modules()[0]["scriptId"] == "7"
    info = await insp.inspect_wasm("7")
    assert info["function_count"] == 1
    dis = await insp.disassemble_wasm("7", max_chunks=2)
    assert dis["lines"] == ["func $f", "  nop", "  end"]
    assert dis["truncated"] is True  # chunk cap honored even if the stream repeats
