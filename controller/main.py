#!/usr/bin/env python3
"""
ClawFactory Controller

Responsibilities:
- Dashboard UI for managing gateway instances
- Gateway start/stop/restart/rebuild
- Pull upstream OpenClaw updates
- Create encrypted snapshots of bot state
- LLM traffic proxy & audit logging
- Gateway config editor
"""

import asyncio
import json
import os
import secrets
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import docker
from fastapi import Cookie, Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

import traffic_log
import scrub

# Configuration from environment
CODE_DIR = Path(os.environ.get("CODE_DIR", os.environ.get("APPROVED_DIR", "/srv/bot/code")))
OPENCLAW_HOME = Path(os.environ.get("OPENCLAW_HOME", "/srv/bot/state"))
AUDIT_LOG = Path(os.environ.get("AUDIT_LOG", "/srv/audit/audit.jsonl"))
INSTANCE_NAME = os.environ.get("INSTANCE_NAME", "default")
GATEWAY_CONTAINER = os.environ.get("GATEWAY_CONTAINER", f"clawfactory-{INSTANCE_NAME}-gateway")
GATEWAY_PORT = os.environ.get("GATEWAY_PORT", "18789")
CONTROLLER_API_TOKEN = os.environ.get("CONTROLLER_API_TOKEN", "")
GATEWAY_INTERNAL_TOKEN = os.environ.get("GATEWAY_INTERNAL_TOKEN", "")
AGENT_API_TOKEN = os.environ.get("AGENT_API_TOKEN", "")
SNAPSHOTS_DIR = Path(os.environ.get("SNAPSHOTS_DIR", "/srv/snapshots"))
AGE_KEY = Path(os.environ.get("AGE_KEY", "/srv/secrets/snapshot.key"))
SECRETS_DIR = Path(os.environ.get("SECRETS_DIR", f"/srv/clawfactory/secrets/{INSTANCE_NAME}"))

# Lima mode: GATEWAY_CONTAINER=local means systemd, not Docker
IS_LIMA_MODE = GATEWAY_CONTAINER == "local"


def gateway_stop():
    """Stop the gateway (systemd in Lima mode, Docker otherwise)."""
    if IS_LIMA_MODE:
        subprocess.run(
            ["systemctl", "stop", f"openclaw-gateway@{INSTANCE_NAME}"],
            capture_output=True, timeout=30
        )
    else:
        client = docker.from_env()
        container = client.containers.get(GATEWAY_CONTAINER)
        container.stop(timeout=30)


def gateway_start():
    """Start the gateway (systemd in Lima mode, Docker otherwise)."""
    if IS_LIMA_MODE:
        subprocess.run(
            ["systemctl", "start", f"openclaw-gateway@{INSTANCE_NAME}"],
            capture_output=True, timeout=30
        )
    else:
        client = docker.from_env()
        container = client.containers.get(GATEWAY_CONTAINER)
        container.start()

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


def check_internal_auth(
    token: Optional[str] = None,
    auth_header: Optional[str] = None,
) -> bool:
    """
    Check if request has valid internal (gateway) or admin authentication.

    Internal endpoints accept GATEWAY_INTERNAL_TOKEN or CONTROLLER_API_TOKEN.
    If neither is configured, access is open (backward compatibility).
    """
    if not GATEWAY_INTERNAL_TOKEN and not CONTROLLER_API_TOKEN:
        return True

    actual_token = None
    if token:
        actual_token = token
    elif auth_header and auth_header.startswith("Bearer "):
        actual_token = auth_header[7:]

    if not actual_token:
        return False

    if GATEWAY_INTERNAL_TOKEN and secrets.compare_digest(actual_token, GATEWAY_INTERNAL_TOKEN):
        return True
    if CONTROLLER_API_TOKEN and secrets.compare_digest(actual_token, CONTROLLER_API_TOKEN):
        return True

    return False


def check_agent_auth(
    token: Optional[str] = None,
    auth_header: Optional[str] = None,
) -> bool:
    """
    Check if request has a valid agent API token.

    Agent endpoints accept only AGENT_API_TOKEN — a scoped token that
    gives the sandboxed agent access to gateway proxy endpoints without
    holding the real gateway token.
    """
    if not AGENT_API_TOKEN:
        return False  # No agent token configured = deny

    actual_token = None
    if token:
        actual_token = token
    elif auth_header and auth_header.startswith("Bearer "):
        actual_token = auth_header[7:]

    if not actual_token:
        return False

    return secrets.compare_digest(actual_token, AGENT_API_TOKEN)


def resolve_agent_file_scope(agent_id: str) -> Optional[Path]:
    """Return the subdirectory of CODE_DIR this agent is allowed to write to.


    Returns None for empty/default/main (full access).
    Returns a Path relative to CODE_DIR for scoped agents.
    """
    if not agent_id or agent_id in ("default", "main"):
        return None  # full access

    # Read agents.list from live config
    try:
        with open(OPENCLAW_HOME / "openclaw.json") as f:
            cfg = json.load(f)
    except Exception:
        return Path(f"agents/{agent_id}")  # safe fallback

    agents_list = cfg.get("agents", {}).get("list", [])
    for agent in agents_list:
        if agent.get("id") == agent_id:
            workspace = agent.get("workspace", "")
            # Compute relative path within code dir
            code_str = str(CODE_DIR)
            if workspace.startswith(code_str):
                rel = workspace[len(code_str):].lstrip("/")
                if rel:
                    return Path(rel)
                return None  # workspace IS code dir → full access
            # Workspace not under code dir — use convention
            return Path(f"agents/{agent_id}")

    # Unknown agent — restrict to agents/<id> by default
    return Path(f"agents/{agent_id}")


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





def get_gateway_status() -> str:
    """Get gateway status (systemd in Lima mode, Docker otherwise)."""
    try:
        if IS_LIMA_MODE:
            result = subprocess.run(
                ["systemctl", "is-active", f"openclaw-gateway@{INSTANCE_NAME}"],
                capture_output=True, text=True, timeout=5
            )
            status = result.stdout.strip()
            # systemctl is-active returns: active, inactive, failed, activating, etc.
            return "running" if status == "active" else status or "unknown"
        else:
            client = docker.from_env()
            container = client.containers.get(GATEWAY_CONTAINER)
            return container.status
    except Exception:
        return "unknown"


def restart_gateway() -> bool:
    """Restart the gateway (systemd in Lima mode, Docker otherwise)."""
    try:
        if IS_LIMA_MODE:
            gateway_stop()
            gateway_start()
        else:
            client = docker.from_env()
            container = client.containers.get(GATEWAY_CONTAINER)
            container.restart(timeout=30)
        audit_log("gateway_restart", {"container": GATEWAY_CONTAINER})
        return True
    except Exception as e:
        audit_log("gateway_restart_error", {"error": str(e)})
        return False


