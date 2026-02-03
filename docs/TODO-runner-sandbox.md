# TODO: Custom Runner Sandbox

If OpenClaw's native sandbox proves insufficient for the ClawFactory security model, implement a dedicated Runner container.

## When to implement

Add Runner if OpenClaw's sandbox cannot guarantee:
- Agent cannot write to `brain_ro/` (active config)
- Agent cannot access `secrets/`
- Agent cannot access Docker socket
- Agent cannot read/write arbitrary host paths

## Architecture

```
Gateway (OpenClaw) ──Unix Socket──► Runner (Python)
                                        │
                                        ▼
                                   brain_work/
```

## Runner design

**Container isolation:**
- `cap_drop: ALL` - no Linux capabilities
- `no-new-privileges: true`
- `read_only: true` filesystem (except explicit mounts)
- Only mount: `brain_work:rw`, `brain.git:rw`, socket volume

**Tool allowlist:**
```python
ALLOWED_TOOLS = {
    "git": ["git"],
    "cat": ["cat"],
    "ls": ["ls"],
    "mkdir": ["mkdir"],
    "cp": ["cp"],
    "mv": ["mv"],
    "rm": ["rm"],
    "diff": ["diff"],
    "jq": ["jq"],
}
```

**Forbidden patterns:**
```python
FORBIDDEN_PATTERNS = [
    "..",        # Path traversal
    "/etc",      # System config
    "/root",     # Root home
    "/var",      # System data
    "sudo",      # Privilege escalation
    "docker",    # Container escape
]
```

**Protocol (JSON over Unix socket):**
```json
// Request
{"id": "uuid", "tool": "git", "args": ["status"], "cwd": "/workspace/brain_work"}

// Response
{"id": "uuid", "success": true, "stdout": "...", "stderr": "...", "exit_code": 0}
```

## Files (preserved in runner/)

- `runner/Dockerfile` - Container definition
- `runner/runner.py` - Socket server + tool execution
- `runner/requirements.txt` - Python dependencies
- `runner/tools/` - Optional tool wrappers

## Docker Compose snippet

```yaml
runner:
  build: ./runner
  container_name: clawfactory-runner
  security_opt:
    - no-new-privileges:true
  cap_drop:
    - ALL
  volumes:
    - ./data/brain_work:/workspace/brain_work:rw
    - ./data/brain.git:/workspace/brain.git:rw
    - socket-volume:/run/clawfactory
```

## Integration with Gateway

Gateway would need `RUNNER_SOCKET=/run/clawfactory/runner.sock` environment variable and code to route tool calls through the socket instead of executing locally.
