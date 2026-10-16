"""
Minimal WebAssembly binary inspection (pure, no dependencies).

Parses the section table, function count, imports, and exports of a .wasm
module from its raw bytes (as returned base64-encoded by
``Debugger.getScriptSource`` for WebAssembly scripts). Enough to answer
"what does this module import/export and how big is it" without a toolchain;
pair with CDP ``Debugger.disassembleWasmModule`` for instruction text.
"""

from __future__ import annotations

from typing import Any, Dict, List

SECTION_NAMES = {
    0: "custom", 1: "type", 2: "import", 3: "function", 4: "table",
    5: "memory", 6: "global", 7: "export", 8: "start", 9: "element",
    10: "code", 11: "data", 12: "datacount",
}
KIND_NAMES = {0: "func", 1: "table", 2: "memory", 3: "global"}


class _Reader:
    def __init__(self, data: bytes, start: int = 0):
        self.data = data
        self.pos = start

    def u32(self) -> int:
        result = 0
        shift = 0
        while True:
            byte = self.data[self.pos]
            self.pos += 1
            result |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return result
            shift += 7

    def name(self) -> str:
        length = self.u32()
        raw = self.data[self.pos:self.pos + length]
        self.pos += length
        return raw.decode("utf-8", errors="replace")


def parse_wasm(data: bytes) -> Dict[str, Any]:
    """Parse section table + function count + imports/exports."""
    if len(data) < 8 or data[:4] != b"\x00asm":
        raise ValueError("not a WebAssembly module (bad magic)")
    version = int.from_bytes(data[4:8], "little")
    sections: List[Dict[str, Any]] = []
    functions = 0
    imports: List[Dict[str, Any]] = []
    exports: List[Dict[str, Any]] = []
    pos = 8
    while pos < len(data):
        section_id = data[pos]
        pos += 1
        reader = _Reader(data, pos)
        size = reader.u32()
        body_start = reader.pos
        body_end = body_start + size
        sections.append({
            "id": section_id,
            "name": SECTION_NAMES.get(section_id, f"section-{section_id}"),
            "size": size,
            "offset": body_start,
        })
        body = _Reader(data, body_start)
        try:
            if section_id == 3:  # function declarations
                functions = body.u32()
            elif section_id == 2:  # imports
                for _ in range(body.u32()):
                    module = body.name()
                    name = body.name()
                    kind = data[body.pos]
                    body.pos += 1
                    # Skip the kind-specific descriptor (indices/limits/types).
                    if kind == 0:
                        body.u32()
                    elif kind in (1, 2):
                        flags = data[body.pos]
                        body.pos += 1
                        body.u32()
                        if flags & 0x01:
                            body.u32()
                    elif kind == 3:
                        body.pos += 2
                    imports.append({"module": module, "name": name, "kind": KIND_NAMES.get(kind, str(kind))})
            elif section_id == 7:  # exports
                for _ in range(body.u32()):
                    name = body.name()
                    kind = data[body.pos]
                    body.pos += 1
                    index = body.u32()
                    exports.append({"name": name, "kind": KIND_NAMES.get(kind, str(kind)), "index": index})
        except Exception:
            pass  # section listing already recorded; detail parse is best-effort
        pos = body_end
    return {
        "version": version,
        "size_bytes": len(data),
        "sections": sections,
        "function_count": functions,
        "imports": imports,
        "exports": exports,
    }
