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
GIT_USER_NAME = os.environ.get("GIT_USER_NAME", "ClawFactory Controller")
GIT_USER_EMAIL = os.environ.get("GIT_USER_EMAIL", "controller@clawfactory.local")

# Detect offline mode (no GitHub configured)
OFFLINE_MODE = not GITHUB_REPO or GITHUB_REPO.strip() == "" or "/" not in GITHUB_REPO

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
    """List remote branches with their latest commit info (only main and proposal/*)."""
    branches = []
    try:
        # Get only main and proposal branches (not all the clutter from parent repos)
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
                    # Only include main and proposal/* branches
                    if branch_name != "main" and not branch_name.startswith("proposal/"):
                        continue
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

            # Handle HTTPS URLs
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
            # Handle SSH URLs (git@github.com:owner/repo.git)
            elif remote_url.startswith("git@github.com:"):
                repo_path = remote_url.replace("git@github.com:", "")
                auth_url = f"https://x-access-token:{github_token}@github.com/{repo_path}"
                subprocess.run(
                    ["git", "remote", "set-url", "origin", auth_url],
                    cwd=APPROVED_DIR,
                    capture_output=True,
                )

        result = subprocess.run(
            ["git", "fetch", "--prune", "origin"],
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

            # Handle HTTPS URLs
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
            # Handle SSH URLs (git@github.com:owner/repo.git)
            elif remote_url.startswith("git@github.com:"):
                repo_path = remote_url.replace("git@github.com:", "")
                auth_url = f"https://x-access-token:{github_token}@github.com/{repo_path}"
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

            # Handle HTTPS URLs
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
            # Handle SSH URLs (git@github.com:owner/repo.git)
            elif remote_url.startswith("git@github.com:"):
                repo_path = remote_url.replace("git@github.com:", "")
                auth_url = f"https://x-access-token:{github_token}@github.com/{repo_path}"
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
                <div class="stat-value {gateway_class}">
                    <span id="gateway-status-indicator" class="status-dot"></span>
                    {gateway_status}
                </div>
                <div class="stat-label">Gateway <span id="gateway-last-update" style="font-size: 0.7rem; color: #666;"></span></div>
            </div>
            <div class="stat">
                <div class="stat-value"><span class="{status_class}">{status_msg}</span></div>
                <div class="stat-label">Sync Status</div>
            </div>
        </div>

        <div id="proposed-config-banner" style="display: none; background: #9c27b0; color: white; padding: 0.75rem 1rem; border-radius: 4px; margin-bottom: 1rem; display: flex; justify-content: space-between; align-items: center;">
            <div>
                <strong>AI Config Proposal</strong>
                <span id="proposed-config-reason" style="margin-left: 0.5rem; opacity: 0.9;"></span>
            </div>
            <div>
                <button onclick="scrollToConfig()" style="background: white; color: #9c27b0; border: none; padding: 0.4rem 0.8rem; border-radius: 4px; cursor: pointer; font-family: monospace;">View & Load</button>
                <button onclick="dismissProposal()" style="background: transparent; color: white; border: 1px solid white; padding: 0.4rem 0.8rem; border-radius: 4px; cursor: pointer; margin-left: 0.5rem; font-family: monospace;">Dismiss</button>
            </div>
        </div>

        <div class="grid">
            <div>
                <h2>Promotion</h2>
                <div class="card">
                    {"" if OFFLINE_MODE else f'''<div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem;">
                        <div>
                            <span style="color: #888;">Local:</span> <span class="sha">{current_sha[:8]}</span>
                            <span style="margin: 0 0.5rem; color: #444;">→</span>
                            <span style="color: #888;">Remote:</span> <span class="sha">{remote_sha[:8]}</span>
                        </div>
                    </div>'''}
                    {"" if OFFLINE_MODE else ("<div style='background: #ff9800; color: #000; padding: 0.75rem; border-radius: 4px; margin-bottom: 1rem; font-weight: bold;'>🔄 New version available on GitHub!</div>" if needs_update else "")}
                    {"" if OFFLINE_MODE else (f'''<details style="margin-bottom: 1rem; background: #1a1a1a; border: 1px solid #ff9800; border-radius: 4px; padding: 0.5rem;">
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
                    </details>''' if needs_update else "")}
                    {f'''<div style="margin-bottom: 1rem;">
                        <div style="background: #1a3a1a; border: 1px solid #4CAF50; border-radius: 4px; padding: 0.75rem; margin-bottom: 1rem;">
                            <strong style="color: #4CAF50;">🔌 Local Mode</strong>
                            <span style="color: #888; margin-left: 0.5rem;">GitHub not configured</span>
                        </div>
                        <div style="margin-bottom: 0.5rem;">
                            <span style="color: #888;">Current commit:</span> <span class="sha">{current_sha[:8]}</span>
                        </div>
                    </div>
                    <div style="display: flex; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 1rem;">
                        <button onclick="pullUpstream()">Pull Latest OpenClaw</button>
                        <button onclick="rebuildGateway()" class="secondary">Rebuild Gateway</button>
                        <button onclick="restartGateway()" class="secondary">Restart Gateway</button>
                    </div>
                    <div id="promote-result" class="result"></div>

                    <h3 style="margin-top: 1.5rem; font-size: 0.9rem; color: #888;">Local Changes</h3>
                    <button onclick="viewLocalChanges()" class="secondary small">View Uncommitted Changes</button>
                    <div id="local-changes" style="margin-top: 0.5rem;"></div>''' if OFFLINE_MODE else f'''<div style="display: flex; gap: 0.5rem; flex-wrap: wrap;">
                        <button onclick="mergeAllAndDeploy()" {"style='background: #ff9800; border-color: #ff9800; animation: pulse 2s infinite;'" if needs_update else ""}>Merge All & Deploy</button>
                        <button onclick="promoteMain()" class="secondary">Deploy Main Only</button>
                    </div>
                    <div id="promote-result" class="result"></div>

                    <h3 style="margin-top: 1.5rem; font-size: 0.9rem; color: #888;">Promote Specific SHA</h3>
                    <div style="display: flex; gap: 0.5rem; flex-wrap: wrap; align-items: center;">
                        <input type="text" id="promote-sha-input" placeholder="Enter full SHA" style="width: 300px;">
                        <button onclick="promoteSha()" class="secondary">Promote SHA</button>
                    </div>'''}
                </div>

                {"" if OFFLINE_MODE else '''<h2>Branches</h2>
                <div class="card">
                    <button onclick="fetchBranches()">Refresh Branches</button>
                    <div id="branches-list" style="margin-top: 0.5rem; max-height: 300px; overflow-y: auto;"></div>
                    <div id="branch-diff-view" style="display: none; margin-top: 1rem; border-top: 1px solid #333; padding-top: 1rem;">
                        <h3 style="color: #2196F3; margin: 0 0 0.5rem 0;">Branch: <span id="branch-diff-name"></span></h3>
                        <div id="branch-diff-content"></div>
                    </div>
                </div>'''}

                <h2>Recent Commits</h2>
                <pre>{commits}</pre>
            </div>

            <div>
                <h2>System</h2>
                <div class="card">
                    <button onclick="restartGateway()" class="danger">Restart Gateway</button>
                    <button onclick="runSecurityAudit()" class="secondary" style="margin-left: 0.5rem;">Security Audit</button>
                    <button onclick="runSecurityAudit(true)" class="secondary">Deep Audit</button>
                    <div id="security-result" class="result"></div>
                </div>

                <h2>Snapshots</h2>
                <div class="card">
                    <button onclick="createSnapshot()">Create Snapshot</button>
                    <button onclick="fetchSnapshots()" class="secondary">Refresh List</button>
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

                <h2>Audit Log</h2>
                <div class="card">
                    <button onclick="fetchAudit()">Refresh</button>
                    <button onclick="fetchAudit(100)" class="secondary">Last 100</button>
                    <pre id="audit-log">Click Refresh to load audit log...</pre>
                </div>

            </div>
        </div>

        <h2>Gateway Logs</h2>
        <div class="card">
            <button onclick="fetchGatewayLogs()">Refresh</button>
            <button onclick="fetchGatewayLogs(200)" class="secondary">Last 200</button>
            <button onclick="fetchGatewayLogs(500)" class="secondary">Last 500</button>
            <label style="margin-left: 1rem; color: #888;">
                <input type="checkbox" id="logs-auto-refresh" onchange="toggleLogsAutoRefresh()"> Auto-refresh
            </label>
            <pre id="gateway-logs" style="max-height: 500px; overflow-y: auto; font-size: 0.8rem;">Click Refresh to load gateway logs...</pre>
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
            <button id="load-proposed-btn" onclick="loadProposedConfig()" style="display: none; background: #9c27b0;">Load Proposed</button>
            <div id="config-result" class="result"></div>
            <div id="proposed-config-info" style="display: none; margin-top: 0.5rem; padding: 0.5rem; background: #2d1f3d; border: 1px solid #9c27b0; border-radius: 4px;">
                <strong style="color: #9c27b0;">Proposed by AI:</strong>
                <span id="proposed-reason-inline" style="color: #ccc;"></span>
                <span id="proposed-time-inline" style="color: #888; font-size: 0.8rem; margin-left: 0.5rem;"></span>
            </div>
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

        <hr style="margin: 2rem 0; border-color: #333;">
        <p>
            {"" if OFFLINE_MODE else f'<a href="https://github.com/{GITHUB_REPO}">GitHub Repo</a> |'}
            <a href="/health">Health API</a> |
            <a href="/status">Status API</a> |
            <a href="/audit">Audit API</a>
            {"| <span style='color: #4CAF50;'>Local Mode</span>" if OFFLINE_MODE else ""}
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
                                    ${{branch.is_proposal ? `<button class="small danger" onclick="denyBranch('${{branch.name}}')">Deny</button>` : ''}}
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
                    html += `<button class="small danger" onclick="denyBranch('${{branch}}')" style="margin-left: 0.5rem;">Deny</button>`;
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
                    diffContent.innerHTML = '<p style="color: #888;">Checking for conflicts and merging...</p>';
                }}

                try {{
                    const resp = await fetch(basePath + '/branches/' + encodeURIComponent(branch) + '/merge', {{ method: 'POST' }});
                    const data = await resp.json();

                    if (data.error) {{
                        let errorHtml = `<p style="color: #ef9a9a; font-weight: bold;">❌ ${{data.error}}</p>`;

                        if (data.has_conflicts) {{
                            errorHtml += `
                                <div style="background: #3d2020; border: 1px solid #ef9a9a; border-radius: 4px; padding: 0.75rem; margin-top: 0.5rem;">
                                    <p style="color: #ef9a9a; margin: 0 0 0.5rem 0;"><strong>⚠️ Merge Conflicts Detected</strong></p>
                                    <p style="color: #ccc; margin: 0; font-size: 0.85rem;">
                                        This branch has conflicts with main that cannot be automatically resolved.<br><br>
                                        <strong>To resolve:</strong><br>
                                        1. <code>git checkout ${{branch}}</code><br>
                                        2. <code>git merge main</code><br>
                                        3. Resolve conflicts in your editor<br>
                                        4. <code>git add . && git commit</code><br>
                                        5. <code>git push</code>
                                    </p>
                                </div>`;
                            if (data.conflict_files && data.conflict_files.length > 0) {{
                                errorHtml += `<pre style="margin-top: 0.5rem; font-size: 0.75rem; color: #ef9a9a; background: #252525; padding: 0.5rem; border-radius: 3px;">${{data.conflict_files.join('\\n')}}</pre>`;
                            }}
                        }}

                        if (diffContent) {{
                            diffContent.innerHTML = errorHtml;
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

            async function denyBranch(branch) {{
                if (!confirm(`Deny and delete branch "${{branch}}"? This cannot be undone.`)) return;

                const diffContent = document.getElementById('branch-diff-content');
                if (diffContent) {{
                    diffContent.innerHTML = '<p style="color: #888;">Deleting branch...</p>';
                }}

                try {{
                    const resp = await fetch(basePath + '/branches/' + encodeURIComponent(branch) + '/deny', {{ method: 'POST' }});
                    const data = await resp.json();

                    if (data.error) {{
                        if (diffContent) {{
                            diffContent.innerHTML = `<p style="color: #ef9a9a;">❌ ${{data.error}}</p>`;
                        }} else {{
                            alert('Error: ' + data.error);
                        }}
                        return;
                    }}

                    // Refresh branches and hide diff view
                    document.getElementById('branch-diff-view').style.display = 'none';
                    fetchBranches();

                    alert('Branch denied and deleted.');
                }} catch(e) {{
                    if (diffContent) {{
                        diffContent.innerHTML = `<p style="color: #ef9a9a;">Error: ${{e.message}}</p>`;
                    }} else {{
                        alert('Error: ' + e.message);
                    }}
                }}
            }}

            // Gateway restart
            async function restartGateway() {{
                if (!confirm('Restart the gateway? This will briefly interrupt the bot.')) return;
                const result = document.getElementById('status-result') || document.getElementById('promote-result');
                result.style.display = 'block';
                result.className = 'result';
                result.textContent = 'Restarting gateway...';
                try {{
                    const resp = await fetch(basePath + '/gateway/restart', {{ method: 'POST' }});
                    const data = await resp.json();
                    if (!resp.ok || data.error || data.detail) {{
                        result.className = 'result error';
                        result.textContent = data.error || data.detail || 'Unknown error';
                    }} else {{
                        result.textContent = 'Gateway restarting... Status: ' + (data.status || 'ok');
                    }}
                }} catch(e) {{
                    result.className = 'result error';
                    result.textContent = 'Error: ' + e.message;
                }}
            }}

            // Offline mode: Pull latest OpenClaw from upstream
            async function pullUpstream() {{
                const result = document.getElementById('promote-result');
                result.style.display = 'block';
                result.className = 'result';
                result.textContent = 'Pulling latest OpenClaw from upstream...';
                try {{
                    const resp = await fetch(basePath + '/pull-upstream', {{ method: 'POST' }});
                    const data = await resp.json();
                    if (!resp.ok || data.error || data.detail) {{
                        result.className = 'result error';
                        result.innerHTML = 'Pull failed: ' + (data.error || data.detail || 'Unknown error');
                    }} else {{
                        result.innerHTML = '<span style="color: #4CAF50;">✅ ' + (data.message || 'Pulled successfully') + '</span>';
                        if (data.changes) {{
                            result.innerHTML += '<br><pre style="margin-top: 0.5rem; font-size: 0.8rem;">' + data.changes + '</pre>';
                        }}
                    }}
                }} catch(e) {{
                    result.className = 'result error';
                    result.textContent = 'Error: ' + e.message;
                }}
            }}

            // Offline mode: Rebuild gateway Docker image
            async function rebuildGateway() {{
                if (!confirm('Rebuild the gateway image? This will rebuild from local source and restart.')) return;
                const result = document.getElementById('promote-result');
                result.style.display = 'block';
                result.className = 'result';
                result.textContent = 'Rebuilding gateway image... This may take a while.';
                try {{
                    const resp = await fetch(basePath + '/gateway/rebuild', {{ method: 'POST' }});
                    const data = await resp.json();
                    if (!resp.ok || data.error || data.detail) {{
                        result.className = 'result error';
                        result.textContent = 'Rebuild failed: ' + (data.error || data.detail || 'Unknown error');
                    }} else {{
                        result.innerHTML = '<span style="color: #4CAF50;">✅ ' + (data.message || 'Rebuild complete') + '</span>';
                    }}
                }} catch(e) {{
                    result.className = 'result error';
                    result.textContent = 'Error: ' + e.message;
                }}
            }}

            // Offline mode: View local uncommitted changes
            async function viewLocalChanges() {{
                const container = document.getElementById('local-changes');
                container.innerHTML = '<span style="color: #888;">Loading...</span>';
                try {{
                    const resp = await fetch(basePath + '/local-changes');
                    const data = await resp.json();
                    if (!resp.ok || data.error || data.detail) {{
                        container.innerHTML = '<span style="color: #ef9a9a;">Error: ' + (data.error || data.detail || 'Unknown error') + '</span>';
                    }} else if (!data.changes || data.changes.trim() === '') {{
                        container.innerHTML = '<span style="color: #4CAF50;">No uncommitted changes</span>';
                    }} else {{
                        container.innerHTML = '<pre style="max-height: 300px; overflow: auto; font-size: 0.8rem;">' + data.changes + '</pre>';
                    }}
                }} catch(e) {{
                    container.innerHTML = '<span style="color: #ef9a9a;">Error: ' + e.message + '</span>';
                }}
            }}

            async function mergeAllAndDeploy() {{
                const result = document.getElementById('promote-result');
                result.style.display = 'block';
                result.className = 'result';
                result.textContent = 'Checking for conflicts and merging proposal branches...';

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
                    if (mergeData.skipped_conflicts && mergeData.skipped_conflicts.length > 0) {{
                        statusText += `⚠️ Skipped (conflicts): ${{mergeData.skipped_conflicts.join(', ')}}\\n`;
                    }}
                    const otherErrors = mergeData.errors ? mergeData.errors.filter(e => !e.has_conflicts) : [];
                    if (otherErrors.length > 0) {{
                        statusText += `❌ Other errors: ${{otherErrors.map(e => e.branch).join(', ')}}\\n`;
                    }}
                    if (mergeData.status === 'no_branches') {{
                        statusText = 'No proposal branches to merge. ';
                    }}

                    // If there are only conflicts and no successful merges, warn but allow deploy
                    if (mergeData.skipped_conflicts && mergeData.skipped_conflicts.length > 0 && (!mergeData.merged || mergeData.merged.length === 0)) {{
                        statusText += '\\n⚠️ All branches have conflicts. Deploying main as-is...\\n';
                    }} else {{
                        result.textContent = statusText + 'Now deploying main...';
                    }}

                    // Then deploy main
                    const deployResp = await fetch(basePath + '/promote-main', {{ method: 'POST' }});
                    if (deployResp.ok) {{
                        let finalHtml = statusText.replace(/\\n/g, '<br>') + '<br><span style="color: #4CAF50;">✅ Deployed! Restarting gateway...</span>';
                        if (mergeData.skipped_conflicts && mergeData.skipped_conflicts.length > 0) {{
                            finalHtml += '<br><br><span style="color: #ff9800;">Note: Resolve conflicts locally for: ' + mergeData.skipped_conflicts.join(', ') + '</span>';
                        }}
                        result.innerHTML = finalHtml;
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
                    const resp = await fetch(basePath + '/promote-main', {{ method: 'POST' }});
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

            async function promoteSha() {{
                const sha = document.getElementById('promote-sha-input').value.trim();
                if (!sha || sha.length < 7) {{
                    alert('Please enter a valid SHA (at least 7 characters)');
                    return;
                }}
                if (!confirm(`Promote SHA "${{sha}}" to approved?`)) return;

                const result = document.getElementById('promote-result');
                result.style.display = 'block';
                result.className = 'result';
                result.textContent = `Promoting SHA ${{sha}}...`;

                try {{
                    const formData = new FormData();
                    formData.append('sha', sha);
                    const resp = await fetch(basePath + '/controller', {{
                        method: 'POST',
                        body: formData
                    }});
                    if (resp.ok) {{
                        result.innerHTML = `<span style="color: #4CAF50;">✅ Promoted SHA ${{sha}}! Restarting gateway...</span>`;
                        document.getElementById('promote-sha-input').value = '';
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
                    if (!resp.ok || data.error || data.detail) {{
                        result.className = 'result error';
                        result.textContent = data.error || data.detail || 'Unknown error';
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
                    let html = '<div style="max-height: 200px; overflow-y: auto;">';
                    let selectHtml = '<option value="latest">latest</option>';
                    data.snapshots.forEach(s => {{
                        const latest = s.latest ? ' <span style="color: #4CAF50;">(latest)</span>' : '';
                        html += `<div style="padding: 0.3rem 0; border-bottom: 1px solid #333; font-size: 0.85rem;">
                            <code>${{s.name}}</code>${{latest}}<br>
                            <small style="color: #888;">${{formatSize(s.size)}} · ${{s.created}}</small>
                        </div>`;
                        selectHtml += `<option value="${{s.name}}">${{s.name}}</option>`;
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
                        result.textContent = data.error || data.detail || 'Unknown error';
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

            // Proposed config functions
            async function checkProposedConfig() {{
                try {{
                    const resp = await fetch(basePath + '/config/proposed');
                    const data = await resp.json();

                    const banner = document.getElementById('proposed-config-banner');
                    const btn = document.getElementById('load-proposed-btn');
                    const info = document.getElementById('proposed-config-info');
                    const reasonSpan = document.getElementById('proposed-config-reason');
                    const reasonInline = document.getElementById('proposed-reason-inline');
                    const timeInline = document.getElementById('proposed-time-inline');

                    if (data.has_proposal) {{
                        banner.style.display = 'flex';
                        btn.style.display = 'inline-block';
                        info.style.display = 'block';
                        reasonSpan.textContent = data.reason || '';
                        reasonInline.textContent = data.reason || '';
                        if (data.timestamp) {{
                            const date = new Date(data.timestamp);
                            timeInline.textContent = date.toLocaleString();
                        }}
                        window.proposedConfig = data.config;
                    }} else {{
                        banner.style.display = 'none';
                        btn.style.display = 'none';
                        info.style.display = 'none';
                        window.proposedConfig = null;
                    }}
                }} catch(e) {{
                    console.error('Error checking proposed config:', e);
                }}
            }}

            async function loadProposedConfig() {{
                if (!window.proposedConfig) {{
                    alert('No proposed config available');
                    return;
                }}

                if (!confirm('Load the AI-proposed config into the editor? You can review and save it after.')) return;

                setEditorValue(JSON.stringify(window.proposedConfig, null, 2));

                const result = document.getElementById('config-result');
                result.style.display = 'block';
                result.className = 'result';
                result.innerHTML = '<span style="color: #9c27b0;">Loaded proposed config. Review and Save & Restart to apply.</span>';

                // Delete the proposal after loading
                try {{
                    await fetch(basePath + '/config/proposed', {{ method: 'DELETE' }});
                    document.getElementById('proposed-config-banner').style.display = 'none';
                    document.getElementById('load-proposed-btn').style.display = 'none';
                    document.getElementById('proposed-config-info').style.display = 'none';
                    window.proposedConfig = null;
                }} catch(e) {{
                    console.error('Error deleting proposal:', e);
                }}

                // Validate the loaded config
                validateConfig();
            }}

            function scrollToConfig() {{
                document.querySelector('h2:has(+ .card #config-editor-wrapper)')?.scrollIntoView({{ behavior: 'smooth' }});
                // Fallback for browsers without :has support
                const configSection = document.getElementById('config-editor-wrapper');
                if (configSection) configSection.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
            }}

            async function dismissProposal() {{
                if (!confirm('Dismiss the AI config proposal? It will be deleted.')) return;
                try {{
                    await fetch(basePath + '/config/proposed', {{ method: 'DELETE' }});
                    document.getElementById('proposed-config-banner').style.display = 'none';
                    document.getElementById('load-proposed-btn').style.display = 'none';
                    document.getElementById('proposed-config-info').style.display = 'none';
                    window.proposedConfig = null;
                }} catch(e) {{
                    alert('Error dismissing proposal: ' + e.message);
                }}
            }}

            // Check for proposed config on page load
            checkProposedConfig();

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

            // Load data on page load
            fetchAudit();
            fetchSnapshots();
            fetchHealth();
            fetchBranches();
            checkConfigBackup();

            // Auto-polling intervals (in ms)
            const POLL_INTERVAL_FAST = 10000;   // 10s for status
            const POLL_INTERVAL_SLOW = 30000;   // 30s for data

            // Gateway status - poll frequently
            setInterval(() => {{
                fetchHealth();
            }}, POLL_INTERVAL_FAST);

            // Proposed config - check for AI proposals
            setInterval(() => {{
                checkProposedConfig();
            }}, POLL_INTERVAL_SLOW);

            console.log('Auto-polling enabled: status every 10s, data every 30s');
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


def git_ensure_config():
    """Ensure git user config is set for merge commits."""
    subprocess.run(
        ["git", "config", "user.name", GIT_USER_NAME],
        cwd=APPROVED_DIR,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", GIT_USER_EMAIL],
        cwd=APPROVED_DIR,
        capture_output=True,
    )


def git_setup_auth():
    """Set up git remote URL with auth token. Returns original URL to restore later."""
    git_ensure_config()  # Ensure git user config is set
    github_token = os.environ.get("GITHUB_TOKEN", "")
    if not github_token:
        return None

    url_result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=APPROVED_DIR,
        capture_output=True,
        text=True,
    )
    remote_url = url_result.stdout.strip()

    # Handle HTTPS URLs
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
        return remote_url

    # Handle SSH URLs (git@github.com:owner/repo.git)
    if remote_url.startswith("git@github.com:"):
        # Convert git@github.com:owner/repo.git to https://x-access-token:{token}@github.com/owner/repo.git
        repo_path = remote_url.replace("git@github.com:", "")
        auth_url = f"https://x-access-token:{github_token}@github.com/{repo_path}"
        subprocess.run(
            ["git", "remote", "set-url", "origin", auth_url],
            cwd=APPROVED_DIR,
            capture_output=True,
        )
        return remote_url

    return None


def git_restore_url(original_url: Optional[str]):
    """Restore the original git remote URL."""
    if original_url:
        subprocess.run(
            ["git", "remote", "set-url", "origin", original_url],
            cwd=APPROVED_DIR,
            capture_output=True,
        )


@app.post("/branches/merge-all")
@app.post("/controller/branches/merge-all")
async def merge_all_branches(
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Merge all proposal branches into main using git directly."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Set up git authentication
    original_url = git_setup_auth()

    try:
        # Fetch and get proposal branches
        subprocess.run(
            ["git", "fetch", "--prune", "origin"],
            cwd=APPROVED_DIR,
            capture_output=True,
            text=True,
            timeout=30,
        )
        branches = git_list_remote_branches()
        proposal_branches = [b for b in branches if b["is_proposal"]]

        if not proposal_branches:
            git_restore_url(original_url)
            return {"status": "no_branches", "merged": [], "errors": []}

        audit_log("merge_all_requested", {"branches": [b["name"] for b in proposal_branches]})

        merged = []
        errors = []
        skipped_conflicts = []

        for branch_info in proposal_branches:
            branch_name = branch_info["name"]
            try:
                # Check for conflicts first
                conflict_check = subprocess.run(
                    ["git", "merge-tree", "--write-tree", "origin/main", f"origin/{branch_name}"],
                    capture_output=True,
                    text=True,
                    cwd=APPROVED_DIR,
                    timeout=30,
                )

                if conflict_check.returncode != 0:
                    skipped_conflicts.append(branch_name)
                    errors.append({"branch": branch_name, "error": "Has merge conflicts", "has_conflicts": True})
                    continue

                # Checkout main
                checkout_result = subprocess.run(
                    ["git", "checkout", "main"],
                    capture_output=True,
                    text=True,
                    cwd=APPROVED_DIR,
                    timeout=30,
                )
                if checkout_result.returncode != 0:
                    errors.append({"branch": branch_name, "error": f"Checkout failed: {checkout_result.stderr}"})
                    continue

                # Pull latest main
                pull_result = subprocess.run(
                    ["git", "pull", "origin", "main"],
                    capture_output=True,
                    text=True,
                    cwd=APPROVED_DIR,
                    timeout=60,
                )
                if pull_result.returncode != 0:
                    errors.append({"branch": branch_name, "error": f"Pull failed: {pull_result.stderr}"})
                    continue

                # Merge the branch (squash)
                merge_result = subprocess.run(
                    ["git", "merge", "--squash", f"origin/{branch_name}"],
                    capture_output=True,
                    text=True,
                    cwd=APPROVED_DIR,
                    timeout=60,
                )
                if merge_result.returncode != 0:
                    subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=APPROVED_DIR, capture_output=True)
                    errors.append({"branch": branch_name, "error": f"Merge failed: {merge_result.stderr}"})
                    continue

                # Commit
                commit_result = subprocess.run(
                    ["git", "commit", "-m", f"Merge {branch_name} (squashed via controller)"],
                    capture_output=True,
                    text=True,
                    cwd=APPROVED_DIR,
                    timeout=30,
                )
                if commit_result.returncode != 0:
                    subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=APPROVED_DIR, capture_output=True)
                    errors.append({"branch": branch_name, "error": f"Commit failed: {commit_result.stderr}"})
                    continue

                # Push
                push_result = subprocess.run(
                    ["git", "push", "origin", "main"],
                    capture_output=True,
                    text=True,
                    cwd=APPROVED_DIR,
                    timeout=60,
                )
                if push_result.returncode != 0:
                    errors.append({"branch": branch_name, "error": f"Push failed: {push_result.stderr}"})
                    continue

                # Delete remote branch
                subprocess.run(
                    ["git", "push", "origin", "--delete", branch_name],
                    capture_output=True,
                    text=True,
                    cwd=APPROVED_DIR,
                    timeout=30,
                )

                merged.append(branch_name)
                audit_log("branch_merged", {"branch": branch_name})

            except Exception as e:
                errors.append({"branch": branch_name, "error": str(e)})

        git_restore_url(original_url)
        return {"status": "completed", "merged": merged, "errors": errors, "skipped_conflicts": skipped_conflicts}

    except Exception as e:
        git_restore_url(original_url)
        return {"error": str(e)}


# Note: git_setup_auth and git_restore_url are defined above merge_all_branches now
# The duplicate definitions below should be removed


@app.post("/branches/{branch:path}/merge")
@app.post("/controller/branches/{branch:path}/merge")
async def merge_branch(
    branch: str,
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Merge a branch into main using git directly."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    audit_log("branch_merge_requested", {"branch": branch})

    # Set up git authentication
    original_url = git_setup_auth()

    try:
        # Fetch latest
        subprocess.run(
            ["git", "fetch", "--prune", "origin"],
            cwd=APPROVED_DIR,
            capture_output=True,
            text=True,
            timeout=30,
        )

        # Check for conflicts using merge-tree
        conflict_check = subprocess.run(
            ["git", "merge-tree", "--write-tree", "origin/main", f"origin/{branch}"],
            capture_output=True,
            text=True,
            cwd=APPROVED_DIR,
            timeout=30,
        )

        # merge-tree returns non-zero if there are conflicts
        if conflict_check.returncode != 0:
            conflict_files = []
            for line in conflict_check.stdout.split("\n"):
                if line.strip() and "CONFLICT" in line:
                    conflict_files.append(line.strip())

            error_msg = "Branch has merge conflicts with main. "
            if conflict_files:
                error_msg += f"Conflicts in: {'; '.join(conflict_files[:5])}"
                if len(conflict_files) > 5:
                    error_msg += f" (+{len(conflict_files) - 5} more)"
            else:
                error_msg += "Please resolve conflicts locally and push."

            audit_log("branch_merge_conflict", {"branch": branch, "conflicts": conflict_files})
            git_restore_url(original_url)
            return {"error": error_msg, "has_conflicts": True, "conflict_files": conflict_files}

        # Checkout main
        checkout_result = subprocess.run(
            ["git", "checkout", "main"],
            capture_output=True,
            text=True,
            cwd=APPROVED_DIR,
            timeout=30,
        )
        if checkout_result.returncode != 0:
            git_restore_url(original_url)
            return {"error": f"Failed to checkout main: {checkout_result.stderr}"}

        # Pull latest main
        pull_result = subprocess.run(
            ["git", "pull", "origin", "main"],
            capture_output=True,
            text=True,
            cwd=APPROVED_DIR,
            timeout=60,
        )
        if pull_result.returncode != 0:
            git_restore_url(original_url)
            return {"error": f"Failed to pull main: {pull_result.stderr}"}

        # Merge the branch (squash for cleaner history)
        merge_result = subprocess.run(
            ["git", "merge", "--squash", f"origin/{branch}"],
            capture_output=True,
            text=True,
            cwd=APPROVED_DIR,
            timeout=60,
        )
        if merge_result.returncode != 0:
            # Reset on failure
            subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=APPROVED_DIR, capture_output=True)
            git_restore_url(original_url)
            return {"error": f"Merge failed: {merge_result.stderr}"}

        # Commit the squashed merge
        commit_result = subprocess.run(
            ["git", "commit", "-m", f"Merge {branch} (squashed via controller)"],
            capture_output=True,
            text=True,
            cwd=APPROVED_DIR,
            timeout=30,
        )
        if commit_result.returncode != 0:
            subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=APPROVED_DIR, capture_output=True)
            git_restore_url(original_url)
            return {"error": f"Commit failed: {commit_result.stderr}"}

        # Push to origin
        push_result = subprocess.run(
            ["git", "push", "origin", "main"],
            capture_output=True,
            text=True,
            cwd=APPROVED_DIR,
            timeout=60,
        )
        if push_result.returncode != 0:
            git_restore_url(original_url)
            return {"error": f"Push failed: {push_result.stderr}"}

        # Delete the remote branch
        subprocess.run(
            ["git", "push", "origin", "--delete", branch],
            capture_output=True,
            text=True,
            cwd=APPROVED_DIR,
            timeout=30,
        )

        git_restore_url(original_url)
        audit_log("branch_merged", {"branch": branch})
        return {"status": "merged", "branch": branch}

    except Exception as e:
        git_restore_url(original_url)
        audit_log("branch_merge_error", {"branch": branch, "error": str(e)})
        return {"error": str(e)}


@app.post("/branches/{branch:path}/merge")
@app.post("/controller/branches/{branch:path}/merge")
async def merge_branch(
    branch: str,
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Merge a branch into main using git directly."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    audit_log("branch_merge_requested", {"branch": branch})

    # Set up git authentication
    original_url = git_setup_auth()

    try:
        # Fetch latest
        subprocess.run(
            ["git", "fetch", "--prune", "origin"],
            cwd=APPROVED_DIR,
            capture_output=True,
            text=True,
            timeout=30,
        )

        # Check for conflicts using merge-tree
        conflict_check = subprocess.run(
            ["git", "merge-tree", "--write-tree", "origin/main", f"origin/{branch}"],
            capture_output=True,
            text=True,
            cwd=APPROVED_DIR,
            timeout=30,
        )

        # merge-tree returns non-zero if there are conflicts
        if conflict_check.returncode != 0:
            conflict_files = []
            for line in conflict_check.stdout.split("\n"):
                if line.strip() and "CONFLICT" in line:
                    conflict_files.append(line.strip())

            error_msg = "Branch has merge conflicts with main. "
            if conflict_files:
                error_msg += f"Conflicts in: {'; '.join(conflict_files[:5])}"
                if len(conflict_files) > 5:
                    error_msg += f" (+{len(conflict_files) - 5} more)"
            else:
                error_msg += "Please resolve conflicts locally and push."

            audit_log("branch_merge_conflict", {"branch": branch, "conflicts": conflict_files})
            git_restore_url(original_url)
            return {"error": error_msg, "has_conflicts": True, "conflict_files": conflict_files}

        # Checkout main
        checkout_result = subprocess.run(
            ["git", "checkout", "main"],
            capture_output=True,
            text=True,
            cwd=APPROVED_DIR,
            timeout=30,
        )
        if checkout_result.returncode != 0:
            git_restore_url(original_url)
            return {"error": f"Failed to checkout main: {checkout_result.stderr}"}

        # Pull latest main
        pull_result = subprocess.run(
            ["git", "pull", "origin", "main"],
            capture_output=True,
            text=True,
            cwd=APPROVED_DIR,
            timeout=60,
        )
        if pull_result.returncode != 0:
            git_restore_url(original_url)
            return {"error": f"Failed to pull main: {pull_result.stderr}"}

        # Merge the branch (squash for cleaner history)
        merge_result = subprocess.run(
            ["git", "merge", "--squash", f"origin/{branch}"],
            capture_output=True,
            text=True,
            cwd=APPROVED_DIR,
            timeout=60,
        )
        if merge_result.returncode != 0:
            # Reset on failure
            subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=APPROVED_DIR, capture_output=True)
            git_restore_url(original_url)
            return {"error": f"Merge failed: {merge_result.stderr}"}

        # Commit the squashed merge
        commit_result = subprocess.run(
            ["git", "commit", "-m", f"Merge {branch} (squashed via controller)"],
            capture_output=True,
            text=True,
            cwd=APPROVED_DIR,
            timeout=30,
        )
        if commit_result.returncode != 0:
            subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=APPROVED_DIR, capture_output=True)
            git_restore_url(original_url)
            return {"error": f"Commit failed: {commit_result.stderr}"}

        # Push to origin
        push_result = subprocess.run(
            ["git", "push", "origin", "main"],
            capture_output=True,
            text=True,
            cwd=APPROVED_DIR,
            timeout=60,
        )
        if push_result.returncode != 0:
            git_restore_url(original_url)
            return {"error": f"Push failed: {push_result.stderr}"}

        # Delete the remote branch
        subprocess.run(
            ["git", "push", "origin", "--delete", branch],
            capture_output=True,
            text=True,
            cwd=APPROVED_DIR,
            timeout=30,
        )

        git_restore_url(original_url)
        audit_log("branch_merged", {"branch": branch})
        return {"status": "merged", "branch": branch}

    except Exception as e:
        git_restore_url(original_url)
        audit_log("branch_merge_error", {"branch": branch, "error": str(e)})
        return {"error": str(e)}


@app.delete("/branches/{branch:path}")
@app.post("/branches/{branch:path}/deny")
@app.delete("/controller/branches/{branch:path}")
@app.post("/controller/branches/{branch:path}/deny")
async def deny_branch(
    branch: str,
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Deny (delete) a proposal branch without merging."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Only allow denying proposal branches
    if not branch.startswith("proposal/"):
        return {"error": "Can only deny proposal/* branches"}

    audit_log("branch_deny_requested", {"branch": branch})

    # Set up git authentication
    original_url = git_setup_auth()

    try:
        # Fetch latest
        subprocess.run(
            ["git", "fetch", "--prune", "origin"],
            cwd=APPROVED_DIR,
            capture_output=True,
            text=True,
            timeout=30,
        )

        # Delete the remote branch
        delete_result = subprocess.run(
            ["git", "push", "origin", "--delete", branch],
            capture_output=True,
            text=True,
            cwd=APPROVED_DIR,
            timeout=30,
        )

        git_restore_url(original_url)

        if delete_result.returncode != 0:
            audit_log("branch_deny_error", {"branch": branch, "stderr": delete_result.stderr})
            return {"error": f"Failed to delete branch: {delete_result.stderr}"}

        audit_log("branch_denied", {"branch": branch})
        return {"status": "denied", "branch": branch}

    except Exception as e:
        git_restore_url(original_url)
        audit_log("branch_deny_error", {"branch": branch, "error": str(e)})
        return {"error": str(e)}


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


def create_snapshot() -> dict:
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

    return {
        "snapshots": list_snapshots(),
        "encryption_ready": AGE_KEY.exists() or ensure_snapshot_key()
    }


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

        audit_log("snapshot_restored", {"snapshot": snapshot_name, "backup": str(backup_dir)})
        return {
            "status": "restored",
            "snapshot": snapshot_name,
            "backup": str(backup_dir),
        }
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
        client = docker.from_env()
        container = client.containers.get(GATEWAY_CONTAINER)
        container.stop(timeout=30)
    except Exception as e:
        audit_log("snapshot_restore_error", {"error": f"Failed to stop gateway: {e}"})
        raise HTTPException(status_code=500, detail=f"Failed to stop gateway: {e}")

    # Restore
    result = restore_snapshot(request.snapshot)
    if "error" in result:
        # Try to restart gateway even on failure
        try:
            container.start()
        except Exception:
            pass
        audit_log("snapshot_restore_error", result)
        raise HTTPException(status_code=500, detail=result["error"])

    # Restart gateway
    try:
        container.start()
    except Exception as e:
        result["warning"] = f"Gateway failed to restart: {e}"

    return result


# ============================================================
# Gateway Config Editor
# ============================================================

GATEWAY_CONFIG_PATH = OPENCLAW_HOME / "openclaw.json"
PROPOSED_CONFIG_PATH = Path("/srv/audit/proposed_config.json")
KNOWN_GOOD_CONFIG_PATH = Path("/srv/audit/known_good_config.json")


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

    # Update meta.lastTouchedAt timestamp
    if "meta" not in config:
        config["meta"] = {}
    config["meta"]["lastTouchedAt"] = datetime.now(timezone.utc).isoformat()

    audit_log("gateway_config_save", {"keys": list(config.keys())})

    try:
        # Save current config as known-good backup before changing
        if GATEWAY_CONFIG_PATH.exists():
            import shutil
            KNOWN_GOOD_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(GATEWAY_CONFIG_PATH, KNOWN_GOOD_CONFIG_PATH)
            audit_log("known_good_config_saved", {})

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
        client = docker.from_env()
        gateway = client.containers.get(GATEWAY_CONTAINER)
        gateway.stop(timeout=30)
        audit_log("gateway_stopped_for_revert", {})

        # Copy known-good back
        shutil.copy(KNOWN_GOOD_CONFIG_PATH, GATEWAY_CONFIG_PATH)
        audit_log("config_reverted", {})

        # Start gateway
        gateway.start()
        audit_log("gateway_started_after_revert", {})

        return {"status": "reverted", "restarted": True}
    except Exception as e:
        audit_log("config_revert_error", {"error": str(e)})
        return {"error": str(e)}


# ============================================================
# Proposed Config (AI suggestions)
# ============================================================

MAX_CONFIG_SIZE = 1024 * 1024  # 1MB max config size
MAX_REASON_LENGTH = 500


@app.post("/config/propose")
@app.post("/controller/config/propose")
async def propose_config(
    request: Request,
):
    """
    AI can propose a config change. Only one proposal stored at a time.
    New proposals overwrite existing ones. No auth required for AI access.
    """
    # Check content length to prevent DoS
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_CONFIG_SIZE:
        audit_log("config_propose_rejected", {"reason": "payload_too_large", "size": content_length})
        raise HTTPException(status_code=413, detail=f"Payload too large. Max size: {MAX_CONFIG_SIZE} bytes")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    config = body.get("config")
    reason = body.get("reason", "No reason provided")

    # Validate config is a dict
    if not isinstance(config, dict):
        audit_log("config_propose_rejected", {"reason": "config_not_dict", "type": type(config).__name__})
        return {"error": "Config must be a JSON object (dict)"}

    if not config:
        return {"error": "No config provided"}

    # Sanitize reason - truncate and strip control chars
    if not isinstance(reason, str):
        reason = "No reason provided"
    reason = reason[:MAX_REASON_LENGTH].strip()
    # Remove control characters except newlines
    reason = ''.join(c for c in reason if c == '\n' or (ord(c) >= 32 and ord(c) != 127))

    # Check serialized size
    serialized = json.dumps(config)
    if len(serialized) > MAX_CONFIG_SIZE:
        audit_log("config_propose_rejected", {"reason": "config_too_large", "size": len(serialized)})
        return {"error": f"Config too large. Max size: {MAX_CONFIG_SIZE} bytes"}

    audit_log("config_proposed", {"reason": reason[:100], "keys": list(config.keys())})

    try:
        PROPOSED_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(PROPOSED_CONFIG_PATH, "w") as f:
            json.dump({
                "config": config,
                "reason": reason,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }, f, indent=2)
        return {"status": "proposed", "message": "Config proposal saved for review"}
    except Exception as e:
        audit_log("config_propose_error", {"error": str(e)})
        return {"error": str(e)}


@app.get("/config/proposed")
@app.get("/controller/config/proposed")
async def get_proposed_config(
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Check if there's a pending config proposal."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not PROPOSED_CONFIG_PATH.exists():
        return {"has_proposal": False}

    try:
        with open(PROPOSED_CONFIG_PATH) as f:
            data = json.load(f)
        return {
            "has_proposal": True,
            "config": data.get("config"),
            "reason": data.get("reason"),
            "timestamp": data.get("timestamp"),
        }
    except Exception as e:
        return {"has_proposal": False, "error": str(e)}


@app.delete("/config/proposed")
@app.delete("/controller/config/proposed")
async def delete_proposed_config(
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Delete the pending config proposal."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    if PROPOSED_CONFIG_PATH.exists():
        PROPOSED_CONFIG_PATH.unlink()
        audit_log("config_proposal_deleted", {})
        return {"status": "deleted"}
    return {"status": "no_proposal"}


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


@app.post("/pull-upstream")
@app.post("/controller/pull-upstream")
async def pull_upstream_endpoint(
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Pull latest OpenClaw from upstream (offline mode)."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    audit_log("pull_upstream_requested", {"source": "api"})

    try:
        # Ensure git user config is set for merge commits
        git_ensure_config()

        # Check if upstream remote exists, add if not
        result = subprocess.run(
            ["git", "remote", "get-url", "upstream"],
            cwd=APPROVED_DIR,
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            # Add upstream remote
            subprocess.run(
                ["git", "remote", "add", "upstream", "https://github.com/openclaw/openclaw.git"],
                cwd=APPROVED_DIR,
                capture_output=True,
                text=True
            )

        # Fetch upstream
        fetch_result = subprocess.run(
            ["git", "fetch", "upstream"],
            cwd=APPROVED_DIR,
            capture_output=True,
            text=True,
            timeout=120
        )
        if fetch_result.returncode != 0:
            return {"error": f"Fetch failed: {fetch_result.stderr}"}

        # Merge upstream/main
        merge_result = subprocess.run(
            ["git", "merge", "upstream/main", "--no-edit", "-m", "Merge upstream OpenClaw"],
            cwd=APPROVED_DIR,
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
    """Rebuild and restart the gateway container (offline mode)."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    audit_log("gateway_rebuild_requested", {"source": "api"})

    try:
        client = docker.from_env()

        # Stop the gateway container
        try:
            container = client.containers.get(GATEWAY_CONTAINER)
            container.stop(timeout=30)
        except docker.errors.NotFound:
            pass

        # Rebuild the image using docker compose
        # Note: This assumes docker compose is available and the compose file is accessible
        rebuild_result = subprocess.run(
            ["docker", "compose", "build", "--no-cache", "gateway"],
            cwd=str(APPROVED_DIR.parent.parent.parent),  # Go up to clawfactory root
            capture_output=True,
            text=True,
            timeout=600  # 10 minute timeout for build
        )

        if rebuild_result.returncode != 0:
            return {"error": f"Build failed: {rebuild_result.stderr}"}

        # Start the gateway container
        start_result = subprocess.run(
            ["docker", "compose", "up", "-d", "gateway"],
            cwd=str(APPROVED_DIR.parent.parent.parent),
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


@app.get("/local-changes")
@app.get("/controller/local-changes")
async def local_changes_endpoint(
    token: Optional[str] = Query(None),
    session: Optional[str] = Cookie(None, alias="clawfactory_session"),
    authorization: Optional[str] = Header(None),
):
    """Get uncommitted changes in approved directory (offline mode)."""
    if CONTROLLER_API_TOKEN and not check_auth(token, session, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        # Get git status and diff
        status_result = subprocess.run(
            ["git", "status", "--short"],
            cwd=APPROVED_DIR,
            capture_output=True,
            text=True,
            timeout=30
        )

        diff_result = subprocess.run(
            ["git", "diff", "--stat"],
            cwd=APPROVED_DIR,
            capture_output=True,
            text=True,
            timeout=30
        )

        changes = ""
        if status_result.stdout:
            changes += "=== Status ===\n" + status_result.stdout + "\n"
        if diff_result.stdout:
            changes += "=== Changes ===\n" + diff_result.stdout

        return {"changes": changes.strip()}
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
        "messages", "commands", "tools", "session", "hooks", "cron", "skills"
    }
    for key in config.keys():
        if key not in valid_root_keys:
            issues.append({
                "severity": "error",
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

            # Convert https://github.com/... or git@github.com:... to https://x-access-token:TOKEN@github.com/...
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
            # Handle SSH URLs (git@github.com:owner/repo.git)
            elif remote_url.startswith("git@github.com:"):
                repo_path = remote_url.replace("git@github.com:", "")
                auth_url = f"https://x-access-token:{github_token}@github.com/{repo_path}"
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
