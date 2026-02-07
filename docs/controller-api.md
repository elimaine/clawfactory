# Controller API

The Controller is your mission control surface — a FastAPI backend that serves the dashboard UI and exposes endpoints for managing your agent fleet. All endpoints require authentication via `?token=` query param or session cookie.

## Endpoints

| Endpoint | Method | What It Does |
|----------|--------|--------------|
| `/controller` | GET | The main dashboard — your command center |
| `/controller` | POST | Promote a specific commit SHA into production |
| `/controller/promote-main` | POST | Pull latest main and cycle the gateway |
| `/snapshot` | POST | Freeze agent state into an encrypted snapshot |
| `/snapshot` | GET | Browse available snapshots |
| `/status` | GET | System-wide health report |
| `/health` | GET | Quick heartbeat check |
| `/audit` | GET | Pull recent audit log entries |
| `/gateway/restart` | POST | Restart the gateway process |
| `/gateway/config` | GET | Fetch the live `openclaw.json` + detected Ollama models |
| `/gateway/config` | POST | Push new config and cycle the gateway |
| `/gateway/config/validate` | GET | Validate config against the OpenClaw schema |
| `/gateway/devices` | GET | List devices waiting for pairing + already paired |
| `/gateway/devices/approve` | POST | Approve a pending device pairing |
| `/gateway/devices/reject` | POST | Reject a device pairing request |
| `/gateway/pairing/{channel}` | GET | List DM-based pairing requests for a channel |
| `/gateway/pairing/approve` | POST | Approve a DM pairing code |
| `/gateway/security-audit` | GET | Run the OpenClaw security audit suite |

## Config Editor

The dashboard includes a built-in Gateway Config editor that makes wrangling `openclaw.json` painless:

- **Load/Save** — Edit the live config directly, saves and restarts the gateway in one shot
- **Schema Validation** — Catches config errors before they take down your agent
- **Ollama Discovery** — Auto-detects locally running Ollama models, click to add them
- **RAM-Aware Context** — Calculates safe context windows based on available system memory
- **Error Navigation** — Clickable JSON errors with "Open in VS Code" and "Jump to line" shortcuts
- **Live Feedback** — Syntax errors light up as you type
