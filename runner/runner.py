#!/usr/bin/env python3
"""
ClawFactory Runner - Tool Execution Sandbox

Listens on a Unix socket for tool execution requests from Gateway.
Executes tools in a restricted environment with write access only to brain_work.

Protocol (JSON over Unix socket):
  Request:  {"id": "uuid", "tool": "git", "args": ["status"], "cwd": "/workspace/brain_work"}
  Response: {"id": "uuid", "success": true, "stdout": "...", "stderr": "...", "exit_code": 0}
"""

import json
import os
import socket
import subprocess
import sys
import traceback
from pathlib import Path

SOCKET_PATH = os.environ.get("RUNNER_SOCKET", "/run/clawfactory/runner.sock")
BRAIN_WORK = Path(os.environ.get("BRAIN_WORK", "/workspace/brain_work"))
BRAIN_GIT = Path(os.environ.get("BRAIN_GIT", "/workspace/brain.git"))

# Allowed tools and their base commands
ALLOWED_TOOLS = {
    "git": ["git"],
    "cat": ["cat"],
    "ls": ["ls"],
    "mkdir": ["mkdir"],
    "cp": ["cp"],
    "mv": ["mv"],
    "rm": ["rm"],
    "echo": ["echo"],
    "diff": ["diff"],
    "patch": ["patch"],
    "jq": ["jq"],
}

# Forbidden patterns in arguments
FORBIDDEN_PATTERNS = [
    "..",        # Path traversal
    "/etc",      # System config
    "/root",     # Root home
    "/home",     # User homes
    "/var",      # System data
    "/usr",      # System binaries
    "/bin",      # Binaries
    "/sbin",     # System binaries
    "docker",    # Docker commands
    "sudo",      # Privilege escalation
    "chmod",     # Permission changes
    "chown",     # Ownership changes
]


def validate_request(request: dict) -> tuple[bool, str]:
    """Validate a tool execution request."""
    tool = request.get("tool")
    args = request.get("args", [])
    cwd = request.get("cwd", str(BRAIN_WORK))

    # Check tool is allowed
    if tool not in ALLOWED_TOOLS:
        return False, f"Tool not allowed: {tool}"

    # Check cwd is within allowed paths
    cwd_path = Path(cwd).resolve()
    if not (str(cwd_path).startswith(str(BRAIN_WORK)) or
            str(cwd_path).startswith(str(BRAIN_GIT))):
        return False, f"Working directory not allowed: {cwd}"

    # Check for forbidden patterns in args
    all_args = " ".join(str(a) for a in args)
    for pattern in FORBIDDEN_PATTERNS:
        if pattern in all_args:
            return False, f"Forbidden pattern in arguments: {pattern}"

    return True, ""


def execute_tool(request: dict) -> dict:
    """Execute a tool and return the result."""
    request_id = request.get("id", "unknown")
    tool = request.get("tool")
    args = request.get("args", [])
    cwd = request.get("cwd", str(BRAIN_WORK))
    timeout = request.get("timeout", 30)

    # Validate
    valid, error = validate_request(request)
    if not valid:
        return {
            "id": request_id,
            "success": False,
            "error": error,
            "stdout": "",
            "stderr": error,
            "exit_code": -1,
        }

    # Build command
    cmd = ALLOWED_TOOLS[tool] + [str(a) for a in args]

    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "id": request_id,
            "success": result.returncode == 0,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {
            "id": request_id,
            "success": False,
            "error": f"Command timed out after {timeout}s",
            "stdout": "",
            "stderr": f"Timeout after {timeout}s",
            "exit_code": -1,
        }
    except Exception as e:
        return {
            "id": request_id,
            "success": False,
            "error": str(e),
            "stdout": "",
            "stderr": str(e),
            "exit_code": -1,
        }


def handle_client(conn: socket.socket):
    """Handle a single client connection."""
    try:
        data = b""
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
            # Check for complete JSON (simple newline delimiter)
            if b"\n" in data:
                break

        if not data:
            return

        request = json.loads(data.decode("utf-8").strip())
        print(f"[runner] Request: {request.get('tool')} {request.get('args', [])}", flush=True)

        response = execute_tool(request)
        print(f"[runner] Response: success={response['success']}, exit={response['exit_code']}", flush=True)

        conn.sendall(json.dumps(response).encode("utf-8") + b"\n")
    except json.JSONDecodeError as e:
        error_response = {"success": False, "error": f"Invalid JSON: {e}"}
        conn.sendall(json.dumps(error_response).encode("utf-8") + b"\n")
    except Exception as e:
        print(f"[runner] Error handling client: {e}", flush=True)
        traceback.print_exc()


def main():
    """Main entry point - start the Unix socket server."""
    # Remove existing socket file
    socket_path = Path(SOCKET_PATH)
    if socket_path.exists():
        socket_path.unlink()

    # Ensure parent directory exists
    socket_path.parent.mkdir(parents=True, exist_ok=True)

    # Create Unix socket
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    server.listen(5)

    # Make socket accessible
    os.chmod(str(socket_path), 0o660)

    print(f"[runner] Listening on {SOCKET_PATH}", flush=True)
    print(f"[runner] Allowed tools: {list(ALLOWED_TOOLS.keys())}", flush=True)
    print(f"[runner] Brain work: {BRAIN_WORK}", flush=True)

    try:
        while True:
            conn, _ = server.accept()
            try:
                handle_client(conn)
            finally:
                conn.close()
    except KeyboardInterrupt:
        print("[runner] Shutting down", flush=True)
    finally:
        server.close()
        if socket_path.exists():
            socket_path.unlink()


if __name__ == "__main__":
    main()
