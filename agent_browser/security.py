"""
Loopback daemon security helpers.

Pure functions (no FastAPI import) so they are unit-testable without a running
server. They provide:
  - Host/Origin validation: a loopback daemon must only answer loopback Hosts
    and must reject cross-site Origins (DNS-rebinding + browser-driven defense).
  - A per-daemon bearer token persisted to a per-port file the local CLI reads.
  - Output-path confinement for the file-writing routes.
"""

import os
import secrets
import tempfile
from typing import Optional
from urllib.parse import urlparse

from agent_browser.evidence import is_within_dir

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def host_allowed(host_header: Optional[str]) -> bool:
    """A loopback daemon must only answer to a loopback Host header."""
    if not host_header:
        # Header-less local clients (e.g. some test clients) are allowed.
        return True
    host = host_header.strip()
    if host.startswith("["):  # IPv6 literal, e.g. [::1]:9224
        host_only = host[1:host.find("]")] if "]" in host else host[1:]
    else:
        host_only = host.split(":", 1)[0]
    return host_only.lower() in _LOOPBACK


def origin_allowed(origin_header: Optional[str]) -> bool:
    """Reject a cross-site Origin so a visited page cannot drive the daemon."""
    if not origin_header:
        return True
    try:
        host = (urlparse(origin_header).hostname or "").lower()
    except Exception:
        return False
    return host in _LOOPBACK


def bearer_ok(auth_header: Optional[str], expected: Optional[str]) -> bool:
    """Constant-time bearer check. No token configured (None) means open."""
    if not expected:
        return True
    if not auth_header:
        return False
    parts = auth_header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    return secrets.compare_digest(parts[1].strip(), expected)


def token_path(port: int) -> str:
    return os.path.join(tempfile.gettempdir(), f"agentbrowser-{int(port)}.token")


def load_or_create_token(port: int, explicit: Optional[str] = None) -> str:
    """Create (or reuse) the daemon token and persist it for the local CLI."""
    token = explicit or os.environ.get("AGENTBROWSER_TOKEN") or secrets.token_urlsafe(32)
    try:
        path = token_path(port)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(token)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
    except Exception:
        pass
    return token


def read_token(port: int) -> Optional[str]:
    """CLI side: resolve the token from the env or the per-port token file."""
    token = os.environ.get("AGENTBROWSER_TOKEN")
    if token:
        return token
    try:
        with open(token_path(port), "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except Exception:
        return None


def confine_path(requested: str, root: str) -> str:
    """
    Resolve a caller-supplied output path under ``root``, refusing any path that
    escapes it. Relative paths are placed under root; absolute paths must already
    be within it. Creates the parent directory for the resolved path.
    """
    root = os.path.abspath(root)
    os.makedirs(root, exist_ok=True)
    if os.path.isabs(requested):
        candidate = os.path.abspath(requested)
    else:
        candidate = os.path.abspath(os.path.join(root, requested))
    if not is_within_dir(candidate, root):
        raise ValueError(f"output path escapes allowed root: {requested}")
    parent = os.path.dirname(candidate)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return candidate
