"""
Source reconstruction from source maps + endpoint inventory.

Rebuilds the original project tree from a source map's ``sources`` /
``sourcesContent`` (webpack/vite/turbopack bundles) entirely in memory --
no disk writes unless the caller explicitly writes the tree (paths are
confined). The endpoint inventory is a fast lexical scan (fetch / axios /
XHR call sites with URL templates), suitable for mapping an app's API
surface on systems you own or are authorized to test.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

_MAX_MAP_BYTES = 10 * 1024 * 1024


def normalize_source_path(raw: str) -> str:
    """
    Normalize a source-map ``sources`` entry into a safe tree-relative path.
    Strips webpack:// prefixes and collapses dot segments; never absolute
    and never escaping (``..`` segments are dropped, not resolved upward).
    """
    path = (raw or "").strip()
    for prefix in ("webpack://", "turbopack://", "vite://"):
        if path.lower().startswith(prefix):
            path = path[len(prefix):]
            break
    path = path.split("?")[0].split("#")[0].replace("\\", "/")
    parts = []
    for seg in path.split("/"):
        if seg in ("", ".", ".."):
            continue
        parts.append(seg)
    return "/".join(parts) or "unnamed.js"


def parse_sourcemap(map_obj: Dict[str, Any], script_url: str = "") -> Dict[str, Any]:
    """Extract the original file tree from one parsed source map."""
    sources = map_obj.get("sources") or []
    contents = map_obj.get("sourcesContent") or []
    files = []
    for i, src in enumerate(sources):
        content = contents[i] if i < len(contents) else None
        files.append({
            "path": normalize_source_path(str(src)),
            "original": str(src),
            "has_content": content is not None,
            "content": content,
            "lines": (content.count("\n") + 1) if content else 0,
        })
    return {
        "script_url": script_url,
        "version": map_obj.get("version"),
        "file": map_obj.get("file"),
        "files": files,
        "files_count": len(files),
        "with_content": sum(1 for f in files if f["has_content"]),
        "names_count": len(map_obj.get("names") or []),
    }


def decode_data_url(url: str) -> Optional[Dict[str, Any]]:
    """Decode an inline ``data:application/json;base64,...`` source map."""
    import base64

    if not url.startswith("data:"):
        return None
    header, _, payload = url.partition(",")
    if not payload:
        return None
    try:
        if ";base64" in header:
            raw = base64.b64decode(payload)
        else:
            from urllib.parse import unquote

            raw = unquote(payload).encode("utf-8")
        if len(raw) > _MAX_MAP_BYTES:
            return None
        return json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return None


_FETCH_RE = re.compile(r"""fetch\(\s*([`'"])((?:https?:)?//[^`'"]+|/[^`'"]+)\1""")
_AXIOS_RE = re.compile(r"""(?:axios|api|http)\.(get|post|put|patch|delete)\(\s*([`'"])((?:https?:)?//[^`'"]+|/[^`'"]+)\2""", re.I)
_XHR_RE = re.compile(r"""\.open\(\s*([`'"])(GET|POST|PUT|PATCH|DELETE)\1\s*,\s*([`'"])((?:https?:)?//[^`'"]+|/[^`'"]+)\3""", re.I)


def _template(path: str) -> str:
    path = re.sub(r"\$\{[^}]+\}", "{var}", path)
    path = re.sub(r"(?<=/)\d+(?=/|$)", "{n}", path)
    return path.split("?")[0][:200]


def endpoint_inventory(files: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Lexical inventory of API call sites across reconstructed sources."""
    found: List[Dict[str, Any]] = []
    seen = set()
    for f in files or []:
        content = f.get("content")
        if not content:
            continue
        hits = []
        for m in _FETCH_RE.finditer(content):
            hits.append(("FETCH", m.group(2), m.start()))
        for m in _AXIOS_RE.finditer(content):
            hits.append((m.group(1).upper(), m.group(3), m.start()))
        for m in _XHR_RE.finditer(content):
            hits.append((m.group(2).upper(), m.group(4), m.start()))
        for method, raw_url, pos in hits:
            template = _template(raw_url)
            key = (method, template, f.get("path"))
            if key in seen:
                continue
            seen.add(key)
            found.append({
                "method": method,
                "path": template,
                "file": f.get("path"),
                "line": content.count("\n", 0, pos) + 1,
            })
    return sorted(found, key=lambda e: (e["path"], e["method"]))


def reconstruct_tree(maps: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge parsed maps into one tree + endpoint inventory."""
    files: List[Dict[str, Any]] = []
    for m in maps or []:
        files.extend(m.get("files") or [])
    return {
        "files": files,
        "files_count": len(files),
        "with_content": sum(1 for f in files if f.get("has_content")),
        "endpoints": endpoint_inventory(files),
    }


def write_tree(tree: Dict[str, Any], output_dir: str) -> Dict[str, Any]:
    """Write reconstructed files under output_dir (every path confined)."""
    import os

    from agent_browser.security import confine_path

    written = []
    for f in tree.get("files") or []:
        if not f.get("has_content"):
            continue
        target = confine_path(f["path"], output_dir)
        with open(target, "w", encoding="utf-8", errors="replace") as fh:
            fh.write(f["content"] or "")
        written.append({"path": f["path"], "file": target, "lines": f.get("lines")})
    return {"written": written, "count": len(written), "output_dir": os.path.abspath(output_dir)}
