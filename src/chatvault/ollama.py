from __future__ import annotations

import os
import re
from urllib.parse import urlparse

# One place that decides which Ollama server ChatVault talks to, shared by
# embeddings (chatvault.embeddings) and generation (chatvault.brain.distill).
# CHATVAULT_OLLAMA_URL points either at a remote box (the Fedora laptop over
# Tailscale: http://<tailnet-ip>:11434) or is unset for the local server.
# Resolved at call time, not import time, so .env (loaded by load_config) and
# per-command `CHATVAULT_OLLAMA_URL=… chatvault embed` both take effect.
# There is deliberately no fallback to localhost when the configured server is
# down: that would silently build a half-remote, half-local index.

DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_KEEP_ALIVE = "30m"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def base_url() -> str:
    """Server root, e.g. `http://100.64.0.2:11434`. Tolerates a pasted full
    endpoint (`…/api/embeddings`) or a trailing slash."""
    raw = os.environ.get("CHATVAULT_OLLAMA_URL", "").strip()
    if not raw:
        return DEFAULT_BASE_URL
    if "://" not in raw:
        raw = "http://" + raw
    return re.sub(r"/api(/.*)?$", "", raw.rstrip("/"))


def is_remote(url: str | None = None) -> bool:
    host = urlparse(url or base_url()).hostname or ""
    return host not in _LOCAL_HOSTS


def keep_alive() -> str:
    """How long Ollama keeps the model loaded after a request (its own default
    is 5 minutes). Sent per request so behaviour doesn't depend on how the
    service was configured on either machine."""
    return os.environ.get("CHATVAULT_OLLAMA_KEEP_ALIVE", "").strip() or DEFAULT_KEEP_ALIVE


def embed_timeout_s(url: str | None = None) -> int:
    # Remote gets headroom for a cold model load plus network; local stays tight.
    return 120 if is_remote(url) else 30


def where(url: str | None = None) -> str:
    u = url or base_url()
    return f"{u} (CHATVAULT_OLLAMA_URL)" if is_remote(u) else u
