"""
Traffic log reader utilities.

Used by controller endpoints to serve traffic data to the UI.
Supports both plaintext (LLM proxy) and encrypted (MITM capture) logs.
"""

import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

TRAFFIC_LOG = Path(os.environ.get("TRAFFIC_LOG", "/srv/audit/traffic.jsonl"))
ENCRYPTED_TRAFFIC_LOG = Path(os.environ.get("ENCRYPTED_TRAFFIC_LOG", "/srv/clawfactory/audit/traffic.enc.jsonl"))
NGINX_LOG = Path(os.environ.get("NGINX_LOG", "/var/log/nginx/access.json"))


def read_traffic_log(
    limit: int = 50,
    offset: int = 0,
    provider: str | None = None,
    status: int | None = None,
    search: str | None = None,
) -> list[dict]:
    """Read traffic log entries with optional filters. Returns newest first."""
    if not TRAFFIC_LOG.exists():
        return []

    entries = []
    with open(TRAFFIC_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Apply filters
            if provider and entry.get("provider") != provider:
                continue
            if status is not None and entry.get("response_status") != status:
                continue
            if search:
                searchable = json.dumps(entry).lower()
                if search.lower() not in searchable:
                    continue

            entries.append(entry)

    # Reverse for newest first, then apply offset/limit
    entries.reverse()
    return entries[offset : offset + limit]


def read_nginx_log(limit: int = 50) -> list[dict]:
    """Read nginx JSON access log entries. Returns newest first."""
    if not NGINX_LOG.exists():
        return []

    entries = []
    with open(NGINX_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    entries.reverse()
    return entries[:limit]


def get_traffic_stats() -> dict:
    """Aggregate stats from traffic log."""
    if not TRAFFIC_LOG.exists():
        return {
            "total_requests": 0,
            "by_provider": {},
            "avg_duration_ms": 0,
            "total_tokens_in": 0,
            "total_tokens_out": 0,
            "error_count": 0,
            "error_rate": 0,
        }

    total = 0
    by_provider: dict[str, int] = {}
    total_duration = 0.0
    total_tokens_in = 0
    total_tokens_out = 0
    error_count = 0

    with open(TRAFFIC_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            total += 1
            prov = entry.get("provider", "unknown")
            by_provider[prov] = by_provider.get(prov, 0) + 1
            total_duration += entry.get("duration_ms", 0)
            total_tokens_in += entry.get("tokens_in", 0)
            total_tokens_out += entry.get("tokens_out", 0)
            resp_status = entry.get("response_status", 0)
            if resp_status >= 400 or entry.get("error"):
                error_count += 1

    return {
        "total_requests": total,
        "by_provider": by_provider,
        "avg_duration_ms": round(total_duration / total, 1) if total > 0 else 0,
        "total_tokens_in": total_tokens_in,
        "total_tokens_out": total_tokens_out,
        "error_count": error_count,
        "error_rate": round(error_count / total * 100, 1) if total > 0 else 0,
    }


def get_llm_session(request_id: str) -> dict | None:
    """Get a single traffic entry by ID."""
    if not TRAFFIC_LOG.exists():
        return None

    with open(TRAFFIC_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("id") == request_id:
                    return entry
            except json.JSONDecodeError:
                continue

    return None


# --- Encrypted traffic log (MITM capture) ---

def _decrypt_lines(fernet_key: bytes, log_path: Path | None = None) -> list[dict]:
    """Decrypt all lines from the encrypted traffic log."""
    path = log_path or ENCRYPTED_TRAFFIC_LOG
    if not path.exists():
        return []

    f = Fernet(fernet_key)
    entries = []
    with open(path, "rb") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                plaintext = f.decrypt(line)
                entries.append(json.loads(plaintext))
            except (InvalidToken, json.JSONDecodeError):
                continue
    return entries


def read_encrypted_traffic_log(
    fernet_key: bytes,
    limit: int = 50,
    offset: int = 0,
    provider: str | None = None,
    status: int | None = None,
    search: str | None = None,
    log_path: Path | None = None,
) -> list[dict]:
    """Read encrypted traffic log entries with optional filters. Returns newest first."""
    entries = _decrypt_lines(fernet_key, log_path)

    filtered = []
    for entry in entries:
        if provider and entry.get("provider") != provider:
            continue
        if status is not None and entry.get("response_status") != status:
            continue
        if search:
            searchable = json.dumps(entry).lower()
            if search.lower() not in searchable:
                continue
        filtered.append(entry)

    filtered.reverse()
    return filtered[offset : offset + limit]


def get_encrypted_traffic_stats(fernet_key: bytes, log_path: Path | None = None) -> dict:
    """Aggregate stats from encrypted traffic log."""
    entries = _decrypt_lines(fernet_key, log_path)

    if not entries:
        return {
            "total_requests": 0,
            "by_provider": {},
            "avg_duration_ms": 0,
            "total_tokens_in": 0,
            "total_tokens_out": 0,
            "error_count": 0,
            "error_rate": 0,
        }

    total = len(entries)
    by_provider: dict[str, int] = {}
    total_duration = 0.0
    total_tokens_in = 0
    total_tokens_out = 0
    error_count = 0

    for entry in entries:
        prov = entry.get("provider", "unknown")
        by_provider[prov] = by_provider.get(prov, 0) + 1
        total_duration += entry.get("duration_ms", 0)
        total_tokens_in += entry.get("tokens_in", 0)
        total_tokens_out += entry.get("tokens_out", 0)
        resp_status = entry.get("response_status", 0)
        if resp_status >= 400 or entry.get("error"):
            error_count += 1

    return {
        "total_requests": total,
        "by_provider": by_provider,
        "avg_duration_ms": round(total_duration / total, 1) if total > 0 else 0,
        "total_tokens_in": total_tokens_in,
        "total_tokens_out": total_tokens_out,
        "error_count": error_count,
        "error_rate": round(error_count / total * 100, 1) if total > 0 else 0,
    }


def get_encrypted_entry(fernet_key: bytes, request_id: str, log_path: Path | None = None) -> dict | None:
    """Get a single entry by ID from the encrypted traffic log."""
    entries = _decrypt_lines(fernet_key, log_path)
    for entry in entries:
        if entry.get("id") == request_id:
            return entry
    return None


def count_encrypted_entries(log_path: Path | None = None) -> int:
    """Count lines in the encrypted traffic log (without decrypting)."""
    path = log_path or ENCRYPTED_TRAFFIC_LOG
    if not path.exists():
        return 0
    count = 0
    with open(path, "rb") as f:
        for line in f:
            if line.strip():
                count += 1
    return count
