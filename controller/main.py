#!/usr/bin/env python3
"""
ClawFactory Controller - Authority & Promotion Service

Responsibilities:
- Receive GitHub webhooks (PR merged → promote)
- Perform promotions (brain_work → brain_ro)
- Restart Gateway after promotion
- Host approval UI for offline mode
- Audit logging
"""

import hashlib
import hmac
import json
import os
import secrets
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import docker
from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

# Configuration from environment
BRAIN_RO = Path(os.environ.get("BRAIN_RO", "/srv/data/brain_ro"))
BRAIN_WORK = Path(os.environ.get("BRAIN_WORK", "/srv/data/brain_work"))
OPENCLAW_HOME = Path(os.environ.get("OPENCLAW_HOME", "/srv/openclaw-home"))
AUDIT_LOG = Path(os.environ.get("AUDIT_LOG", "/srv/audit/audit.jsonl"))
GITHUB_WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
ALLOWED_MERGE_ACTORS = os.environ.get("ALLOWED_MERGE_ACTORS", "").split(",")
INSTANCE_NAME = os.environ.get("INSTANCE_NAME", "default")
GATEWAY_CONTAINER = os.environ.get("GATEWAY_CONTAINER", f"clawfactory-{INSTANCE_NAME}-gateway")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")
CONTROLLER_API_TOKEN = os.environ.get("CONTROLLER_API_TOKEN", "")

app = FastAPI(title="ClawFactory Controller", version="1.0.0")

# Session storage (persisted to file)
SESSIONS_FILE = Path("/srv/audit/sessions.json")


def load_sessions() -> set[str]:
    """Load sessions from file."""
    if SESSIONS_FILE.exists():
        try:
            with open(SESSIONS_FILE) as f:
                data = json.load(f)
                return set(data.get("sessions", []))
        except Exception:
            pass
    return set()


def save_sessions(sessions: set[str]):
    """Save sessions to file."""
    SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SESSIONS_FILE, "w") as f:
        json.dump({"sessions": list(sessions)}, f)


valid_sessions: set[str] = load_sessions()


