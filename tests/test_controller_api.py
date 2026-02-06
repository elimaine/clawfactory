#!/usr/bin/env python3
"""
ClawFactory Controller API Tests

Tests the controller API endpoints to ensure they work correctly.
Run with: python -m pytest tests/test_controller_api.py -v

Requires a running testbot instance:
  ./clawfactory.sh -i testbot start
"""

import os
import pytest
import requests
from pathlib import Path

# Configuration
BASE_URL = os.environ.get("CONTROLLER_URL", "http://localhost:8080/controller")
TOKEN = os.environ.get("CONTROLLER_TOKEN", "")

# Try to load token from secrets if not in env
if not TOKEN:
    token_file = Path(__file__).parent.parent / "secrets" / "testbot" / "controller.env"
    if token_file.exists():
        for line in token_file.read_text().splitlines():
            if line.startswith("CONTROLLER_API_TOKEN="):
                TOKEN = line.split("=", 1)[1].strip()
                break


def get_headers():
    """Get auth headers."""
    if TOKEN:
        return {"Authorization": f"Bearer {TOKEN}"}
    return {}


def get_params():
    """Get query params with token."""
    if TOKEN:
        return {"token": TOKEN}
    return {}


class TestHealthEndpoints:
    """Test health and status endpoints."""

    def test_health(self):
        """Test /health endpoint returns ok."""
        resp = requests.get(f"{BASE_URL}/health", timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("status") == "ok"

    def test_status(self):
        """Test /status endpoint returns instance info."""
        resp = requests.get(f"{BASE_URL}/status", params=get_params(), timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert "gateway_status" in data or "approved_sha" in data


class TestSnapshotEndpoints:
    """Test snapshot management endpoints."""

    def test_snapshot_list(self):
        """Test GET /snapshot returns list."""
        resp = requests.get(f"{BASE_URL}/snapshot", params=get_params(), timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert "snapshots" in data
        assert isinstance(data["snapshots"], list)
        # Check encryption_ready flag exists
        assert "encryption_ready" in data

    def test_snapshot_create(self):
        """Test POST /snapshot creates a snapshot."""
        resp = requests.post(f"{BASE_URL}/snapshot", params=get_params(), timeout=60)
        assert resp.status_code == 200
        data = resp.json()
        # Should have name and size, or error with clear message
        if "error" in data or "detail" in data:
            # Acceptable errors
            error = data.get("error") or data.get("detail")
            assert "key" in error.lower() or "encrypt" in error.lower(), f"Unexpected error: {error}"
        else:
            assert "name" in data, f"Missing 'name' in response: {data}"
            assert "size" in data, f"Missing 'size' in response: {data}"
            assert data["name"].startswith("snapshot-")
            assert isinstance(data["size"], int)


class TestGatewayEndpoints:
    """Test gateway management endpoints."""

    def test_gateway_status(self):
        """Test gateway status is returned."""
        resp = requests.get(f"{BASE_URL}/status", params=get_params(), timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        # Should have some gateway info
        assert "gateway_status" in data

    def test_gateway_logs(self):
        """Test /gateway/logs endpoint."""
        resp = requests.get(
            f"{BASE_URL}/gateway/logs",
            params={**get_params(), "lines": 10},
            timeout=10
        )
        assert resp.status_code == 200
        data = resp.json()
        # Should have logs or error
        assert "logs" in data or "error" in data

    def test_gateway_config_get(self):
        """Test GET /gateway/config returns config."""
        resp = requests.get(f"{BASE_URL}/gateway/config", params=get_params(), timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        # Should have config or error
        if "error" not in data and "detail" not in data:
            assert "config" in data or "raw" in data

    def test_gateway_devices(self):
        """Test /gateway/devices endpoint."""
        resp = requests.get(f"{BASE_URL}/gateway/devices", params=get_params(), timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        # Should have paired/pending lists or error
        assert "paired" in data or "devices" in data or "error" in data or "detail" in data


class TestBranchEndpoints:
    """Test git branch management endpoints."""

    def test_branches_list(self):
        """Test /branches returns branch list."""
        resp = requests.get(f"{BASE_URL}/branches", params=get_params(), timeout=10)
        assert resp.status_code == 200
        data = resp.json()
        # Should have branches list or error
        assert "branches" in data or "error" in data


class TestLocalChangesEndpoint:
    """Test offline mode endpoints."""

    def test_local_changes(self):
        """Test /local-changes endpoint."""
        resp = requests.get(f"{BASE_URL}/local-changes", params=get_params(), timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        # Should have changes field or error
        assert "changes" in data or "error" in data


class TestSecurityAudit:
    """Test security audit endpoint."""

    def test_security_audit(self):
        """Test /gateway/security-audit endpoint."""
        resp = requests.get(
            f"{BASE_URL}/gateway/security-audit",
            params={**get_params(), "deep": "false"},
            timeout=10
        )
        assert resp.status_code == 200
        data = resp.json()
        # Should have audit results (findings or issues)
        assert "findings" in data or "issues" in data or "error" in data or "detail" in data


class TestAuditLog:
    """Test audit log endpoint."""

    def test_audit_log(self):
        """Test /audit endpoint returns entries."""
        resp = requests.get(f"{BASE_URL}/audit", params=get_params(), timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert "entries" in data
        assert isinstance(data["entries"], list)


class TestUIEndpoint:
    """Test that the UI loads."""

    def test_ui_loads(self):
        """Test / returns HTML UI."""
        resp = requests.get(f"{BASE_URL}/", params=get_params(), timeout=5, allow_redirects=False)
        # May return 200 with HTML or 307 redirect
        assert resp.status_code in [200, 307, 302]
        if resp.status_code == 200:
            assert "text/html" in resp.headers.get("content-type", "")
            assert "ClawFactory" in resp.text or "Controller" in resp.text


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
