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
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import docker
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

# Configuration from environment
BRAIN_RO = Path(os.environ.get("BRAIN_RO", "/srv/sandyclaws/brain_ro"))
BRAIN_WORK = Path(os.environ.get("BRAIN_WORK", "/srv/sandyclaws/brain_work"))
OPENCLAW_HOME = Path(os.environ.get("OPENCLAW_HOME", "/srv/openclaw-home"))
AUDIT_LOG = Path(os.environ.get("AUDIT_LOG", "/srv/audit/audit.jsonl"))
GITHUB_WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
ALLOWED_MERGE_ACTORS = os.environ.get("ALLOWED_MERGE_ACTORS", "").split(",")
GATEWAY_CONTAINER = os.environ.get("GATEWAY_CONTAINER", "clawfactory-gateway")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "elimaine/sandyclaws-brain")

app = FastAPI(title="ClawFactory Controller", version="1.0.0")


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

@app.get("/promote", response_class=HTMLResponse)
async def promote_ui():
    """Simple approval UI for promoting changes."""
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

    status_msg = "✅ Up to date" if not needs_update else f"⚠️ Update available: {remote_sha[:8]}"

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>ClawFactory - Promote</title>
        <style>
            body {{ font-family: monospace; padding: 2rem; background: #1a1a1a; color: #e0e0e0; }}
            h1 {{ color: #4CAF50; }}
            pre {{ background: #2d2d2d; padding: 1rem; overflow-x: auto; }}
            button {{ background: #4CAF50; color: white; border: none; padding: 1rem 2rem;
                     font-size: 1.2rem; cursor: pointer; margin: 1rem 0; }}
            button:hover {{ background: #45a049; }}
            .warning {{ color: #ff9800; }}
            .sha {{ color: #2196F3; }}
            .status {{ padding: 0.5rem; margin: 1rem 0; }}
        </style>
    </head>
    <body>
        <h1>ClawFactory Controller</h1>
        <p>Current active SHA: <span class="sha">{current_sha[:8]}</span></p>
        <p>Remote main SHA: <span class="sha">{remote_sha[:8]}</span></p>
        <p class="status">{status_msg}</p>

        <h2>Recent commits on origin/main:</h2>
        <pre>{commits}</pre>

        <h3>Pull Latest Main</h3>
        <form action="/promote/main" method="POST">
            <p class="warning">⚠️ This will pull latest main and restart the Gateway.</p>
            <button type="submit">Pull Main & Restart</button>
        </form>

        <h3>Or Promote Specific SHA</h3>
        <form action="/promote" method="POST">
            <label>SHA to promote:</label><br>
            <input type="text" name="sha" placeholder="Enter full SHA" style="width: 400px; padding: 0.5rem; font-family: monospace;"><br>
            <button type="submit">Promote SHA & Restart</button>
        </form>

        <hr style="margin: 2rem 0; border-color: #444;">
        <p><a href="https://github.com/{GITHUB_REPO}" style="color: #2196F3;">View on GitHub</a></p>
    </body>
    </html>
    """


@app.post("/promote")
async def promote_manual(request: Request):
    """Handle manual promotion of specific SHA from UI."""
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
            <a href="/promote" style="color: #2196F3;">← Back</a>
        </body>
        </html>
    """)


@app.post("/promote/main")
async def promote_main_endpoint():
    """Pull latest main and restart gateway."""
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
            <a href="/promote" style="color: #2196F3;">← Back</a>
        </body>
        </html>
    """)


# ============================================================
# Health & Status
# ============================================================

@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok"}


@app.get("/status")
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
async def memory_backup():
    """Backup agent memory to GitHub."""
    audit_log("memory_backup_requested", {})

    result = backup_memory()
    if not result["files"]:
        return {"status": "no_changes", "files": []}

    if not commit_and_push_memory():
        raise HTTPException(status_code=500, detail="Failed to push memory backup")

    audit_log("memory_backup_success", {"files": result["files"]})
    return {"status": "backed_up", "files": result["files"]}


@app.get("/memory/status")
async def memory_status():
    """Get memory backup status."""
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
