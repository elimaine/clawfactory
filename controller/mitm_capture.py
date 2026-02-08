"""
mitmproxy addon for ClawFactory MITM TLS capture.

Captures all outbound HTTP/HTTPS flows from the gateway, encrypts each
log entry with Fernet, and appends to traffic.enc.jsonl.

Usage (via mitmdump):
    mitmdump --mode transparent --listen-port 8888 \
        -s /srv/clawfactory/controller/mitm_capture.py \
        --set confdir=/srv/clawfactory/mitm-ca

Environment variables (set via systemd override):
    TRAFFIC_LOG           - Path to encrypted JSONL log
    FERNET_KEY_FILE       - Path to plaintext Fernet key (exists while capture active)
    CAPTURE_STATE_FILE    - "1" = logging enabled, "0" = skip logging
"""

import json
import os
import time
import uuid
from pathlib import Path

from cryptography.fernet import Fernet
from mitmproxy import http

TRAFFIC_LOG = Path(os.environ.get("TRAFFIC_LOG", "/srv/clawfactory/audit/traffic.enc.jsonl"))
FERNET_KEY_FILE = Path(os.environ.get("FERNET_KEY_FILE", "/srv/clawfactory/audit/traffic.fernet.key"))
CAPTURE_STATE_FILE = Path(os.environ.get("CAPTURE_STATE_FILE", "/srv/clawfactory/audit/capture_enabled"))

# Cache the Fernet instance (reloaded if key file changes)
_fernet: Fernet | None = None
_fernet_mtime: float = 0


def _get_fernet() -> Fernet | None:
    """Load or reload the Fernet key from disk."""
    global _fernet, _fernet_mtime

    if not FERNET_KEY_FILE.exists():
        _fernet = None
        return None

    mtime = FERNET_KEY_FILE.stat().st_mtime
    if _fernet is not None and mtime == _fernet_mtime:
        return _fernet

    try:
        key = FERNET_KEY_FILE.read_bytes().strip()
        _fernet = Fernet(key)
        _fernet_mtime = mtime
        return _fernet
    except Exception:
        _fernet = None
        return None


def _is_capture_enabled() -> bool:
    """Check if capture logging is enabled."""
    if not CAPTURE_STATE_FILE.exists():
        return False
    try:
        return CAPTURE_STATE_FILE.read_text().strip() == "1"
    except Exception:
        return False


def _detect_provider(host: str) -> str:
    """Guess the provider from the hostname."""
    h = host.lower()
    if "anthropic" in h:
        return "anthropic"
    if "openai" in h:
        return "openai"
    if "openrouter" in h:
        return "openrouter"
    if "googleapis" in h or "generativelanguage" in h:
        return "gemini"
    if "cloudflare" in h:
        return "cloudflare"
    if "mistral" in h:
        return "mistral"
    if "groq" in h:
        return "groq"
    if "together" in h:
        return "together"
    if "cohere" in h:
        return "cohere"
    return "other"


def _is_llm_call(request_body: dict | None) -> bool:
    """Heuristic: does the request body look like an LLM API call?"""
    if not isinstance(request_body, dict):
        return False
    llm_fields = {"messages", "model", "stream", "prompt", "max_tokens"}
    return bool(llm_fields & request_body.keys())


def _safe_json(data: bytes | str | None, max_bytes: int = 256_000) -> dict | str | None:
    """Try to parse body as JSON; fall back to truncated string."""
    if data is None:
        return None
    if isinstance(data, bytes):
        data = data[:max_bytes]
        try:
            return json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            try:
                return data.decode("utf-8", errors="replace")[:4000]
            except Exception:
                return f"<binary {len(data)} bytes>"
    return str(data)[:4000]


def _extract_tokens(body: dict | None, provider: str) -> tuple[int, int]:
    """Extract token counts from a response body if available."""
    if not isinstance(body, dict):
        return 0, 0
    usage = body.get("usage", {})
    if isinstance(usage, dict):
        return (
            usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0),
            usage.get("completion_tokens", 0) or usage.get("output_tokens", 0),
        )
    return 0, 0


class MitmCapture:
    """mitmproxy addon that encrypts and logs all captured flows."""

    def response(self, flow: http.HTTPFlow):
        """Called when a full request/response pair is available."""
        if not _is_capture_enabled():
            return

        fernet = _get_fernet()
        if fernet is None:
            return

        request = flow.request
        response = flow.response

        req_body = _safe_json(request.get_content())
        resp_body = _safe_json(response.get_content()) if response else None
        host = request.pretty_host
        provider = _detect_provider(host)
        is_llm = _is_llm_call(req_body if isinstance(req_body, dict) else None)
        tokens_in, tokens_out = _extract_tokens(
            resp_body if isinstance(resp_body, dict) else None, provider
        )

        duration_ms = 0
        if flow.response and flow.request.timestamp_start:
            duration_ms = round(
                (flow.response.timestamp_end - flow.request.timestamp_start) * 1000, 1
            )

        entry = {
            "id": str(uuid.uuid4()),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            "provider": provider,
            "host": host,
            "method": request.method,
            "path": request.path,
            "url": request.pretty_url,
            "is_llm": is_llm,
            "request_headers": dict(request.headers),
            "request_body": req_body,
            "response_status": response.status_code if response else 0,
            "response_headers": dict(response.headers) if response else {},
            "response_body": resp_body,
            "duration_ms": duration_ms,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "streaming": "text/event-stream" in (response.headers.get("content-type", "") if response else ""),
        }

        try:
            plaintext = json.dumps(entry, default=str).encode("utf-8")
            ciphertext = fernet.encrypt(plaintext)
            TRAFFIC_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(TRAFFIC_LOG, "ab") as f:
                f.write(ciphertext + b"\n")
        except Exception:
            pass  # Don't let logging failures break the proxy


addons = [MitmCapture()]
