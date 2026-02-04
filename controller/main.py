#!/usr/bin/env python3
"""
ClawFactory Controller - Authority & Promotion Service

Responsibilities:
- Receive GitHub webhooks (PR merged → pull main)
- Perform promotions (pull main after PR merge)
- Restart Gateway after promotion
- Create encrypted snapshots of bot state
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
from pydantic import BaseModel

# Configuration from environment
APPROVED_DIR = Path(os.environ.get("APPROVED_DIR", "/srv/bot/approved"))
OPENCLAW_HOME = Path(os.environ.get("OPENCLAW_HOME", "/srv/bot/state"))
AUDIT_LOG = Path(os.environ.get("AUDIT_LOG", "/srv/audit/audit.jsonl"))
GITHUB_WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
ALLOWED_MERGE_ACTORS = os.environ.get("ALLOWED_MERGE_ACTORS", "").split(",")
INSTANCE_NAME = os.environ.get("INSTANCE_NAME", "default")
GATEWAY_CONTAINER = os.environ.get("GATEWAY_CONTAINER", f"clawfactory-{INSTANCE_NAME}-gateway")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")
CONTROLLER_API_TOKEN = os.environ.get("CONTROLLER_API_TOKEN", "")
SNAPSHOTS_DIR = Path(os.environ.get("SNAPSHOTS_DIR", "/srv/snapshots"))
AGE_KEY = Path(os.environ.get("AGE_KEY", "/srv/secrets/snapshot.key"))

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
            cwd=APPROVED_DIR,
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
            cwd=APPROVED_DIR,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def git_list_remote_branches() -> list:
    """List remote branches with their latest commit info."""
    branches = []
    try:
        # Get all remote branches
        result = subprocess.run(
            ["git", "branch", "-r", "--format=%(refname:short)|%(objectname:short)|%(committerdate:relative)|%(subject)"],
            cwd=APPROVED_DIR,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                if not line or "origin/HEAD" in line:
                    continue
                parts = line.split("|", 3)
                if len(parts) >= 4:
                    branch_name = parts[0].replace("origin/", "")
                    branches.append({
                        "name": branch_name,
                        "sha": parts[1],
                        "date": parts[2],
                        "message": parts[3][:60] + ("..." if len(parts[3]) > 60 else ""),
                        "is_main": branch_name == "main",
                        "is_proposal": branch_name.startswith("proposal/"),
                    })
    except Exception as e:
        audit_log("git_list_branches_error", {"error": str(e)})
    return branches


def git_get_branch_diff(branch: str) -> dict:
    """Get diff between a branch and main."""
    result = {"commits": [], "files": [], "diff": "", "ahead": 0, "behind": 0}
    try:
        # Get commit count ahead/behind
        count_result = subprocess.run(
            ["git", "rev-list", "--left-right", "--count", f"origin/main...origin/{branch}"],
            cwd=APPROVED_DIR,
            capture_output=True,
            text=True,
        )
        if count_result.returncode == 0:
            parts = count_result.stdout.strip().split()
            if len(parts) == 2:
                result["behind"] = int(parts[0])
                result["ahead"] = int(parts[1])

        # Get commits on branch not in main
        log_result = subprocess.run(
            ["git", "log", "--format=%h %s", f"origin/main..origin/{branch}"],
            cwd=APPROVED_DIR,
            capture_output=True,
            text=True,
        )
        if log_result.returncode == 0 and log_result.stdout.strip():
            result["commits"] = log_result.stdout.strip().split("\n")

        # Get changed files
        files_result = subprocess.run(
            ["git", "diff", "--name-only", f"origin/main...origin/{branch}"],
            cwd=APPROVED_DIR,
            capture_output=True,
            text=True,
        )
        if files_result.returncode == 0 and files_result.stdout.strip():
            result["files"] = files_result.stdout.strip().split("\n")

        # Get diff (truncated)
        diff_result = subprocess.run(
            ["git", "diff", f"origin/main...origin/{branch}"],
            cwd=APPROVED_DIR,
            capture_output=True,
            text=True,
        )
        if diff_result.returncode == 0:
            diff_content = diff_result.stdout
            if len(diff_content) > 50000:
                diff_content = diff_content[:50000] + "\n\n... (diff truncated) ..."
            result["diff"] = diff_content

    except Exception as e:
        audit_log("git_branch_diff_error", {"branch": branch, "error": str(e)})
    return result


def git_fetch_origin() -> bool:
    """Fetch latest from origin."""
    try:
        # Get GitHub token for authentication
        github_token = os.environ.get("GITHUB_TOKEN", "")
        remote_url = None

        if github_token:
            # Get and modify remote URL with token
            url_result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=APPROVED_DIR,
                capture_output=True,
                text=True,
            )
            remote_url = url_result.stdout.strip()

            if remote_url.startswith("https://github.com/"):
                auth_url = remote_url.replace(
                    "https://github.com/",
                    f"https://x-access-token:{github_token}@github.com/"
                )
                subprocess.run(
                    ["git", "remote", "set-url", "origin", auth_url],
                    cwd=APPROVED_DIR,
                    capture_output=True,
                )

        result = subprocess.run(
            ["git", "fetch", "origin"],
            cwd=APPROVED_DIR,
            capture_output=True,
            text=True,
            timeout=30,
        )

        # Restore original remote URL
        if github_token and remote_url:
            subprocess.run(
                ["git", "remote", "set-url", "origin", remote_url],
                cwd=APPROVED_DIR,
                capture_output=True,
            )

        return result.returncode == 0
    except Exception as e:
        audit_log("git_fetch_error", {"error": str(e)})
        return False


def git_get_remote_sha(fetch_first: bool = False) -> Optional[str]:
    """Get the SHA of origin/main."""
    try:
        if fetch_first:
            git_fetch_origin()
        result = subprocess.run(
            ["git", "rev-parse", "origin/main"],
            cwd=APPROVED_DIR,
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
    Promote a SHA to approved by checking it out.

    This is the ONLY way active config changes.
    """
    try:
        # Get GitHub token for authentication
        github_token = os.environ.get("GITHUB_TOKEN", "")
        remote_url = None

        if github_token:
            # Get and modify remote URL with token
            url_result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=APPROVED_DIR,
                capture_output=True,
                text=True,
            )
            remote_url = url_result.stdout.strip()

            if remote_url.startswith("https://github.com/"):
                auth_url = remote_url.replace(
                    "https://github.com/",
                    f"https://x-access-token:{github_token}@github.com/"
                )
                subprocess.run(
                    ["git", "remote", "set-url", "origin", auth_url],
                    cwd=APPROVED_DIR,
                    capture_output=True,
                )

        # Fetch first to ensure we have the SHA
        subprocess.run(
            ["git", "fetch", "origin"],
            cwd=APPROVED_DIR,
            capture_output=True,
            text=True,
            timeout=60,
        )

        # Restore original remote URL after fetch
        if github_token and remote_url:
            subprocess.run(
                ["git", "remote", "set-url", "origin", remote_url],
                cwd=APPROVED_DIR,
                capture_output=True,
            )

        # Checkout the SHA
        result = subprocess.run(
            ["git", "checkout", sha],
            cwd=APPROVED_DIR,
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


def git_get_pending_changes() -> dict:
    """Get commits and changed files between local and origin/main."""
    result = {
        "commits": [],
        "files": [],
        "diff_stat": "",
        "diff": "",
    }

    try:
        # Get full commit log with messages between HEAD and origin/main
        log_result = subprocess.run(
            ["git", "log", "--format=%h %s%n%b%n---", "HEAD..origin/main"],
            cwd=APPROVED_DIR,
            capture_output=True,
            text=True,
        )
        if log_result.returncode == 0 and log_result.stdout.strip():
            # Parse commits - split by --- separator
            raw_commits = log_result.stdout.strip().split("\n---\n")
            result["commits"] = [c.strip() for c in raw_commits if c.strip()]

        # Get changed files
        files_result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD..origin/main"],
            cwd=APPROVED_DIR,
            capture_output=True,
            text=True,
        )
        if files_result.returncode == 0 and files_result.stdout.strip():
            result["files"] = files_result.stdout.strip().split("\n")

        # Get diff stat
        stat_result = subprocess.run(
            ["git", "diff", "--stat", "HEAD..origin/main"],
            cwd=APPROVED_DIR,
            capture_output=True,
            text=True,
        )
        if stat_result.returncode == 0:
            result["diff_stat"] = stat_result.stdout.strip()

        # Get actual diff (truncate if too large)
        diff_result = subprocess.run(
            ["git", "diff", "HEAD..origin/main"],
            cwd=APPROVED_DIR,
            capture_output=True,
            text=True,
        )
        if diff_result.returncode == 0:
            diff_content = diff_result.stdout
            # Truncate if over 50KB
            if len(diff_content) > 50000:
                diff_content = diff_content[:50000] + "\n\n... (diff truncated, too large to display) ..."
            result["diff"] = diff_content

    except Exception as e:
        audit_log("git_pending_changes_error", {"error": str(e)})

    return result