# ============================================================
# Dashboard UI
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
                    <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><rect x='12' y='28' width='40' height='28' fill='%23455a64' rx='2'/><rect x='16' y='32' width='8' height='10' fill='%2390caf9'/><rect x='28' y='32' width='8' height='10' fill='%2390caf9'/><rect x='40' y='32' width='8' height='10' fill='%2390caf9'/><rect x='20' y='20' width='24' height='10' fill='%23546e7a'/><rect x='30' y='8' width='8' height='14' fill='%23607d8b'/><ellipse cx='34' cy='6' rx='5' ry='3' fill='%23ff5722'/><path d='M8 38 Q2 32 8 26 L12 30 Q10 34 12 38 Z' fill='%23e64a19'/><path d='M4 34 Q-2 30 4 24' stroke='%23ff7043' stroke-width='3' fill='none' stroke-linecap='round'/><path d='M56 38 Q62 32 56 26 L52 30 Q54 34 52 38 Z' fill='%23e64a19'/><path d='M60 34 Q66 30 60 24' stroke='%23ff7043' stroke-width='3' fill='none' stroke-linecap='round'/><circle cx='6' cy='22' r='3' fill='%23ff8a65'/><circle cx='58' cy='22' r='3' fill='%23ff8a65'/><rect x='24' y='48' width='16' height='8' fill='%23546e7a'/></svg>">
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

    # Get gateway status
    gateway_status = get_gateway_status()
    gateway_class = "success" if gateway_status == "running" else ("warning" if gateway_status == "unknown" else "error")

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>ClawFactory [{INSTANCE_NAME}]</title>
        <!-- Favicon: Factory with lobster claws -->
        <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><rect x='12' y='28' width='40' height='28' fill='%23455a64' rx='2'/><rect x='16' y='32' width='8' height='10' fill='%2390caf9'/><rect x='28' y='32' width='8' height='10' fill='%2390caf9'/><rect x='40' y='32' width='8' height='10' fill='%2390caf9'/><rect x='20' y='20' width='24' height='10' fill='%23546e7a'/><rect x='30' y='8' width='8' height='14' fill='%23607d8b'/><ellipse cx='34' cy='6' rx='5' ry='3' fill='%23ff5722'/><path d='M8 38 Q2 32 8 26 L12 30 Q10 34 12 38 Z' fill='%23e64a19'/><path d='M4 34 Q-2 30 4 24' stroke='%23ff7043' stroke-width='3' fill='none' stroke-linecap='round'/><path d='M56 38 Q62 32 56 26 L52 30 Q54 34 52 38 Z' fill='%23e64a19'/><path d='M60 34 Q66 30 60 24' stroke='%23ff7043' stroke-width='3' fill='none' stroke-linecap='round'/><circle cx='6' cy='22' r='3' fill='%23ff8a65'/><circle cx='58' cy='22' r='3' fill='%23ff8a65'/><rect x='24' y='48' width='16' height='8' fill='%23546e7a'/></svg>">
        <!-- CodeMirror for JSON editing -->
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/codemirror.min.css">
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/theme/material-darker.min.css">
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/fold/foldgutter.min.css">
        <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/codemirror.min.js"></script>
        <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/javascript/javascript.min.js"></script>
        <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/edit/matchbrackets.min.js"></script>
        <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/edit/closebrackets.min.js"></script>
        <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/fold/foldcode.min.js"></script>
        <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/fold/foldgutter.min.js"></script>
        <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/fold/brace-fold.min.js"></script>
        <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/search/searchcursor.min.js"></script>
        <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/yaml/yaml.min.js"></script>
        <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/markdown/markdown.min.js"></script>
        <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/python/python.min.js"></script>
        <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/shell/shell.min.js"></script>
        <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/toml/toml.min.js"></script>
        <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/css/css.min.js"></script>
        <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/htmlmixed/htmlmixed.min.js"></script>
        <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/xml/xml.min.js"></script>
        <style>
            * {{ box-sizing: border-box; }}
            body {{ font-family: monospace; padding: 0; background: #1a1a1a; color: #e0e0e0; margin: 0; display: flex; min-height: 100vh; }}
            #sidebar {{ width: 220px; min-width: 220px; background: #151515; border-right: 1px solid #333; padding: 1rem 0; position: fixed; top: 0; left: 0; bottom: 0; overflow-y: auto; z-index: 100; }}
            #sidebar .brand {{ padding: 0.75rem 1rem; font-size: 1.2rem; color: #4CAF50; font-weight: bold; border-bottom: 1px solid #333; margin-bottom: 0.5rem; }}
            #sidebar .brand span {{ color: #2196F3; font-size: 0.9rem; }}
            #sidebar a {{ display: block; padding: 0.6rem 1rem; color: #888; text-decoration: none; border-left: 3px solid transparent; }}
            #sidebar a:hover {{ background: #1e1e1e; color: #e0e0e0; }}
            #sidebar a.active {{ color: #4CAF50; border-left-color: #4CAF50; background: #1a2a1a; }}
            #content {{ margin-left: 220px; flex: 1; padding: 1rem 1.5rem; overflow-y: auto; max-width: 1200px; }}
            .page {{ display: none; }}
            .page.active {{ display: block; }}
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
            @keyframes pulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: 0.7; }} }}
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
            /* Diff syntax highlighting */
            .diff-view {{ font-family: monospace; font-size: 0.75rem; line-height: 1.4; }}
            .diff-view .diff-header {{ color: #61afef; font-weight: bold; }}
            .diff-view .diff-file {{ color: #e5c07b; font-weight: bold; }}
            .diff-view .diff-hunk {{ color: #c678dd; }}
            .diff-view .diff-add {{ color: #98c379; background: rgba(152, 195, 121, 0.1); }}
            .diff-view .diff-del {{ color: #e06c75; background: rgba(224, 108, 117, 0.1); }}
            .diff-view .diff-context {{ color: #abb2bf; }}
            /* CodeMirror customizations */
            .CodeMirror {{ height: 400px; font-size: 0.8rem; border: 1px solid #444; border-radius: 4px; }}
            .CodeMirror-gutters {{ background: #1e1e1e; border-right: 1px solid #333; }}
            .CodeMirror-linenumber {{ color: #5c6370; }}
            /* Traffic table styles */
            .traffic-table {{ width: 100%; border-collapse: collapse; font-size: 0.8rem; }}
            .traffic-table th {{ text-align: left; padding: 0.5rem; border-bottom: 2px solid #444; color: #888; }}
            .traffic-table td {{ padding: 0.5rem; border-bottom: 1px solid #333; }}
            .traffic-table tr:hover {{ background: #252525; }}
            .traffic-table .provider {{ font-weight: bold; }}
            .traffic-table .provider-anthropic {{ color: #d4a574; }}
            .traffic-table .provider-openai {{ color: #74b9ff; }}
            .traffic-table .provider-gemini {{ color: #a29bfe; }}
            .traffic-detail {{ background: #1e1e1e; padding: 1rem; border-radius: 4px; margin-top: 0.5rem; }}
            .traffic-detail pre {{ max-height: 400px; overflow: auto; }}
            .sub-tabs {{ display: flex; gap: 0; border-bottom: 1px solid #444; margin-bottom: 1rem; }}
            .sub-tab {{ background: none; border: none; padding: 0.5rem 1rem; color: #888; cursor: pointer; border-bottom: 2px solid transparent; border-radius: 0; font-family: monospace; }}
            .sub-tab:hover {{ color: #e0e0e0; }}
            .sub-tab.active {{ color: #4CAF50; border-bottom-color: #4CAF50; }}
            .sub-content {{ display: none; }}
            .sub-content.active {{ display: block; }}
            .scrub-rule {{ background: #252525; padding: 0.75rem; border-radius: 4px; margin-bottom: 0.5rem; border: 1px solid #333; }}
            .scrub-rule.builtin {{ border-left: 3px solid #2196F3; }}
            /* Mobile responsive */
            @media (max-width: 768px) {{
                #sidebar {{ display: none; }}
                #content {{ margin-left: 0; padding: 0.75rem; }}
                .grid {{ grid-template-columns: 1fr; gap: 1rem; }}
                .stats {{ justify-content: space-around; }}
                button {{ padding: 0.5rem 0.8rem; font-size: 0.85rem; }}
                input {{ max-width: 100%; }}
                pre {{ font-size: 0.75rem; max-height: 200px; }}
            }}
        </style>
    </head>
    <body>
        <nav id="sidebar">
            <div class="brand">ClawFactory <span>[{INSTANCE_NAME}]</span></div>
            <a href="#dashboard" class="active" onclick="switchPage('dashboard')">Dashboard</a>
            <a href="#gateway" onclick="switchPage('gateway')">Gateway</a>
            <a href="#logs" onclick="switchPage('logs')">Logs</a>
            <a href="#snapshots" onclick="switchPage('snapshots')">Snapshots</a>
            <a href="#settings" onclick="switchPage('settings')">Settings</a>
        </nav>

        <main id="content">
        <!-- ==================== DASHBOARD PAGE ==================== -->
        <div id="page-dashboard" class="page active">
        <h1>Dashboard</h1>

        <div class="stats">
            <div class="stat">
                <div class="stat-value {gateway_class}">
                    <span id="gateway-status-indicator" class="status-dot"></span>
                    <a href="http://localhost:{GATEWAY_PORT}" target="_blank" style="color: inherit; text-decoration: none;" title="Open Gateway on port {GATEWAY_PORT}">{gateway_status}</a>
                </div>
                <div class="stat-label">Gateway <a href="http://localhost:{GATEWAY_PORT}" target="_blank" style="color: #2196F3; text-decoration: none;">:{GATEWAY_PORT}</a> <span id="gateway-last-update" style="font-size: 0.7rem; color: #666;"></span></div>
            </div>
        </div>

        <div class="grid">
            <div>
                <h2>Controls</h2>
                <div class="card">
                    <div style="display: flex; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 1rem;">
                        <button onclick="pullUpstream()">Pull Latest OpenClaw</button>
                        <button onclick="rebuildGateway()" class="secondary">Rebuild Gateway</button>
                        <button onclick="restartGateway()" class="danger">Restart Gateway</button>
                    </div>
                    <div id="promote-result" class="result"></div>
                </div>
            </div>
        </div>
        </div><!-- /page-dashboard -->

        <!-- ==================== GATEWAY PAGE ==================== -->
        <div id="page-gateway" class="page">
        <h1>Gateway <a href="http://localhost:{GATEWAY_PORT}" target="_blank" style="color: #2196F3; font-size: 0.8rem; text-decoration: none;">:{GATEWAY_PORT}</a></h1>

        <h2>System Controls</h2>
        <div class="card">
            <button onclick="restartGateway()" class="danger">Restart Gateway</button>
            <div id="gateway-system-result" class="result"></div>
        </div>

        <h2>Gateway Config</h2>
        <div class="card">
            <p style="color: #888; font-size: 0.85rem;">Edit openclaw.json. Save will stop gateway, apply changes, and restart.</p>
            <div style="margin-bottom: 0.5rem;">
                <label style="color: #888; font-size: 0.85rem;">Available RAM for Ollama: </label>
                <input type="number" id="available-ram" value="64" min="8" max="512" style="width: 60px; padding: 0.3rem; background: #2d2d2d; border: 1px solid #444; color: #e0e0e0; border-radius: 4px;">
                <span style="color: #888; font-size: 0.85rem;">GB</span>
                <span style="color: #666; font-size: 0.75rem; margin-left: 1rem;">(used to calculate safe context windows)</span>
            </div>
            <button onclick="loadConfig()">Load Config</button>
            <button onclick="validateConfig()" class="secondary">Validate</button>
            <button onclick="saveConfig()" class="danger">Save &amp; Restart</button>
            <button onclick="formatConfig()" class="secondary">Format JSON</button>
            <button id="revert-config-btn" onclick="revertConfig()" class="secondary" style="display: none;">Revert to Backup</button>
            <div id="config-result" class="result"></div>
            <div id="ollama-models" style="margin-top: 0.5rem;"></div>
            <div style="display: flex; justify-content: space-between; margin-top: 0.5rem; font-size: 0.75rem; color: #888;">
                <span id="cursor-pos">Line 1, Col 1</span>
                <span id="json-status"></span>
            </div>
            <textarea id="config-editor-raw" style="display: none;"></textarea>
            <div id="config-editor-wrapper" style="margin-top: 0.25rem;"></div>
        </div>

        <h2>Gateway Pairing</h2>
        <div class="card">
            <p style="color: #888; font-size: 0.85rem; margin-bottom: 1rem;">
                Manage device connections and DM pairing across all channels.
            </p>

            <div class="tab-buttons" style="margin-bottom: 1rem;">
                <button class="tab-button active" onclick="showTab('devices')">Devices</button>
                <button class="tab-button" onclick="showTab('channels')">Channels</button>
            </div>

            <div id="tab-devices" class="tab-content active">
                <p style="color: #888; font-size: 0.85rem;">iOS, Android, and browser clients connecting to this gateway.</p>
                <button onclick="fetchDevices()" class="secondary">Refresh</button>
                <div id="devices-list" style="margin-top: 0.5rem;"></div>
                <div id="devices-result" class="result"></div>
            </div>

            <div id="tab-channels" class="tab-content">
                <p style="color: #888; font-size: 0.85rem;">DM pairing for messaging channels. Users send a pairing code to start chatting.</p>

                <div style="display: flex; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 1rem;">
                    <button onclick="refreshAllChannels()" class="secondary">Refresh All</button>
                </div>

                <div id="channels-grid" style="display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 1rem;">
                    <div class="channel-card" data-channel="discord">
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem;">
                            <strong style="color: #5865F2;">Discord</strong>
                            <span id="discord-status" class="channel-status" style="font-size: 0.75rem; color: #888;">--</span>
                        </div>
                        <div id="discord-pending" style="font-size: 0.85rem; color: #888;">Click refresh to load</div>
                        <button onclick="fetchChannelPairing('discord')" class="small secondary" style="margin-top: 0.5rem;">Refresh</button>
                    </div>

                    <div class="channel-card" data-channel="telegram">
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem;">
                            <strong style="color: #0088cc;">Telegram</strong>
                            <span id="telegram-status" class="channel-status" style="font-size: 0.75rem; color: #888;">--</span>
                        </div>
                        <div id="telegram-pending" style="font-size: 0.85rem; color: #888;">Click refresh to load</div>
                        <button onclick="fetchChannelPairing('telegram')" class="small secondary" style="margin-top: 0.5rem;">Refresh</button>
                    </div>

                    <div class="channel-card" data-channel="slack">
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem;">
                            <strong style="color: #E01E5A;">Slack</strong>
                            <span id="slack-status" class="channel-status" style="font-size: 0.75rem; color: #888;">--</span>
                        </div>
                        <div id="slack-pending" style="font-size: 0.85rem; color: #888;">Click refresh to load</div>
                        <button onclick="fetchChannelPairing('slack')" class="small secondary" style="margin-top: 0.5rem;">Refresh</button>
                    </div>
                </div>

                <div style="margin-top: 1.5rem; padding-top: 1rem; border-top: 1px solid #333;">
                    <h3 style="margin: 0 0 0.5rem 0; font-size: 0.9rem; color: #888;">Approve Pairing Code</h3>
                    <div style="display: flex; gap: 0.5rem; flex-wrap: wrap; align-items: center;">
                        <select id="pairing-channel" style="padding: 0.5rem; background: #2d2d2d; border: 1px solid #444; color: #e0e0e0; border-radius: 4px;">
                            <option value="discord">Discord</option>
                            <option value="telegram">Telegram</option>
                            <option value="slack">Slack</option>
                        </select>
                        <input type="text" id="pairing-code" placeholder="ABCD1234" style="width: 120px; text-transform: uppercase;">
                        <button onclick="approvePairingCode()">Approve</button>
                    </div>
                    <div id="pairing-result" class="result"></div>
                </div>
            </div>
        </div>
        </div><!-- /page-gateway -->

        <!-- ==================== LOGS PAGE ==================== -->
        <div id="page-logs" class="page">
        <h1>Logs</h1>

        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem;">
            <div class="sub-tabs" style="margin-bottom: 0;">
                <button class="sub-tab active" onclick="switchSubTab('traffic')">Traffic</button>
                <button class="sub-tab" onclick="switchSubTab('llm-sessions')">LLM Sessions</button>
                <button class="sub-tab" onclick="switchSubTab('audit')">Audit</button>
                <button class="sub-tab" onclick="switchSubTab('gateway-stdout')">Gateway Stdout</button>
                <button class="sub-tab" onclick="switchSubTab('scrub-rules')">Scrub Rules</button>
            </div>
            <div style="display: flex; align-items: center; gap: 0.5rem;">
                <span id="capture-status-dot" style="width: 8px; height: 8px; border-radius: 50%; background: #666; display: inline-block;"></span>
                <span id="capture-status-text" style="font-size: 0.8rem; color: #888;">MITM Capture: --</span>
                <span id="capture-entry-count" style="font-size: 0.75rem; color: #666;"></span>
                <button id="capture-toggle-btn" onclick="toggleCapture()" class="small" style="font-size: 0.75rem;">--</button>
            </div>
        </div>

        <!-- Traffic sub-tab -->
        <div id="sub-traffic" class="sub-content active">
            <div style="display: flex; gap: 0.5rem; flex-wrap: wrap; align-items: center; margin-bottom: 1rem;">
                <select id="traffic-provider-filter" style="padding: 0.4rem; background: #2d2d2d; border: 1px solid #444; color: #e0e0e0; border-radius: 4px;">
                    <option value="">All Providers</option>
                </select>
                <input type="text" id="traffic-search" placeholder="Search..." style="width: 200px; padding: 0.4rem;">
                <button onclick="fetchTraffic()" class="secondary small">Search</button>
                <button onclick="decryptTraffic()" class="small" style="background: #1565C0;">Decrypt &amp; View</button>
                <button onclick="fetchTrafficStats()" class="secondary small">Stats</button>
                <button onclick="deleteTrafficLogs()" class="small danger">Delete Logs</button>
            </div>
            <div id="traffic-stats" style="display: none; margin-bottom: 1rem;"></div>
            <div id="traffic-table-container">
                <p style="color: #888;">Click Search to load proxy traffic, or Decrypt &amp; View for MITM-captured traffic.</p>
            </div>
            <div id="traffic-detail" style="display: none;"></div>
        </div>

        <!-- LLM Sessions sub-tab -->
        <div id="sub-llm-sessions" class="sub-content">
            <p style="color: #888; font-size: 0.85rem;">Click on a traffic entry to view full request/response details.</p>
            <div id="llm-session-detail" style="margin-top: 1rem;">
                <p style="color: #888;">Select a traffic entry from the Traffic tab to view session details.</p>
            </div>
        </div>

        <!-- Audit sub-tab -->
        <div id="sub-audit" class="sub-content">
            <button onclick="fetchAudit()">Refresh</button>
            <button onclick="fetchAudit(100)" class="secondary">Last 100</button>
            <pre id="audit-log" style="margin-top: 0.5rem;">Click Refresh to load audit log...</pre>
        </div>

        <!-- Gateway Stdout sub-tab -->
        <div id="sub-gateway-stdout" class="sub-content">
            <button onclick="fetchGatewayLogs()">Refresh</button>
            <button onclick="fetchGatewayLogs(200)" class="secondary">Last 200</button>
            <button onclick="fetchGatewayLogs(500)" class="secondary">Last 500</button>
            <label style="margin-left: 1rem; color: #888;">
                <input type="checkbox" id="logs-auto-refresh" onchange="toggleLogsAutoRefresh()"> Auto-refresh
            </label>
            <pre id="gateway-logs" style="max-height: 500px; overflow-y: auto; font-size: 0.8rem; margin-top: 0.5rem;">Click Refresh to load gateway logs...</pre>
        </div>

        <!-- Scrub Rules sub-tab -->
        <div id="sub-scrub-rules" class="sub-content">
            <p style="color: #888; font-size: 0.85rem; margin-bottom: 1rem;">
                Regex patterns applied to scrub sensitive data from traffic logs before they're written to disk.
            </p>
            <button onclick="fetchScrubRules()">Load Rules</button>
            <button onclick="saveScrubRules()" class="secondary">Save Rules</button>
            <div id="scrub-rules-list" style="margin-top: 1rem;">
                <p style="color: #888;">Click Load Rules to view current scrub rules.</p>
            </div>

            <div style="margin-top: 1.5rem; padding-top: 1rem; border-top: 1px solid #333;">
                <h3 style="margin: 0 0 0.5rem 0;">Add Custom Rule</h3>
                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 0.5rem;">
                    <div>
                        <label style="color: #888; font-size: 0.85rem;">Name:</label>
                        <input type="text" id="scrub-rule-name" placeholder="My Rule" style="width: 100%;">
                    </div>
                    <div>
                        <label style="color: #888; font-size: 0.85rem;">ID:</label>
                        <input type="text" id="scrub-rule-id" placeholder="my-rule" style="width: 100%;">
                    </div>
                    <div style="grid-column: 1 / -1;">
                        <label style="color: #888; font-size: 0.85rem;">Pattern (regex):</label>
                        <input type="text" id="scrub-rule-pattern" placeholder="secret_[A-Za-z0-9]+" style="width: 100%;">
                    </div>
                    <div style="grid-column: 1 / -1;">
                        <label style="color: #888; font-size: 0.85rem;">Replacement:</label>
                        <input type="text" id="scrub-rule-replacement" value="***REDACTED***" style="width: 100%;">
                    </div>
                </div>
                <div style="margin-top: 0.5rem; display: flex; gap: 0.5rem;">
                    <button onclick="addScrubRule()" class="small">Add Rule</button>
                    <button onclick="testScrubRuleUI()" class="small secondary">Test Pattern</button>
                </div>
                <div style="margin-top: 0.5rem;">
                    <label style="color: #888; font-size: 0.85rem;">Test sample:</label>
                    <input type="text" id="scrub-test-sample" placeholder="Enter text to test against..." style="width: 100%;">
                </div>
                <div id="scrub-test-result" class="result" style="margin-top: 0.5rem;"></div>
            </div>
        </div>

        </div><!-- /page-logs -->

        <!-- ==================== SNAPSHOTS PAGE ==================== -->
        <div id="page-snapshots" class="page">
        <h1>Snapshots</h1>

        <h2>Snapshots</h2>
        <div class="card">
            <div style="display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap;">
                <input type="text" id="snapshot-name-input" placeholder="Name (optional)" style="width: 200px; padding: 0.3rem 0.5rem; background: #222; color: #eee; border: 1px solid #444; border-radius: 3px;">
                <button onclick="createSnapshot()">Create Snapshot</button>
                <button onclick="syncSnapshots()" class="secondary">Sync to Host</button>
                <button onclick="fetchSnapshots()" class="secondary">Refresh List</button>
                <button onclick="deleteAllSnapshots()" class="danger">Delete All</button>
            </div>
            <div id="snapshot-result" class="result"></div>
            <div id="snapshot-list" style="margin-top: 0.5rem;"></div>
            <div style="margin-top: 1rem; padding-top: 1rem; border-top: 1px solid #333;">
                <label style="color: #888;">Restore from snapshot:</label>
                <select id="snapshot-select" style="margin: 0.5rem 0; padding: 0.3rem; background: #222; color: #eee; border: 1px solid #444;">
                    <option value="latest">latest</option>
                </select>
                <button onclick="restoreSnapshot()" class="danger">Restore</button>
                <div id="restore-result" class="result" style="margin-top: 0.5rem;"></div>
            </div>
        </div>

        <h2>Security Audit</h2>
        <div class="card">
            <button onclick="runSecurityAudit()" class="secondary">Security Audit</button>
            <button onclick="runSecurityAudit(true)" class="secondary">Deep Audit</button>
            <div id="security-result" class="result"></div>
        </div>

        </div><!-- /page-snapshots -->

        <!-- ==================== SNAPSHOT BROWSER OVERLAY ==================== -->
        <div id="snapshot-browser-overlay" style="display:none; position:fixed; top:0; left:0; right:0; bottom:0; background:#1a1a1a; z-index:1000; flex-direction:column;">
            <!-- Top bar -->
            <div style="display:flex; align-items:center; gap:0.75rem; padding:0.5rem 1rem; background:#151515; border-bottom:1px solid #333; flex-shrink:0;">
                <span style="color:#4CAF50; font-weight:bold; font-size:1.1rem;">Browse</span>
                <span id="sb-snapshot-name" style="color:#888; font-size:0.9rem;"></span>
                <div style="flex:1;"></div>
                <input type="text" id="sb-save-name" placeholder="New snapshot name" style="width:200px; padding:0.3rem 0.5rem; background:#222; color:#eee; border:1px solid #444; border-radius:3px; font-family:monospace;">
                <button onclick="saveWorkspaceAsSnapshot()" class="secondary" style="padding:0.3rem 0.75rem; font-size:0.85rem;">Save as New Snapshot</button>
                <button onclick="closeSnapshotBrowser()" style="background:#c62828; padding:0.3rem 0.75rem; font-size:0.85rem;">Close</button>
            </div>
            <!-- Main content -->
            <div style="display:flex; flex:1; overflow:hidden;">
                <!-- Left panel: file tree -->
                <div style="width:280px; min-width:280px; background:#1e1e1e; border-right:1px solid #333; display:flex; flex-direction:column; overflow:hidden;">
                    <div style="padding:0.4rem 0.75rem; border-bottom:1px solid #333; display:flex; align-items:center; gap:0.5rem;">
                        <span style="color:#888; font-size:0.8rem; flex:1;">FILES</span>
                        <button onclick="refreshFileTree()" style="background:none; color:#888; border:none; padding:0.1rem 0.3rem; font-size:0.75rem; cursor:pointer;" title="Refresh">&#x21bb;</button>
                    </div>
                    <div id="sb-file-tree" style="flex:1; overflow-y:auto; padding:0.25rem 0; font-size:0.8rem;"></div>
                    <div id="sb-drop-zone" style="padding:0.75rem; border-top:1px solid #333; text-align:center; color:#666; font-size:0.8rem; cursor:pointer; min-height:50px; display:flex; align-items:center; justify-content:center;"
                         ondragover="handleDragOver(event)" ondrop="handleFileDrop(event)" ondragleave="this.style.borderColor='#333'; this.style.background='transparent';">
                        Drop files here to upload
                    </div>
                </div>
                <!-- Right panel: editor -->
                <div style="flex:1; display:flex; flex-direction:column; overflow:hidden;">
                    <!-- Editor toolbar -->
                    <div id="sb-editor-toolbar" style="display:none; padding:0.3rem 0.75rem; background:#252525; border-bottom:1px solid #333; align-items:center; gap:0.5rem;">
                        <span id="sb-current-file" style="color:#4CAF50; font-size:0.85rem; flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;"></span>
                        <button onclick="saveCurrentFile()" id="sb-save-btn" style="padding:0.2rem 0.6rem; font-size:0.8rem;">Save File</button>
                        <button onclick="downloadCurrentFile()" class="secondary" style="padding:0.2rem 0.6rem; font-size:0.8rem;">Download</button>
                    </div>
                    <!-- Editor area -->
                    <div id="sb-editor-area" style="flex:1; overflow:hidden; display:flex; align-items:center; justify-content:center;">
                        <div id="sb-welcome" style="color:#555; text-align:center;">
                            <div style="font-size:1.5rem; margin-bottom:0.5rem;">&#128193;</div>
                            <div>Select a file from the tree to view or edit</div>
                        </div>
                        <div id="sb-binary-msg" style="display:none; color:#888; text-align:center;">
                            <div style="font-size:1.5rem; margin-bottom:0.5rem;">&#128230;</div>
                            <div>Binary file — cannot edit</div>
                            <button onclick="downloadCurrentFile()" class="secondary" style="margin-top:0.75rem;">Download</button>
                        </div>
                        <div id="sb-codemirror-wrap" style="display:none; width:100%; height:100%;"></div>
                    </div>
                </div>
            </div>
        </div>

        <!-- ==================== SETTINGS PAGE ==================== -->
        <div id="page-settings" class="page">
        <h1>Settings</h1>

        <h2>Instance Info</h2>
        <div class="card">
            <div style="display: flex; flex-direction: column; gap: 0.5rem;">
                <div><span style="color: #888;">Instance:</span> <span style="color: #4CAF50;">{INSTANCE_NAME}</span></div>
                <div><span style="color: #888;">Gateway Port:</span> <span>{GATEWAY_PORT}</span></div>
                <div><span style="color: #888;">Mode:</span> <span style="color: #2196F3;">{"Lima" if IS_LIMA_MODE else "Docker"}</span></div>
            </div>
        </div>

        </div><!-- /page-settings -->

        </main><!-- /content -->

        <style>
            .channel-card {{
                background: #2d2d2d;
                padding: 1rem;
                border-radius: 4px;
                border: 1px solid #444;
            }}
            .channel-card:hover {{
                border-color: #555;
            }}
            .channel-status.connected {{ color: #4CAF50; }}
            .channel-status.pending {{ color: #ff9800; }}
            .channel-status.error {{ color: #ef9a9a; }}

            .status-dot {{
                display: inline-block;
                width: 8px;
                height: 8px;
                border-radius: 50%;
                margin-right: 6px;
                background: #666;
                animation: pulse 2s infinite;
            }}
            .status-dot.online {{
                background: #4CAF50;
                box-shadow: 0 0 6px #4CAF50;
            }}
            .status-dot.offline {{
                background: #ef9a9a;
                box-shadow: 0 0 6px #ef9a9a;
                animation: none;
            }}
            @keyframes pulse {{
                0%, 100% {{ opacity: 1; }}
                50% {{ opacity: 0.5; }}
            }}
        </style>

        <script>
            // Detect base path from current URL (handles /controller via Tailscale)
            const basePath = window.location.pathname.includes('/controller') ? '/controller' : '';

            // ---- HTML escaping (XSS prevention) ----
            function escHtml(str) {{
                if (str === null || str === undefined) return '';
                return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#039;');
            }}

            // ---- Sidebar navigation ----
            let configEditor;
            function switchPage(name) {{
                document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
                document.querySelectorAll('#sidebar a').forEach(a => a.classList.remove('active'));
                const page = document.getElementById('page-' + name);
                if (page) page.classList.add('active');
                const link = document.querySelector('#sidebar a[href="#' + name + '"]');
                if (link) link.classList.add('active');
                window.location.hash = name;

                // Refresh CodeMirror when gateway tab activates (sizing fix)
                if (name === 'gateway' && typeof configEditor !== 'undefined' && configEditor) {{
                    setTimeout(() => configEditor.refresh(), 50);
                }}
                // Auto-load data when switching to logs
                if (name === 'logs') {{
                    const activeSubTab = document.querySelector('.sub-tab.active');
                    if (activeSubTab && activeSubTab.textContent === 'Traffic') fetchTraffic();
                }}
                // Auto-load snapshots when switching to snapshots
                if (name === 'snapshots') {{
                    fetchSnapshots();
                }}
            }}

            // Hash-based routing
            function handleHash() {{
                const hash = window.location.hash.replace('#', '') || 'dashboard';
                switchPage(hash);
            }}
            window.addEventListener('hashchange', handleHash);
            // Set initial page from hash
            if (window.location.hash) handleHash();

            // Sub-tab switching (Logs page)
            function switchSubTab(name) {{
                document.querySelectorAll('.sub-content').forEach(c => c.classList.remove('active'));
                document.querySelectorAll('.sub-tab').forEach(t => t.classList.remove('active'));
                const content = document.getElementById('sub-' + name);
                if (content) content.classList.add('active');
                event.target.classList.add('active');
            }}

            // ---- Traffic functions ----
            let trafficPage = 0;

            async function loadTrafficProviders() {{
                try {{
                    const resp = await fetch(basePath + '/traffic/providers');
                    const data = await resp.json();
                    const select = document.getElementById('traffic-provider-filter');
                    if (select && data.providers) {{
                        data.providers.forEach(p => {{
                            const opt = document.createElement('option');
                            opt.value = p;
                            opt.textContent = p;
                            select.appendChild(opt);
                        }});
                    }}
                }} catch(e) {{ /* ignore */ }}
            }}
            loadTrafficProviders();

            async function fetchTraffic(page = 0) {{
                trafficPage = page;
                const container = document.getElementById('traffic-table-container');
                const provider = document.getElementById('traffic-provider-filter').value;
                const search = document.getElementById('traffic-search').value;
                container.innerHTML = '<p style="color: #888;">Loading traffic...</p>';
                try {{
                    let url = basePath + '/traffic?limit=50&offset=' + (page * 50);
                    if (provider) url += '&provider=' + provider;
                    if (search) url += '&search=' + encodeURIComponent(search);
                    const resp = await fetch(url);
                    const data = await resp.json();
                    renderTrafficTable(data.entries || []);
                }} catch(e) {{
                    container.innerHTML = '<p style="color: #ef9a9a;">Error: ' + escHtml(e.message) + '</p>';
                }}
            }}

            let lastTrafficDecrypted = false;

            function renderTrafficTable(entries, isDecrypted = false) {{
                lastTrafficDecrypted = isDecrypted;
                const container = document.getElementById('traffic-table-container');
                if (!entries || entries.length === 0) {{
                    container.innerHTML = '<p style="color: #888;">No traffic entries found.</p>';
                    return;
                }}
                let html = '';
                if (isDecrypted) {{
                    html += '<div style="margin-bottom: 0.5rem; font-size: 0.75rem; color: #1565C0; background: #0d2137; padding: 0.3rem 0.6rem; border-radius: 4px; display: inline-block;">Showing decrypted MITM traffic (not written to disk)</div>';
                }}
                html += '<table class="traffic-table"><thead><tr>';
                html += '<th>Time</th><th>Provider</th><th>Method</th><th>Path</th><th>Status</th><th>Duration</th><th>Tokens</th><th></th>';
                html += '</tr></thead><tbody>';
                entries.forEach(e => {{
                    const ts = e.timestamp ? escHtml(e.timestamp.slice(11, 19)) : '--';
                    const provClass = 'provider-' + escHtml(e.provider || 'unknown');
                    const statusColor = (e.response_status >= 400) ? '#ef9a9a' : '#a5d6a7';
                    const tokens = (e.tokens_in || 0) + (e.tokens_out || 0);
                    const tokenStr = tokens > 0 ? tokens.toLocaleString() : '--';
                    const duration = e.duration_ms ? Math.round(e.duration_ms) + 'ms' : '--';
                    const streamBadge = e.streaming ? ' <span style="color: #888; font-size: 0.7rem;">SSE</span>' : '';
                    const llmBadge = e.is_llm ? ' <span style="color: #ff9800; font-size: 0.7rem;">LLM</span>' : '';
                    const safeId = escHtml(e.id);
                    const detailFn = isDecrypted ? 'viewDecryptedDetail' : 'viewTrafficDetail';
                    html += `<tr style="cursor: pointer;" onclick="${{detailFn}}('${{safeId}}')">`;
                    html += `<td style="color: #888;">${{ts}}</td>`;
                    html += `<td class="provider ${{provClass}}">${{escHtml(e.provider || '?')}}</td>`;
                    html += `<td>${{escHtml(e.method || '?')}}</td>`;
                    html += `<td style="max-width: 250px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${{escHtml(e.path || e.url || '?')}}${{streamBadge}}${{llmBadge}}</td>`;
                    html += `<td style="color: ${{statusColor}};">${{escHtml(e.response_status || '?')}}</td>`;
                    html += `<td>${{duration}}</td>`;
                    html += `<td>${{tokenStr}}</td>`;
                    html += `<td><button class="small secondary" onclick="event.stopPropagation(); ${{detailFn}}('${{safeId}}')">Detail</button></td>`;
                    html += '</tr>';
                }});
                html += '</tbody></table>';

                // Pagination
                const pageFn = isDecrypted ? 'decryptTraffic' : 'fetchTraffic';
                html += '<div style="margin-top: 0.5rem; display: flex; gap: 0.5rem;">';
                if (trafficPage > 0) html += `<button class="small secondary" onclick="${{pageFn}}(${{trafficPage - 1}})">Previous</button>`;
                if (entries.length === 50) html += `<button class="small secondary" onclick="${{pageFn}}(${{trafficPage + 1}})">Next</button>`;
                html += '</div>';

                container.innerHTML = html;
            }}

            async function viewTrafficDetail(id) {{
                // Switch to LLM Sessions sub-tab and show detail
                const detail = document.getElementById('llm-session-detail');
                detail.innerHTML = '<p style="color: #888;">Loading details...</p>';

                // Switch to LLM Sessions tab
                document.querySelectorAll('.sub-content').forEach(c => c.classList.remove('active'));
                document.querySelectorAll('.sub-tab').forEach(t => t.classList.remove('active'));
                document.getElementById('sub-llm-sessions').classList.add('active');
                document.querySelectorAll('.sub-tab').forEach(t => {{
                    if (t.textContent === 'LLM Sessions') t.classList.add('active');
                }});

                try {{
                    const resp = await fetch(basePath + '/traffic/' + id);
                    const data = await resp.json();

                    let html = '<div class="traffic-detail">';
                    html += `<div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem;">`;
                    html += `<h3 style="margin: 0; color: #4CAF50;">Request Detail</h3>`;
                    html += `<button class="small secondary" onclick="document.querySelectorAll('.sub-content').forEach(c=>c.classList.remove('active'));document.getElementById('sub-traffic').classList.add('active');document.querySelectorAll('.sub-tab').forEach(t=>{{t.classList.remove('active');if(t.textContent==='Traffic')t.classList.add('active');}});">Back to Traffic</button>`;
                    html += `</div>`;

                    html += `<div class="stats">`;
                    html += `<div class="stat"><div class="stat-value provider-${{escHtml(data.provider || '')}}">${{escHtml(data.provider || '?')}}</div><div class="stat-label">Provider</div></div>`;
                    html += `<div class="stat"><div class="stat-value">${{escHtml(data.response_status || '?')}}</div><div class="stat-label">Status</div></div>`;
                    html += `<div class="stat"><div class="stat-value">${{Math.round(data.duration_ms || 0)}}ms</div><div class="stat-label">Duration</div></div>`;
                    html += `<div class="stat"><div class="stat-value">${{(data.tokens_in || 0).toLocaleString()}}</div><div class="stat-label">Tokens In</div></div>`;
                    html += `<div class="stat"><div class="stat-value">${{(data.tokens_out || 0).toLocaleString()}}</div><div class="stat-label">Tokens Out</div></div>`;
                    html += `</div>`;

                    html += `<p style="color: #888; font-size: 0.85rem;"><strong>ID:</strong> ${{escHtml(data.id)}} | <strong>Time:</strong> ${{escHtml(data.timestamp)}} | <strong>Method:</strong> ${{escHtml(data.method)}} <strong>Path:</strong> ${{escHtml(data.path)}}${{data.streaming ? ' | <span style="color: #ff9800;">Streaming</span>' : ''}}</p>`;

                    html += `<details style="margin-top: 1rem;"><summary style="cursor: pointer; color: #2196F3;">Request Headers</summary>`;
                    html += `<pre style="margin-top: 0.5rem;">${{escHtml(JSON.stringify(data.request_headers || {{}}, null, 2))}}</pre></details>`;

                    html += `<details open style="margin-top: 0.5rem;"><summary style="cursor: pointer; color: #4CAF50;">Request Body</summary>`;
                    html += `<pre style="margin-top: 0.5rem;">${{escHtml(JSON.stringify(data.request_body || null, null, 2))}}</pre></details>`;

                    html += `<details open style="margin-top: 0.5rem;"><summary style="cursor: pointer; color: #ff9800;">Response Body</summary>`;
                    html += `<pre style="margin-top: 0.5rem;">${{escHtml(JSON.stringify(data.response_body || null, null, 2))}}</pre></details>`;

                    if (data.error) {{
                        html += `<div style="margin-top: 0.5rem; padding: 0.5rem; background: #3d2020; border: 1px solid #ef9a9a; border-radius: 4px;"><strong style="color: #ef9a9a;">Error:</strong> ${{escHtml(data.error)}}</div>`;
                    }}

                    html += '</div>';
                    detail.innerHTML = html;
                }} catch(e) {{
                    detail.innerHTML = '<p style="color: #ef9a9a;">Error loading detail: ' + escHtml(e.message) + '</p>';
                }}
            }}

            async function viewDecryptedDetail(id) {{
                // Same as viewTrafficDetail but uses decrypt endpoint
                const detail = document.getElementById('llm-session-detail');
                detail.innerHTML = '<p style="color: #888;">Decrypting entry...</p>';

                document.querySelectorAll('.sub-content').forEach(c => c.classList.remove('active'));
                document.querySelectorAll('.sub-tab').forEach(t => t.classList.remove('active'));
                document.getElementById('sub-llm-sessions').classList.add('active');
                document.querySelectorAll('.sub-tab').forEach(t => {{
                    if (t.textContent === 'LLM Sessions') t.classList.add('active');
                }});

                try {{
                    const resp = await fetch(basePath + '/traffic/decrypt/' + id);
                    const data = await resp.json();

                    let html = '<div class="traffic-detail">';
                    html += `<div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem;">`;
                    html += `<h3 style="margin: 0; color: #1565C0;">Decrypted Request Detail</h3>`;
                    html += `<button class="small secondary" onclick="document.querySelectorAll('.sub-content').forEach(c=>c.classList.remove('active'));document.getElementById('sub-traffic').classList.add('active');document.querySelectorAll('.sub-tab').forEach(t=>{{t.classList.remove('active');if(t.textContent==='Traffic')t.classList.add('active');}});">Back to Traffic</button>`;
                    html += `</div>`;
                    html += `<div style="margin-bottom: 0.5rem; font-size: 0.75rem; color: #1565C0; background: #0d2137; padding: 0.3rem 0.6rem; border-radius: 4px; display: inline-block;">Decrypted in memory only</div>`;

                    html += `<div class="stats">`;
                    html += `<div class="stat"><div class="stat-value provider-${{escHtml(data.provider || '')}}">${{escHtml(data.provider || '?')}}</div><div class="stat-label">Provider</div></div>`;
                    html += `<div class="stat"><div class="stat-value">${{escHtml(data.response_status || '?')}}</div><div class="stat-label">Status</div></div>`;
                    html += `<div class="stat"><div class="stat-value">${{Math.round(data.duration_ms || 0)}}ms</div><div class="stat-label">Duration</div></div>`;
                    html += `<div class="stat"><div class="stat-value">${{(data.tokens_in || 0).toLocaleString()}}</div><div class="stat-label">Tokens In</div></div>`;
                    html += `<div class="stat"><div class="stat-value">${{(data.tokens_out || 0).toLocaleString()}}</div><div class="stat-label">Tokens Out</div></div>`;
                    html += `</div>`;

                    html += `<p style="color: #888; font-size: 0.85rem;"><strong>ID:</strong> ${{escHtml(data.id)}} | <strong>Time:</strong> ${{escHtml(data.timestamp)}} | <strong>Host:</strong> ${{escHtml(data.host || '?')}} | <strong>Method:</strong> ${{escHtml(data.method)}} <strong>URL:</strong> ${{escHtml(data.url || data.path)}}${{data.streaming ? ' | <span style="color: #ff9800;">Streaming</span>' : ''}}${{data.is_llm ? ' | <span style="color: #ff9800;">LLM</span>' : ''}}</p>`;

                    html += `<details style="margin-top: 1rem;"><summary style="cursor: pointer; color: #2196F3;">Request Headers</summary>`;
                    html += `<pre style="margin-top: 0.5rem;">${{escHtml(JSON.stringify(data.request_headers || {{}}, null, 2))}}</pre></details>`;

                    html += `<details open style="margin-top: 0.5rem;"><summary style="cursor: pointer; color: #4CAF50;">Request Body</summary>`;
                    html += `<pre style="margin-top: 0.5rem;">${{escHtml(JSON.stringify(data.request_body || null, null, 2))}}</pre></details>`;

                    html += `<details style="margin-top: 0.5rem;"><summary style="cursor: pointer; color: #2196F3;">Response Headers</summary>`;
                    html += `<pre style="margin-top: 0.5rem;">${{escHtml(JSON.stringify(data.response_headers || {{}}, null, 2))}}</pre></details>`;

                    html += `<details open style="margin-top: 0.5rem;"><summary style="cursor: pointer; color: #ff9800;">Response Body</summary>`;
                    html += `<pre style="margin-top: 0.5rem;">${{escHtml(JSON.stringify(data.response_body || null, null, 2))}}</pre></details>`;

                    html += '</div>';
                    detail.innerHTML = html;
                }} catch(e) {{
                    detail.innerHTML = '<p style="color: #ef9a9a;">Error decrypting entry: ' + escHtml(e.message) + '</p>';
                }}
            }}

            async function fetchTrafficStats() {{
                const container = document.getElementById('traffic-stats');
                container.style.display = 'block';
                container.innerHTML = '<p style="color: #888;">Loading stats...</p>';
                try {{
                    const resp = await fetch(basePath + '/traffic/stats');
                    const s = await resp.json();
                    let html = '<div class="card"><div class="stats">';
                    html += `<div class="stat"><div class="stat-value">${{s.total_requests}}</div><div class="stat-label">Total Requests</div></div>`;
                    html += `<div class="stat"><div class="stat-value">${{Math.round(s.avg_duration_ms)}}ms</div><div class="stat-label">Avg Duration</div></div>`;
                    html += `<div class="stat"><div class="stat-value">${{(s.total_tokens_in + s.total_tokens_out).toLocaleString()}}</div><div class="stat-label">Total Tokens</div></div>`;
                    html += `<div class="stat"><div class="stat-value">${{s.error_rate}}%</div><div class="stat-label">Error Rate</div></div>`;
                    html += '</div>';
                    if (Object.keys(s.by_provider).length > 0) {{
                        html += '<div style="margin-top: 0.5rem; font-size: 0.85rem;">';
                        Object.entries(s.by_provider).forEach(([prov, count]) => {{
                            html += `<span class="provider provider-${{escHtml(prov)}}" style="margin-right: 1rem;">${{escHtml(prov)}}: ${{count}}</span>`;
                        }});
                        html += '</div>';
                    }}
                    html += '</div>';
                    container.innerHTML = html;
                }} catch(e) {{
                    container.innerHTML = '<p style="color: #ef9a9a;">Error: ' + escHtml(e.message) + '</p>';
                }}
            }}

            // ---- MITM Capture toggle ----
            let captureEnabled = null;
            let captureEntryCount = 0;

            async function fetchCaptureStatus() {{
                try {{
                    const resp = await fetch(basePath + '/capture');
                    const data = await resp.json();
                    captureEnabled = data.enabled;
                    captureEntryCount = data.entry_count || 0;
                    updateCaptureUI();
                }} catch(e) {{
                    document.getElementById('capture-status-text').textContent = 'MITM Capture: error';
                }}
            }}

            function updateCaptureUI() {{
                const dot = document.getElementById('capture-status-dot');
                const text = document.getElementById('capture-status-text');
                const btn = document.getElementById('capture-toggle-btn');
                const countEl = document.getElementById('capture-entry-count');
                if (captureEnabled) {{
                    dot.style.background = '#4CAF50';
                    dot.style.boxShadow = '0 0 6px #4CAF50';
                    text.textContent = 'MITM Capture: ON';
                    text.style.color = '#4CAF50';
                    btn.textContent = 'Disable';
                    btn.className = 'small danger';
                }} else {{
                    dot.style.background = '#888';
                    dot.style.boxShadow = 'none';
                    text.textContent = 'MITM Capture: OFF';
                    text.style.color = '#888';
                    btn.textContent = 'Enable';
                    btn.className = 'small';
                }}
                if (countEl) {{
                    countEl.textContent = captureEntryCount > 0 ? `(${{captureEntryCount}} entries)` : '';
                }}
            }}

            async function toggleCapture() {{
                const newState = !captureEnabled;
                const action = newState ? 'enable' : 'disable';
                const msg = newState
                    ? 'Enable MITM capture?\\n\\nThis will:\\n- Start mitmproxy transparent proxy\\n- Redirect all gateway HTTPS traffic through it\\n- Log encrypted traffic entries'
                    : 'Disable MITM capture?\\n\\nThis will:\\n- Remove traffic redirect rules\\n- Stop mitmproxy';
                if (!confirm(msg)) return;
                try {{
                    btn = document.getElementById('capture-toggle-btn');
                    btn.textContent = '...';
                    btn.disabled = true;
                    const resp = await fetch(basePath + '/capture', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ enabled: newState }})
                    }});
                    const data = await resp.json();
                    captureEnabled = data.enabled;
                    updateCaptureUI();
                    btn.disabled = false;
                }} catch(e) {{
                    alert('Error toggling capture: ' + e.message);
                    document.getElementById('capture-toggle-btn').disabled = false;
                }}
            }}

            // ---- Decrypt & View (MITM encrypted traffic) ----
            async function decryptTraffic(page = 0) {{
                trafficPage = page;
                const container = document.getElementById('traffic-table-container');
                const provider = document.getElementById('traffic-provider-filter').value;
                const search = document.getElementById('traffic-search').value;
                container.innerHTML = '<p style="color: #888;">Decrypting traffic...</p>';
                try {{
                    let url = basePath + '/traffic/decrypt?limit=50&offset=' + (page * 50);
                    if (provider) url += '&provider=' + provider;
                    if (search) url += '&search=' + encodeURIComponent(search);
                    const resp = await fetch(url);
                    if (!resp.ok) {{
                        const err = await resp.json();
                        container.innerHTML = '<p style="color: #ef9a9a;">' + escHtml(err.detail || 'Decryption failed') + '</p>';
                        return;
                    }}
                    const data = await resp.json();
                    renderTrafficTable(data.entries || [], true);
                }} catch(e) {{
                    container.innerHTML = '<p style="color: #ef9a9a;">Error: ' + escHtml(e.message) + '</p>';
                }}
            }}

            // ---- Delete encrypted traffic logs ----
            async function deleteTrafficLogs() {{
                const deleteKey = confirm('Also delete the encryption key?\\n\\nOK = Delete logs + key (old logs become unreadable)\\nCancel = Delete logs only (key preserved for new captures)');
                if (!confirm('Delete all captured traffic logs?\\n\\nThis cannot be undone.')) return;
                try {{
                    const resp = await fetch(basePath + '/traffic/delete', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ delete_key: deleteKey }})
                    }});
                    const data = await resp.json();
                    let msg = 'Logs deleted.';
                    if (data.deleted_key) msg += ' Encryption key also deleted.';
                    alert(msg);
                    document.getElementById('traffic-table-container').innerHTML = '<p style="color: #888;">Logs deleted.</p>';
                    fetchCaptureStatus();
                }} catch(e) {{
                    alert('Error deleting logs: ' + e.message);
                }}
            }}

            // Load capture status on page load
            fetchCaptureStatus();

            // ---- Scrub Rules functions ----
            let currentScrubRules = [];

            async function fetchScrubRules() {{
                const list = document.getElementById('scrub-rules-list');
                list.innerHTML = '<p style="color: #888;">Loading rules...</p>';
                try {{
                    const resp = await fetch(basePath + '/scrub-rules');
                    const data = await resp.json();
                    currentScrubRules = data.rules || [];
                    renderScrubRules();
                }} catch(e) {{
                    list.innerHTML = '<p style="color: #ef9a9a;">Error: ' + escHtml(e.message) + '</p>';
                }}
            }}

            function renderScrubRules() {{
                const list = document.getElementById('scrub-rules-list');
                if (!currentScrubRules || currentScrubRules.length === 0) {{
                    list.innerHTML = '<p style="color: #888;">No rules configured.</p>';
                    return;
                }}
                let html = '';
                currentScrubRules.forEach((r, idx) => {{
                    const builtinClass = r.builtin ? ' builtin' : '';
                    const enabledColor = r.enabled ? '#4CAF50' : '#888';
                    html += `<div class="scrub-rule${{builtinClass}}">`;
                    html += `<div style="display: flex; justify-content: space-between; align-items: center;">`;
                    html += `<div><strong style="color: ${{enabledColor}};">${{escHtml(r.name || r.id)}}</strong>`;
                    if (r.builtin) html += ` <span style="color: #2196F3; font-size: 0.75rem;">built-in</span>`;
                    html += `<br><code style="font-size: 0.75rem; color: #888;">${{escHtml(r.pattern)}}</code></div>`;
                    html += `<div style="display: flex; gap: 0.3rem;">`;
                    html += `<button class="small ${{r.enabled ? 'danger' : ''}}" onclick="toggleScrubRule(${{idx}})">${{r.enabled ? 'Disable' : 'Enable'}}</button>`;
                    if (!r.builtin) html += `<button class="small danger" onclick="removeScrubRule(${{idx}})">Remove</button>`;
                    html += `</div></div></div>`;
                }});
                list.innerHTML = html;
            }}

            function toggleScrubRule(idx) {{
                if (currentScrubRules[idx]) {{
                    currentScrubRules[idx].enabled = !currentScrubRules[idx].enabled;
                    renderScrubRules();
                }}
            }}

            function removeScrubRule(idx) {{
                if (currentScrubRules[idx] && !currentScrubRules[idx].builtin) {{
                    currentScrubRules.splice(idx, 1);
                    renderScrubRules();
                }}
            }}

            function addScrubRule() {{
                const name = document.getElementById('scrub-rule-name').value.trim();
                const id = document.getElementById('scrub-rule-id').value.trim();
                const pattern = document.getElementById('scrub-rule-pattern').value.trim();
                const replacement = document.getElementById('scrub-rule-replacement').value || '***REDACTED***';
                if (!name || !id || !pattern) {{
                    alert('Name, ID, and Pattern are required.');
                    return;
                }}
                currentScrubRules.push({{ id, name, pattern, replacement, enabled: true, builtin: false }});
                renderScrubRules();
                document.getElementById('scrub-rule-name').value = '';
                document.getElementById('scrub-rule-id').value = '';
                document.getElementById('scrub-rule-pattern').value = '';
            }}

            async function saveScrubRules() {{
                try {{
                    const resp = await fetch(basePath + '/scrub-rules', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ rules: currentScrubRules }})
                    }});
                    const data = await resp.json();
                    alert('Scrub rules saved (' + data.rule_count + ' rules).');
                }} catch(e) {{
                    alert('Error saving rules: ' + e.message);
                }}
            }}

            async function testScrubRuleUI() {{
                const pattern = document.getElementById('scrub-rule-pattern').value.trim();
                const replacement = document.getElementById('scrub-rule-replacement').value || '***REDACTED***';
                const sample = document.getElementById('scrub-test-sample').value;
                const result = document.getElementById('scrub-test-result');

                if (!pattern || !sample) {{
                    result.style.display = 'block';
                    result.className = 'result error';
                    result.textContent = 'Enter both a pattern and sample text.';
                    return;
                }}

                try {{
                    const resp = await fetch(basePath + '/scrub-rules/test', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ pattern, replacement, sample }})
                    }});
                    const data = await resp.json();
                    result.style.display = 'block';
                    if (data.valid) {{
                        result.className = 'result';
                        result.innerHTML = `<strong>Matches:</strong> ${{data.matches}}<br><strong>Result:</strong> <code>${{escHtml(data.result)}}</code>`;
                    }} else {{
                        result.className = 'result error';
                        result.textContent = 'Invalid regex: ' + data.error;
                    }}
                }} catch(e) {{
                    result.style.display = 'block';
                    result.className = 'result error';
                    result.textContent = 'Error: ' + e.message;
                }}
            }}

            // Initialize CodeMirror editor
            document.addEventListener('DOMContentLoaded', function() {{
                configEditor = CodeMirror(document.getElementById('config-editor-wrapper'), {{
                    mode: {{ name: 'javascript', json: true }},
                    theme: 'material-darker',
                    lineNumbers: true,
                    matchBrackets: true,
                    autoCloseBrackets: true,
                    foldGutter: true,
                    gutters: ['CodeMirror-linenumbers', 'CodeMirror-foldgutter'],
                    tabSize: 2,
                    indentWithTabs: false,
                    lineWrapping: false,
                    placeholder: 'Click "Load Config" to view...'
                }});

                // Update cursor position display
                configEditor.on('cursorActivity', function() {{
                    const cursor = configEditor.getCursor();
                    document.getElementById('cursor-pos').textContent = `Line ${{cursor.line + 1}}, Col ${{cursor.ch + 1}}`;
                }});

                // Live JSON validation
                configEditor.on('change', function() {{
                    const jsonStatus = document.getElementById('json-status');
                    const value = configEditor.getValue();
                    if (!value.trim()) {{
                        jsonStatus.textContent = '';
                        return;
                    }}
                    try {{
                        JSON.parse(value);
                        jsonStatus.innerHTML = '<span style="color: #4CAF50;">✓ Valid JSON</span>';
                    }} catch(e) {{
                        const match = e.message.match(/position\s+(\d+)/i);
                        if (match) {{
                            const pos = parseInt(match[1]);
                            const cmPos = configEditor.posFromIndex(pos);
                            jsonStatus.innerHTML = `<span style="color: #ef9a9a;">✗ Error at line ${{cmPos.line + 1}}</span>`;
                        }} else {{
                            jsonStatus.innerHTML = '<span style="color: #ef9a9a;">✗ Invalid JSON</span>';
                        }}
                    }}
                }});
            }});

            // Helper to get/set editor value
            function getEditorValue() {{
                return configEditor ? configEditor.getValue() : '';
            }}
            function setEditorValue(value) {{
                if (configEditor) configEditor.setValue(value);
            }}

            async function fetchHealth() {{
                const statusIndicator = document.getElementById('gateway-status-indicator');
                const lastUpdate = document.getElementById('gateway-last-update');
                try {{
                    const resp = await fetch(basePath + '/health');
                    const data = await resp.json();
                    const now = new Date().toLocaleTimeString();
                    if (statusIndicator) {{
                        statusIndicator.className = data.status === 'healthy' ? 'status-dot online' : 'status-dot offline';
                    }}
                    if (lastUpdate) {{
                        lastUpdate.textContent = '(' + now + ')';
                    }}
                }} catch(e) {{
                    if (statusIndicator) {{
                        statusIndicator.className = 'status-dot offline';
                    }}
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

            // Gateway logs
            let logsAutoRefreshInterval = null;

            async function fetchGatewayLogs(lines = 100) {{
                const log = document.getElementById('gateway-logs');
                try {{
                    const resp = await fetch(basePath + '/gateway/logs?lines=' + lines);
                    const data = await resp.json();
                    if (data.error) {{
                        log.textContent = 'Error: ' + data.error;
                    }} else if (data.logs) {{
                        log.textContent = data.logs;
                        // Auto-scroll to bottom
                        log.scrollTop = log.scrollHeight;
                    }} else {{
                        log.textContent = 'No logs available.';
                    }}
                }} catch(e) {{
                    log.textContent = 'Error: ' + e.message;
                }}
            }}

            function toggleLogsAutoRefresh() {{
                const checkbox = document.getElementById('logs-auto-refresh');
                if (checkbox.checked) {{
                    fetchGatewayLogs();
                    logsAutoRefreshInterval = setInterval(() => fetchGatewayLogs(), 3000);
                }} else {{
                    if (logsAutoRefreshInterval) {{
                        clearInterval(logsAutoRefreshInterval);
                        logsAutoRefreshInterval = null;
                    }}
                }}
            }}

            // Snapshots
            async function createSnapshot() {{
                const result = document.getElementById('snapshot-result');
                result.style.display = 'block';
                result.className = 'result';
                result.textContent = 'Creating snapshot...';
                const nameInput = document.getElementById('snapshot-name-input');
                const snapshotName = nameInput ? nameInput.value.trim() : '';
                try {{
                    const resp = await fetch(basePath + '/snapshot', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ name: snapshotName }})
                    }});
                    const data = await resp.json();
                    if (!resp.ok || data.error || data.detail) {{
                        result.className = 'result error';
                        result.textContent = data.error || data.detail || 'Unknown error';
                    }} else {{
                        result.innerHTML = 'Created: ' + escapeHtml(data.name) + ' (' + formatSize(data.size) + ') '
                            + '<a href="' + basePath + '/snapshot/download/' + encodeURIComponent(data.name) + '" '
                            + 'style="color: #2196F3; margin-left: 0.5rem;" download>Download</a>';
                        if (nameInput) nameInput.value = '';
                        fetchSnapshots();
                    }}
                }} catch(e) {{
                    result.className = 'result error';
                    result.textContent = 'Error: ' + e.message;
                }}
            }}

            async function syncSnapshots() {{
                const result = document.getElementById('snapshot-result');
                result.style.display = 'block';
                result.className = 'result';
                result.textContent = 'Syncing snapshots to host...';
                try {{
                    const resp = await fetch(basePath + '/snapshot/sync', {{ method: 'POST' }});
                    const data = await resp.json();
                    if (!resp.ok || data.error || data.detail) {{
                        result.className = 'result error';
                        result.textContent = data.error || data.detail || 'Sync failed';
                    }} else {{
                        result.textContent = 'Synced ' + (data.count || 0) + ' snapshot(s) to host';
                    }}
                }} catch(e) {{
                    result.className = 'result error';
                    result.textContent = 'Error: ' + e.message;
                }}
            }}

            async function fetchSnapshots() {{
                const list = document.getElementById('snapshot-list');
                const select = document.getElementById('snapshot-select');
                list.innerHTML = '<p style="color: #888;">Loading...</p>';
                try {{
                    const resp = await fetch(basePath + '/snapshot');
                    const data = await resp.json();
                    if (!data.snapshots || data.snapshots.length === 0) {{
                        list.innerHTML = '<p style="color: #888; font-size: 0.85rem;">No snapshots yet.</p>';
                        select.innerHTML = '<option value="latest">latest</option>';
                        return;
                    }}
                    let html = '<div style="max-height: 300px; overflow-y: auto;">';
                    let selectHtml = '<option value="latest">latest</option>';
                    data.snapshots.forEach(s => {{
                        const latest = s.latest ? ' <span style="color: #4CAF50;">(latest)</span>' : '';
                        const displayLabel = s.label || 'snapshot';
                        const displayName = displayLabel === 'snapshot' ? '' : `<strong>${{displayLabel}}</strong> · `;
                        html += `<div style="padding: 0.4rem 0; border-bottom: 1px solid #333; font-size: 0.85rem; display: flex; justify-content: space-between; align-items: center; gap: 0.5rem;">
                            <div style="flex: 1; min-width: 0;">
                                <div>${{displayName}}<small style="color: #888;">${{s.created}}</small>${{latest}}</div>
                                <div><code style="font-size: 0.75rem; color: #666;">${{s.name}}</code> · <small style="color: #888;">${{formatSize(s.size)}}</small></div>
                            </div>
                            <div style="display: flex; gap: 0.3rem; flex-shrink: 0;">
                                <button onclick="openSnapshotBrowser('${{s.name}}')" style="background: #2196F3; color: white; border: none; padding: 0.2rem 0.5rem; border-radius: 3px; cursor: pointer; font-size: 0.75rem;">Browse</button>
                                <button onclick="renameSnapshot('${{s.name}}')" style="background: #555; color: white; border: none; padding: 0.2rem 0.5rem; border-radius: 3px; cursor: pointer; font-size: 0.75rem;">Rename</button>
                                <button onclick="deleteSnapshot('${{s.name}}')" style="background: #c62828; color: white; border: none; padding: 0.2rem 0.5rem; border-radius: 3px; cursor: pointer; font-size: 0.75rem;">Delete</button>
                            </div>
                        </div>`;
                        const selectLabel = displayLabel === 'snapshot' ? s.created : `${{displayLabel}} (${{s.created}})`;
                        selectHtml += `<option value="${{s.name}}">${{selectLabel}}</option>`;
                    }});
                    html += '</div>';
                    list.innerHTML = html;
                    select.innerHTML = selectHtml;
                }} catch(e) {{
                    list.innerHTML = `<p class="error" style="color: #ef9a9a;">Error: ${{e.message}}</p>`;
                }}
            }}

            async function restoreSnapshot() {{
                const select = document.getElementById('snapshot-select');
                const snapshot = select.value;
                const result = document.getElementById('restore-result');

                if (!confirm(`Restore from "${{snapshot}}"? This will:\\n- Stop the gateway\\n- Replace current state with snapshot\\n- Restart the gateway\\n\\nCurrent state will be backed up.`)) {{
                    return;
                }}

                result.style.display = 'block';
                result.className = 'result';
                result.textContent = 'Restoring... (this may take a minute)';

                try {{
                    const resp = await fetch(basePath + '/snapshot/restore', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ snapshot: snapshot }})
                    }});
                    const data = await resp.json();
                    if (data.error || data.detail) {{
                        result.className = 'result error';
                        result.textContent = 'Error: ' + (data.error || data.detail);
                    }} else {{
                        result.className = 'result';
                        result.textContent = 'Restored from ' + data.snapshot + '. Backup at: ' + data.backup;
                        if (data.warning) {{
                            result.textContent += '\\nWarning: ' + data.warning;
                        }}
                    }}
                }} catch(e) {{
                    result.className = 'result error';
                    result.textContent = 'Error: ' + e.message;
                }}
            }}

            async function deleteSnapshot(name) {{
                if (!confirm(`Delete snapshot "${{name}}"?`)) return;
                const result = document.getElementById('snapshot-result');
                result.style.display = 'block';
                result.className = 'result';
                result.textContent = 'Deleting...';
                try {{
                    const resp = await fetch(basePath + '/snapshot/delete', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ snapshot: name }})
                    }});
                    const data = await resp.json();
                    if (data.error || data.detail) {{
                        result.className = 'result error';
                        result.textContent = 'Error: ' + (data.error || data.detail);
                    }} else {{
                        result.textContent = 'Deleted: ' + data.deleted;
                        fetchSnapshots();
                    }}
                }} catch(e) {{
                    result.className = 'result error';
                    result.textContent = 'Error: ' + e.message;
                }}
            }}

            async function deleteAllSnapshots() {{
                if (!confirm('Delete ALL snapshots? This cannot be undone.')) return;
                const result = document.getElementById('snapshot-result');
                result.style.display = 'block';
                result.className = 'result';
                result.textContent = 'Deleting all snapshots...';
                try {{
                    const resp = await fetch(basePath + '/snapshot/delete', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ snapshot: 'all' }})
                    }});
                    const data = await resp.json();
                    if (data.error || data.detail) {{
                        result.className = 'result error';
                        result.textContent = 'Error: ' + (data.error || data.detail);
                    }} else {{
                        result.textContent = 'Deleted ' + data.deleted + ' snapshot(s)';
                        fetchSnapshots();
                    }}
                }} catch(e) {{
                    result.className = 'result error';
                    result.textContent = 'Error: ' + e.message;
                }}
            }}

            async function renameSnapshot(name) {{
                const newName = prompt('Enter new name for snapshot:', '');
                if (newName === null || newName.trim() === '') return;
                const result = document.getElementById('snapshot-result');
                result.style.display = 'block';
                result.className = 'result';
                result.textContent = 'Renaming...';
                try {{
                    const resp = await fetch(basePath + '/snapshot/rename', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ snapshot: name, new_name: newName.trim() }})
                    }});
                    const data = await resp.json();
                    if (data.error || data.detail) {{
                        result.className = 'result error';
                        result.textContent = 'Error: ' + (data.error || data.detail);
                    }} else {{
                        result.textContent = 'Renamed to: ' + data.new_name;
                        fetchSnapshots();
                    }}
                }} catch(e) {{
                    result.className = 'result error';
                    result.textContent = 'Error: ' + e.message;
                }}
            }}

            function formatSize(bytes) {{
                if (bytes < 1024) return bytes + ' B';
                if (bytes < 1024*1024) return (bytes/1024).toFixed(1) + ' KB';
                return (bytes/(1024*1024)).toFixed(1) + ' MB';
            }}

            function escapeHtml(str) {{
                const d = document.createElement('div');
                d.textContent = str;
                return d.innerHTML;
            }}

            // ==================== Snapshot Browser ====================
            let sbWorkspaceId = null;
            let sbEditor = null;
            let sbCurrentPath = null;
            let sbDirty = false;
            let sbCollapsedDirs = {{}};

            function getModeForFile(filename) {{
                const ext = filename.split('.').pop().toLowerCase();
                const modes = {{
                    'js': 'javascript', 'mjs': 'javascript', 'cjs': 'javascript',
                    'json': {{name: 'javascript', json: true}},
                    'ts': 'javascript', 'tsx': 'javascript', 'jsx': 'javascript',
                    'py': 'python', 'pyw': 'python',
                    'sh': 'shell', 'bash': 'shell', 'zsh': 'shell',
                    'yml': 'yaml', 'yaml': 'yaml',
                    'md': 'markdown', 'markdown': 'markdown',
                    'toml': 'toml',
                    'css': 'css', 'scss': 'css', 'less': 'css',
                    'html': 'htmlmixed', 'htm': 'htmlmixed',
                    'xml': 'xml', 'svg': 'xml',
                }};
                return modes[ext] || null;
            }}

            async function openSnapshotBrowser(name) {{
                const result = document.getElementById('snapshot-result');
                result.style.display = 'block';
                result.textContent = 'Opening snapshot browser...';
                try {{
                    const resp = await fetch(basePath + '/snapshot/browse/open', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ snapshot: name }})
                    }});
                    const data = await resp.json();
                    if (data.detail) throw new Error(data.detail);
                    sbWorkspaceId = data.workspace_id;
                    document.getElementById('sb-snapshot-name').textContent = data.snapshot_name;
                    document.getElementById('sb-save-name').value = '';
                    document.getElementById('snapshot-browser-overlay').style.display = 'flex';
                    result.style.display = 'none';
                    sbCurrentPath = null;
                    sbDirty = false;
                    sbCollapsedDirs = {{}};
                    showWelcome();
                    await refreshFileTree();
                    // Init CodeMirror if needed
                    if (!sbEditor) {{
                        const wrap = document.getElementById('sb-codemirror-wrap');
                        sbEditor = CodeMirror(wrap, {{
                            theme: 'material-darker',
                            lineNumbers: true,
                            matchBrackets: true,
                            autoCloseBrackets: true,
                            foldGutter: true,
                            gutters: ['CodeMirror-linenumbers', 'CodeMirror-foldgutter'],
                            readOnly: false,
                            lineWrapping: true,
                        }});
                        sbEditor.on('change', () => {{ sbDirty = true; }});
                    }}
                }} catch(e) {{
                    result.className = 'result error';
                    result.textContent = 'Error: ' + e.message;
                }}
            }}

            async function closeSnapshotBrowser() {{
                if (sbDirty && !confirm('You have unsaved changes. Close anyway?')) return;
                if (sbWorkspaceId) {{
                    try {{
                        navigator.sendBeacon(basePath + '/snapshot/browse/close?token=' + encodeURIComponent(new URLSearchParams(window.location.search).get('token') || ''),
                            new Blob([JSON.stringify({{workspace_id: sbWorkspaceId}})], {{type: 'application/json'}}));
                    }} catch(e) {{}}
                }}
                sbWorkspaceId = null;
                sbCurrentPath = null;
                sbDirty = false;
                document.getElementById('snapshot-browser-overlay').style.display = 'none';
                fetchSnapshots();
            }}

            function showWelcome() {{
                document.getElementById('sb-welcome').style.display = '';
                document.getElementById('sb-binary-msg').style.display = 'none';
                document.getElementById('sb-codemirror-wrap').style.display = 'none';
                document.getElementById('sb-editor-toolbar').style.display = 'none';
            }}

            async function refreshFileTree() {{
                if (!sbWorkspaceId) return;
                const tree = document.getElementById('sb-file-tree');
                tree.innerHTML = '<div style="padding:0.5rem; color:#888;">Loading...</div>';
                try {{
                    const resp = await fetch(basePath + '/snapshot/browse/files?workspace_id=' + encodeURIComponent(sbWorkspaceId));
                    const data = await resp.json();
                    if (data.detail) throw new Error(data.detail);
                    const root = buildFileTree(data.files);
                    tree.innerHTML = '';
                    renderTree(root, tree, 0);
                }} catch(e) {{
                    tree.innerHTML = '<div style="padding:0.5rem; color:#ef9a9a;">Error: ' + e.message + '</div>';
                }}
            }}

            function buildFileTree(files) {{
                const root = {{ children: {{}}, files: [] }};
                files.forEach(f => {{
                    const parts = f.path.split('/');
                    let node = root;
                    if (f.is_dir) {{
                        parts.forEach(p => {{
                            if (!node.children[p]) node.children[p] = {{ children: {{}}, files: [] }};
                            node = node.children[p];
                        }});
                        node._meta = f;
                    }} else {{
                        const dir = parts.slice(0, -1);
                        dir.forEach(p => {{
                            if (!node.children[p]) node.children[p] = {{ children: {{}}, files: [] }};
                            node = node.children[p];
                        }});
                        node.files.push(f);
                    }}
                }});
                return root;
            }}

            function renderTree(node, container, depth) {{
                // Dirs first, sorted
                const dirs = Object.keys(node.children).sort();
                dirs.forEach(name => {{
                    const child = node.children[name];
                    const path = child._meta ? child._meta.path : name;
                    const collapsed = sbCollapsedDirs[path];
                    const div = document.createElement('div');
                    const row = document.createElement('div');
                    row.style.cssText = 'display:flex; align-items:center; padding:0.15rem 0.5rem; padding-left:' + (depth * 16 + 8) + 'px; cursor:pointer; color:#e0e0e0; white-space:nowrap;';
                    row.onmouseover = () => {{ row.style.background = '#2a2a2a'; acts.style.visibility = 'visible'; }};
                    row.onmouseout = () => {{ row.style.background = ''; acts.style.visibility = 'hidden'; }};

                    const arrow = document.createElement('span');
                    arrow.style.cssText = 'width:16px; text-align:center; flex-shrink:0; color:#888; font-size:0.7rem;';
                    arrow.textContent = collapsed ? '\u25b6' : '\u25bc';
                    row.appendChild(arrow);

                    const label = document.createElement('span');
                    label.style.cssText = 'flex:1; overflow:hidden; text-overflow:ellipsis; color:#90CAF9;';
                    label.textContent = name;
                    row.appendChild(label);

                    const acts = document.createElement('span');
                    acts.style.cssText = 'visibility:hidden; display:flex; gap:0.2rem; flex-shrink:0;';
                    function mkAct(label, color, handler) {{
                        const s = document.createElement('span');
                        s.textContent = label;
                        s.title = label === '\u2b07' ? 'Download' : label === '\u270e' ? 'Rename' : label === '\u29c9' ? 'Duplicate' : 'Delete';
                        s.style.cssText = 'cursor:pointer; color:' + color + '; font-size:0.7rem;';
                        s.onclick = (e) => {{ e.stopPropagation(); handler(); }};
                        return s;
                    }}
                    acts.appendChild(mkAct('\u2b07', '#4CAF50', () => downloadDir(path)));
                    acts.appendChild(mkAct('\u270e', '#888', () => renameItem(path)));
                    acts.appendChild(mkAct('\u29c9', '#888', () => duplicateItem(path)));
                    acts.appendChild(mkAct('\u2715', '#c62828', () => deleteItem(path)));
                    row.appendChild(acts);

                    row.onclick = () => {{
                        sbCollapsedDirs[path] = !sbCollapsedDirs[path];
                        refreshFileTree();
                    }};
                    div.appendChild(row);

                    if (!collapsed) {{
                        const sub = document.createElement('div');
                        renderTree(child, sub, depth + 1);
                        div.appendChild(sub);
                    }}
                    container.appendChild(div);
                }});

                // Files, sorted
                const files = (node.files || []).sort((a, b) => a.path.localeCompare(b.path));
                files.forEach(f => {{
                    const fname = f.path.split('/').pop();
                    const row = document.createElement('div');
                    row.style.cssText = 'display:flex; align-items:center; padding:0.15rem 0.5rem; padding-left:' + (depth * 16 + 24) + 'px; cursor:pointer; color:#e0e0e0; white-space:nowrap;';
                    if (f.path === sbCurrentPath) row.style.background = '#2a3a2a';
                    row.onmouseover = () => {{ if (f.path !== sbCurrentPath) row.style.background = '#2a2a2a'; acts.style.visibility = 'visible'; }};
                    row.onmouseout = () => {{ if (f.path !== sbCurrentPath) row.style.background = ''; acts.style.visibility = 'hidden'; }};

                    const icon = document.createElement('span');
                    icon.style.cssText = 'width:16px; text-align:center; flex-shrink:0; color:#888; font-size:0.65rem;';
                    icon.textContent = f.is_binary ? '\u25a0' : '\u25a1';
                    row.appendChild(icon);

                    const label = document.createElement('span');
                    label.style.cssText = 'flex:1; overflow:hidden; text-overflow:ellipsis;';
                    label.textContent = fname;
                    row.appendChild(label);

                    const size = document.createElement('span');
                    size.style.cssText = 'color:#666; font-size:0.7rem; margin-left:0.5rem; flex-shrink:0;';
                    size.textContent = formatSize(f.size);
                    row.appendChild(size);

                    const acts = document.createElement('span');
                    acts.style.cssText = 'visibility:hidden; display:flex; gap:0.2rem; flex-shrink:0; margin-left:0.3rem;';
                    const renBtn = document.createElement('span');
                    renBtn.textContent = '\u270e';
                    renBtn.title = 'Rename';
                    renBtn.style.cssText = 'cursor:pointer; color:#888; font-size:0.7rem;';
                    renBtn.onclick = (e) => {{ e.stopPropagation(); renameItem(f.path); }};
                    acts.appendChild(renBtn);
                    const delBtn = document.createElement('span');
                    delBtn.textContent = '\u2715';
                    delBtn.title = 'Delete';
                    delBtn.style.cssText = 'cursor:pointer; color:#c62828; font-size:0.7rem;';
                    delBtn.onclick = (e) => {{ e.stopPropagation(); deleteItem(f.path); }};
                    acts.appendChild(delBtn);
                    row.appendChild(acts);

                    row.onclick = () => openFile(f.path);
                    container.appendChild(row);
                }});
            }}

            async function openFile(path) {{
                if (sbDirty && sbCurrentPath && !confirm('Discard unsaved changes to ' + sbCurrentPath + '?')) return;
                try {{
                    const resp = await fetch(basePath + '/snapshot/browse/file?workspace_id=' + encodeURIComponent(sbWorkspaceId) + '&path=' + encodeURIComponent(path));
                    const data = await resp.json();
                    if (data.detail) throw new Error(data.detail);

                    sbCurrentPath = path;
                    document.getElementById('sb-current-file').textContent = path;
                    document.getElementById('sb-editor-toolbar').style.display = 'flex';
                    document.getElementById('sb-welcome').style.display = 'none';

                    if (data.binary) {{
                        document.getElementById('sb-binary-msg').style.display = '';
                        document.getElementById('sb-codemirror-wrap').style.display = 'none';
                        document.getElementById('sb-save-btn').style.display = 'none';
                    }} else {{
                        document.getElementById('sb-binary-msg').style.display = 'none';
                        document.getElementById('sb-codemirror-wrap').style.display = '';
                        document.getElementById('sb-save-btn').style.display = '';
                        const mode = getModeForFile(path);
                        sbEditor.setOption('mode', mode);
                        sbEditor.setValue(data.content || '');
                        sbDirty = false;
                        setTimeout(() => sbEditor.refresh(), 10);
                    }}
                    refreshFileTree();
                }} catch(e) {{
                    alert('Error opening file: ' + e.message);
                }}
            }}

            async function saveCurrentFile() {{
                if (!sbCurrentPath || !sbWorkspaceId) return;
                try {{
                    const resp = await fetch(basePath + '/snapshot/browse/file', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ workspace_id: sbWorkspaceId, path: sbCurrentPath, content: sbEditor.getValue() }})
                    }});
                    const data = await resp.json();
                    if (data.detail) throw new Error(data.detail);
                    sbDirty = false;
                    const btn = document.getElementById('sb-save-btn');
                    const orig = btn.textContent;
                    btn.textContent = 'Saved!';
                    btn.style.background = '#2E7D32';
                    setTimeout(() => {{ btn.textContent = orig; btn.style.background = ''; }}, 1500);
                }} catch(e) {{
                    alert('Save failed: ' + e.message);
                }}
            }}

            async function renameItem(path) {{
                const oldName = path.split('/').pop();
                const newName = prompt('Rename "' + oldName + '" to:', oldName);
                if (!newName || newName === oldName) return;
                try {{
                    const resp = await fetch(basePath + '/snapshot/browse/rename', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ workspace_id: sbWorkspaceId, path: path, new_name: newName }})
                    }});
                    const data = await resp.json();
                    if (data.detail) throw new Error(data.detail);
                    if (sbCurrentPath === path) {{ sbCurrentPath = null; showWelcome(); }}
                    refreshFileTree();
                }} catch(e) {{
                    alert('Rename failed: ' + e.message);
                }}
            }}

            async function duplicateItem(path) {{
                const name = path.split('/').pop();
                const ext = name.includes('.') ? '.' + name.split('.').pop() : '';
                const base = ext ? name.slice(0, -ext.length) : name;
                const destName = prompt('Duplicate "' + name + '" as:', base + '-copy' + ext);
                if (!destName) return;
                try {{
                    const resp = await fetch(basePath + '/snapshot/browse/duplicate', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ workspace_id: sbWorkspaceId, path: path, dest_name: destName }})
                    }});
                    const data = await resp.json();
                    if (data.detail) throw new Error(data.detail);
                    refreshFileTree();
                }} catch(e) {{
                    alert('Duplicate failed: ' + e.message);
                }}
            }}

            async function deleteItem(path) {{
                if (!confirm('Delete "' + path.split('/').pop() + '"?')) return;
                try {{
                    const resp = await fetch(basePath + '/snapshot/browse/delete-file', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ workspace_id: sbWorkspaceId, path: path }})
                    }});
                    const data = await resp.json();
                    if (data.detail) throw new Error(data.detail);
                    if (sbCurrentPath === path) {{ sbCurrentPath = null; showWelcome(); }}
                    refreshFileTree();
                }} catch(e) {{
                    alert('Delete failed: ' + e.message);
                }}
            }}

            function handleDragOver(e) {{
                e.preventDefault();
                e.stopPropagation();
                e.currentTarget.style.borderColor = '#4CAF50';
                e.currentTarget.style.background = '#1a2a1a';
            }}

            async function handleFileDrop(e) {{
                e.preventDefault();
                e.stopPropagation();
                e.currentTarget.style.borderColor = '#333';
                e.currentTarget.style.background = 'transparent';
                if (!sbWorkspaceId) return;
                const files = e.dataTransfer.files;
                for (let i = 0; i < files.length; i++) {{
                    const file = files[i];
                    const formData = new FormData();
                    formData.append('workspace_id', sbWorkspaceId);
                    formData.append('path', '');
                    formData.append('file', file);
                    try {{
                        const resp = await fetch(basePath + '/snapshot/browse/upload', {{
                            method: 'POST',
                            body: formData
                        }});
                        const data = await resp.json();
                        if (data.detail) throw new Error(data.detail);
                    }} catch(err) {{
                        alert('Upload failed for ' + file.name + ': ' + err.message);
                    }}
                }}
                refreshFileTree();
            }}

            async function saveWorkspaceAsSnapshot() {{
                if (!sbWorkspaceId) return;
                const name = document.getElementById('sb-save-name').value.trim();
                try {{
                    const resp = await fetch(basePath + '/snapshot/browse/save', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ workspace_id: sbWorkspaceId, name: name }})
                    }});
                    const data = await resp.json();
                    if (data.detail) throw new Error(data.detail);
                    let msg = 'Snapshot created: ' + data.name + ' (' + formatSize(data.size) + ')';
                    if (data.synced) msg += ' — synced to host';
                    alert(msg);
                }} catch(e) {{
                    alert('Save failed: ' + e.message);
                }}
            }}

            function downloadCurrentFile() {{
                if (!sbCurrentPath || !sbWorkspaceId) return;
                const url = basePath + '/snapshot/browse/file/download?workspace_id=' + encodeURIComponent(sbWorkspaceId) + '&path=' + encodeURIComponent(sbCurrentPath);
                window.open(url, '_blank');
            }}

            function downloadDir(path) {{
                const url = basePath + '/snapshot/browse/file/download?workspace_id=' + encodeURIComponent(sbWorkspaceId) + '&path=' + encodeURIComponent(path);
                window.open(url, '_blank');
            }}

            window.addEventListener('beforeunload', (e) => {{
                if (sbWorkspaceId) {{
                    e.preventDefault();
                    e.returnValue = '';
                    try {{
                        navigator.sendBeacon(basePath + '/snapshot/browse/close?token=' + encodeURIComponent(new URLSearchParams(window.location.search).get('token') || ''),
                            new Blob([JSON.stringify({{workspace_id: sbWorkspaceId}})], {{type: 'application/json'}}));
                    }} catch(ex) {{}}
                }}
            }});

            // Store config path for editor links
            let configHostPath = '';

            // Parse JSON error to extract line/column
            function parseJsonError(errorMsg, jsonText) {{
                // Try to extract position from error message
                // Common formats: "at position 123", "at line 5 column 10", "Unexpected token X in JSON at position 456"
                let line = 1, col = 1, pos = -1;

                const posMatch = errorMsg.match(/position\s+(\d+)/i);
                if (posMatch) {{
                    pos = parseInt(posMatch[1]);
                    // Convert position to line/column
                    let currentPos = 0;
                    const lines = jsonText.split('\\n');
                    for (let i = 0; i < lines.length; i++) {{
                        if (currentPos + lines[i].length >= pos) {{
                            line = i + 1;
                            col = pos - currentPos + 1;
                            break;
                        }}
                        currentPos += lines[i].length + 1; // +1 for newline
                    }}
                }}

                const lineMatch = errorMsg.match(/line\s+(\d+)/i);
                if (lineMatch) line = parseInt(lineMatch[1]);

                const colMatch = errorMsg.match(/column\s+(\d+)/i);
                if (colMatch) col = parseInt(colMatch[1]);

                return {{ line, col, pos }};
            }}

            // Format JSON error with clickable link
            function formatJsonError(errorMsg, jsonText) {{
                const {{ line, col }} = parseJsonError(errorMsg, jsonText);
                let html = `<span style="color: #ef9a9a;">Invalid JSON: ${{errorMsg}}</span><br>`;
                if (configHostPath) {{
                    const vscodeUrl = `vscode://file/${{window.location.origin.includes('localhost') ? '/Users/elimaine/code/clawfactory/' : ''}}${{configHostPath}}:${{line}}:${{col}}`;
                    html += `<a href="${{vscodeUrl}}" style="color: #2196F3;">Open in VS Code at line ${{line}}</a>`;
                    html += ` | <a href="#" onclick="jumpToLine(${{line}}); return false;" style="color: #4CAF50;">Jump to line ${{line}}</a>`;
                }} else {{
                    html += `<a href="#" onclick="jumpToLine(${{line}}); return false;" style="color: #4CAF50;">Jump to line ${{line}}</a>`;
                }}
                return html;
            }}

            // Jump to line in CodeMirror editor
            function jumpToLine(lineNum) {{
                if (!configEditor) return;
                const line = lineNum - 1;
                configEditor.focus();
                configEditor.setCursor({{ line: line, ch: 0 }});
                configEditor.setSelection(
                    {{ line: line, ch: 0 }},
                    {{ line: line, ch: configEditor.getLine(line)?.length || 0 }}
                );
                // Scroll to center the line
                const coords = configEditor.charCoords({{ line: line, ch: 0 }}, 'local');
                configEditor.scrollTo(null, coords.top - configEditor.getScrollInfo().clientHeight / 2);
            }}

            // Config editor
            async function loadConfig() {{
                const result = document.getElementById('config-result');
                const ollamaDiv = document.getElementById('ollama-models');
                result.style.display = 'block';
                result.className = 'result';
                result.textContent = 'Loading...';
                try {{
                    const resp = await fetch(basePath + '/gateway/config');
                    const data = await resp.json();
                    if (!resp.ok || data.error || data.detail) {{
                        result.className = 'result error';
                        result.textContent = data.error || data.detail || 'Unknown error';
                        return;
                    }}
                    setEditorValue(JSON.stringify(data.config, null, 2));
                    configHostPath = data.config_path || '';

                    // Show Ollama models if available
                    if (data.ollama_models && data.ollama_models.length > 0) {{
                        // Store raw model data globally
                        window.ollamaModelsRaw = data.ollama_models;

                        renderOllamaModels();
                    }} else {{
                        ollamaDiv.innerHTML = '<p style="color: #888; font-size: 0.85rem;">No Ollama models detected. Is Ollama running?</p>';
                    }}

                    result.textContent = 'Config loaded. Validating...';
                    // Also validate the config
                    validateConfig();
                }} catch(e) {{
                    result.className = 'result error';
                    result.textContent = 'Error: ' + e.message;
                }}
            }}

            async function saveConfig() {{
                const result = document.getElementById('config-result');
                const editorValue = getEditorValue();

                // Validate JSON first
                let config;
                try {{
                    config = JSON.parse(editorValue);
                }} catch(e) {{
                    result.style.display = 'block';
                    result.className = 'result error';
                    result.innerHTML = formatJsonError(e.message, editorValue);
                    return;
                }}

                if (!confirm('This will stop the gateway, save the config, and restart. Continue?')) return;

                result.style.display = 'block';
                result.className = 'result';
                result.textContent = 'Saving config and restarting gateway...';

                try {{
                    const resp = await fetch(basePath + '/gateway/config', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ config }})
                    }});
                    const data = await resp.json();
                    if (!resp.ok || data.error || data.detail) {{
                        result.className = 'result error';
                        let errMsg = escapeHtml(data.error || data.detail || 'Unknown error');
                        if (data.validation_errors && data.validation_errors.length) {{
                            errMsg += '<br><ul style="margin: 0.3rem 0 0 1rem; padding: 0;">';
                            data.validation_errors.forEach(e => {{ errMsg += '<li>' + escapeHtml(e) + '</li>'; }});
                            errMsg += '</ul>';
                        }}
                        result.innerHTML = errMsg;
                    }} else {{
                        result.textContent = 'Config saved. Gateway restarting...';
                        // Check for backup after save
                        checkConfigBackup();
                    }}
                }} catch(e) {{
                    result.className = 'result error';
                    result.textContent = 'Error: ' + e.message;
                }}
            }}

            async function checkConfigBackup() {{
                try {{
                    const resp = await fetch(basePath + '/gateway/config/known-good');
                    const data = await resp.json();
                    const btn = document.getElementById('revert-config-btn');
                    if (btn) {{
                        if (data.has_backup) {{
                            const ts = data.timestamp ? new Date(data.timestamp).toLocaleString() : '';
                            btn.style.display = 'inline-block';
                            btn.title = 'Backup from: ' + ts;
                        }} else {{
                            btn.style.display = 'none';
                        }}
                    }}
                }} catch(e) {{
                    console.error('Error checking backup:', e);
                }}
            }}

            async function revertConfig() {{
                if (!confirm('Revert to the last known-good config? This will restart the gateway.')) return;

                const result = document.getElementById('config-result');
                result.style.display = 'block';
                result.className = 'result';
                result.textContent = 'Reverting config...';

                try {{
                    const resp = await fetch(basePath + '/gateway/config/revert', {{ method: 'POST' }});
                    const data = await resp.json();
                    if (!resp.ok || data.error || data.detail) {{
                        result.className = 'result error';
                        result.textContent = data.error || data.detail || 'Unknown error';
                    }} else {{
                        result.innerHTML = '<span style="color: #4CAF50;">Config reverted. Gateway restarting...</span>';
                        // Reload config into editor
                        setTimeout(() => loadConfig(), 2000);
                    }}
                }} catch(e) {{
                    result.className = 'result error';
                    result.textContent = 'Error: ' + e.message;
                }}
            }}

            function formatConfig() {{
                const result = document.getElementById('config-result');
                const editorValue = getEditorValue();
                try {{
                    const config = JSON.parse(editorValue);
                    setEditorValue(JSON.stringify(config, null, 2));
                    result.style.display = 'block';
                    result.className = 'result';
                    result.textContent = 'JSON formatted.';
                }} catch(e) {{
                    result.style.display = 'block';
                    result.className = 'result error';
                    result.innerHTML = formatJsonError(e.message, editorValue);
                }}
            }}

            async function validateConfig() {{
                const result = document.getElementById('config-result');
                result.style.display = 'block';
                result.className = 'result';
                result.textContent = 'Validating config...';

                const editorValue = getEditorValue();
                let config;
                try {{
                    config = JSON.parse(editorValue);
                }} catch(e) {{
                    result.className = 'result error';
                    result.innerHTML = formatJsonError(e.message, editorValue);
                    return;
                }}

                try {{
                    const resp = await fetch(basePath + '/gateway/config/validate', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ config }})
                    }});
                    const data = await resp.json();

                    if (data.error) {{
                        result.className = 'result error';
                        result.textContent = data.error;
                        return;
                    }}

                    if (data.valid) {{
                        result.innerHTML = '<span style="color: #4CAF50;">Config is valid</span>';
                    }} else {{
                        let html = '<span style="color: #ef9a9a; font-weight: bold;">Config has errors:</span><br>';
                        if (data.issues && data.issues.length > 0) {{
                            data.issues.forEach(issue => {{
                                const color = issue.severity === 'error' ? '#ef9a9a' : '#ffcc80';
                                html += `<div style="margin: 0.3rem 0; padding: 0.3rem; background: #333; border-radius: 3px;">`;
                                html += `<span style="color: ${{color}};">${{issue.message}}</span>`;
                                if (issue.key) {{
                                    html += ` <a href="#" onclick="searchInEditor('${{issue.key}}'); return false;" style="color: #2196F3; font-size: 0.85rem;">Find in editor</a>`;
                                }}
                                html += `</div>`;
                            }});
                        }}
                        if (data.raw) {{
                            html += `<pre style="margin-top: 0.5rem; font-size: 0.75rem; color: #888; white-space: pre-wrap;">${{data.raw}}</pre>`;
                        }}
                        result.className = 'result error';
                        result.innerHTML = html;
                    }}
                }} catch(e) {{
                    result.className = 'result error';
                    result.textContent = 'Error: ' + e.message;
                }}
            }}

            function searchInEditor(text) {{
                if (!configEditor) return;
                const cursor = configEditor.getSearchCursor(text);
                if (cursor.findNext()) {{
                    configEditor.setSelection(cursor.from(), cursor.to());
                    configEditor.scrollIntoView({{ from: cursor.from(), to: cursor.to() }}, 100);
                    configEditor.focus();
                }}
            }}

            // Calculate safe context window based on RAM and model size
            function calcSafeContext(paramBillions, maxContext, availableRamGb) {{
                // Model weights (Q4 quantized): ~0.5-0.6 GB per billion params
                const modelRam = paramBillions * 0.6;
                // System overhead
                const systemRam = 6;
                // Available for KV cache
                const kvRam = availableRamGb - modelRam - systemRam;

                if (kvRam <= 0) return 4096; // Minimum

                // KV cache estimates (fp16, typical GQA models):
                // - 7B model: ~0.5GB per 8k context
                // - 14B model: ~1GB per 8k context
                // - 32B model: ~2GB per 8k context (GQA helps)
                // - 70B model: ~4GB per 8k context
                // Formula: GB per 8k ≈ paramBillions * 0.06
                const gbPer8k = paramBillions * 0.06;
                const maxContextFromRam = Math.floor((kvRam / gbPer8k) * 8192);

                // Cap at model's actual max and round to nice number
                let safeContext = Math.min(maxContextFromRam, maxContext);
                // Round down to nearest 4k
                safeContext = Math.floor(safeContext / 4096) * 4096;
                // Minimum 4k, max what model supports
                return Math.max(4096, Math.min(safeContext, maxContext));
            }}

            function renderOllamaModels() {{
                const ollamaDiv = document.getElementById('ollama-models');
                const availableRam = parseInt(document.getElementById('available-ram').value) || 64;
                const editorValue = getEditorValue();

                if (!window.ollamaModelsRaw || window.ollamaModelsRaw.length === 0) {{
                    ollamaDiv.innerHTML = '<p style="color: #888; font-size: 0.85rem;">No Ollama models detected.</p>';
                    return;
                }}

                // Get already configured model IDs
                let configuredIds = new Set();
                try {{
                    const config = JSON.parse(editorValue);
                    const models = config?.models?.providers?.ollama?.models || [];
                    models.forEach(m => configuredIds.add(m.id));
                }} catch(e) {{
                    // Ignore parse errors
                }}

                // Build config entries with RAM-adjusted context
                window.ollamaModels = {{}};
                window.ollamaModelsRaw.forEach(m => {{
                    const safeCtx = calcSafeContext(m.param_billions || 7, m.context_window || 4096, availableRam);
                    window.ollamaModels[m.id] = {{
                        id: m.id,
                        name: m.friendly_name,
                        reasoning: m.reasoning,
                        input: ["text"],
                        cost: {{ input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }},
                        contextWindow: safeCtx,
                        maxTokens: Math.min(Math.floor(safeCtx / 4), 8192)
                    }};
                }});

                // Filter out models already in config
                const availableModels = window.ollamaModelsRaw.filter(m => !configuredIds.has(m.id));

                if (availableModels.length === 0) {{
                    ollamaDiv.innerHTML = '<p style="color: #888; font-size: 0.85rem;">All Ollama models already in config.</p>';
                    return;
                }}

                let html = '<div style="background: #252525; padding: 0.5rem; border-radius: 4px; margin-bottom: 0.5rem;">';
                html += '<strong style="color: #4CAF50;">Ollama Models:</strong> ';
                html += '<span style="color: #888; font-size: 0.85rem;">(click to add to config, context adjusted for ' + availableRam + 'GB RAM)</span><br>';
                availableModels.forEach(m => {{
                    const safeCtx = window.ollamaModels[m.id].contextWindow;
                    const maxCtx = m.context_window || 4096;
                    const reasoningBadge = m.reasoning ? ' <span style="color: #ff9800; font-size: 0.7rem;">⚡reasoning</span>' : '';
                    const ctxColor = safeCtx < maxCtx ? '#ff9800' : '#4CAF50';
                    const ctxStr = ` <span style="color: ${{ctxColor}}; font-size: 0.7rem;">${{(safeCtx/1024).toFixed(0)}}k</span>`;
                    const paramStr = m.parameters ? ` <span style="color: #666; font-size: 0.7rem;">${{m.parameters}}</span>` : '';
                    html += `<code style="cursor: pointer; background: #333; padding: 0.2rem 0.4rem; margin: 0.2rem; display: inline-block; border-radius: 3px;" onclick="addOllamaModel('${{m.id}}')">${{m.id}}${{paramStr}}${{ctxStr}}${{reasoningBadge}}</code>`;
                }});
                html += '</div>';
                ollamaDiv.innerHTML = html;
            }}

            // Re-render when RAM changes
            document.getElementById('available-ram').addEventListener('change', renderOllamaModels);

            // Note: Cursor position and live JSON validation are handled by CodeMirror events (see initialization above)

            function addOllamaModel(modelId) {{
                const result = document.getElementById('config-result');
                const editorValue = getEditorValue();

                if (!window.ollamaModels || !window.ollamaModels[modelId]) {{
                    result.style.display = 'block';
                    result.className = 'result error';
                    result.textContent = 'Model config not found. Reload config first.';
                    return;
                }}

                let config;
                try {{
                    config = JSON.parse(editorValue);
                }} catch(e) {{
                    result.style.display = 'block';
                    result.className = 'result error';
                    result.textContent = 'Invalid JSON in editor. Load config first.';
                    return;
                }}

                // Ensure path exists: models.providers.ollama.models
                if (!config.models) config.models = {{}};
                if (!config.models.providers) config.models.providers = {{}};
                if (!config.models.providers.ollama) {{
                    config.models.providers.ollama = {{
                        baseUrl: "{"http://host.lima.internal:11434/v1" if IS_LIMA_MODE else "http://host.docker.internal:11434/v1"}",
                        apiKey: "ollama-local",
                        models: []
                    }};
                }}
                if (!config.models.providers.ollama.models) {{
                    config.models.providers.ollama.models = [];
                }}

                // Check if already exists
                const existing = config.models.providers.ollama.models.find(m => m.id === modelId);
                if (existing) {{
                    result.style.display = 'block';
                    result.className = 'result';
                    result.textContent = modelId + ' already in config.';
                    return;
                }}

                // Add the model
                config.models.providers.ollama.models.push(window.ollamaModels[modelId]);
                setEditorValue(JSON.stringify(config, null, 2));

                result.style.display = 'block';
                result.className = 'result';
                result.textContent = 'Added ' + modelId + ' to config. Click Save & Restart to apply.';

                // Re-render to remove the button
                renderOllamaModels();
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
                    if (!resp.ok || data.error || data.detail) {{
                        list.innerHTML = `<p class="error" style="color: #ef9a9a;">${{data.error || data.detail || 'Unknown error'}}</p>`;
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

            // Channel pairing (new unified view)
            async function fetchChannelPairing(channel) {{
                const pendingDiv = document.getElementById(channel + '-pending');
                const statusSpan = document.getElementById(channel + '-status');

                if (pendingDiv) pendingDiv.innerHTML = '<span style="color: #888;">Loading...</span>';

                try {{
                    const resp = await fetch(basePath + '/gateway/pairing/' + channel);
                    const data = await resp.json();

                    if (data.error) {{
                        if (pendingDiv) pendingDiv.innerHTML = `<span style="color: #ef9a9a;">${{data.error}}</span>`;
                        if (statusSpan) {{
                            statusSpan.textContent = 'error';
                            statusSpan.className = 'channel-status error';
                        }}
                        return;
                    }}

                    const pending = data.pending || [];
                    if (statusSpan) {{
                        if (pending.length > 0) {{
                            statusSpan.textContent = pending.length + ' pending';
                            statusSpan.className = 'channel-status pending';
                        }} else {{
                            statusSpan.textContent = 'ready';
                            statusSpan.className = 'channel-status connected';
                        }}
                    }}

                    if (pendingDiv) {{
                        if (pending.length > 0) {{
                            let html = '';
                            pending.forEach(p => {{
                                html += `<div style="background: #252525; padding: 0.5rem; border-radius: 3px; margin-bottom: 0.5rem;">
                                    <strong style="color: #ff9800;">${{p.code}}</strong>
                                    <span style="color: #888; font-size: 0.8rem; margin-left: 0.5rem;">from ${{p.senderId || p.userId || 'unknown'}}</span>
                                </div>`;
                            }});
                            pendingDiv.innerHTML = html;
                        }} else {{
                            pendingDiv.innerHTML = '<span style="color: #4CAF50;">No pending requests</span>';
                        }}
                    }}
                }} catch(e) {{
                    if (pendingDiv) pendingDiv.innerHTML = `<span style="color: #ef9a9a;">Error: ${{e.message}}</span>`;
                    if (statusSpan) {{
                        statusSpan.textContent = 'error';
                        statusSpan.className = 'channel-status error';
                    }}
                }}
            }}

            async function refreshAllChannels() {{
                const channels = ['discord', 'telegram', 'slack'];
                await Promise.all(channels.map(ch => fetchChannelPairing(ch)));
            }}

            // Legacy function for backwards compatibility
            async function fetchPairing(channel) {{
                return fetchChannelPairing(channel);
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
                    if (!resp.ok || data.error || data.detail) {{
                        result.className = 'result error';
                        result.textContent = data.error || data.detail || 'Unknown error';
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
                    if (!resp.ok || data.error || data.detail) {{
                        result.className = 'result error';
                        result.textContent = data.error || data.detail || 'Unknown error';
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

            async function restartGateway() {{
                const result = document.getElementById('promote-result');
                result.style.display = 'block';
                result.className = 'result';
                result.textContent = 'Restarting gateway...';
                try {{
                    const resp = await fetch(basePath + '/gateway/restart', {{ method: 'POST' }});
                    const data = await resp.json();
                    result.textContent = data.status || JSON.stringify(data);
                    setTimeout(fetchHealth, 3000);
                }} catch(e) {{
                    result.className = 'result error';
                    result.textContent = 'Error: ' + e.message;
                }}
            }}

            async function rebuildGateway() {{
                const result = document.getElementById('promote-result');
                result.style.display = 'block';
                result.className = 'result';
                result.textContent = 'Rebuilding gateway...';
                try {{
                    const resp = await fetch(basePath + '/gateway/rebuild', {{ method: 'POST' }});
                    const data = await resp.json();
                    result.textContent = data.status || JSON.stringify(data);
                    setTimeout(fetchHealth, 5000);
                }} catch(e) {{
                    result.className = 'result error';
                    result.textContent = 'Error: ' + e.message;
                }}
            }}

            async function pullUpstream() {{
                const result = document.getElementById('promote-result');
                result.style.display = 'block';
                result.className = 'result';
                result.textContent = 'Pulling latest OpenClaw...';
                try {{
                    const resp = await fetch(basePath + '/pull-upstream', {{ method: 'POST' }});
                    const data = await resp.json();
                    result.textContent = data.output || data.status || JSON.stringify(data);
                }} catch(e) {{
                    result.className = 'result error';
                    result.textContent = 'Error: ' + e.message;
                }}
            }}

            // Load data on page load
            fetchHealth();
            checkConfigBackup();

            // Auto-polling intervals (in ms)
            const POLL_INTERVAL_FAST = 10000;   // 10s for status
            const POLL_INTERVAL_SLOW = 30000;   // 30s for data

            // Gateway status - poll frequently
            setInterval(() => {{
                fetchHealth();
            }}, POLL_INTERVAL_FAST);

            console.log('ClawFactory UI loaded. Auto-polling enabled.');
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
    gateway_status = get_gateway_status()

    return {
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
# Traffic Logs & Scrub Rules
# ============================================================

@app.get("/traffic")
@app.get("/controller/traffic")
async def get_traffic(
    limit: int = 50,
    offset: int = 0,
    provider: Optional[str] = None,
    status: Optional[int] = None,
    search: Optional[str] = None,
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """List traffic log entries (paginated, filterable)."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")
    # Validate parameters
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    if provider and provider not in ("anthropic", "openai", "gemini"):
        raise HTTPException(status_code=400, detail="Invalid provider")
    if status is not None and not (100 <= status <= 599):
        raise HTTPException(status_code=400, detail="Invalid status code")
    if search and len(search) > 500:
        raise HTTPException(status_code=400, detail="Search query too long")
    entries = traffic_log.read_traffic_log(limit=limit, offset=offset, provider=provider, status=status, search=search)
    return {"entries": entries}


@app.get("/traffic/stats")
@app.get("/controller/traffic/stats")
async def get_traffic_stats(
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Aggregate traffic stats."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return traffic_log.get_traffic_stats()


@app.get("/traffic/providers")
@app.get("/controller/traffic/providers")
async def get_traffic_providers(
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """List known providers from gateway config and traffic logs."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")
    providers = set()
    # From gateway config
    try:
        with open(GATEWAY_CONFIG_PATH) as f:
            config = json.load(f)
        for name in config.get("models", {}).get("providers", {}):
            providers.add(name)
    except Exception:
        pass
    # From traffic log stats
    try:
        stats = traffic_log.get_traffic_stats()
        for name in stats.get("by_provider", {}):
            providers.add(name)
    except Exception:
        pass
    return {"providers": sorted(providers)}


@app.get("/traffic/inbound")
@app.get("/controller/traffic/inbound")
async def get_inbound_traffic(
    limit: int = 50,
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Nginx access log entries (inbound traffic)."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")
    limit = max(1, min(limit, 500))
    return {"entries": traffic_log.read_nginx_log(limit=limit)}


@app.get("/traffic/{request_id}")
@app.get("/controller/traffic/{request_id}")
async def get_traffic_detail(
    request_id: str,
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Single traffic entry detail with full request/response."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")
    entry = traffic_log.get_llm_session(request_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Traffic entry not found")
    return entry


@app.get("/scrub-rules")
@app.get("/controller/scrub-rules")
async def get_scrub_rules(
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Get current scrub rules."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return {"rules": scrub.load_rules()}


@app.post("/scrub-rules")
@app.post("/controller/scrub-rules")
async def save_scrub_rules(
    request: Request,
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Save scrub rules."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")
    data = await request.json()
    rules = data.get("rules", [])
    if not isinstance(rules, list):
        raise HTTPException(status_code=400, detail="rules must be a list")
    if len(rules) > 100:
        raise HTTPException(status_code=400, detail="Too many rules (max 100)")
    # Validate each rule
    for rule in rules:
        if not isinstance(rule, dict):
            raise HTTPException(status_code=400, detail="Each rule must be an object")
        if not rule.get("id") or not isinstance(rule.get("id"), str):
            raise HTTPException(status_code=400, detail="Each rule must have a string 'id'")
        if len(rule.get("id", "")) > 64:
            raise HTTPException(status_code=400, detail="Rule ID too long (max 64)")
        if rule.get("pattern") and len(rule["pattern"]) > 1000:
            raise HTTPException(status_code=400, detail="Pattern too long (max 1000)")
    scrub.save_rules(rules)
    audit_log("scrub_rules_updated", {"rule_count": len(rules)})
    return {"status": "saved", "rule_count": len(rules)}


@app.post("/scrub-rules/test")
@app.post("/controller/scrub-rules/test")
async def test_scrub_rule(
    request: Request,
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Test a regex pattern against sample text."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")
    data = await request.json()
    pattern = data.get("pattern", "")
    replacement = data.get("replacement", "***REDACTED***")
    sample = data.get("sample", "")
    if not isinstance(pattern, str) or not isinstance(replacement, str) or not isinstance(sample, str):
        raise HTTPException(status_code=400, detail="pattern, replacement, and sample must be strings")
    if len(pattern) > 1000:
        raise HTTPException(status_code=400, detail="Pattern too long (max 1000)")
    if len(sample) > 10000:
        raise HTTPException(status_code=400, detail="Sample too long (max 10000)")
    if len(replacement) > 500:
        raise HTTPException(status_code=400, detail="Replacement too long (max 500)")
    return scrub.test_pattern(pattern, replacement, sample)


CAPTURE_STATE_FILE = Path(os.environ.get("CAPTURE_STATE_FILE", "/srv/audit/capture_enabled"))
LLM_PROXY_URL = os.environ.get("LLM_PROXY_URL", "http://llm-proxy:9090")
ENCRYPTED_TRAFFIC_LOG = Path(os.environ.get("ENCRYPTED_TRAFFIC_LOG", "/srv/clawfactory/audit/traffic.enc.jsonl"))
FERNET_KEY_FILE = Path(os.environ.get("FERNET_KEY_FILE", "/srv/clawfactory/audit/traffic.fernet.key"))
FERNET_KEY_AGE = Path(os.environ.get("FERNET_KEY_AGE", "/srv/clawfactory/audit/traffic.fernet.key.age"))
MITM_CA_DIR = Path(os.environ.get("MITM_CA_DIR", "/srv/clawfactory/mitm-ca"))


def _ensure_fernet_key() -> bytes | None:
    """Generate Fernet key, encrypt with age, return raw key bytes."""
    from cryptography.fernet import Fernet as _Fernet

    # If age-encrypted key already exists, we can regenerate plaintext from it
    if FERNET_KEY_AGE.exists() and AGE_KEY.exists():
        try:
            result = subprocess.run(
                ["age", "--decrypt", "-i", str(AGE_KEY), str(FERNET_KEY_AGE)],
                capture_output=True,
            )
            if result.returncode == 0:
                key = result.stdout.strip()
                FERNET_KEY_FILE.write_bytes(key)
                FERNET_KEY_FILE.chmod(0o600)
                return key
        except Exception:
            pass

    # Generate new key
    key = _Fernet.generate_key()
    FERNET_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    FERNET_KEY_FILE.write_bytes(key)
    FERNET_KEY_FILE.chmod(0o600)

    # Encrypt with age public key
    if AGE_KEY.exists():
        pub_path = AGE_KEY.with_suffix(".pub")
        if pub_path.exists():
            try:
                pub_key = pub_path.read_text().strip().split("\n")[-1].strip()
                result = subprocess.run(
                    ["age", "-r", pub_key, "-o", str(FERNET_KEY_AGE)],
                    input=key,
                    capture_output=True,
                )
                if result.returncode == 0:
                    FERNET_KEY_AGE.chmod(0o600)
            except Exception:
                pass

    return key


def _decrypt_fernet_key() -> bytes | None:
    """Decrypt the Fernet key using the age private key."""
    if not FERNET_KEY_AGE.exists() or not AGE_KEY.exists():
        return None
    try:
        result = subprocess.run(
            ["age", "--decrypt", "-i", str(AGE_KEY), str(FERNET_KEY_AGE)],
            capture_output=True,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _mitm_iptables(action: str):
    """Add or remove iptables REDIRECT rules for MITM capture.

    action: '-A' to add, '-D' to delete
    """
    svc_user = f"openclaw-{INSTANCE_NAME}"
    for dport in ("443", "80"):
        # Always delete first to prevent duplicate rules
        subprocess.run(
            [
                "iptables", "-t", "nat", "-D", "OUTPUT",
                "-m", "owner", "--uid-owner", svc_user,
                "-p", "tcp", "--dport", dport,
                "-j", "REDIRECT", "--to-port", "8888",
            ],
            capture_output=True,
        )
        if action == "-A":
            subprocess.run(
                [
                    "iptables", "-t", "nat", "-A", "OUTPUT",
                    "-m", "owner", "--uid-owner", svc_user,
                    "-p", "tcp", "--dport", dport,
                    "-j", "REDIRECT", "--to-port", "8888",
                ],
                capture_output=True,
            )


def _install_mitm_ca():
    """Install mitmproxy CA into system trust store if available."""
    ca_cert = MITM_CA_DIR / "mitmproxy-ca-cert.pem"
    if ca_cert.exists():
        try:
            subprocess.run(
                ["cp", str(ca_cert), "/usr/local/share/ca-certificates/mitmproxy-ca.crt"],
                capture_output=True,
            )
            subprocess.run(["update-ca-certificates"], capture_output=True)
        except Exception:
            pass


@app.get("/capture")
@app.get("/controller/capture")
async def get_capture(
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Get capture enabled state with MITM status."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")
    enabled = False
    if CAPTURE_STATE_FILE.exists():
        try:
            enabled = CAPTURE_STATE_FILE.read_text().strip() == "1"
        except Exception:
            pass

    # Count encrypted entries without decrypting
    entry_count = traffic_log.count_encrypted_entries(ENCRYPTED_TRAFFIC_LOG)

    # Check mitmproxy service status
    mitm_active = False
    if IS_LIMA_MODE:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", "clawfactory-mitm"],
                capture_output=True, text=True,
            )
            mitm_active = result.stdout.strip() == "active"
        except Exception:
            pass

    return {
        "enabled": enabled,
        "mitm_active": mitm_active,
        "entry_count": entry_count,
    }


@app.post("/capture")
@app.post("/controller/capture")
async def set_capture(
    request: Request,
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Toggle MITM capture on/off."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")
    data = await request.json()
    enabled = bool(data.get("enabled", True))

    CAPTURE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    if enabled:
        # Ensure snapshot key exists (needed for age encryption of Fernet key)
        ensure_snapshot_key()

        # Generate or recover Fernet key
        key = _ensure_fernet_key()
        if not key:
            raise HTTPException(status_code=500, detail="Failed to generate encryption key")

        # Start mitmproxy service
        if IS_LIMA_MODE:
            subprocess.run(
                ["systemctl", "start", "clawfactory-mitm"],
                capture_output=True,
            )
            # Wait briefly for CA to be generated, then install it
            import asyncio
            await asyncio.sleep(2)
            _install_mitm_ca()

        # Add iptables redirect rules
        _mitm_iptables("-A")

        # Write state
        CAPTURE_STATE_FILE.write_text("1")

        # Delete plaintext Fernet key (age-encrypted copy persists)
        if FERNET_KEY_AGE.exists():
            FERNET_KEY_FILE.unlink(missing_ok=True)

    else:
        # Remove iptables redirect rules
        _mitm_iptables("-D")

        # Stop mitmproxy service
        if IS_LIMA_MODE:
            subprocess.run(
                ["systemctl", "stop", "clawfactory-mitm"],
                capture_output=True,
            )

        # Write state
        CAPTURE_STATE_FILE.write_text("0")

        # Clean up plaintext Fernet key
        FERNET_KEY_FILE.unlink(missing_ok=True)

    audit_log("capture_toggled", {"enabled": enabled, "mitm": True})
    return {"enabled": enabled}


@app.get("/traffic/decrypt")
@app.get("/controller/traffic/decrypt")
async def decrypt_traffic(
    limit: int = 50,
    offset: int = 0,
    provider: Optional[str] = None,
    status: Optional[int] = None,
    search: Optional[str] = None,
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Decrypt and return encrypted traffic log entries. Never writes plaintext to disk."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    fernet_key = _decrypt_fernet_key()
    if not fernet_key:
        raise HTTPException(status_code=404, detail="No encryption key found. Has capture been enabled?")

    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    entries = traffic_log.read_encrypted_traffic_log(
        fernet_key=fernet_key,
        limit=limit,
        offset=offset,
        provider=provider,
        status=status,
        search=search,
        log_path=ENCRYPTED_TRAFFIC_LOG,
    )
    return {"entries": entries}


@app.get("/traffic/decrypt/stats")
@app.get("/controller/traffic/decrypt/stats")
async def decrypt_traffic_stats(
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Aggregate stats from encrypted traffic log."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    fernet_key = _decrypt_fernet_key()
    if not fernet_key:
        raise HTTPException(status_code=404, detail="No encryption key found")

    return traffic_log.get_encrypted_traffic_stats(fernet_key, ENCRYPTED_TRAFFIC_LOG)


@app.get("/traffic/decrypt/{request_id}")
@app.get("/controller/traffic/decrypt/{request_id}")
async def decrypt_traffic_detail(
    request_id: str,
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Single decrypted traffic entry detail."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    fernet_key = _decrypt_fernet_key()
    if not fernet_key:
        raise HTTPException(status_code=404, detail="No encryption key found")

    entry = traffic_log.get_encrypted_entry(fernet_key, request_id, ENCRYPTED_TRAFFIC_LOG)
    if not entry:
        raise HTTPException(status_code=404, detail="Traffic entry not found")
    return entry


@app.post("/traffic/delete")
@app.post("/controller/traffic/delete")
async def delete_traffic_logs(
    request: Request,
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Delete encrypted traffic logs and optionally the encryption key."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    data = await request.json()
    delete_key = bool(data.get("delete_key", False))

    deleted_log = False
    deleted_key = False

    if ENCRYPTED_TRAFFIC_LOG.exists():
        ENCRYPTED_TRAFFIC_LOG.unlink()
        deleted_log = True

    if delete_key:
        FERNET_KEY_FILE.unlink(missing_ok=True)
        FERNET_KEY_AGE.unlink(missing_ok=True)
        deleted_key = True

    audit_log("traffic_logs_deleted", {
        "deleted_log": deleted_log,
        "deleted_key": deleted_key,
    })

    return {"deleted_log": deleted_log, "deleted_key": deleted_key}


# ============================================================
# Encrypted Snapshots
# ============================================================

def ensure_snapshot_key() -> bool:
    """Ensure snapshot encryption key exists, generate if missing."""
    if AGE_KEY.exists():
        return True

    # Try to generate the key
    try:
        AGE_KEY.parent.mkdir(parents=True, exist_ok=True)
        pub_path = AGE_KEY.with_suffix(".pub")

        # Generate key using age-keygen
        with open(pub_path, "w") as pub_file:
            result = subprocess.run(
                ["age-keygen", "-o", str(AGE_KEY)],
                capture_output=True,
                text=True,
                stderr=pub_file
            )

        if result.returncode == 0 and AGE_KEY.exists():
            # Set permissions
            AGE_KEY.chmod(0o600)
            pub_path.chmod(0o644)
            audit_log("snapshot_key_generated", {"path": str(AGE_KEY)})
            return True
    except Exception as e:
        audit_log("snapshot_key_generation_failed", {"error": str(e)})

    return False


def sanitize_snapshot_name(name: str) -> str:
    """Sanitize a snapshot name to alphanumeric, hyphens, underscores only."""
    import re
    # Replace spaces and non-allowed chars with hyphens
    sanitized = re.sub(r'[^a-zA-Z0-9_-]', '-', name.strip())
    # Collapse multiple hyphens
    sanitized = re.sub(r'-+', '-', sanitized).strip('-')
    # Max 50 chars
    return sanitized[:50]


def create_snapshot(name: str = "") -> dict:
    """Create an encrypted snapshot of bot state."""
    if not ensure_snapshot_key():
        return {"error": "No encryption key found and failed to generate one"}

    # Get public key from private key file
    pubkey = None
    with open(AGE_KEY) as f:
        for line in f:
            if "public key:" in line:
                pubkey = line.split(": ")[1].strip()
                break

    if not pubkey:
        return {"error": "Could not read public key from key file"}

    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    if name:
        sanitized = sanitize_snapshot_name(name)
        snapshot_name = f"{sanitized}--{timestamp}.tar.age"
    else:
        snapshot_name = f"snapshot--{timestamp}.tar.age"
    snapshot_path = SNAPSHOTS_DIR / snapshot_name

    # Create tarball of state (excluding installed packages and session logs)
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        # Create tar
        result = subprocess.run(
            [
                "tar", "-C", str(OPENCLAW_HOME), "-cf", tmp_path,
                "--exclude=*.tmp*",
                "--exclude=agents/*/sessions/*.jsonl",
                "--exclude=installed",
                "--exclude=installed/*",
                "--exclude=workspace/*/.git",
                "--exclude=sandboxes",
                "--exclude=subagents",
                "--exclude=media",
                "."
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return {"error": f"Failed to create tarball: {result.stderr}"}

        # Encrypt with age
        result = subprocess.run(
            ["age", "-r", pubkey, "-o", str(snapshot_path), tmp_path],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return {"error": f"Failed to encrypt: {result.stderr}"}

        # Update latest symlink
        latest_link = SNAPSHOTS_DIR / "latest.tar.age"
        if latest_link.is_symlink():
            latest_link.unlink()
        latest_link.symlink_to(snapshot_name)

        # Get size
        size = snapshot_path.stat().st_size

        audit_log("snapshot_created", {"name": snapshot_name, "size": size})

        return {
            "status": "created",
            "name": snapshot_name,
            "size": size,
            "path": str(snapshot_path),
        }
    finally:
        # Clean up temp file
        Path(tmp_path).unlink(missing_ok=True)


def list_snapshots() -> list:
    """List available snapshots."""
    if not SNAPSHOTS_DIR.exists():
        return []

    snapshots = []
    latest_target = None

    latest_link = SNAPSHOTS_DIR / "latest.tar.age"
    if latest_link.is_symlink():
        latest_target = latest_link.resolve().name

    for f in sorted(SNAPSHOTS_DIR.glob("*.tar.age"), reverse=True):
        if f.name == "latest.tar.age":
            continue
        # Parse name--timestamp.tar.age format
        stem = f.name.replace(".tar.age", "")
        if "--" in stem:
            label, timestamp = stem.rsplit("--", 1)
        else:
            # Legacy format: snapshot-timestamp.tar.age
            label = "snapshot"
            timestamp = stem.replace("snapshot-", "")
        snapshots.append({
            "name": f.name,
            "label": label,
            "size": f.stat().st_size,
            "latest": f.name == latest_target,
            "created": timestamp,
        })

    return snapshots


# ============================================================
# Snapshot Workspace Browser
# ============================================================

import re as _re
import uuid as _uuid
import time as _time

_snapshot_workspaces: dict[str, dict] = {}  # {uuid: {path, snapshot_name, created_at}}
SNAPSHOT_WORKSPACE_TIMEOUT = 3600  # 1hr auto-cleanup


def _validate_workspace_path(workspace_id: str, file_path: str) -> Path:
    """Resolve path within workspace, raise HTTPException on traversal."""
    if workspace_id not in _snapshot_workspaces:
        raise HTTPException(status_code=404, detail="Workspace not found")

    workspace_root = Path(_snapshot_workspaces[workspace_id]["path"])

    # Reject null bytes, control characters
    if '\x00' in file_path or any(ord(c) < 32 for c in file_path):
        raise HTTPException(status_code=400, detail="Invalid path characters")

    # Strip leading slash, reject ..
    file_path = file_path.lstrip("/")
    if ".." in file_path.split("/"):
        raise HTTPException(status_code=400, detail="Path traversal not allowed")

    # Whitelist characters
    if not _re.match(r'^[a-zA-Z0-9._\-/]+$', file_path):
        raise HTTPException(status_code=400, detail="Path contains invalid characters")

    resolved = (workspace_root / file_path).resolve()
    if not str(resolved).startswith(str(workspace_root.resolve())):
        raise HTTPException(status_code=400, detail="Path traversal not allowed")

    return resolved


def cleanup_stale_workspaces():
    """Remove workspaces older than timeout."""
    now = _time.time()
    stale = [wid for wid, w in _snapshot_workspaces.items()
             if now - w["created_at"] > SNAPSHOT_WORKSPACE_TIMEOUT]
    for wid in stale:
        workspace_path = Path(_snapshot_workspaces[wid]["path"])
        if workspace_path.exists():
            shutil.rmtree(str(workspace_path), ignore_errors=True)
        del _snapshot_workspaces[wid]
        audit_log("snapshot_workspace_expired", {"workspace_id": wid})


def open_snapshot_workspace(snapshot_name: str) -> dict:
    """Decrypt+extract snapshot to a temp workspace."""
    cleanup_stale_workspaces()

    if not AGE_KEY.exists():
        raise HTTPException(status_code=500, detail="No decryption key found")

    # Resolve snapshot path
    if snapshot_name == "latest":
        snapshot_path = SNAPSHOTS_DIR / "latest.tar.age"
        if snapshot_path.is_symlink():
            snapshot_path = snapshot_path.resolve()
    else:
        snapshot_path = SNAPSHOTS_DIR / snapshot_name
        if not snapshot_path.exists():
            snapshot_path = SNAPSHOTS_DIR / f"{snapshot_name}.tar.age"

    if not snapshot_path.exists():
        raise HTTPException(status_code=404, detail=f"Snapshot not found: {snapshot_name}")

    workspace_id = str(_uuid.uuid4())
    workspace_path = Path(f"/tmp/cf-snapshot-edit-{workspace_id}")
    workspace_path.mkdir(mode=0o700, parents=True)

    try:
        result = subprocess.run(
            f"age -d -i {AGE_KEY} {snapshot_path} | tar -C {workspace_path} -xf -",
            shell=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            shutil.rmtree(str(workspace_path), ignore_errors=True)
            raise HTTPException(status_code=500, detail=f"Decrypt/extract failed: {result.stderr}")
    except subprocess.TimeoutExpired:
        shutil.rmtree(str(workspace_path), ignore_errors=True)
        raise HTTPException(status_code=500, detail="Decrypt/extract timed out")

    _snapshot_workspaces[workspace_id] = {
        "path": str(workspace_path),
        "snapshot_name": snapshot_name,
        "created_at": _time.time(),
    }
    audit_log("snapshot_workspace_opened", {"workspace_id": workspace_id, "snapshot": snapshot_name})
    return {"workspace_id": workspace_id, "snapshot_name": snapshot_name}


def close_snapshot_workspace(workspace_id: str) -> dict:
    """Clean up a workspace."""
    if workspace_id not in _snapshot_workspaces:
        raise HTTPException(status_code=404, detail="Workspace not found")

    workspace_path = Path(_snapshot_workspaces[workspace_id]["path"])
    if workspace_path.exists():
        shutil.rmtree(str(workspace_path), ignore_errors=True)
    del _snapshot_workspaces[workspace_id]
    audit_log("snapshot_workspace_closed", {"workspace_id": workspace_id})
    return {"status": "closed"}


def list_workspace_files(workspace_id: str) -> list:
    """List all files in a workspace."""
    if workspace_id not in _snapshot_workspaces:
        raise HTTPException(status_code=404, detail="Workspace not found")

    workspace_root = Path(_snapshot_workspaces[workspace_id]["path"])
    files = []

    for dirpath, dirnames, filenames in os.walk(workspace_root):
        rel_dir = os.path.relpath(dirpath, workspace_root)
        if rel_dir == ".":
            rel_dir = ""

        # Add directories
        for d in sorted(dirnames):
            rel_path = f"{rel_dir}/{d}" if rel_dir else d
            files.append({"path": rel_path, "size": 0, "is_dir": True, "is_binary": False})

        # Add files
        for fname in sorted(filenames):
            full_path = Path(dirpath) / fname
            rel_path = f"{rel_dir}/{fname}" if rel_dir else fname
            size = full_path.stat().st_size if full_path.exists() else 0

            # Detect binary via null byte check in first 8KB
            is_binary = False
            try:
                with open(full_path, "rb") as f:
                    chunk = f.read(8192)
                    if b'\x00' in chunk:
                        is_binary = True
            except (OSError, IOError):
                is_binary = True

            files.append({"path": rel_path, "size": size, "is_dir": False, "is_binary": is_binary})

    return files


def read_workspace_file(workspace_id: str, file_path: str) -> dict:
    """Read a file from the workspace."""
    resolved = _validate_workspace_path(workspace_id, file_path)

    if not resolved.exists():
        raise HTTPException(status_code=404, detail="File not found")
    if resolved.is_dir():
        raise HTTPException(status_code=400, detail="Cannot read a directory")

    size = resolved.stat().st_size

    # Check binary
    try:
        with open(resolved, "rb") as f:
            chunk = f.read(8192)
            if b'\x00' in chunk:
                return {"binary": True, "size": size}
    except (OSError, IOError):
        return {"binary": True, "size": size}

    if size > 2 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large (>2MB)")

    content = resolved.read_text(errors="replace")
    return {"content": content, "size": size}


def write_workspace_file(workspace_id: str, file_path: str, content: str) -> dict:
    """Write text content to a file in the workspace."""
    resolved = _validate_workspace_path(workspace_id, file_path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content)
    return {"status": "saved", "size": len(content.encode())}


def upload_workspace_file(workspace_id: str, file_path: str, data: bytes) -> dict:
    """Write binary data to a file in the workspace."""
    resolved = _validate_workspace_path(workspace_id, file_path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_bytes(data)
    return {"status": "uploaded", "size": len(data)}


def delete_workspace_file(workspace_id: str, file_path: str) -> dict:
    """Delete a file or directory in the workspace."""
    resolved = _validate_workspace_path(workspace_id, file_path)

    if not resolved.exists():
        raise HTTPException(status_code=404, detail="File not found")

    if resolved.is_dir():
        shutil.rmtree(str(resolved))
    else:
        resolved.unlink()

    return {"status": "deleted"}


def rename_workspace_file(workspace_id: str, old_path: str, new_path: str) -> dict:
    """Rename/move a file or directory within the workspace."""
    resolved_old = _validate_workspace_path(workspace_id, old_path)
    resolved_new = _validate_workspace_path(workspace_id, new_path)

    if not resolved_old.exists():
        raise HTTPException(status_code=404, detail="Source not found")
    if resolved_new.exists():
        raise HTTPException(status_code=400, detail="Destination already exists")

    resolved_new.parent.mkdir(parents=True, exist_ok=True)
    resolved_old.rename(resolved_new)
    return {"status": "renamed"}


def duplicate_workspace_dir(workspace_id: str, src_path: str, dest_path: str) -> dict:
    """Copy a file or directory within the workspace."""
    resolved_src = _validate_workspace_path(workspace_id, src_path)
    resolved_dest = _validate_workspace_path(workspace_id, dest_path)

    if not resolved_src.exists():
        raise HTTPException(status_code=404, detail="Source not found")
    if resolved_dest.exists():
        raise HTTPException(status_code=400, detail="Destination already exists")

    resolved_dest.parent.mkdir(parents=True, exist_ok=True)

    if resolved_src.is_dir():
        shutil.copytree(str(resolved_src), str(resolved_dest))
    else:
        shutil.copy2(str(resolved_src), str(resolved_dest))

    return {"status": "duplicated"}


def download_workspace_dir(workspace_id: str, dir_path: str) -> Path:
    """Create a temporary .tar.gz of a directory within the workspace."""
    resolved = _validate_workspace_path(workspace_id, dir_path)
    if not resolved.exists() or not resolved.is_dir():
        raise HTTPException(status_code=404, detail="Directory not found")
    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
    tmp.close()
    subprocess.run(
        ["tar", "-C", str(resolved.parent), "-czf", tmp.name, resolved.name],
        capture_output=True, text=True,
    )
    return Path(tmp.name)


def save_workspace_as_snapshot(workspace_id: str, name: str = "") -> dict:
    """Create a new snapshot from workspace contents."""
    if workspace_id not in _snapshot_workspaces:
        raise HTTPException(status_code=404, detail="Workspace not found")

    workspace_path = Path(_snapshot_workspaces[workspace_id]["path"])

    if not ensure_snapshot_key():
        raise HTTPException(status_code=500, detail="No encryption key found and failed to generate one")

    # Get public key
    pubkey = None
    with open(AGE_KEY) as f:
        for line in f:
            if "public key:" in line:
                pubkey = line.split(": ")[1].strip()
                break
    if not pubkey:
        raise HTTPException(status_code=500, detail="Could not read public key")

    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    if name:
        sanitized = sanitize_snapshot_name(name)
        snapshot_name = f"{sanitized}--{timestamp}.tar.age"
    else:
        snapshot_name = f"snapshot--{timestamp}.tar.age"
    snapshot_path = SNAPSHOTS_DIR / snapshot_name

    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        result = subprocess.run(
            ["tar", "-C", str(workspace_path), "-cf", tmp_path, "."],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise HTTPException(status_code=500, detail=f"Failed to create tarball: {result.stderr}")

        result = subprocess.run(
            ["age", "-r", pubkey, "-o", str(snapshot_path), tmp_path],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise HTTPException(status_code=500, detail=f"Failed to encrypt: {result.stderr}")

        # Update latest symlink
        latest_link = SNAPSHOTS_DIR / "latest.tar.age"
        if latest_link.is_symlink():
            latest_link.unlink()
        latest_link.symlink_to(snapshot_name)

        size = snapshot_path.stat().st_size
        audit_log("snapshot_created_from_workspace", {"name": snapshot_name, "size": size, "workspace_id": workspace_id})

        # Auto-sync to host
        synced = False
        if IS_LIMA_MODE:
            try:
                pickup_dir = Path("/tmp/clawfactory-snapshot-sync")
                pickup_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(snapshot_path), str(pickup_dir / snapshot_name))
                synced = True
            except Exception as e:
                audit_log("snapshot_sync_failed", {"error": str(e)})

        return {"status": "created", "name": snapshot_name, "size": size, "synced": synced}
    finally:
        Path(tmp_path).unlink(missing_ok=True)


class SnapshotCreateRequest(BaseModel):
    name: str = ""


@app.post("/snapshot")
@app.post("/controller/snapshot")
async def snapshot_create(
    request: Optional[SnapshotCreateRequest] = None,
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Create an encrypted snapshot of bot state."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    snapshot_name = request.name if request else ""
    audit_log("snapshot_requested", {"name": snapshot_name})
    result = create_snapshot(name=snapshot_name)

    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])

    return result


@app.post("/snapshot/sync")
@app.post("/controller/snapshot/sync")
async def snapshot_sync(
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Sync snapshots to host via rsync (Lima mode only)."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not IS_LIMA_MODE:
        return {"error": "Snapshot sync is only available in Lima mode"}

    if not SNAPSHOTS_DIR.exists():
        return {"count": 0, "message": "No snapshots directory"}

    snapshots = [f for f in SNAPSHOTS_DIR.glob("*.tar.age") if f.name != "latest.tar.age"]
    if not snapshots:
        return {"count": 0, "message": "No snapshots to sync"}

    # Copy snapshots to a well-known pickup location that lima_sync can reach
    pickup_dir = Path("/tmp/clawfactory-snapshot-sync")
    pickup_dir.mkdir(parents=True, exist_ok=True)
    import shutil
    count = 0
    for snap in snapshots:
        dest = pickup_dir / snap.name
        if not dest.exists() or dest.stat().st_mtime < snap.stat().st_mtime:
            shutil.copy2(snap, dest)
            count += 1

    audit_log("snapshot_sync", {"count": count, "total": len(snapshots)})
    return {"count": len(snapshots), "synced": count, "pickup": str(pickup_dir)}


@app.get("/snapshot/download/{name}")
@app.get("/controller/snapshot/download/{name}")
async def snapshot_download(
    name: str,
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Download a snapshot file."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    snapshot_path = SNAPSHOTS_DIR / name
    if not snapshot_path.exists():
        snapshot_path = SNAPSHOTS_DIR / f"{name}.tar.age"
    if not snapshot_path.exists():
        raise HTTPException(status_code=404, detail="Snapshot not found")

    from starlette.responses import FileResponse
    return FileResponse(
        str(snapshot_path),
        media_type="application/octet-stream",
        filename=snapshot_path.name,
    )


@app.get("/snapshot")
@app.get("/controller/snapshot")
async def snapshot_list(
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """List available snapshots."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    return {
        "snapshots": list_snapshots(),
        "encryption_ready": AGE_KEY.exists() or ensure_snapshot_key()
    }


def delete_snapshot(snapshot_name: str) -> dict:
    """Delete a snapshot file."""
    if not SNAPSHOTS_DIR.exists():
        return {"error": "Snapshots directory does not exist"}

    if snapshot_name == "latest":
        return {"error": "Cannot delete 'latest' directly. Delete the actual snapshot file instead."}

    snapshot_path = SNAPSHOTS_DIR / snapshot_name
    if not snapshot_path.exists():
        snapshot_path = SNAPSHOTS_DIR / f"{snapshot_name}.tar.age"

    if not snapshot_path.exists():
        return {"error": f"Snapshot not found: {snapshot_name}"}

    if snapshot_path.is_symlink():
        return {"error": "Cannot delete symlinks directly"}

    # Safety: only allow deleting files matching the snapshot pattern
    if not snapshot_path.name.endswith(".tar.age") or "--" not in snapshot_path.name.replace(".tar.age", ""):
        # Also accept legacy snapshot-<timestamp>.tar.age format
        if not snapshot_path.name.startswith("snapshot-") or not snapshot_path.name.endswith(".tar.age"):
            return {"error": "Invalid snapshot filename"}

    # Check if this is the latest symlink target
    latest_link = SNAPSHOTS_DIR / "latest.tar.age"
    update_latest = False
    if latest_link.is_symlink() and latest_link.resolve() == snapshot_path.resolve():
        update_latest = True

    size = snapshot_path.stat().st_size
    snapshot_path.unlink()
    audit_log("snapshot_deleted", {"name": snapshot_name, "size": size})

    # If we deleted the latest target, point latest to the next most recent
    if update_latest:
        latest_link.unlink(missing_ok=True)
        remaining = sorted([f for f in SNAPSHOTS_DIR.glob("*.tar.age") if f.name != "latest.tar.age"], reverse=True)
        if remaining:
            latest_link.symlink_to(remaining[0].name)
            audit_log("snapshot_latest_updated", {"name": remaining[0].name})

    return {"deleted": snapshot_name, "size": size}


def delete_all_snapshots() -> dict:
    """Delete all snapshots."""
    if not SNAPSHOTS_DIR.exists():
        return {"deleted": 0}

    count = 0
    total_size = 0
    for f in SNAPSHOTS_DIR.glob("*.tar.age"):
        if f.name == "latest.tar.age":
            continue
        total_size += f.stat().st_size
        f.unlink()
        count += 1

    # Remove latest symlink
    latest_link = SNAPSHOTS_DIR / "latest.tar.age"
    latest_link.unlink(missing_ok=True)

    audit_log("snapshots_deleted_all", {"count": count, "total_size": total_size})
    return {"deleted": count, "total_size": total_size}


class SnapshotDeleteRequest(BaseModel):
    snapshot: str = ""


@app.post("/snapshot/delete")
@app.post("/controller/snapshot/delete")
async def snapshot_delete_endpoint(
    request: SnapshotDeleteRequest,
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Delete a snapshot or all snapshots."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not request.snapshot:
        return {"error": "No snapshot specified"}

    if request.snapshot == "all":
        audit_log("snapshot_delete_all_requested", {})
        result = delete_all_snapshots()
    else:
        audit_log("snapshot_delete_requested", {"snapshot": request.snapshot})
        result = delete_snapshot(request.snapshot)

    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])

    return result


class SnapshotRenameRequest(BaseModel):
    snapshot: str
    new_name: str


def rename_snapshot(old_filename: str, new_name: str) -> dict:
    """Rename a snapshot, preserving the timestamp suffix."""
    if not SNAPSHOTS_DIR.exists():
        return {"error": "Snapshots directory does not exist"}

    old_path = SNAPSHOTS_DIR / old_filename
    if not old_path.exists():
        return {"error": f"Snapshot not found: {old_filename}"}

    if not old_path.name.endswith(".tar.age"):
        return {"error": "Invalid snapshot filename"}

    # Extract timestamp from old name
    stem = old_path.name.replace(".tar.age", "")
    if "--" in stem:
        _, timestamp = stem.rsplit("--", 1)
    else:
        # Legacy format: snapshot-<timestamp>
        timestamp = stem.replace("snapshot-", "")

    sanitized = sanitize_snapshot_name(new_name)
    if not sanitized:
        return {"error": "Invalid name — must contain alphanumeric characters, hyphens, or underscores"}

    new_filename = f"{sanitized}--{timestamp}.tar.age"
    new_path = SNAPSHOTS_DIR / new_filename

    if new_path.exists():
        return {"error": f"A snapshot with that name already exists: {new_filename}"}

    # Rename the file
    old_path.rename(new_path)

    # Update latest symlink if it pointed to the old file
    latest_link = SNAPSHOTS_DIR / "latest.tar.age"
    if latest_link.is_symlink() and latest_link.resolve().name == old_filename:
        latest_link.unlink()
        latest_link.symlink_to(new_filename)

    audit_log("snapshot_renamed", {"old": old_filename, "new": new_filename})
    return {"status": "renamed", "old_name": old_filename, "new_name": new_filename}


@app.post("/snapshot/rename")
@app.post("/controller/snapshot/rename")
async def snapshot_rename_endpoint(
    request: SnapshotRenameRequest,
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Rename a snapshot."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    audit_log("snapshot_rename_requested", {"snapshot": request.snapshot, "new_name": request.new_name})
    result = rename_snapshot(request.snapshot, request.new_name)

    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])

    return result


def _migrate_docker_paths() -> bool:
    """Fix Docker-era paths in restored openclaw.json so snapshots from Docker work in Lima."""
    import re
    config_path = OPENCLAW_HOME / "openclaw.json"
    if not config_path.exists():
        return False

    content = config_path.read_text()
    original = content

    # /home/node/.openclaw/workspace → OPENCLAW_HOME/workspace
    content = content.replace("/home/node/.openclaw/workspace", f"{OPENCLAW_HOME}/workspace")
    # /home/node/.openclaw → OPENCLAW_HOME
    content = content.replace("/home/node/.openclaw", str(OPENCLAW_HOME))
    # /home/node → service user home
    content = content.replace("/home/node", f"/home/openclaw-{INSTANCE_NAME}")
    # host.docker.internal → 127.0.0.1
    content = content.replace("host.docker.internal", "127.0.0.1")

    if content != original:
        config_path.write_text(content)
        # Ensure workspace dir exists
        (OPENCLAW_HOME / "workspace").mkdir(parents=True, exist_ok=True)
        audit_log("snapshot_docker_paths_migrated", {"config": str(config_path)})
        return True
    return False


def restore_snapshot(snapshot_name: str) -> dict:
    """Restore state from an encrypted snapshot."""
    import tempfile

    if not AGE_KEY.exists():
        return {"error": "No decryption key found"}

    # Resolve snapshot path
    if snapshot_name == "latest":
        snapshot_path = SNAPSHOTS_DIR / "latest.tar.age"
        if snapshot_path.is_symlink():
            snapshot_path = snapshot_path.resolve()
    else:
        snapshot_path = SNAPSHOTS_DIR / snapshot_name
        if not snapshot_path.exists():
            # Try with .tar.age extension
            snapshot_path = SNAPSHOTS_DIR / f"{snapshot_name}.tar.age"

    if not snapshot_path.exists():
        return {"error": f"Snapshot not found: {snapshot_name}"}

    # Backup current state
    backup_dir = Path(f"{OPENCLAW_HOME}.backup-{int(datetime.now().timestamp())}")
    if OPENCLAW_HOME.exists():
        import shutil
        shutil.move(str(OPENCLAW_HOME), str(backup_dir))

    OPENCLAW_HOME.mkdir(parents=True, exist_ok=True)

    # Decrypt and extract
    try:
        result = subprocess.run(
            f"age -d -i {AGE_KEY} {snapshot_path} | tar -C {OPENCLAW_HOME} -xf -",
            shell=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            # Restore backup on failure
            if backup_dir.exists():
                import shutil
                shutil.rmtree(str(OPENCLAW_HOME), ignore_errors=True)
                shutil.move(str(backup_dir), str(OPENCLAW_HOME))
            return {"error": f"Restore failed: {result.stderr}"}

        # Migrate Docker-era paths if present
        migrated = _migrate_docker_paths()

        # Fix ownership so the gateway user can read restored files
        if IS_LIMA_MODE:
            svc_user = f"openclaw-{INSTANCE_NAME}"
            subprocess.run(
                ["chown", "-R", f"{svc_user}:{svc_user}", str(OPENCLAW_HOME)],
                capture_output=True, timeout=30
            )

        audit_log("snapshot_restored", {"snapshot": snapshot_name, "backup": str(backup_dir), "migrated": migrated})
        result = {
            "status": "restored",
            "snapshot": snapshot_name,
            "backup": str(backup_dir),
        }
        if migrated:
            result["warning"] = "Migrated Docker-era paths in restored config"
        return result
    except Exception as e:
        # Restore backup on failure
        if backup_dir.exists():
            import shutil
            shutil.rmtree(str(OPENCLAW_HOME), ignore_errors=True)
            shutil.move(str(backup_dir), str(OPENCLAW_HOME))
        return {"error": str(e)}


class SnapshotRestoreRequest(BaseModel):
    snapshot: str = "latest"


@app.post("/snapshot/restore")
@app.post("/controller/snapshot/restore")
async def snapshot_restore_endpoint(
    request: SnapshotRestoreRequest,
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Restore from a snapshot. Stops gateway, restores, restarts."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    audit_log("snapshot_restore_requested", {"snapshot": request.snapshot})

    # Stop gateway first
    try:
        gateway_stop()
    except Exception as e:
        audit_log("snapshot_restore_error", {"error": f"Failed to stop gateway: {e}"})
        raise HTTPException(status_code=500, detail=f"Failed to stop gateway: {e}")

    # Restore
    result = restore_snapshot(request.snapshot)
    if "error" in result:
        # Try to restart gateway even on failure
        try:
            gateway_start()
        except Exception:
            pass
        audit_log("snapshot_restore_error", result)
        raise HTTPException(status_code=500, detail=result["error"])

    # Restart gateway
    try:
        gateway_start()
    except Exception as e:
        result["warning"] = f"Gateway failed to restart: {e}"

    return result


# ============================================================
# Snapshot Browse API Endpoints
# ============================================================

class SnapshotBrowseOpenRequest(BaseModel):
    snapshot: str


class SnapshotBrowseCloseRequest(BaseModel):
    workspace_id: str


class SnapshotBrowseFileWriteRequest(BaseModel):
    workspace_id: str
    path: str
    content: str


class SnapshotBrowseDeleteRequest(BaseModel):
    workspace_id: str
    path: str


class SnapshotBrowseRenameRequest(BaseModel):
    workspace_id: str
    path: str
    new_name: str


class SnapshotBrowseDuplicateRequest(BaseModel):
    workspace_id: str
    path: str
    dest_name: str


class SnapshotBrowseSaveRequest(BaseModel):
    workspace_id: str
    name: str = ""


@app.post("/snapshot/browse/open")
@app.post("/controller/snapshot/browse/open")
async def snapshot_browse_open(
    request: SnapshotBrowseOpenRequest,
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return open_snapshot_workspace(request.snapshot)


@app.post("/snapshot/browse/close")
@app.post("/controller/snapshot/browse/close")
async def snapshot_browse_close(
    request: SnapshotBrowseCloseRequest,
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return close_snapshot_workspace(request.workspace_id)


@app.get("/snapshot/browse/files")
@app.get("/controller/snapshot/browse/files")
async def snapshot_browse_files(
    workspace_id: str = Query(...),
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return {"files": list_workspace_files(workspace_id)}


@app.get("/snapshot/browse/file")
@app.get("/controller/snapshot/browse/file")
async def snapshot_browse_file_read(
    workspace_id: str = Query(...),
    path: str = Query(...),
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return read_workspace_file(workspace_id, path)


@app.get("/snapshot/browse/file/download")
@app.get("/controller/snapshot/browse/file/download")
async def snapshot_browse_file_download(
    workspace_id: str = Query(...),
    path: str = Query(...),
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")
    resolved = _validate_workspace_path(workspace_id, path)
    if not resolved.exists():
        raise HTTPException(status_code=404, detail="File not found")
    from starlette.responses import FileResponse
    if resolved.is_dir():
        from starlette.background import BackgroundTask
        tmp_path = download_workspace_dir(workspace_id, path)
        return FileResponse(
            str(tmp_path),
            media_type="application/gzip",
            filename=resolved.name + ".tar.gz",
            background=BackgroundTask(lambda: tmp_path.unlink(missing_ok=True)),
        )
    return FileResponse(str(resolved), media_type="application/octet-stream", filename=resolved.name)


@app.post("/snapshot/browse/file")
@app.post("/controller/snapshot/browse/file")
async def snapshot_browse_file_write(
    request: SnapshotBrowseFileWriteRequest,
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return write_workspace_file(request.workspace_id, request.path, request.content)


@app.post("/snapshot/browse/upload")
@app.post("/controller/snapshot/browse/upload")
async def snapshot_browse_upload(
    workspace_id: str = Form(...),
    path: str = Form(""),
    file: UploadFile = File(...),
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")
    # Sanitize filename
    filename = _re.sub(r'[^a-zA-Z0-9._\-]', '_', file.filename or "upload") if file.filename else "upload"
    upload_path = f"{path}/{filename}" if path else filename
    data = await file.read()
    return upload_workspace_file(workspace_id, upload_path, data)


@app.post("/snapshot/browse/delete-file")
@app.post("/controller/snapshot/browse/delete-file")
async def snapshot_browse_delete(
    request: SnapshotBrowseDeleteRequest,
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return delete_workspace_file(request.workspace_id, request.path)


@app.post("/snapshot/browse/rename")
@app.post("/controller/snapshot/browse/rename")
async def snapshot_browse_rename(
    request: SnapshotBrowseRenameRequest,
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")
    # Build new path: same parent dir, new name
    old_parts = request.path.rstrip("/").rsplit("/", 1)
    parent = old_parts[0] if len(old_parts) > 1 else ""
    new_name_sanitized = _re.sub(r'[^a-zA-Z0-9._\-]', '_', request.new_name)
    new_path = f"{parent}/{new_name_sanitized}" if parent else new_name_sanitized
    return rename_workspace_file(request.workspace_id, request.path, new_path)


@app.post("/snapshot/browse/duplicate")
@app.post("/controller/snapshot/browse/duplicate")
async def snapshot_browse_duplicate(
    request: SnapshotBrowseDuplicateRequest,
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")
    old_parts = request.path.rstrip("/").rsplit("/", 1)
    parent = old_parts[0] if len(old_parts) > 1 else ""
    dest_sanitized = _re.sub(r'[^a-zA-Z0-9._\-]', '_', request.dest_name)
    dest_path = f"{parent}/{dest_sanitized}" if parent else dest_sanitized
    return duplicate_workspace_dir(request.workspace_id, request.path, dest_path)


@app.post("/snapshot/browse/save")
@app.post("/controller/snapshot/browse/save")
async def snapshot_browse_save(
    request: SnapshotBrowseSaveRequest,
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return save_workspace_as_snapshot(request.workspace_id, request.name)


# ============================================================
# Gateway Config Editor
# ============================================================

GATEWAY_CONFIG_PATH = OPENCLAW_HOME / "openclaw.json"
KNOWN_GOOD_CONFIG_PATH = Path("/srv/audit/known_good_config.json")


def validate_gateway_config(config: dict) -> tuple[bool, list[str]]:
    """Validate config by running the gateway in dry-run mode against a temp copy.

    Returns (valid, errors) where errors is a list of validation error strings.
    """
    import tempfile, shutil
    tmpdir = tempfile.mkdtemp(prefix="clawfactory-validate-")
    try:
        with open(os.path.join(tmpdir, "openclaw.json"), "w") as f:
            json.dump(config, f, indent=2)

        svc_user = f"openclaw-{INSTANCE_NAME}"
        env = {
            "OPENCLAW_STATE_DIR": tmpdir,
            "HOME": f"/home/{svc_user}",
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        }
        # Load gateway env vars (API keys etc.) so config validation can resolve providers
        gateway_env_file = SECRETS_DIR / "gateway.env"
        if gateway_env_file.exists():
            with open(gateway_env_file) as ef:
                for line in ef:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        env[k] = v

        if IS_LIMA_MODE:
            cmd = [
                "sudo", "-u", svc_user,
                "env"] + [f"{k}={v}" for k, v in env.items()] + [
                "node", "dist/index.js", "gateway", "run",
                "--bind", "loopback", "--port", "19999",
            ]
        else:
            cmd = [
                "node", "dist/index.js", "gateway", "run",
                "--bind", "loopback", "--port", "19999",
            ]

        try:
            result = subprocess.run(
                cmd,
                cwd=str(CODE_DIR),
                capture_output=True, text=True,
                timeout=10,
                env=env if not IS_LIMA_MODE else None,
            )
            output = result.stdout + "\n" + result.stderr
        except subprocess.TimeoutExpired as e:
            # Timeout = gateway started successfully (config is valid)
            output = (e.stdout or b"").decode(errors="replace") + "\n" + (e.stderr or b"").decode(errors="replace")
            if "Config invalid" not in output:
                return True, []

        # Parse validation errors from output
        if "Config invalid" in output or "Unrecognized key" in output:
            errors = []
            for line in output.split("\n"):
                line = line.strip().lstrip("- ")
                if "Unrecognized key" in line or "Required" in line or "Expected" in line or "Invalid" in line:
                    # Strip ANSI codes
                    import re
                    clean = re.sub(r'\x1b\[[0-9;]*m', '', line)
                    if clean and clean not in errors:
                        errors.append(clean)
            return False, errors if errors else ["Config validation failed (unknown schema error)"]

        return True, []
    except Exception as e:
        # If validation itself fails, log but allow (don't block on validation infra issues)
        audit_log("config_validate_error", {"error": str(e)})
        return True, []
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def fetch_ollama_models() -> list:
    """Fetch available models from Ollama with full details."""
    import urllib.request
    import urllib.error

    # Read Ollama baseUrl from gateway config if available
    config_base = None
    try:
        with open(GATEWAY_CONFIG_PATH) as f:
            cfg = json.load(f)
        ollama_url = cfg.get("models", {}).get("providers", {}).get("ollama", {}).get("baseUrl", "")
        if ollama_url:
            # Strip /v1 suffix — Ollama native API doesn't use it
            config_base = ollama_url.rstrip("/").removesuffix("/v1")
    except Exception:
        pass

    # Try config URL first, then common fallbacks
    fallback_urls = [
        "http://192.168.5.2:11434",
        "http://localhost:11434",
        "http://ollama:11434",
    ] if IS_LIMA_MODE else [
        "http://host.docker.internal:11434",
        "http://localhost:11434",
        "http://ollama:11434",
    ]

    base_urls = ([config_base] if config_base else []) + [u for u in fallback_urls if u != config_base]

    working_base = None
    models_list = []

    # First get list of models
    for base in base_urls:
        try:
            req = urllib.request.Request(f"{base}/api/tags", method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                models_list = data.get("models", [])
                working_base = base
                break
        except Exception:
            continue

    if not working_base or not models_list:
        return []

    # Fetch details for each model
    models = []
    for m in models_list:
        name = m.get("name", "")
        details = m.get("details", {})

        # Get full model info for context window
        context_window = 4096  # default
        is_reasoning = False
        try:
            show_data = json.dumps({"name": name}).encode()
            req = urllib.request.Request(
                f"{working_base}/api/show",
                data=show_data,
                method="POST",
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                info = json.loads(resp.read().decode())
                model_info = info.get("model_info", {})

                # Find context length (different models use different keys)
                for key, val in model_info.items():
                    if "context_length" in key.lower() and isinstance(val, int):
                        context_window = val
                        break

                # Check if it's a reasoning model
                model_file = info.get("modelfile", "").lower()
                if "reason" in name.lower() or "qwq" in name.lower() or "r1" in name.lower():
                    is_reasoning = True
        except Exception:
            pass

        # Build friendly name
        family = details.get("family", "")
        params = details.get("parameter_size", "")
        friendly_name = name.split(":")[0].upper()
        if params:
            friendly_name += f" {params}"

        # Parse parameter count from string like "32.8B" or "14B"
        param_billions = 0
        if params:
            try:
                param_billions = float(params.replace("B", "").replace("b", ""))
            except ValueError:
                pass

        models.append({
            "name": name,
            "id": name,  # Just the model name, not ollama/ prefix
            "friendly_name": friendly_name,
            "size": m.get("size", 0),
            "family": family,
            "parameters": params,
            "param_billions": param_billions,
            "quantization": details.get("quantization_level", ""),
            "context_window": context_window,  # Model's max capability
            "reasoning": is_reasoning,
        })

    return models


@app.get("/gateway/config")
@app.get("/controller/gateway/config")
async def gateway_config_get(
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Get the gateway openclaw.json config and available Ollama models."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not GATEWAY_CONFIG_PATH.exists():
        return {"error": "Config file not found"}

    try:
        with open(GATEWAY_CONFIG_PATH) as f:
            config = json.load(f)

        # Fetch available Ollama models
        ollama_models = fetch_ollama_models()

        # Build the host path for editor links
        # Container path: /srv/bot/state/openclaw.json
        # Host path: bot_repos/{instance}/state/openclaw.json
        host_config_path = f"bot_repos/{INSTANCE_NAME}/state/openclaw.json"

        return {
            "config": config,
            "ollama_models": ollama_models,
            "config_path": host_config_path,
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/gateway/config")
@app.post("/controller/gateway/config")
async def gateway_config_save(
    request: Request,
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Save gateway config. Stops gateway, writes config, restarts gateway."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.json()
    config = body.get("config")

    if not config:
        return {"error": "No config provided"}

    # Update meta.lastTouchedAt timestamp
    if "meta" not in config:
        config["meta"] = {}
    config["meta"]["lastTouchedAt"] = datetime.now(timezone.utc).isoformat()

    # Validate before saving
    valid, errors = validate_gateway_config(config)
    if not valid:
        audit_log("gateway_config_save_rejected", {"errors": errors})
        return {"error": "Config validation failed", "validation_errors": errors}

    audit_log("gateway_config_save", {"keys": list(config.keys())})

    try:
        # Save current config as known-good backup before changing
        if GATEWAY_CONFIG_PATH.exists():
            import shutil
            KNOWN_GOOD_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(GATEWAY_CONFIG_PATH, KNOWN_GOOD_CONFIG_PATH)
            audit_log("known_good_config_saved", {})

        # Stop the gateway first
        gateway_stop()
        audit_log("gateway_stopped_for_config", {})

        # Write the config
        with open(GATEWAY_CONFIG_PATH, "w") as f:
            json.dump(config, f, indent=2)
        audit_log("gateway_config_written", {})

        # Start the gateway
        gateway_start()
        audit_log("gateway_started_after_config", {})

        return {"status": "saved", "restarted": True}
    except Exception as e:
        audit_log("gateway_config_error", {"error": str(e)})
        return {"error": str(e)}


@app.get("/gateway/config/known-good")
@app.get("/controller/gateway/config/known-good")
async def gateway_config_known_good_get(
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Check if a known-good config backup exists."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not KNOWN_GOOD_CONFIG_PATH.exists():
        return {"has_backup": False}

    try:
        stat = KNOWN_GOOD_CONFIG_PATH.stat()
        return {
            "has_backup": True,
            "timestamp": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        }
    except Exception as e:
        return {"has_backup": False, "error": str(e)}


@app.post("/gateway/config/revert")
@app.post("/controller/gateway/config/revert")
async def gateway_config_revert(
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Revert to the known-good config backup."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not KNOWN_GOOD_CONFIG_PATH.exists():
        return {"error": "No known-good backup exists"}

    audit_log("config_revert_requested", {})

    try:
        import shutil

        # Stop gateway
        gateway_stop()
        audit_log("gateway_stopped_for_revert", {})

        # Copy known-good back
        shutil.copy(KNOWN_GOOD_CONFIG_PATH, GATEWAY_CONFIG_PATH)
        audit_log("config_reverted", {})

        # Start gateway
        gateway_start()
        audit_log("gateway_started_after_revert", {})

        return {"status": "reverted", "restarted": True}
    except Exception as e:
        audit_log("config_revert_error", {"error": str(e)})
        return {"error": str(e)}


# ============================================================
# Gateway Pairing (Device + DM)
# ============================================================

def run_gateway_command(cmd: list[str], timeout: int = 30) -> tuple[bool, str]:
    """Run a command inside the gateway (subprocess in Lima mode, docker exec otherwise)."""
    try:
        if IS_LIMA_MODE:
            svc_user = f"openclaw-{INSTANCE_NAME}"
            result = subprocess.run(
                ["sudo", "-u", svc_user] + cmd,
                cwd=str(CODE_DIR),
                capture_output=True, text=True, timeout=timeout,
            )
            output = result.stdout + result.stderr
            return result.returncode == 0, output
        else:
            client = docker.from_env()
            gateway = client.containers.get(GATEWAY_CONTAINER)
            exit_code, output = gateway.exec_run(cmd, demux=False)
            return exit_code == 0, output.decode() if output else ""
    except Exception as e:
        return False, str(e)


@app.post("/gateway/restart")
@app.post("/controller/gateway/restart")
async def gateway_restart_endpoint(
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Restart the gateway container."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    audit_log("gateway_restart_requested", {"source": "api"})

    if not restart_gateway():
        raise HTTPException(status_code=500, detail="Failed to restart gateway")

    return {"status": "restarting", "container": GATEWAY_CONTAINER}


@app.post("/pull-upstream")
@app.post("/controller/pull-upstream")
async def pull_upstream_endpoint(
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Pull latest OpenClaw from upstream."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    audit_log("pull_upstream_requested", {"source": "api"})

    try:
        # Ensure git user config is set for merge commits
        subprocess.run(["git", "config", "user.name", "ClawFactory Controller"],
                       cwd=CODE_DIR, capture_output=True)
        subprocess.run(["git", "config", "user.email", "controller@clawfactory.local"],
                       cwd=CODE_DIR, capture_output=True)

        # Check if upstream remote exists, add if not
        result = subprocess.run(
            ["git", "remote", "get-url", "upstream"],
            cwd=CODE_DIR,
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            # Add upstream remote
            subprocess.run(
                ["git", "remote", "add", "upstream", "https://github.com/openclaw/openclaw.git"],
                cwd=CODE_DIR,
                capture_output=True,
                text=True
            )

        # Fetch upstream
        fetch_result = subprocess.run(
            ["git", "fetch", "upstream"],
            cwd=CODE_DIR,
            capture_output=True,
            text=True,
            timeout=120
        )
        if fetch_result.returncode != 0:
            return {"error": f"Fetch failed: {fetch_result.stderr}"}

        # Merge upstream/main
        merge_result = subprocess.run(
            ["git", "merge", "upstream/main", "--no-edit", "-m", "Merge upstream OpenClaw"],
            cwd=CODE_DIR,
            capture_output=True,
            text=True,
            timeout=60
        )
        if merge_result.returncode != 0:
            return {"error": f"Merge failed: {merge_result.stderr}"}

        return {
            "message": "Pulled and merged upstream/main successfully",
            "changes": merge_result.stdout or "Already up to date"
        }
    except subprocess.TimeoutExpired:
        return {"error": "Operation timed out"}
    except Exception as e:
        return {"error": str(e)}


@app.post("/gateway/rebuild")
@app.post("/controller/gateway/rebuild")
async def gateway_rebuild_endpoint(
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Rebuild and restart the gateway container."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    audit_log("gateway_rebuild_requested", {"source": "api"})

    try:
        if IS_LIMA_MODE:
            # Lima mode: rebuild via pnpm install + build, then restart service
            gateway_stop()

            svc_user = f"openclaw-{INSTANCE_NAME}"
            install_result = subprocess.run(
                ["sudo", "-u", svc_user, "pnpm", "install", "--frozen-lockfile"],
                cwd=str(CODE_DIR),
                capture_output=True, text=True, timeout=300,
            )
            if install_result.returncode != 0:
                gateway_start()
                return {"error": f"Install failed: {install_result.stderr}"}

            build_result = subprocess.run(
                ["sudo", "-u", svc_user, "pnpm", "run", "build"],
                cwd=str(CODE_DIR),
                capture_output=True, text=True, timeout=300,
            )
            if build_result.returncode != 0:
                gateway_start()
                return {"error": f"Build failed: {build_result.stderr}"}

            gateway_start()
        else:
            client = docker.from_env()

            # Stop the gateway container
            try:
                container = client.containers.get(GATEWAY_CONTAINER)
                container.stop(timeout=30)
            except docker.errors.NotFound:
                pass

            # Rebuild the image using docker compose
            rebuild_result = subprocess.run(
                ["docker", "compose", "build", "--no-cache", "gateway"],
                cwd=str(CODE_DIR.parent.parent.parent),  # Go up to clawfactory root
                capture_output=True,
                text=True,
                timeout=600  # 10 minute timeout for build
            )

            if rebuild_result.returncode != 0:
                return {"error": f"Build failed: {rebuild_result.stderr}"}

            # Start the gateway container
            start_result = subprocess.run(
                ["docker", "compose", "up", "-d", "gateway"],
                cwd=str(CODE_DIR.parent.parent.parent),
                capture_output=True,
                text=True,
                timeout=60
            )

            if start_result.returncode != 0:
                return {"error": f"Start failed: {start_result.stderr}"}

        return {"message": "Gateway rebuilt and restarted successfully"}
    except subprocess.TimeoutExpired:
        return {"error": "Operation timed out"}
    except Exception as e:
        return {"error": str(e)}


@app.get("/gateway/logs")
@app.get("/controller/gateway/logs")
async def gateway_logs_endpoint(
    lines: int = Query(100, ge=1, le=2000),
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Get gateway container logs."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        if IS_LIMA_MODE:
            result = subprocess.run(
                ["journalctl", "-u", f"openclaw-gateway@{INSTANCE_NAME}", "-n", str(lines), "--no-pager"],
                capture_output=True, text=True, timeout=10,
            )
            logs = result.stdout if result.returncode == 0 else f"Error reading logs: {result.stderr}"
        else:
            client = docker.from_env()
            container = client.containers.get(GATEWAY_CONTAINER)
            logs = container.logs(tail=lines, timestamps=False).decode("utf-8", errors="replace")
        return {"logs": logs, "lines": lines, "container": GATEWAY_CONTAINER}
    except docker.errors.NotFound:
        return {"error": f"Container {GATEWAY_CONTAINER} not found"}
    except Exception as e:
        return {"error": str(e)}


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


@app.post("/gateway/config/validate")
@app.post("/controller/gateway/config/validate")
async def gateway_config_validate(
    request: Request,
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Validate config from editor (not deployed config)."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.json()
    config = body.get("config")

    if not config:
        return {"valid": False, "issues": [{"severity": "error", "message": "No config provided"}]}

    issues = []

    # Check it's a dict
    if not isinstance(config, dict):
        return {"valid": False, "issues": [{"severity": "error", "message": "Config must be a JSON object"}]}

    # Check for required/recommended keys
    if "gateway" not in config:
        issues.append({"severity": "warn", "message": "Missing 'gateway' section"})

    if "models" not in config and "agents" not in config:
        issues.append({"severity": "warn", "message": "No models or agents configured"})

    # Check for common invalid keys at root level
    valid_root_keys = {
        "meta", "wizard", "models", "agents", "channels", "gateway", "plugins",
        "messages", "commands", "tools", "session", "hooks", "cron", "skills",
        "env", "auth", "talk",
    }
    for key in config.keys():
        if key not in valid_root_keys:
            issues.append({
                "severity": "warn",
                "message": f"Unknown config key: {key}",
                "key": key,
            })

    # Check gateway section
    if "gateway" in config and isinstance(config["gateway"], dict):
        gw = config["gateway"]
        if "auth" in gw and isinstance(gw["auth"], dict):
            auth_mode = gw["auth"].get("mode")
            if auth_mode == "token" and not gw["auth"].get("token"):
                issues.append({"severity": "warn", "message": "Gateway auth mode is 'token' but no token set"})

    is_valid = not any(i["severity"] == "error" for i in issues)

    return {
        "valid": is_valid,
        "issues": issues,
    }


# ============================================================
# Internal Endpoints (gateway auth — GATEWAY_INTERNAL_TOKEN)
# ============================================================
# Internal helpers
# ============================================================

def _read_env_file(path: Path) -> dict[str, str]:
    """Read a key=value env file into a dict, preserving order."""
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, val = line.partition("=")
            # Strip optional quotes
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                val = val[1:-1]
            result[key.strip()] = val
    return result


def _write_env_file(path: Path, env: dict[str, str]):
    """Write a dict back to a key=value env file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for k, v in env.items():
        # Quote values that contain spaces or special chars
        if " " in v or "'" in v or '"' in v:
            lines.append(f'{k}="{v}"')
        else:
            lines.append(f"{k}={v}")
    path.write_text("\n".join(lines) + "\n")


# Dangerous operations (restore, delete, rebuild) require CONTROLLER_API_TOKEN.

@app.post("/internal/snapshot")
async def internal_snapshot_create():
    """Create snapshot - internal endpoint (no auth, localhost only)."""
    audit_log("snapshot_requested", {"source": "internal"})
    result = create_snapshot()
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    return result


@app.get("/internal/snapshot")
async def internal_snapshot_list():
    """List snapshots - internal endpoint (no auth, localhost only)."""
    return {"snapshots": list_snapshots()}


# ============================================================
# Agent API
# ============================================================
# Scoped endpoints for the sandboxed agent. Authenticated with
# AGENT_API_TOKEN so the agent never holds the real gateway token.


@app.put("/agent/files/{filepath:path}")
async def agent_write_file(
    filepath: str,
    request: Request,
    token: Optional[str] = Query(None),
    authorization: Optional[str] = Header(None),
    agent_id: Optional[str] = Query(None),
):
    """Write a file to the code dir — agent-scoped (requires AGENT_API_TOKEN).

    Path is relative to CODE_DIR. Only allows writing inside it.
    When agent_id is provided, writes are restricted to that agent's workspace.
    """
    if not check_agent_auth(token, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Security: block null bytes
    if "\x00" in filepath:
        raise HTTPException(status_code=400, detail="Invalid path")

    # Security: block path traversal
    if ".." in filepath or filepath.startswith("/"):
        audit_log("agent_file_rejected", {"path": filepath, "reason": "path_traversal"})
        raise HTTPException(status_code=400, detail="Invalid path (no .. or absolute paths)")

    # Per-agent file scope enforcement
    scope = resolve_agent_file_scope((agent_id or "").strip())
    if scope is not None:
        if not filepath.startswith(str(scope)):
            audit_log("agent_file_rejected", {
                "path": filepath, "agent": agent_id,
                "scope": str(scope), "reason": "out_of_scope"
            })
            raise HTTPException(
                status_code=403,
                detail=f"Agent '{agent_id}' can only write to {scope}/"
            )

    # Security: block dangerous directories
    path_lower = filepath.lower()
    blocked_prefixes = (".git/", "node_modules/", ".github/workflows/")
    if any(path_lower.startswith(p) or f"/{p}" in path_lower for p in blocked_prefixes):
        audit_log("agent_file_rejected", {"path": filepath, "reason": "blocked_directory"})
        raise HTTPException(status_code=400, detail="Cannot write to protected directories (.git, node_modules, .github/workflows)")

    # Block writing outside code dir (resolve follows symlinks)
    target = (CODE_DIR / filepath).resolve()
    if not str(target).startswith(str(CODE_DIR.resolve())):
        audit_log("agent_file_rejected", {"path": filepath, "reason": "outside_code_dir"})
        raise HTTPException(status_code=400, detail="Path resolves outside code directory")

    # Block sensitive files
    basename = target.name.lower()
    blocked_names = {".env", ".env.local", ".env.production", "credentials.json",
                     "secrets.json", ".git-credentials", ".npmrc", ".netrc"}
    if basename in blocked_names:
        audit_log("agent_file_rejected", {"path": filepath, "reason": "sensitive_file"})
        raise HTTPException(status_code=400, detail="Cannot write sensitive files")

    # Size limit: 1MB
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > 1_048_576:
        raise HTTPException(status_code=413, detail="File too large (max 1MB)")

    body = await request.body()
    if len(body) > 1_048_576:
        raise HTTPException(status_code=413, detail="File too large (max 1MB)")

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)

        # Fix ownership so the gateway user can stage/commit these files
        svc_user = f"openclaw-{INSTANCE_NAME}"
        try:
            shutil.chown(str(target), user=svc_user, group=svc_user)
            # Also fix any newly-created parent dirs between CODE_DIR and target
            p = target.parent
            code_resolved = CODE_DIR.resolve()
            while p.resolve() != code_resolved and str(p.resolve()).startswith(str(code_resolved)):
                shutil.chown(str(p), user=svc_user, group=svc_user)
                p = p.parent
        except (LookupError, OSError):
            pass  # non-Lima or user doesn't exist — skip silently

        audit_log("agent_file_written", {"path": filepath, "size": len(body), "agent": agent_id or "unscoped"})
        return {"status": "written", "path": filepath, "size": len(body)}
    except Exception as e:
        audit_log("agent_file_error", {"path": filepath, "error": str(e), "agent": agent_id or "unscoped"})
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/agent/files/{filepath:path}")
async def agent_read_file(
    filepath: str,
    token: Optional[str] = Query(None),
    authorization: Optional[str] = Header(None),
):
    """Read a file from the code dir — agent-scoped (requires AGENT_API_TOKEN)."""
    if not check_agent_auth(token, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    if ".." in filepath or filepath.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid path")

    target = (CODE_DIR / filepath).resolve()
    if not str(target).startswith(str(CODE_DIR.resolve())):
        raise HTTPException(status_code=400, detail="Path resolves outside code directory")

    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found")

    if not target.is_file():
        raise HTTPException(status_code=400, detail="Not a file")

    # Size limit for reads: 2MB
    if target.stat().st_size > 2_097_152:
        raise HTTPException(status_code=413, detail="File too large to read (max 2MB)")

    try:
        from fastapi.responses import Response
        content = target.read_bytes()
        return Response(content=content, media_type="application/octet-stream")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/agent/gateway/status")
async def agent_gateway_status(
    token: Optional[str] = Query(None),
    authorization: Optional[str] = Header(None),
):
    """Gateway status — agent-scoped (requires AGENT_API_TOKEN)."""
    if not check_agent_auth(token, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    status = get_gateway_status()
    return {"gateway": status, "port": GATEWAY_PORT, "instance": INSTANCE_NAME}


@app.get("/agent/gateway/channels")
async def agent_gateway_channels(
    token: Optional[str] = Query(None),
    authorization: Optional[str] = Header(None),
):
    """Channel connection status — agent-scoped (requires AGENT_API_TOKEN)."""
    if not check_agent_auth(token, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    success, output = run_gateway_command(
        ["node", "dist/index.js", "channels", "status", "--json"], timeout=15
    )
    if not success:
        return {"error": f"Failed to get channel status: {output}"}

    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return {"raw": output}


@app.post("/agent/gateway/restart")
async def agent_gateway_restart(
    token: Optional[str] = Query(None),
    authorization: Optional[str] = Header(None),
    agent_id: Optional[str] = Query(None),
):
    """Restart the gateway — agent-scoped (requires AGENT_API_TOKEN).

    Only unscoped callers (no agent_id, or main/default) can restart.
    """
    if not check_agent_auth(token, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    scope = resolve_agent_file_scope((agent_id or "").strip())
    if scope is not None:
        audit_log("gateway_restart_denied", {"agent": agent_id, "reason": "scoped_agent"})
        raise HTTPException(
            status_code=403,
            detail=f"Agent '{agent_id}' is not allowed to restart the gateway"
        )

    audit_log("gateway_restart_requested", {"source": "agent", "agent": agent_id or "unscoped"})

    if not restart_gateway():
        raise HTTPException(status_code=500, detail="Failed to restart gateway")

    return {"status": "restarted"}


@app.get("/agent/gateway/config")
async def agent_gateway_config(
    token: Optional[str] = Query(None),
    authorization: Optional[str] = Header(None),
):
    """Read current openclaw.json config — agent-scoped (requires AGENT_API_TOKEN)."""
    if not check_agent_auth(token, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    config_path = OPENCLAW_HOME / "openclaw.json"
    try:
        with open(config_path) as f:
            config = json.load(f)
        # Strip secrets before returning to agent
        safe = {k: v for k, v in config.items() if k not in ("talk",)}
        if "gateway" in safe and "auth" in safe["gateway"]:
            safe["gateway"]["auth"] = {"mode": safe["gateway"]["auth"].get("mode", "unknown")}
        if "env" in safe and "vars" in safe["env"]:
            safe["env"]["vars"] = {k: "***" for k in safe["env"]["vars"]}
        for pname, pconf in safe.get("models", {}).get("providers", {}).items():
            if "apiKey" in pconf:
                pconf["apiKey"] = "***"
            if "headers" in pconf:
                pconf["headers"] = {k: "***" for k in pconf["headers"]}
        return safe
    except FileNotFoundError:
        return {"error": "Config file not found"}
    except Exception as e:
        return {"error": str(e)}


# ============================================================
# Startup / Shutdown
# ============================================================

@app.on_event("shutdown")
async def _cleanup_snapshot_workspaces():
    """Clean up all snapshot browse workspaces on shutdown."""
    for wid, w in list(_snapshot_workspaces.items()):
        workspace_path = Path(w["path"])
        if workspace_path.exists():
            shutil.rmtree(str(workspace_path), ignore_errors=True)
    _snapshot_workspaces.clear()
