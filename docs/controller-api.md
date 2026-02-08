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
| `/capture` | GET | Get capture status (enabled, mitm_active, entry count) |
| `/capture` | POST | Toggle MITM capture — starts/stops mitmproxy, manages iptables rules |
| `/traffic/decrypt` | GET | Decrypt and view encrypted traffic log entries (supports provider, status, search filters) |
| `/traffic/decrypt/stats` | GET | Aggregate stats from encrypted traffic log (decrypts on the fly) |
| `/traffic/decrypt/{id}` | GET | Get a single decrypted traffic entry by request ID |
| `/traffic/delete` | POST | Delete encrypted traffic log and optionally rotate the Fernet key |

## MITM Traffic Capture

The controller can enable transparent MITM capture of all outbound HTTP/HTTPS traffic from the gateway. When enabled:

1. A Fernet encryption key is generated and wrapped with the instance's age key
2. `mitmproxy` starts in transparent mode on port 8888
3. `iptables` rules redirect gateway user traffic through the proxy
4. Every request/response is encrypted with Fernet and appended to `traffic.enc.jsonl`

Logs are **never stored in plaintext on disk**. The "Decrypt & View" button in the Logs page decrypts entries on demand using the age private key to unwrap the Fernet key. Capture is **off by default** and does not persist across VM reboots.

## Config Editor

The dashboard includes a built-in Gateway Config editor that makes wrangling `openclaw.json` painless:

- **Load/Save** — Edit the live config directly, saves and restarts the gateway in one shot
- **Schema Validation** — Catches config errors before they take down your agent
- **Ollama Discovery** — Auto-detects locally running Ollama models, click to add them
- **RAM-Aware Context** — Calculates safe context windows based on available system memory
- **Error Navigation** — Clickable JSON errors with "Open in VS Code" and "Jump to line" shortcuts
- **Live Feedback** — Syntax errors light up as you type
