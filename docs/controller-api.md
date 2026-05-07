# Controller API

The controller is a FastAPI app served at both root paths and `/controller` paths. Most endpoints exist in pairs:

```text
/snapshot
/controller/snapshot
```

The UI is available at:

```text
/
/controller
```

## Authentication

When `CONTROLLER_API_TOKEN` is set, controller endpoints accept any of:

- `?token=<token>`;
- `Authorization: Bearer <token>`;
- `clawfactory_session` cookie created by logging into the UI with `?token=...`.

When `CONTROLLER_API_TOKEN` is empty, most controller endpoints are open for backward compatibility.

Agent endpoints require `AGENT_API_TOKEN`. Internal gateway helper auth accepts `GATEWAY_INTERNAL_TOKEN` or `CONTROLLER_API_TOKEN`.

Current exception: `/health`, `/status`, and `/audit` do not enforce controller auth in the implementation.

## Health And Status

```text
GET /health
GET /status
GET /audit?limit=50
```

`/status` returns gateway status and audit log path. `/audit` returns recent audit JSONL entries.

## Traffic And Scrub Rules

```text
GET  /traffic
GET  /traffic/stats
GET  /traffic/providers
GET  /traffic/inbound
GET  /traffic/{request_id}
GET  /scrub-rules
POST /scrub-rules
POST /scrub-rules/test
POST /traffic/delete
```

Plaintext traffic comes from `TRAFFIC_LOG`, normally `audit/traffic.jsonl` or `/srv/clawfactory/audit/traffic.jsonl`. Scrub rules are stored in `scrub_rules.json`; built-in rules redact common API key and authorization patterns.

Despite its path, `/traffic/delete` currently deletes the encrypted MITM traffic log and optionally its key, not the plaintext `traffic.jsonl` proxy log.

Encrypted MITM traffic endpoints:

```text
GET  /capture
POST /capture
GET  /traffic/decrypt
GET  /traffic/decrypt/stats
GET  /traffic/decrypt/{request_id}
```

Capture toggling is Lima-oriented. It starts/stops mitmproxy, changes iptables owner redirects, and manages a Fernet key encrypted with age.

## Snapshots

```text
GET  /snapshot
POST /snapshot
POST /snapshot/sync
GET  /snapshot/download/{name}
POST /snapshot/delete
POST /snapshot/rename
POST /snapshot/restore
```

Create body:

```json
{"name": "before-change"}
```

Restore body:

```json
{"snapshot": "latest"}
```

Snapshot browse endpoints:

```text
POST /snapshot/browse/open
POST /snapshot/browse/close
GET  /snapshot/browse/files
GET  /snapshot/browse/file
GET  /snapshot/browse/file/download
POST /snapshot/browse/file
POST /snapshot/browse/upload
POST /snapshot/browse/delete-file
POST /snapshot/browse/rename
POST /snapshot/browse/duplicate
POST /snapshot/browse/save
```

Browse paths are constrained to the temporary workspace and reject traversal.

Agent-scoped snapshot helpers:

```text
GET  /agent/snapshot
POST /agent/snapshot
```

These require `AGENT_API_TOKEN`, not controller auth. They are intended for agents that need to list or create snapshots without receiving `CONTROLLER_API_TOKEN`. The create body is the same as `/snapshot`.

## Preview Ports

```text
GET    /previews
POST   /previews
DELETE /previews/{id-or-alias}
POST   /agent/previews?agent_id=<agent-id>
```

Controller preview endpoints require controller auth. The agent endpoint requires `AGENT_API_TOKEN` and an explicit `agent_id`.

Create body:

```json
{"port": 6969, "alias": "my-app", "name": "My app"}
```

Preview aliases are optional. When provided, they must match `[a-z0-9][a-z0-9-]{2,40}` and must not be reserved controller paths. Registered previews are served at:

```text
/previews/<alias-or-generated-id>/
```

Preview routes are intentionally not protected by controller auth. The app listening behind the preview port must provide its own authentication if the content is sensitive. The controller validates that the port is a loopback listener owned by the instance user and blocks internal service ports.

## Gateway Operations

```text
GET  /gateway/config
POST /gateway/config
GET  /gateway/config/known-good
POST /gateway/config/revert
POST /gateway/config/validate
POST /gateway/restart
POST /gateway/rebuild
GET  /gateway/logs
GET  /gateway/devices
POST /gateway/devices/approve
POST /gateway/devices/reject
GET  /gateway/pairing/{channel}
POST /gateway/pairing/approve
GET  /gateway/security-audit
POST /pull-upstream
POST /killswitch
```

The config save flow validates, backs up the current config to `audit/known_good_config.json`, stops the gateway, writes `openclaw.json`, and restarts the gateway.

`/pull-upstream` fetches and merges `upstream/main` in the OpenClaw code directory. The CLI `update` command is the more complete update flow.

## Agent API

```text
PUT  /agent/files/{filepath}
GET  /agent/files/{filepath}
GET  /agent/gateway/status
GET  /agent/gateway/channels
POST /agent/gateway/restart
GET  /agent/gateway/config
POST /agent/temporal/start
GET  /agent/temporal/status/{workflow_id}
POST /agent/temporal/run/{name}
```

Agent file writes are scoped by `agent_id` when possible. The controller blocks path traversal, protected directories, common secret filenames, writes over 1 MB, and reads over 2 MB.

Agent system endpoints:

```text
POST /agent/system/apt-install
POST /agent/system/run-installer
POST /agent/system/env-set
POST /agent/system/batch
```

These require `AGENT_API_TOKEN` and reject scoped sub-agents. They run inside the VM and record approval entries in `state/setup-extras.json`:

- `apt-install` validates an apt package name, optionally registers an apt source, runs `apt-get install`, and records a setup-extras entry.
- `run-installer` runs a shell command after a verify check fails, then verifies again and records a setup-extras entry.
- `env-set` writes to `runtime.env` or `runtime.controller.env`, stores the proposed secret value encrypted with age, and only restarts the affected service when `apply: true`.
- `batch` runs multiple setup-extras operations and can restart dirty services once at the end with `apply: true`.

The VM systemd overrides load those runtime env overlay files when present. The host operator promotes approved setup-extras entries with `./clawfactory.sh -i <instance> setup-extras approve <id>` or `approve --all`.

## Temporal

```text
POST   /temporal/start
GET    /temporal/workflows
GET    /temporal/workflow/{workflow_id}
GET    /temporal/definitions
GET    /temporal/definition/{name}
POST   /temporal/definition
DELETE /temporal/definition/{name}
POST   /temporal/definition/{name}/run
```

These return `503` when the controller cannot connect to Temporal.

## Not Present

Routes for GitHub webhook promotion, `/branches`, and `/local-changes` are not implemented in `controller/main.py` at the time of this rewrite.