def format_diff_html(diff_text: str) -> str:
    """Format diff text with syntax highlighting HTML."""
    if not diff_text:
        return "No diff"

    import html
    lines = diff_text.split("\n")
    formatted_lines = []

    for line in lines:
        escaped = html.escape(line)
        if line.startswith("diff --git"):
            formatted_lines.append(f'<span class="diff-header">{escaped}</span>')
        elif line.startswith("---") or line.startswith("+++"):
            formatted_lines.append(f'<span class="diff-file">{escaped}</span>')
        elif line.startswith("@@"):
            formatted_lines.append(f'<span class="diff-hunk">{escaped}</span>')
        elif line.startswith("+"):
            formatted_lines.append(f'<span class="diff-add">{escaped}</span>')
        elif line.startswith("-"):
            formatted_lines.append(f'<span class="diff-del">{escaped}</span>')
        else:
            formatted_lines.append(f'<span class="diff-context">{escaped}</span>')

    return "\n".join(formatted_lines)


def promote_main() -> bool:
    """Pull latest main branch to approved."""
    try:
        # Get GitHub token for authentication
        github_token = os.environ.get("GITHUB_TOKEN", "")
        remote_url = None

        if github_token:
            # Get and modify remote URL with token
            url_result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=APPROVED_DIR,
                capture_output=True,
                text=True,
            )
            remote_url = url_result.stdout.strip()

            if remote_url.startswith("https://github.com/"):
                auth_url = remote_url.replace(
                    "https://github.com/",
                    f"https://x-access-token:{github_token}@github.com/"
                )
                subprocess.run(
                    ["git", "remote", "set-url", "origin", auth_url],
                    cwd=APPROVED_DIR,
                    capture_output=True,
                )

        result = subprocess.run(
            ["git", "pull", "origin", "main"],
            cwd=APPROVED_DIR,
            capture_output=True,
            text=True,
            timeout=60,
        )

        # Restore original remote URL
        if github_token and remote_url:
            subprocess.run(
                ["git", "remote", "set-url", "origin", remote_url],
                cwd=APPROVED_DIR,
                capture_output=True,
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

    # Fetch latest from remote (uses GITHUB_TOKEN for auth)
    git_fetch_origin()

    # Get recent commits on origin/main
    try:
        result = subprocess.run(
            ["git", "log", "origin/main", "--oneline", "-10"],
            cwd=APPROVED_DIR,
            capture_output=True,
            text=True,
        )
        commits = result.stdout.strip() if result.returncode == 0 else "Error reading commits"
    except Exception as e:
        commits = f"Error: {e}"

    current_sha = git_get_main_sha() or "unknown"
    # Fetch from origin to get latest remote SHA
    remote_sha = git_get_remote_sha(fetch_first=True) or "unknown"
    needs_update = current_sha != remote_sha and remote_sha != "unknown"

    status_msg = "Up to date" if not needs_update else f"⚠️ Update available"
    status_class = "success" if not needs_update else "warning"

    # Get pending changes if update available
    pending_changes = git_get_pending_changes() if needs_update else {"commits": [], "files": [], "diff_stat": ""}

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
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem;">
                        <div>
                            <span style="color: #888;">Local:</span> <span class="sha">{current_sha[:8]}</span>
                            <span style="margin: 0 0.5rem; color: #444;">→</span>
                            <span style="color: #888;">Remote:</span> <span class="sha">{remote_sha[:8]}</span>
                        </div>
                    </div>
                    {"<div style='background: #ff9800; color: #000; padding: 0.75rem; border-radius: 4px; margin-bottom: 1rem; font-weight: bold;'>🔄 New version available on GitHub!</div>" if needs_update else ""}
                    {f'''<details style="margin-bottom: 1rem; background: #1a1a1a; border: 1px solid #ff9800; border-radius: 4px; padding: 0.5rem;">
                        <summary style="cursor: pointer; color: #ff9800; font-weight: bold;">📋 View {len(pending_changes["commits"])} pending commit(s) and {len(pending_changes["files"])} file(s)</summary>
                        <div style="margin-top: 0.75rem; padding-top: 0.75rem; border-top: 1px solid #333;">
                            <div style="margin-bottom: 0.75rem;">
                                <strong style="color: #4CAF50;">Commits:</strong>
                                <pre style="margin: 0.5rem 0; font-size: 0.8rem; color: #ccc; background: #252525; padding: 0.5rem; border-radius: 3px; overflow-x: auto; white-space: pre-wrap;">{chr(10).join(pending_changes["commits"]) or "No commits"}</pre>
                            </div>
                            <div style="margin-bottom: 0.75rem;">
                                <strong style="color: #2196F3;">Changed Files:</strong>
                                <pre style="margin: 0.5rem 0; font-size: 0.8rem; color: #ccc; background: #252525; padding: 0.5rem; border-radius: 3px; overflow-x: auto;">{chr(10).join(pending_changes["files"]) or "No files"}</pre>
                            </div>
                            <details style="margin-bottom: 0.75rem;">
                                <summary style="cursor: pointer; color: #9c27b0; font-size: 0.9rem;">📄 View Full Diff</summary>
                                <pre class="diff-view" style="margin: 0.5rem 0; background: #1e1e1e; padding: 0.5rem; border-radius: 3px; overflow-x: auto; max-height: 400px; overflow-y: auto;">{format_diff_html(pending_changes.get("diff", ""))}</pre>
                            </details>
                            <div>
                                <strong style="color: #888;">Stats:</strong>
                                <pre style="margin: 0.5rem 0; font-size: 0.75rem; color: #888; background: #252525; padding: 0.5rem; border-radius: 3px; overflow-x: auto;">{pending_changes["diff_stat"] or "No changes"}</pre>
                            </div>
                        </div>
                    </details>''' if needs_update else ""}
                    <div style="display: flex; gap: 0.5rem; flex-wrap: wrap;">
                        <button onclick="mergeAllAndDeploy()" {"style='background: #ff9800; border-color: #ff9800; animation: pulse 2s infinite;'" if needs_update else ""}>Merge All & Deploy</button>
                        <button onclick="promoteMain()" class="secondary">Deploy Main Only</button>
                    </div>
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

                <h2>Branches</h2>
                <div class="card">
                    <button onclick="fetchBranches()">Refresh Branches</button>
                    <div id="branches-list" style="margin-top: 0.5rem;"></div>
                    <div id="branch-diff-view" style="display: none; margin-top: 1rem; border-top: 1px solid #333; padding-top: 1rem;">
                        <h3 style="color: #2196F3; margin: 0 0 0.5rem 0;">Branch: <span id="branch-diff-name"></span></h3>
                        <div id="branch-diff-content"></div>
                    </div>
                </div>

                <h2>Recent Commits</h2>
                <pre>{commits}</pre>
            </div>

            <div>
                <h2>System Status</h2>
                <div class="card">
                    <button onclick="fetchHealth()">Health Check</button>
                    <button onclick="fetchStatus()" class="secondary">Full Status</button>
                    <button onclick="restartGateway()" class="danger">Restart Gateway</button>
                    <div id="status-result" class="result"></div>
                    <button onclick="runSecurityAudit()" class="secondary" style="margin-top: 0.5rem;">Security Audit</button>
                    <button onclick="runSecurityAudit(true)" class="secondary">Deep Audit</button>
                    <div id="security-result" class="result"></div>
                </div>

                <h2>Snapshots</h2>
                <div class="card">
                    <button onclick="createSnapshot()">Create Snapshot</button>
                    <button onclick="fetchSnapshots()" class="secondary">Refresh List</button>
                    <div id="snapshot-result" class="result"></div>
                    <div id="snapshot-list" style="margin-top: 0.5rem;"></div>
                </div>

                <h2>Audit Log</h2>
                <div class="card">
                    <button onclick="fetchAudit()">Refresh</button>
                    <button onclick="fetchAudit(100)" class="secondary">Last 100</button>
                    <pre id="audit-log">Click Refresh to load audit log...</pre>
                </div>
            </div>
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

            // Initialize CodeMirror editor
            let configEditor;
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

            // Branch management
            async function fetchBranches() {{
                const list = document.getElementById('branches-list');
                list.innerHTML = '<p style="color: #888;">Loading branches...</p>';
                try {{
                    const resp = await fetch(basePath + '/branches');
                    const data = await resp.json();
                    if (data.error) {{
                        list.innerHTML = `<p style="color: #ef9a9a;">${{data.error}}</p>`;
                        return;
                    }}
                    if (!data.branches || data.branches.length === 0) {{
                        list.innerHTML = '<p style="color: #888;">No branches found.</p>';
                        return;
                    }}

                    let html = '';
                    // Sort: main first, then proposals, then others
                    const sorted = data.branches.sort((a, b) => {{
                        if (a.is_main) return -1;
                        if (b.is_main) return 1;
                        if (a.is_proposal && !b.is_proposal) return -1;
                        if (!a.is_proposal && b.is_proposal) return 1;
                        return a.name.localeCompare(b.name);
                    }});

                    for (const branch of sorted) {{
                        const color = branch.is_main ? '#4CAF50' : branch.is_proposal ? '#ff9800' : '#2196F3';
                        const badge = branch.is_main ? '(active)' : branch.is_proposal ? '(proposal)' : '';
                        html += `
                            <div class="pending-item">
                                <div class="pending-item-info">
                                    <span style="color: ${{color}}; font-weight: bold;">${{branch.name}}</span>
                                    <span style="color: #666; font-size: 0.8rem;">${{badge}}</span><br>
                                    <span style="color: #888; font-size: 0.8rem;">
                                        <span class="sha">${{branch.sha}}</span> · ${{branch.date}} · ${{branch.message}}
                                    </span>
                                </div>
                                <div class="pending-item-actions">
                                    ${{!branch.is_main ? `<button class="small secondary" onclick="viewBranchDiff('${{branch.name}}')">View Diff</button>` : ''}}
                                    ${{branch.is_proposal ? `<button class="small" onclick="mergeBranch('${{branch.name}}')">Merge</button>` : ''}}
                                </div>
                            </div>
                        `;
                    }}
                    list.innerHTML = html;
                }} catch(e) {{
                    list.innerHTML = `<p style="color: #ef9a9a;">Error: ${{e.message}}</p>`;
                }}
            }}

            async function viewBranchDiff(branch) {{
                const diffView = document.getElementById('branch-diff-view');
                const diffName = document.getElementById('branch-diff-name');
                const diffContent = document.getElementById('branch-diff-content');

                diffView.style.display = 'block';
                diffName.textContent = branch;
                diffContent.innerHTML = '<p style="color: #888;">Loading diff...</p>';

                try {{
                    const resp = await fetch(basePath + '/branches/' + encodeURIComponent(branch) + '/diff');
                    const data = await resp.json();

                    if (data.error) {{
                        diffContent.innerHTML = `<p style="color: #ef9a9a;">${{data.error}}</p>`;
                        return;
                    }}

                    let html = `
                        <p style="color: #888; font-size: 0.85rem;">
                            <span style="color: #4CAF50;">+${{data.ahead}} ahead</span> /
                            <span style="color: #ef9a9a;">-${{data.behind}} behind</span> main
                        </p>
                    `;

                    if (data.commits && data.commits.length > 0) {{
                        html += `<details open style="margin: 0.5rem 0;">
                            <summary style="cursor: pointer; color: #4CAF50;">Commits (${{data.commits.length}})</summary>
                            <pre style="margin: 0.5rem 0; font-size: 0.8rem; background: #252525; padding: 0.5rem; border-radius: 3px;">${{data.commits.join('\\n')}}</pre>
                        </details>`;
                    }}

                    if (data.files && data.files.length > 0) {{
                        html += `<details style="margin: 0.5rem 0;">
                            <summary style="cursor: pointer; color: #2196F3;">Changed Files (${{data.files.length}})</summary>
                            <pre style="margin: 0.5rem 0; font-size: 0.8rem; background: #252525; padding: 0.5rem; border-radius: 3px;">${{data.files.join('\\n')}}</pre>
                        </details>`;
                    }}

                    if (data.diff) {{
                        html += `<details style="margin: 0.5rem 0;">
                            <summary style="cursor: pointer; color: #9c27b0;">Full Diff</summary>
                            <pre class="diff-view" style="margin: 0.5rem 0; font-size: 0.75rem; background: #1e1e1e; padding: 0.5rem; border-radius: 3px; max-height: 400px; overflow: auto;">${{formatDiffHtml(data.diff)}}</pre>
                        </details>`;
                    }}

                    html += `<button class="small" onclick="mergeBranch('${{branch}}')" style="margin-top: 0.5rem;">Merge to Main</button>`;
                    html += `<button class="small secondary" onclick="document.getElementById('branch-diff-view').style.display='none'" style="margin-left: 0.5rem;">Close</button>`;

                    diffContent.innerHTML = html;
                }} catch(e) {{
                    diffContent.innerHTML = `<p style="color: #ef9a9a;">Error: ${{e.message}}</p>`;
                }}
            }}

            function formatDiffHtml(diff) {{
                if (!diff) return 'No diff';
                return diff.split('\\n').map(line => {{
                    const escaped = line.replace(/</g, '&lt;').replace(/>/g, '&gt;');
                    if (line.startsWith('diff --git')) return `<span class="diff-header">${{escaped}}</span>`;
                    if (line.startsWith('---') || line.startsWith('+++')) return `<span class="diff-file">${{escaped}}</span>`;
                    if (line.startsWith('@@')) return `<span class="diff-hunk">${{escaped}}</span>`;
                    if (line.startsWith('+')) return `<span class="diff-add">${{escaped}}</span>`;
                    if (line.startsWith('-')) return `<span class="diff-del">${{escaped}}</span>`;
                    return `<span class="diff-context">${{escaped}}</span>`;
                }}).join('\\n');
            }}

            async function mergeBranch(branch) {{
                if (!confirm(`Merge branch "${{branch}}" into main?`)) return;

                const diffContent = document.getElementById('branch-diff-content');
                if (diffContent) {{
                    diffContent.innerHTML = '<p style="color: #888;">Merging...</p>';
                }}

                try {{
                    const resp = await fetch(basePath + '/branches/' + encodeURIComponent(branch) + '/merge', {{ method: 'POST' }});
                    const data = await resp.json();

                    if (data.error) {{
                        if (diffContent) {{
                            diffContent.innerHTML = `<p style="color: #ef9a9a;">Error: ${{data.error}}</p>`;
                        }} else {{
                            alert('Error: ' + data.error);
                        }}
                        return;
                    }}

                    // Refresh branches and hide diff view
                    document.getElementById('branch-diff-view').style.display = 'none';
                    fetchBranches();

                    // Show success
                    alert('Branch merged successfully! Refresh the page to see updates.');
                }} catch(e) {{
                    if (diffContent) {{
                        diffContent.innerHTML = `<p style="color: #ef9a9a;">Error: ${{e.message}}</p>`;
                    }} else {{
                        alert('Error: ' + e.message);
                    }}
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

            // Gateway restart
            async function restartGateway() {{
                if (!confirm('Restart the gateway? This will briefly interrupt the bot.')) return;
                const result = document.getElementById('status-result');
                result.style.display = 'block';
                result.className = 'result';
                result.textContent = 'Restarting gateway...';
                try {{
                    const resp = await fetch(basePath + '/gateway/restart', {{ method: 'POST' }});
                    const data = await resp.json();
                    if (data.error) {{
                        result.className = 'result error';
                        result.textContent = data.error;
                    }} else {{
                        result.textContent = 'Gateway restarting... Status: ' + (data.status || 'ok');
                    }}
                }} catch(e) {{
                    result.className = 'result error';
                    result.textContent = 'Error: ' + e.message;
                }}
            }}

            async function mergeAllAndDeploy() {{
                const result = document.getElementById('promote-result');
                result.style.display = 'block';
                result.className = 'result';
                result.textContent = 'Merging all proposal branches...';

                try {{
                    // First merge all proposal branches
                    const mergeResp = await fetch(basePath + '/branches/merge-all', {{ method: 'POST' }});
                    const mergeData = await mergeResp.json();

                    if (mergeData.error) {{
                        result.className = 'result error';
                        result.textContent = 'Merge failed: ' + mergeData.error;
                        return;
                    }}

                    let statusText = '';
                    if (mergeData.merged && mergeData.merged.length > 0) {{
                        statusText += `✅ Merged: ${{mergeData.merged.join(', ')}}\\n`;
                    }}
                    if (mergeData.errors && mergeData.errors.length > 0) {{
                        statusText += `⚠️ Errors: ${{mergeData.errors.map(e => e.branch).join(', ')}}\\n`;
                    }}
                    if (mergeData.status === 'no_branches') {{
                        statusText = 'No proposal branches to merge. ';
                    }}

                    result.textContent = statusText + 'Now deploying main...';

                    // Then deploy main
                    const deployResp = await fetch(basePath + '/controller/promote-main', {{ method: 'POST' }});
                    if (deployResp.ok) {{
                        result.innerHTML = statusText.replace(/\\n/g, '<br>') + '<br><span style="color: #4CAF50;">✅ Deployed! Restarting gateway...</span>';
                        setTimeout(() => window.location.reload(), 3000);
                    }} else {{
                        const text = await deployResp.text();
                        result.className = 'result error';
                        result.innerHTML = statusText.replace(/\\n/g, '<br>') + '<br>Deploy failed: ' + (text || deployResp.statusText);
                    }}
                }} catch(e) {{
                    result.className = 'result error';
                    result.textContent = 'Error: ' + e.message;
                }}
            }}

            async function promoteMain() {{
                const result = document.getElementById('promote-result');
                result.style.display = 'block';
                result.className = 'result';
                result.textContent = 'Deploying main branch...';
                try {{
                    const resp = await fetch(basePath + '/controller/promote-main', {{ method: 'POST' }});
                    if (resp.ok) {{
                        result.innerHTML = '<span style="color: #4CAF50;">✅ Deployed! Restarting gateway...</span>';
                        setTimeout(() => window.location.reload(), 3000);
                    }} else {{
                        const text = await resp.text();
                        result.className = 'result error';
                        result.textContent = 'Failed: ' + (text || resp.statusText);
                    }}
                }} catch(e) {{
                    result.className = 'result error';
                    result.textContent = 'Error: ' + e.message;
                }}
            }}

            // Snapshots
            async function createSnapshot() {{
                const result = document.getElementById('snapshot-result');
                result.style.display = 'block';
                result.className = 'result';
                result.textContent = 'Creating snapshot...';
                try {{
                    const resp = await fetch(basePath + '/snapshot', {{ method: 'POST' }});
                    const data = await resp.json();
                    if (data.error) {{
                        result.className = 'result error';
                        result.textContent = data.error;
                    }} else {{
                        result.textContent = 'Created: ' + data.name + ' (' + formatSize(data.size) + ')';
                        fetchSnapshots();
                    }}
                }} catch(e) {{
                    result.className = 'result error';
                    result.textContent = 'Error: ' + e.message;
                }}
            }}

            async function fetchSnapshots() {{
                const list = document.getElementById('snapshot-list');
                list.innerHTML = '<p style="color: #888;">Loading...</p>';
                try {{
                    const resp = await fetch(basePath + '/snapshot');
                    const data = await resp.json();
                    if (!data.snapshots || data.snapshots.length === 0) {{
                        list.innerHTML = '<p style="color: #888; font-size: 0.85rem;">No snapshots yet.</p>';
                        return;
                    }}
                    let html = '<div style="max-height: 200px; overflow-y: auto;">';
                    data.snapshots.forEach(s => {{
                        const latest = s.latest ? ' <span style="color: #4CAF50;">(latest)</span>' : '';
                        html += `<div style="padding: 0.3rem 0; border-bottom: 1px solid #333; font-size: 0.85rem;">
                            <code>${{s.name}}</code>${{latest}}<br>
                            <small style="color: #888;">${{formatSize(s.size)}} · ${{s.created}}</small>
                        </div>`;
                    }});
                    html += '</div>';
                    list.innerHTML = html;
                }} catch(e) {{
                    list.innerHTML = `<p class="error" style="color: #ef9a9a;">Error: ${{e.message}}</p>`;
                }}
            }}

            function formatSize(bytes) {{
                if (bytes < 1024) return bytes + ' B';
                if (bytes < 1024*1024) return (bytes/1024).toFixed(1) + ' KB';
                return (bytes/(1024*1024)).toFixed(1) + ' MB';
            }}

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
                    if (data.error) {{
                        result.className = 'result error';
                        result.textContent = data.error;
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
                    if (data.error) {{
                        result.className = 'result error';
                        result.textContent = data.error;
                    }} else {{
                        result.textContent = 'Config saved. Gateway restarting...';
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
                result.textContent = 'Validating config against OpenClaw schema...';

                try {{
                    const resp = await fetch(basePath + '/gateway/config/validate');
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
                        baseUrl: "http://host.docker.internal:11434/v1",
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

            // Load data on page load
            fetchAudit();
            fetchSnapshots();
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
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Handle manual promotion of specific SHA from UI."""
    # Check authentication
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
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


@app.post("/promote-main")
@app.post("/controller/promote-main")
async def promote_main_endpoint(
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Pull latest main and restart gateway."""
    # Check authentication
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
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
        "approved_sha": current_sha,
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
# Branches
# ============================================================

@app.get("/branches")
@app.get("/controller/branches")
async def list_branches(
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """List all remote branches."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Fetch latest first
    git_fetch_origin()
    branches = git_list_remote_branches()
    return {"branches": branches}


@app.get("/branches/{branch:path}/diff")
@app.get("/controller/branches/{branch:path}/diff")
async def get_branch_diff(
    branch: str,
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Get diff between a branch and main."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    diff = git_get_branch_diff(branch)
    return {"branch": branch, **diff}


@app.post("/branches/{branch:path}/merge")
@app.post("/controller/branches/{branch:path}/merge")
async def merge_branch(
    branch: str,
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Merge a branch into main via GitHub API."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    audit_log("branch_merge_requested", {"branch": branch})

    # Use gh CLI to create and merge PR
    try:
        # Create PR
        pr_result = subprocess.run(
            ["gh", "pr", "create", "--repo", GITHUB_REPO, "--base", "main",
             "--head", branch, "--title", f"Merge {branch}", "--body", "Merged via ClawFactory controller"],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if pr_result.returncode != 0 and "already exists" not in pr_result.stderr:
            # Try to find existing PR
            list_result = subprocess.run(
                ["gh", "pr", "list", "--repo", GITHUB_REPO, "--head", branch, "--json", "number"],
                capture_output=True,
                text=True,
            )
            if list_result.returncode != 0:
                return {"error": f"Failed to create PR: {pr_result.stderr}"}

        # Merge the PR
        merge_result = subprocess.run(
            ["gh", "pr", "merge", "--repo", GITHUB_REPO, "--head", branch, "--squash", "--delete-branch"],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if merge_result.returncode != 0:
            return {"error": f"Failed to merge: {merge_result.stderr}"}

        audit_log("branch_merged", {"branch": branch})
        return {"status": "merged", "branch": branch}

    except Exception as e:
        audit_log("branch_merge_error", {"branch": branch, "error": str(e)})
        return {"error": str(e)}


@app.post("/branches/merge-all")
@app.post("/controller/branches/merge-all")
async def merge_all_branches(
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Merge all proposal branches into main."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Fetch and get proposal branches
    git_fetch_origin()
    branches = git_list_remote_branches()
    proposal_branches = [b for b in branches if b["is_proposal"]]

    if not proposal_branches:
        return {"status": "no_branches", "merged": [], "errors": []}

    audit_log("merge_all_requested", {"branches": [b["name"] for b in proposal_branches]})

    merged = []
    errors = []

    for branch in proposal_branches:
        branch_name = branch["name"]
        try:
            # Create PR
            pr_result = subprocess.run(
                ["gh", "pr", "create", "--repo", GITHUB_REPO, "--base", "main",
                 "--head", branch_name, "--title", f"Merge {branch_name}",
                 "--body", "Merged via ClawFactory controller"],
                capture_output=True,
                text=True,
                timeout=30,
            )

            # Merge the PR (even if PR creation said "already exists")
            merge_result = subprocess.run(
                ["gh", "pr", "merge", "--repo", GITHUB_REPO, "--head", branch_name,
                 "--squash", "--delete-branch"],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if merge_result.returncode == 0:
                merged.append(branch_name)
                audit_log("branch_merged", {"branch": branch_name})
            else:
                errors.append({"branch": branch_name, "error": merge_result.stderr})

        except Exception as e:
            errors.append({"branch": branch_name, "error": str(e)})

    return {"status": "completed", "merged": merged, "errors": errors}


# ============================================================
# Memory Backup
# ============================================================

def backup_memory() -> dict:
    """
    List memory files in approved repo ready for commit.

    Memory persists directly in approved/workspace/memory/ via volume mount.
    """
    memory_dir = APPROVED_DIR / "workspace" / "memory"
    long_term = APPROVED_DIR / "workspace" / "MEMORY.md"

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
            cwd=APPROVED_DIR,
            capture_output=True,
        )

        # Check if there are changes to commit
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=APPROVED_DIR,
        )
        if result.returncode == 0:
            # No changes
            return True

        # Commit
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        result = subprocess.run(
            ["git", "commit", "-m", f"Backup agent memory - {timestamp}"],
            cwd=APPROVED_DIR,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            audit_log("memory_commit_error", {"stderr": result.stderr})
            return False

        # Push (with GitHub token if available)
        github_token = os.environ.get("GITHUB_TOKEN", "")
        remote_url = None

        if github_token:
            # Get and modify remote URL with token
            url_result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=APPROVED_DIR,
                capture_output=True,
                text=True,
            )
            remote_url = url_result.stdout.strip()

            if remote_url.startswith("https://github.com/"):
                auth_url = remote_url.replace(
                    "https://github.com/",
                    f"https://x-access-token:{github_token}@github.com/"
                )
                subprocess.run(
                    ["git", "remote", "set-url", "origin", auth_url],
                    cwd=APPROVED_DIR,
                    capture_output=True,
                )

        result = subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=APPROVED_DIR,
            capture_output=True,
            text=True,
            timeout=60,
        )

        # Restore original remote URL
        if github_token and remote_url:
            subprocess.run(
                ["git", "remote", "set-url", "origin", remote_url],
                cwd=APPROVED_DIR,
                capture_output=True,
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

    # Memory markdown in approved repo
    memory_dir = APPROVED_DIR / "workspace" / "memory"
    long_term = APPROVED_DIR / "workspace" / "MEMORY.md"

    files = []
    if memory_dir.exists():
        files.extend([f.name for f in memory_dir.glob("*.md")])
    if long_term.exists():
        files.append("MEMORY.md")

    # Embeddings database in state
    embeddings_db = OPENCLAW_HOME / "memory" / "main.sqlite"

    return {
        "memory_files": files,
        "embeddings_db": str(embeddings_db) if embeddings_db.exists() else None,
        "embeddings_size": embeddings_db.stat().st_size if embeddings_db.exists() else 0,
    }


# ============================================================
# Encrypted Snapshots
# ============================================================

def create_snapshot() -> dict:
    """Create an encrypted snapshot of bot state."""
    if not AGE_KEY.exists():
        return {"error": "No encryption key found. Run: ./clawfactory.sh snapshot keygen"}

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
    snapshot_name = f"snapshot-{timestamp}.tar.age"
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

    for f in sorted(SNAPSHOTS_DIR.glob("snapshot-*.tar.age"), reverse=True):
        snapshots.append({
            "name": f.name,
            "size": f.stat().st_size,
            "latest": f.name == latest_target,
            "created": f.name.replace("snapshot-", "").replace(".tar.age", ""),
        })

    return snapshots


@app.post("/snapshot")
@app.post("/controller/snapshot")
async def snapshot_create(
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Create an encrypted snapshot of bot state."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    audit_log("snapshot_requested", {})
    result = create_snapshot()

    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])

    return result


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

    return {"snapshots": list_snapshots()}


# ============================================================
# Gateway Config Editor
# ============================================================

GATEWAY_CONFIG_PATH = OPENCLAW_HOME / "openclaw.json"


def fetch_ollama_models() -> list:
    """Fetch available models from Ollama with full details."""
    import urllib.request
    import urllib.error

    # Try common Ollama endpoints
    base_urls = [
        "http://host.docker.internal:11434",
        "http://localhost:11434",
        "http://ollama:11434",
    ]

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

    audit_log("gateway_config_save", {"keys": list(config.keys())})

    try:
        # Stop the gateway first
        client = docker.from_env()
        gateway = client.containers.get(GATEWAY_CONTAINER)
        gateway.stop(timeout=30)
        audit_log("gateway_stopped_for_config", {})

        # Write the config
        with open(GATEWAY_CONFIG_PATH, "w") as f:
            json.dump(config, f, indent=2)
        audit_log("gateway_config_written", {})

        # Start the gateway
        gateway.start()
        audit_log("gateway_started_after_config", {})

        return {"status": "saved", "restarted": True}
    except Exception as e:
        audit_log("gateway_config_error", {"error": str(e)})
        return {"error": str(e)}


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


@app.get("/gateway/config/validate")
@app.get("/controller/gateway/config/validate")
async def gateway_config_validate(
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Validate gateway config using openclaw doctor."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Run openclaw doctor --json in the gateway container
    success, output = run_gateway_command(["node", "dist/index.js", "doctor", "--json"], timeout=30)

    # Try to parse JSON output
    try:
        data = json.loads(output)
        return {
            "valid": data.get("valid", success),
            "issues": data.get("issues", []),
            "raw": None,
        }
    except json.JSONDecodeError:
        # Doctor may output non-JSON on error - parse the text
        issues = []
        if "Unrecognized key" in output or "Unknown config" in output:
            # Extract the problematic keys
            import re
            key_matches = re.findall(r'["\']?([\w.]+)["\']?\s*:\s*Unrecognized key', output)
            unknown_matches = re.findall(r'- ([\w.]+)', output)
            for key in key_matches + unknown_matches:
                issues.append({
                    "severity": "error",
                    "message": f"Unknown config key: {key}",
                    "key": key,
                })

        if "Config invalid" in output:
            # Try to extract the problem description
            problem_match = re.search(r'Problem:\s*\n\s*-\s*(.+)', output)
            if problem_match:
                issues.append({
                    "severity": "error",
                    "message": problem_match.group(1).strip(),
                })

        return {
            "valid": success and not issues,
            "issues": issues,
            "raw": output[:1000] if not issues else None,
        }


# ============================================================
# Internal Endpoints (no auth - Docker network only)
# ============================================================
# These endpoints are NOT exposed via the proxy, only accessible
# from within the Docker network (gateway container).

@app.post("/internal/snapshot")
async def internal_snapshot_create():
    """Create snapshot - internal endpoint (no auth)."""
    audit_log("snapshot_requested", {"source": "internal"})
    result = create_snapshot()
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    return result


@app.get("/internal/snapshot")
async def internal_snapshot_list():
    """List snapshots - internal endpoint (no auth)."""
    return {"snapshots": list_snapshots()}


@app.post("/internal/memory/backup")
async def internal_memory_backup():
    """Backup memory - internal endpoint (no auth)."""
    audit_log("memory_backup_requested", {"source": "internal"})
    result = backup_memory()
    if not result["files"]:
        return {"status": "no_changes", "files": []}
    if not commit_and_push_memory():
        raise HTTPException(status_code=500, detail="Failed to push memory backup")
    audit_log("memory_backup_success", {"files": result["files"]})
    return {"status": "backed_up", "files": result["files"]}


@app.get("/internal/memory/status")
async def internal_memory_status():
    """Get memory status - internal endpoint (no auth)."""
    memory_dir = APPROVED_DIR / "workspace" / "memory"
    long_term = APPROVED_DIR / "workspace" / "MEMORY.md"
    files = []
    if memory_dir.exists():
        files.extend([f.name for f in memory_dir.glob("*.md")])
    if long_term.exists():
        files.append("MEMORY.md")
    embeddings_db = OPENCLAW_HOME / "memory" / "main.sqlite"
    return {
        "memory_files": files,
        "embeddings_db": str(embeddings_db) if embeddings_db.exists() else None,
        "embeddings_size": embeddings_db.stat().st_size if embeddings_db.exists() else 0,
    }


class GitPushRequest(BaseModel):
    branch: str


@app.post("/internal/git/push")
async def internal_git_push(request: GitPushRequest):
    """Push a branch to origin - internal endpoint (no auth).

    Only allows pushing proposal/* branches for security.
    The bot commits locally, then calls this to push.
    """
    branch = request.branch

    # Security: Only allow proposal branches
    if not branch.startswith("proposal/"):
        audit_log("git_push_rejected", {"branch": branch, "reason": "not a proposal branch"})
        raise HTTPException(
            status_code=400,
            detail="Only proposal/* branches can be pushed. Create a branch like 'proposal/my-change'"
        )

    # Validate branch name (no shell injection)
    import re
    if not re.match(r'^proposal/[a-zA-Z0-9_\-/]+$', branch):
        audit_log("git_push_rejected", {"branch": branch, "reason": "invalid branch name"})
        raise HTTPException(status_code=400, detail="Invalid branch name")

    audit_log("git_push_requested", {"branch": branch})

    try:
        # Check if branch exists locally
        result = subprocess.run(
            ["git", "rev-parse", "--verify", branch],
            cwd=APPROVED_DIR,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return {"error": f"Branch '{branch}' does not exist locally"}

        # Get GitHub token from environment for authentication
        github_token = os.environ.get("GITHUB_TOKEN", "")

        # Build environment with credentials if available
        push_env = os.environ.copy()
        if github_token:
            # Use token for HTTPS authentication
            push_env["GIT_ASKPASS"] = "echo"
            push_env["GIT_USERNAME"] = "x-access-token"
            push_env["GIT_PASSWORD"] = github_token

            # Configure git to use the token via credential helper
            subprocess.run(
                ["git", "config", "credential.helper", ""],
                cwd=APPROVED_DIR,
                capture_output=True,
            )

            # Get remote URL and inject token
            remote_result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=APPROVED_DIR,
                capture_output=True,
                text=True,
            )
            remote_url = remote_result.stdout.strip()

            # Convert https://github.com/... to https://x-access-token:TOKEN@github.com/...
            if remote_url.startswith("https://github.com/"):
                auth_url = remote_url.replace(
                    "https://github.com/",
                    f"https://x-access-token:{github_token}@github.com/"
                )
                # Temporarily set the authenticated URL
                subprocess.run(
                    ["git", "remote", "set-url", "origin", auth_url],
                    cwd=APPROVED_DIR,
                    capture_output=True,
                )

        # Push the branch
        result = subprocess.run(
            ["git", "push", "-u", "origin", branch],
            cwd=APPROVED_DIR,
            capture_output=True,
            text=True,
            timeout=60,
            env=push_env,
        )

        # Restore original remote URL (remove token from URL)
        if github_token and remote_url:
            subprocess.run(
                ["git", "remote", "set-url", "origin", remote_url],
                cwd=APPROVED_DIR,
                capture_output=True,
            )

        if result.returncode != 0:
            audit_log("git_push_failed", {"branch": branch, "stderr": result.stderr})
            return {
                "error": f"Push failed: {result.stderr}",
                "hint": "Check if GITHUB_TOKEN is configured in controller.env"
            }

        audit_log("git_push_success", {"branch": branch})
        return {
            "status": "pushed",
            "branch": branch,
            "output": result.stdout or result.stderr,
        }

    except subprocess.TimeoutExpired:
        audit_log("git_push_timeout", {"branch": branch})
        return {"error": "Push timed out"}
    except Exception as e:
        audit_log("git_push_error", {"branch": branch, "error": str(e)})
        return {"error": str(e)}


@app.get("/internal/git/status")
async def internal_git_status():
    """Get git status - internal endpoint (no auth)."""
    try:
        # Get current branch
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=APPROVED_DIR,
            capture_output=True,
            text=True,
        )
        current_branch = result.stdout.strip()

        # Get status
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=APPROVED_DIR,
            capture_output=True,
            text=True,
        )
        changes = result.stdout.strip().split("\n") if result.stdout.strip() else []

        # List local branches
        result = subprocess.run(
            ["git", "branch", "--list"],
            cwd=APPROVED_DIR,
            capture_output=True,
            text=True,
        )
        branches = [b.strip().lstrip("* ") for b in result.stdout.strip().split("\n") if b.strip()]

        return {
            "current_branch": current_branch,
            "changes": changes,
            "branches": branches,
            "approved_dir": str(APPROVED_DIR),
        }
    except Exception as e:
        return {"error": str(e)}