# Log startup
def log_startup():
    """Log controller startup."""
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": "controller_started",
        "version": "1.0.0",
    }
    with open(AUDIT_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


log_startup()


# ============================================================
# Authentication
# ============================================================

def verify_token(token: str) -> bool:
    """Verify the API token."""
    if not CONTROLLER_API_TOKEN:
        return True  # No token configured = no auth required
    return secrets.compare_digest(token, CONTROLLER_API_TOKEN)


def create_session() -> str:
    """Create a new session token."""
    session = secrets.token_hex(32)
    valid_sessions.add(session)
    save_sessions(valid_sessions)
    audit_log("session_created", {"session_prefix": session[:8]})
    return session


def verify_session(session: str) -> bool:
    """Verify a session token."""
    return session in valid_sessions


def check_auth(
    token: Optional[str] = None,
    session: Optional[str] = None,
    auth_header: Optional[str] = None,
) -> bool:
    """
    Check if request is authenticated.

    Accepts:
    - ?token=... query parameter
    - clawfactory_session cookie
    - Authorization: Bearer ... header
    """
    # If no token configured, skip auth
    if not CONTROLLER_API_TOKEN:
        return True

    # Check session cookie
    if session and verify_session(session):
        return True

    # Check token query param
    if token and verify_token(token):
        return True

    # Check Authorization header
    if auth_header and auth_header.startswith("Bearer "):
        bearer_token = auth_header[7:]
        if verify_token(bearer_token):
            return True

    return False


# ============================================================
# Audit Logging
# ============================================================

def audit_log(event: str, details: dict):
    """Append an event to the audit log."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **details,
    }
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(AUDIT_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")
    print(f"[audit] {event}: {details}")


# ============================================================
# Git Operations
# ============================================================

def git_fetch_main() -> bool:
    """Fetch latest main branch from GitHub."""
    try:
        result = subprocess.run(
            ["git", "fetch", "origin", "main"],
            cwd=BRAIN_RO,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return result.returncode == 0
    except Exception as e:
        audit_log("git_fetch_error", {"error": str(e)})
        return False


def git_get_main_sha() -> Optional[str]:
    """Get the SHA of main branch."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=BRAIN_RO,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def git_get_remote_sha() -> Optional[str]:
    """Get the SHA of origin/main."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "origin/main"],
            cwd=BRAIN_RO,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def promote_sha(sha: str) -> bool:
    """
    Promote a SHA to brain_ro by checking it out.

    This is the ONLY way active config changes.
    """
    try:
        # Fetch first to ensure we have the SHA
        subprocess.run(
            ["git", "fetch", "origin"],
            cwd=BRAIN_RO,
            capture_output=True,
            text=True,
            timeout=60,
        )

        # Checkout the SHA
        result = subprocess.run(
            ["git", "checkout", sha],
            cwd=BRAIN_RO,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            audit_log("promote_error", {"sha": sha, "stderr": result.stderr})
            return False

        audit_log("promote_success", {"sha": sha})
        return True
    except Exception as e:
        audit_log("promote_error", {"sha": sha, "error": str(e)})
        return False


def promote_main() -> bool:
    """Pull latest main branch to brain_ro."""
    try:
        result = subprocess.run(
            ["git", "pull", "origin", "main"],
            cwd=BRAIN_RO,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            audit_log("promote_error", {"stderr": result.stderr})
            return False

        sha = git_get_main_sha()
        audit_log("promote_success", {"sha": sha})
        return True
    except Exception as e:
        audit_log("promote_error", {"error": str(e)})
        return False


def restart_gateway() -> bool:
    """Restart the Gateway container."""
    try:
        client = docker.from_env()
        container = client.containers.get(GATEWAY_CONTAINER)
        container.restart(timeout=30)
        audit_log("gateway_restart", {"container": GATEWAY_CONTAINER})
        return True
    except Exception as e:
        audit_log("gateway_restart_error", {"error": str(e)})
        return False


# ============================================================
# GitHub Webhook
# ============================================================

def verify_github_signature(payload: bytes, signature: str) -> bool:
    """Verify GitHub webhook signature."""
    if not GITHUB_WEBHOOK_SECRET:
        return False

    expected = "sha256=" + hmac.new(
        GITHUB_WEBHOOK_SECRET.encode(),
        payload,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(expected, signature)


@app.post("/webhook/github")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str = Header(None),
    x_github_event: str = Header(None),
):
    """
    Handle GitHub webhook events.

    Only processes PR merge events from allowed actors.
    """
    payload = await request.body()

    # Verify signature
    if not verify_github_signature(payload, x_hub_signature_256 or ""):
        audit_log("webhook_rejected", {"reason": "invalid_signature"})
        raise HTTPException(status_code=401, detail="Invalid signature")

    data = json.loads(payload)

    # Only handle PR events
    if x_github_event != "pull_request":
        return {"status": "ignored", "reason": "not a PR event"}

    # Only handle merged PRs
    action = data.get("action")
    pr = data.get("pull_request", {})
    merged = pr.get("merged", False)

    if action != "closed" or not merged:
        return {"status": "ignored", "reason": "not a merge event"}

    # Verify merge actor
    merged_by = pr.get("merged_by", {}).get("login", "")
    if merged_by not in ALLOWED_MERGE_ACTORS:
        audit_log("webhook_rejected", {
            "reason": "unauthorized_actor",
            "actor": merged_by,
        })
        raise HTTPException(status_code=403, detail="Unauthorized merge actor")

    # Verify target branch
    base_branch = pr.get("base", {}).get("ref", "")
    if base_branch != "main":
        return {"status": "ignored", "reason": "not main branch"}

    audit_log("webhook_received", {
        "pr": pr.get("number"),
        "title": pr.get("title"),
        "merged_by": merged_by,
    })

    # Perform promotion - pull latest main
    if not promote_main():
        raise HTTPException(status_code=500, detail="Failed to promote")

    sha = git_get_main_sha()

    if not restart_gateway():
        # Promotion succeeded but restart failed - log but don't fail
        return {"status": "partial", "sha": sha, "restart": False}

    return {"status": "promoted", "sha": sha}


# ============================================================
# Manual Promotion (Offline Mode)
# ============================================================

@app.get("/controller", response_class=HTMLResponse)
async def promote_ui(
    request: Request,
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
):
    """Simple approval UI for promoting changes."""
    # Check authentication
    auth_result = None
    if CONTROLLER_API_TOKEN:
        if session and verify_session(session):
            auth_result = True
        elif token and verify_token(token):
            auth_result = "set_session"
        else:
            # Show login page
            return HTMLResponse(f"""
                <!DOCTYPE html>
                <html>
                <head>
                    <title>ClawFactory - Login</title>
                    <style>
                        body {{ font-family: monospace; padding: 2rem; background: #1a1a1a; color: #e0e0e0; }}
                        h1 {{ color: #4CAF50; }}
                        input {{ padding: 0.5rem; font-family: monospace; width: 400px; }}
                        button {{ background: #4CAF50; color: white; border: none; padding: 0.5rem 1rem; cursor: pointer; }}
                    </style>
                </head>
                <body>
                    <h1>ClawFactory <span style="color: #2196F3">[{INSTANCE_NAME}]</span></h1>
                    <p>Authentication required.</p>
                    <form method="GET" action="/controller">
                        <input type="password" name="token" placeholder="Enter API token" autofocus>
                        <button type="submit">Login</button>
                    </form>
                </body>
                </html>
            """, status_code=401)

    # Fetch latest from remote
    subprocess.run(["git", "fetch", "origin"], cwd=BRAIN_RO, capture_output=True)

    # Get recent commits on origin/main
    try:
        result = subprocess.run(
            ["git", "log", "origin/main", "--oneline", "-10"],
            cwd=BRAIN_RO,
            capture_output=True,
            text=True,
        )
        commits = result.stdout.strip() if result.returncode == 0 else "Error reading commits"
    except Exception as e:
        commits = f"Error: {e}"

    current_sha = git_get_main_sha() or "unknown"
    remote_sha = git_get_remote_sha() or "unknown"
    needs_update = current_sha != remote_sha

    status_msg = "Up to date" if not needs_update else f"Update available: {remote_sha[:8]}"
    status_class = "success" if not needs_update else "warning"

    # Get gateway status
    try:
        client = docker.from_env()
        gateway = client.containers.get(GATEWAY_CONTAINER)
        gateway_status = gateway.status
        gateway_class = "success" if gateway_status == "running" else "error"
    except Exception:
        gateway_status = "unknown"
        gateway_class = "warning"

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>ClawFactory [{INSTANCE_NAME}]</title>
        <style>
            * {{ box-sizing: border-box; }}
            body {{ font-family: monospace; padding: 1rem; background: #1a1a1a; color: #e0e0e0; max-width: 1200px; margin: 0 auto; }}
            h1 {{ color: #4CAF50; margin-bottom: 0.5rem; font-size: 1.5rem; }}
            h2 {{ color: #888; font-size: 1rem; margin-top: 1.5rem; border-bottom: 1px solid #333; padding-bottom: 0.5rem; }}
            h3 {{ color: #666; font-size: 0.9rem; margin: 1rem 0 0.5rem 0; }}
            pre {{ background: #2d2d2d; padding: 0.75rem; overflow-x: auto; max-height: 250px; overflow-y: auto; font-size: 0.85rem; word-break: break-all; white-space: pre-wrap; }}
            button {{ background: #4CAF50; color: white; border: none; padding: 0.6rem 1rem;
                     font-size: 0.9rem; cursor: pointer; margin: 0.3rem 0.3rem 0.3rem 0; font-family: monospace; border-radius: 4px; }}
            button:hover {{ background: #45a049; }}
            button.secondary {{ background: #2196F3; }}
            button.secondary:hover {{ background: #1976D2; }}
            button.danger {{ background: #f44336; }}
            button.danger:hover {{ background: #d32f2f; }}
            button.small {{ padding: 0.4rem 0.6rem; font-size: 0.8rem; }}
            input {{ padding: 0.5rem; font-family: monospace; background: #2d2d2d; border: 1px solid #444; color: #e0e0e0; border-radius: 4px; width: 100%; max-width: 300px; }}
            .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; }}
            .card {{ background: #252525; padding: 1rem; border-radius: 4px; }}
            .status {{ display: inline-block; padding: 0.25rem 0.5rem; border-radius: 3px; font-size: 0.85rem; }}
            .success {{ background: #1b5e20; color: #a5d6a7; }}
            .warning {{ background: #e65100; color: #ffcc80; }}
            .error {{ background: #b71c1c; color: #ef9a9a; }}
            .sha {{ color: #2196F3; font-family: monospace; }}
            .result {{ margin-top: 1rem; padding: 0.75rem; background: #2d2d2d; border-left: 3px solid #4CAF50; display: none; font-size: 0.85rem; }}
            .result.error {{ border-left-color: #f44336; }}
            .stats {{ display: flex; gap: 1rem; margin: 1rem 0; flex-wrap: wrap; }}
            .stat {{ text-align: center; min-width: 80px; }}
            .stat-value {{ font-size: 1.2rem; color: #4CAF50; }}
            .stat-label {{ font-size: 0.75rem; color: #888; }}
            a {{ color: #2196F3; }}
            #audit-log {{ max-height: 300px; overflow-y: auto; }}
            .pending-item {{ background: #333; padding: 0.5rem; margin: 0.5rem 0; border-radius: 4px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 0.5rem; }}
            .pending-item-info {{ flex: 1; min-width: 150px; }}
            .pending-item-actions {{ display: flex; gap: 0.3rem; }}
            .tab-buttons {{ display: flex; gap: 0.5rem; margin-bottom: 1rem; flex-wrap: wrap; }}
            .tab-button {{ background: #333; border: none; padding: 0.5rem 1rem; color: #888; cursor: pointer; border-radius: 4px 4px 0 0; }}
            .tab-button.active {{ background: #252525; color: #4CAF50; }}
            .tab-content {{ display: none; }}
            .tab-content.active {{ display: block; }}
            /* Mobile responsive */
            @media (max-width: 768px) {{
                body {{ padding: 0.75rem; }}
                h1 {{ font-size: 1.3rem; }}
                .grid {{ grid-template-columns: 1fr; gap: 1rem; }}
                .stats {{ justify-content: space-around; }}
                button {{ padding: 0.5rem 0.8rem; font-size: 0.85rem; }}
                input {{ max-width: 100%; }}
                pre {{ font-size: 0.75rem; max-height: 200px; }}
            }}
        </style>
    </head>
    <body>
        <h1>ClawFactory <span style="color: #2196F3">[{INSTANCE_NAME}]</span></h1>

        <div class="stats">
            <div class="stat">
                <div class="stat-value">{current_sha[:8]}</div>
                <div class="stat-label">Active SHA</div>
            </div>
            <div class="stat">
                <div class="stat-value {gateway_class}">{gateway_status}</div>
                <div class="stat-label">Gateway</div>
            </div>
            <div class="stat">
                <div class="stat-value"><span class="{status_class}">{status_msg}</span></div>
                <div class="stat-label">Sync Status</div>
            </div>
        </div>

        <div class="grid">
            <div>
                <h2>Promotion</h2>
                <div class="card">
                    <p>Remote main: <span class="sha">{remote_sha[:8]}</span></p>
                    <form action="/controller/promote-main" method="POST" style="display: inline;">
                        <button type="submit">Pull Main & Restart</button>
                    </form>
                    <div id="promote-result" class="result"></div>

                    <h3 style="margin-top: 1.5rem; font-size: 0.9rem; color: #888;">Promote Specific SHA</h3>
                    <form action="/controller" method="POST">
                        <input type="text" name="sha" placeholder="Enter full SHA" style="width: 300px;"><br>
                        <button type="submit" class="secondary" style="margin-top: 0.5rem;">Promote SHA</button>
                    </form>
                </div>

                <h2>Memory</h2>
                <div class="card">
                    <button onclick="backupMemory()" class="secondary">Backup Memory to GitHub</button>
                    <button onclick="fetchMemoryStatus()" class="secondary">Check Status</button>
                    <div id="memory-result" class="result"></div>
                </div>

                <h2>Recent Commits</h2>
                <pre>{commits}</pre>
            </div>

            <div>
                <h2>System Status</h2>
                <div class="card">
                    <button onclick="fetchHealth()">Health Check</button>
                    <button onclick="fetchStatus()" class="secondary">Full Status</button>
                    <button onclick="runSecurityAudit()" class="secondary">Security Audit</button>
                    <button onclick="runSecurityAudit(true)" class="secondary">Deep Audit</button>
                    <div id="status-result" class="result"></div>
                    <div id="security-result" class="result"></div>
                </div>

                <h2>Audit Log</h2>
                <div class="card">
                    <button onclick="fetchAudit()">Refresh</button>
                    <button onclick="fetchAudit(100)" class="secondary">Last 100</button>
                    <pre id="audit-log">Click Refresh to load audit log...</pre>
                </div>
            </div>
        </div>

        <h2>Gateway Pairing</h2>
        <div class="card">
            <div class="tab-buttons">
                <button class="tab-button active" onclick="showTab('devices')">Device Pairing</button>
                <button class="tab-button" onclick="showTab('dm')">DM Pairing (Discord)</button>
            </div>

            <div id="tab-devices" class="tab-content active">
                <p style="color: #888; font-size: 0.85rem;">Approve devices connecting to the gateway (iOS, Android, browser clients)</p>
                <button onclick="fetchDevices()" class="secondary">Refresh Devices</button>
                <div id="devices-list"></div>
                <div id="devices-result" class="result"></div>
            </div>

            <div id="tab-dm" class="tab-content">
                <p style="color: #888; font-size: 0.85rem;">Approve DM senders by their pairing code (Discord, Telegram, etc.)</p>
                <button onclick="fetchPairing('discord')" class="secondary">List Discord Pending</button>
                <button onclick="fetchPairing('telegram')" class="secondary">List Telegram</button>
                <div id="pairing-list"></div>
                <h3>Approve Code</h3>
                <div style="display: flex; gap: 0.5rem; flex-wrap: wrap; align-items: center;">
                    <select id="pairing-channel" style="padding: 0.5rem; background: #2d2d2d; border: 1px solid #444; color: #e0e0e0; border-radius: 4px;">
                        <option value="discord">Discord</option>
                        <option value="telegram">Telegram</option>
                        <option value="whatsapp">WhatsApp</option>
                        <option value="slack">Slack</option>
                    </select>
                    <input type="text" id="pairing-code" placeholder="ABCD1234" style="width: 120px; text-transform: uppercase;">
                    <button onclick="approvePairingCode()">Approve</button>
                </div>
                <div id="pairing-result" class="result"></div>
            </div>
        </div>

        <hr style="margin: 2rem 0; border-color: #333;">
        <p>
            <a href="https://github.com/{GITHUB_REPO}">GitHub Repo</a> |
            <a href="/health">Health API</a> |
            <a href="/status">Status API</a> |
            <a href="/audit">Audit API</a>
        </p>

        <script>
            // Detect base path from current URL (handles /controller via Tailscale)
            const basePath = window.location.pathname.includes('/controller') ? '/controller' : '';

            async function fetchHealth() {{
                const result = document.getElementById('status-result');
                result.style.display = 'block';
                result.className = 'result';
                try {{
                    const resp = await fetch(basePath + '/health');
                    const data = await resp.json();
                    result.textContent = JSON.stringify(data, null, 2);
                }} catch(e) {{
                    result.className = 'result error';
                    result.textContent = 'Error: ' + e.message;
                }}
            }}

            async function fetchStatus() {{
                const result = document.getElementById('status-result');
                result.style.display = 'block';
                result.className = 'result';
                try {{
                    const resp = await fetch(basePath + '/status');
                    const data = await resp.json();
                    result.textContent = JSON.stringify(data, null, 2);
                }} catch(e) {{
                    result.className = 'result error';
                    result.textContent = 'Error: ' + e.message;
                }}
            }}

            async function fetchAudit(limit = 20) {{
                try {{
                    const resp = await fetch(basePath + '/audit?limit=' + limit);
                    const data = await resp.json();
                    const log = document.getElementById('audit-log');
                    if (data.entries && data.entries.length > 0) {{
                        log.textContent = data.entries.map(e =>
                            `[${{e.timestamp.slice(0,19)}}] ${{e.event}}`
                        ).reverse().join('\\n');
                    }} else {{
                        log.textContent = 'No audit entries yet.';
                    }}
                }} catch(e) {{
                    document.getElementById('audit-log').textContent = 'Error: ' + e.message;
                }}
            }}

            async function backupMemory() {{
                const result = document.getElementById('memory-result');
                result.style.display = 'block';
                result.className = 'result';
                result.textContent = 'Backing up...';
                try {{
                    const resp = await fetch(basePath + '/memory/backup', {{ method: 'POST' }});
                    const data = await resp.json();
                    result.textContent = JSON.stringify(data, null, 2);
                }} catch(e) {{
                    result.className = 'result error';
                    result.textContent = 'Error: ' + e.message;
                }}
            }}

            async function fetchMemoryStatus() {{
                const result = document.getElementById('memory-result');
                result.style.display = 'block';
                result.className = 'result';
                try {{
                    const resp = await fetch(basePath + '/memory/status');
                    const data = await resp.json();
                    result.textContent = JSON.stringify(data, null, 2);
                }} catch(e) {{
                    result.className = 'result error';
                    result.textContent = 'Error: ' + e.message;
                }}
            }}

            // Tab switching
            function showTab(tabName) {{
                document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
                document.querySelectorAll('.tab-button').forEach(b => b.classList.remove('active'));
                document.getElementById('tab-' + tabName).classList.add('active');
                event.target.classList.add('active');
            }}

            // Device pairing
            async function fetchDevices() {{
                const list = document.getElementById('devices-list');
                list.innerHTML = '<p style="color: #888;">Loading...</p>';
                try {{
                    const resp = await fetch(basePath + '/gateway/devices');
                    const data = await resp.json();
                    if (data.error) {{
                        list.innerHTML = `<p class="error" style="color: #ef9a9a;">${{data.error}}</p>`;
                        return;
                    }}
                    let html = '';
                    if (data.pending && data.pending.length > 0) {{
                        html += '<h3>Pending Approval</h3>';
                        data.pending.forEach(d => {{
                            html += `<div class="pending-item">
                                <div class="pending-item-info">
                                    <strong>${{d.displayName || d.deviceId}}</strong><br>
                                    <small style="color: #888;">Role: ${{d.role || 'unknown'}} | IP: ${{d.remoteIp || '?'}}</small>
                                </div>
                                <div class="pending-item-actions">
                                    <button class="small" onclick="approveDevice('${{d.requestId}}')">Approve</button>
                                    <button class="small danger" onclick="rejectDevice('${{d.requestId}}')">Reject</button>
                                </div>
                            </div>`;
                        }});
                    }} else {{
                        html += '<p style="color: #888; font-size: 0.85rem;">No pending device requests.</p>';
                    }}
                    if (data.paired && data.paired.length > 0) {{
                        html += '<h3>Paired Devices</h3>';
                        data.paired.forEach(d => {{
                            html += `<div class="pending-item" style="background: #2a3a2a;">
                                <div class="pending-item-info">
                                    <strong>${{d.displayName || d.deviceId}}</strong><br>
                                    <small style="color: #888;">Roles: ${{(d.roles || []).join(', ') || 'none'}}</small>
                                </div>
                            </div>`;
                        }});
                    }}
                    list.innerHTML = html || '<p style="color: #888;">No devices.</p>';
                }} catch(e) {{
                    list.innerHTML = `<p class="error" style="color: #ef9a9a;">Error: ${{e.message}}</p>`;
                }}
            }}

            async function approveDevice(requestId) {{
                const result = document.getElementById('devices-result');
                result.style.display = 'block';
                result.className = 'result';
                try {{
                    const resp = await fetch(basePath + '/gateway/devices/approve', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ requestId }})
                    }});
                    const data = await resp.json();
                    result.textContent = data.status || JSON.stringify(data);
                    fetchDevices();
                }} catch(e) {{
                    result.className = 'result error';
                    result.textContent = 'Error: ' + e.message;
                }}
            }}

            async function rejectDevice(requestId) {{
                const result = document.getElementById('devices-result');
                result.style.display = 'block';
                result.className = 'result';
                try {{
                    const resp = await fetch(basePath + '/gateway/devices/reject', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ requestId }})
                    }});
                    const data = await resp.json();
                    result.textContent = data.status || JSON.stringify(data);
                    fetchDevices();
                }} catch(e) {{
                    result.className = 'result error';
                    result.textContent = 'Error: ' + e.message;
                }}
            }}

            // DM pairing
            async function fetchPairing(channel) {{
                const list = document.getElementById('pairing-list');
                list.innerHTML = '<p style="color: #888;">Loading...</p>';
                try {{
                    const resp = await fetch(basePath + '/gateway/pairing/' + channel);
                    const data = await resp.json();
                    if (data.error) {{
                        list.innerHTML = `<p class="error" style="color: #ef9a9a;">${{data.error}}</p>`;
                        return;
                    }}
                    let html = `<h3>${{channel.charAt(0).toUpperCase() + channel.slice(1)}} Pending</h3>`;
                    if (data.pending && data.pending.length > 0) {{
                        data.pending.forEach(p => {{
                            html += `<div class="pending-item">
                                <div class="pending-item-info">
                                    <strong>Code: ${{p.code}}</strong><br>
                                    <small style="color: #888;">From: ${{p.senderId || p.userId || '?'}}</small>
                                </div>
                            </div>`;
                        }});
                    }} else {{
                        html += '<p style="color: #888; font-size: 0.85rem;">No pending requests.</p>';
                    }}
                    list.innerHTML = html;
                }} catch(e) {{
                    list.innerHTML = `<p class="error" style="color: #ef9a9a;">Error: ${{e.message}}</p>`;
                }}
            }}

            async function approvePairingCode() {{
                const channel = document.getElementById('pairing-channel').value;
                const code = document.getElementById('pairing-code').value.toUpperCase().trim();
                const result = document.getElementById('pairing-result');
                if (!code) {{
                    result.style.display = 'block';
                    result.className = 'result error';
                    result.textContent = 'Please enter a pairing code';
                    return;
                }}
                result.style.display = 'block';
                result.className = 'result';
                result.textContent = 'Approving...';
                try {{
                    const resp = await fetch(basePath + '/gateway/pairing/approve', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ channel, code }})
                    }});
                    const data = await resp.json();
                    if (data.error) {{
                        result.className = 'result error';
                        result.textContent = data.error;
                    }} else {{
                        result.textContent = data.status || 'Approved!';
                        document.getElementById('pairing-code').value = '';
                    }}
                }} catch(e) {{
                    result.className = 'result error';
                    result.textContent = 'Error: ' + e.message;
                }}
            }}

            // Security audit
            async function runSecurityAudit(deep = false) {{
                const result = document.getElementById('security-result');
                result.style.display = 'block';
                result.className = 'result';
                result.innerHTML = '<p style="color: #888;">Running security audit' + (deep ? ' (deep)' : '') + '...</p>';
                try {{
                    const resp = await fetch(basePath + '/gateway/security-audit?deep=' + deep);
                    const data = await resp.json();
                    if (data.error) {{
                        result.className = 'result error';
                        result.textContent = data.error;
                        return;
                    }}
                    // Format the security audit nicely
                    let html = '<h3 style="margin: 0 0 0.5rem 0;">Security Audit</h3>';
                    const s = data.summary || {{}};
                    const criticalColor = s.critical > 0 ? '#ef9a9a' : '#a5d6a7';
                    const warnColor = s.warn > 0 ? '#ffcc80' : '#a5d6a7';
                    html += `<p><span style="color: ${{criticalColor}};">${{s.critical || 0}} critical</span> · `;
                    html += `<span style="color: ${{warnColor}};">${{s.warn || 0}} warnings</span> · `;
                    html += `<span style="color: #888;">${{s.info || 0}} info</span></p>`;
                    if (data.findings && data.findings.length > 0) {{
                        html += '<div style="margin-top: 0.5rem;">';
                        data.findings.forEach(f => {{
                            const severityColor = f.severity === 'critical' ? '#ef9a9a' :
                                                  f.severity === 'warn' ? '#ffcc80' : '#888';
                            html += `<div style="margin: 0.5rem 0; padding: 0.5rem; background: #333; border-radius: 4px;">`;
                            html += `<strong style="color: ${{severityColor}};">[${{f.severity.toUpperCase()}}]</strong> ${{f.title}}<br>`;
                            html += `<small style="color: #888;">${{f.detail || ''}}</small>`;
                            if (f.remediation) {{
                                html += `<br><small style="color: #4CAF50;">Fix: ${{f.remediation}}</small>`;
                            }}
                            html += '</div>';
                        }});
                        html += '</div>';
                    }} else {{
                        html += '<p style="color: #a5d6a7;">No findings.</p>';
                    }}
                    result.innerHTML = html;
                }} catch(e) {{
                    result.className = 'result error';
                    result.textContent = 'Error: ' + e.message;
                }}
            }}

            // Load audit on page load
            fetchAudit();
        </script>
    </body>
    </html>
    """

    response = HTMLResponse(html)

    # Set session cookie if authenticated via token
    if auth_result == "set_session":
        session_token = create_session()
        response.set_cookie(
            key="clawfactory_session",
            value=session_token,
            httponly=True,
            samesite="lax",
            path="/",
            max_age=2592000,  # 30 days
        )

    return response


@app.get("/", response_class=HTMLResponse)
async def root_dashboard(
    request: Request,
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
):
    """Serve dashboard at root (same as /controller for Tailscale path compatibility)."""
    return await promote_ui(request, token, session)


@app.post("/controller")
async def promote_manual(
    request: Request,
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
):
    """Handle manual promotion of specific SHA from UI."""
    # Check authentication
    if CONTROLLER_API_TOKEN and not (session and verify_session(session)):
        raise HTTPException(status_code=401, detail="Unauthorized")

    form = await request.form()
    sha = form.get("sha", "").strip()

    if not sha or len(sha) < 7:
        raise HTTPException(status_code=400, detail="Invalid SHA")

    audit_log("manual_promote_requested", {"sha": sha})

    if not promote_sha(sha):
        raise HTTPException(status_code=500, detail="Promotion failed")

    restart_gateway()

    return HTMLResponse(f"""
        <html>
        <body style="font-family: monospace; padding: 2rem; background: #1a1a1a; color: #4CAF50;">
            <h1>✅ Promoted</h1>
            <p>SHA: {sha}</p>
            <p>Gateway restarting...</p>
            <a href="/controller" style="color: #2196F3;">← Back</a>
        </body>
        </html>
    """)


@app.post("/controller/promote-main")
async def promote_main_endpoint(
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
):
    """Pull latest main and restart gateway."""
    # Check authentication
    if CONTROLLER_API_TOKEN and not (session and verify_session(session)):
        raise HTTPException(status_code=401, detail="Unauthorized")

    audit_log("promote_main_requested", {})

    if not promote_main():
        raise HTTPException(status_code=500, detail="Promotion failed")

    sha = git_get_main_sha()
    restart_gateway()

    return HTMLResponse(f"""
        <html>
        <body style="font-family: monospace; padding: 2rem; background: #1a1a1a; color: #4CAF50;">
            <h1>✅ Promoted to Main</h1>
            <p>SHA: {sha}</p>
            <p>Gateway restarting...</p>
            <a href="/controller" style="color: #2196F3;">← Back</a>
        </body>
        </html>
    """)


# ============================================================
# Health & Status
# ============================================================

@app.get("/health")
@app.get("/controller/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok"}


@app.get("/status")
@app.get("/controller/status")
async def status():
    """Get current system status."""
    current_sha = git_get_main_sha()

    try:
        client = docker.from_env()
        gateway = client.containers.get(GATEWAY_CONTAINER)
        gateway_status = gateway.status
    except Exception:
        gateway_status = "unknown"

    return {
        "brain_sha": current_sha,
        "gateway_status": gateway_status,
        "audit_log": str(AUDIT_LOG),
    }


@app.get("/audit")
@app.get("/controller/audit")
async def get_audit(limit: int = 50):
    """Get recent audit log entries."""
    if not AUDIT_LOG.exists():
        return {"entries": []}

    with open(AUDIT_LOG) as f:
        lines = f.readlines()

    entries = [json.loads(line) for line in lines[-limit:]]
    return {"entries": entries}


# ============================================================
# Memory Backup
# ============================================================

def backup_memory() -> dict:
    """
    List memory files in brain_work ready for commit.

    Memory now persists directly in brain_work via volume mount,
    so no copying is needed - just list what's there.
    """
    memory_dir = BRAIN_WORK / "workspace" / "memory"
    long_term = BRAIN_WORK / "workspace" / "MEMORY.md"

    files = []

    if memory_dir.exists():
        files.extend([f"memory/{f.name}" for f in memory_dir.glob("*.md")])

    if long_term.exists():
        files.append("MEMORY.md")

    return {"files": files}


def commit_and_push_memory() -> bool:
    """Commit memory changes and push to GitHub."""
    try:
        # Add memory files
        subprocess.run(
            ["git", "add", "workspace/memory/", "workspace/MEMORY.md"],
            cwd=BRAIN_WORK,
            capture_output=True,
        )

        # Check if there are changes to commit
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=BRAIN_WORK,
        )
        if result.returncode == 0:
            # No changes
            return True

        # Commit
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        result = subprocess.run(
            ["git", "commit", "-m", f"Backup agent memory - {timestamp}"],
            cwd=BRAIN_WORK,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            audit_log("memory_commit_error", {"stderr": result.stderr})
            return False

        # Push
        result = subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=BRAIN_WORK,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            audit_log("memory_push_error", {"stderr": result.stderr})
            return False

        return True
    except Exception as e:
        audit_log("memory_backup_error", {"error": str(e)})
        return False


@app.post("/memory/backup")
@app.post("/controller/memory/backup")
async def memory_backup(
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Backup agent memory to GitHub."""
    # Check authentication
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    audit_log("memory_backup_requested", {})

    result = backup_memory()
    if not result["files"]:
        return {"status": "no_changes", "files": []}

    if not commit_and_push_memory():
        raise HTTPException(status_code=500, detail="Failed to push memory backup")

    audit_log("memory_backup_success", {"files": result["files"]})
    return {"status": "backed_up", "files": result["files"]}


@app.get("/memory/status")
@app.get("/controller/memory/status")
async def memory_status(
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Get memory backup status."""
    # Check authentication
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    memory_src = OPENCLAW_HOME / "workspace" / "memory"
    long_term_src = OPENCLAW_HOME / "workspace" / "MEMORY.md"

    files = []
    if memory_src.exists():
        files.extend([f.name for f in memory_src.glob("*.md")])
    if long_term_src.exists():
        files.append("MEMORY.md")

    return {
        "memory_files": files,
        "openclaw_home": str(OPENCLAW_HOME),
    }


# ============================================================
# Gateway Pairing (Device + DM)
# ============================================================

def run_gateway_command(cmd: list[str], timeout: int = 30) -> tuple[bool, str]:
    """Run a command inside the gateway container."""
    try:
        client = docker.from_env()
        gateway = client.containers.get(GATEWAY_CONTAINER)
        exit_code, output = gateway.exec_run(cmd, demux=False)
        return exit_code == 0, output.decode() if output else ""
    except Exception as e:
        return False, str(e)


@app.get("/gateway/devices")
@app.get("/controller/gateway/devices")
async def gateway_devices(
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """List pending and paired devices."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Run openclaw devices list --json in the gateway container
    success, output = run_gateway_command(["node", "dist/index.js", "devices", "list", "--json"])
    if not success:
        return {"error": f"Failed to list devices: {output}"}

    try:
        data = json.loads(output)
        return data
    except json.JSONDecodeError:
        return {"error": f"Invalid JSON response: {output[:500]}"}


@app.post("/gateway/devices/approve")
@app.post("/controller/gateway/devices/approve")
async def gateway_device_approve(
    request: Request,
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Approve a pending device pairing request."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.json()
    request_id = body.get("requestId")
    if not request_id:
        return {"error": "Missing requestId"}

    success, output = run_gateway_command(["node", "dist/index.js", "devices", "approve", request_id])
    audit_log("device_approve", {"requestId": request_id, "success": success})

    if not success:
        return {"error": f"Failed to approve: {output}"}
    return {"status": "approved", "output": output}


@app.post("/gateway/devices/reject")
@app.post("/controller/gateway/devices/reject")
async def gateway_device_reject(
    request: Request,
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Reject a pending device pairing request."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.json()
    request_id = body.get("requestId")
    if not request_id:
        return {"error": "Missing requestId"}

    success, output = run_gateway_command(["node", "dist/index.js", "devices", "reject", request_id])
    audit_log("device_reject", {"requestId": request_id, "success": success})

    if not success:
        return {"error": f"Failed to reject: {output}"}
    return {"status": "rejected", "output": output}


@app.get("/gateway/pairing/{channel}")
@app.get("/controller/gateway/pairing/{channel}")
async def gateway_pairing_list(
    channel: str,
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """List pending DM pairing requests for a channel."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    valid_channels = ["discord", "telegram", "whatsapp", "slack", "signal", "imessage"]
    if channel not in valid_channels:
        return {"error": f"Invalid channel. Valid: {', '.join(valid_channels)}"}

    success, output = run_gateway_command(["node", "dist/index.js", "pairing", "list", channel, "--json"])
    if not success:
        return {"error": f"Failed to list pairing: {output}", "pending": []}

    try:
        data = json.loads(output)
        return {"pending": data.get("pending", []), "channel": channel}
    except json.JSONDecodeError:
        # If not JSON, return raw output
        return {"pending": [], "raw": output[:500], "channel": channel}


@app.post("/gateway/pairing/approve")
@app.post("/controller/gateway/pairing/approve")
async def gateway_pairing_approve(
    request: Request,
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Approve a DM pairing code."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.json()
    channel = body.get("channel", "discord")
    code = body.get("code", "").upper().strip()

    if not code:
        return {"error": "Missing code"}

    valid_channels = ["discord", "telegram", "whatsapp", "slack", "signal", "imessage"]
    if channel not in valid_channels:
        return {"error": f"Invalid channel. Valid: {', '.join(valid_channels)}"}

    success, output = run_gateway_command(["node", "dist/index.js", "pairing", "approve", channel, code])
    audit_log("pairing_approve", {"channel": channel, "code": code, "success": success})

    if not success:
        return {"error": f"Failed to approve: {output}"}
    return {"status": "approved", "channel": channel, "code": code}


@app.get("/gateway/security-audit")
@app.get("/controller/gateway/security-audit")
async def gateway_security_audit(
    deep: bool = False,
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Run OpenClaw security audit."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    cmd = ["node", "dist/index.js", "security", "audit", "--json"]
    if deep:
        cmd.append("--deep")

    success, output = run_gateway_command(cmd, timeout=60)
    audit_log("security_audit", {"deep": deep, "success": success})

    if not success:
        return {"error": f"Failed to run security audit: {output}"}

    try:
        data = json.loads(output)
        return data
    except json.JSONDecodeError:
        return {"error": f"Invalid JSON response: {output[:500]}"}
