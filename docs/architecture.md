# Architecture

ClawFactory is a wrapper around OpenClaw that makes the runtime operable. It does not replace OpenClaw; it gives the operator a repeatable place for code, state, secrets, snapshots, logs, remote access, and emergency shutdown.

## Core Model

Each bot is an instance. The instance name controls container names, service names, ports, secrets, snapshots, and local paths.

```text
bot_repos/<instance>/code       OpenClaw checkout and workspace source
bot_repos/<instance>/state      OpenClaw runtime state
secrets/<instance>              env files, tokens, snapshot keys
snapshots/<instance>            age-encrypted state snapshots
audit                           controller events, traffic logs, scrub rules
```

The split matters. Code can be updated from upstream and rebuilt. State is treated as operational data and backed up through encrypted snapshots. Secrets stay outside the bot repo.

## Runtime Services

Docker Compose mode runs four services:

- `proxy`: nginx on localhost ports, forwarding gateway and controller traffic.
- `gateway`: the OpenClaw gateway built from `bot_repos/<instance>/code`.
- `controller`: FastAPI management UI and API.
- `llm-proxy`: FastAPI reverse proxy for Anthropic, OpenAI, and Gemini traffic logging.

Lima mode runs services inside the `clawfactory` Linux VM:

- `openclaw-gateway@<instance>`: per-instance OpenClaw gateway systemd service.
- `clawfactory-controller`: FastAPI controller for the active instance.
- `clawfactory-llm-proxy`: optional provider proxy on port `9090`.
- `clawfactory-mitm`: transparent mitmproxy service used only when encrypted capture is enabled.
- `clawfactory-temporal`: Temporal dev server with SQLite storage.
- `clawfactory-temporal-worker`: workflow worker for built-in and custom workflows.
- `nginx` and `docker`: VM-local proxy and sandbox support.

The active mode is selected by `.clawfactory.conf` through `SANDBOX_MODE`. Current supported values are `lima`, `sysbox`, and `none`.

## Request Flow

In Docker Compose mode:

```text
host localhost port
  -> nginx proxy
  -> gateway or controller
```

Gateway outbound model calls are configured through environment variables:

```text
gateway
  -> llm-proxy
  -> provider API
```

Only Anthropic, OpenAI, and Gemini are wired this way by default in Docker mode.

In Lima mode:

```text
host localhost port
  -> Lima VZ forwarded port
  -> nginx or direct service port in VM
  -> gateway or controller
```

Lima no longer rewrites provider base URLs automatically. The LLM proxy is running, but provider logging through it must be configured explicitly. Encrypted MITM capture is the Lima-specific capture path.

## Controller Responsibilities

The controller owns operator workflows:

- gateway start, stop, restart, rebuild, status, logs, device pairing, and security audit;
- OpenClaw `openclaw.json` read, validate, save, known-good backup, and revert;
- age-encrypted snapshot create, list, rename, delete, restore, download, browse, edit, and save-as-new;
- LLM traffic log read, filter, stats, detail, scrub rules, and deletion;
- encrypted MITM traffic decrypt, stats, and detail;
- agent-scoped file and gateway endpoints through `AGENT_API_TOKEN`;
- unscoped agent system endpoints for apt packages, shell installers, and runtime env overlays;
- Temporal workflow start/status/list and workflow-definition CRUD when Temporal is connected.

The UI is embedded directly in `controller/main.py`. There is no separate frontend build for the controller.

## Trust Boundaries

The operator controls controller access with `CONTROLLER_API_TOKEN`. The gateway gets provider and channel tokens from `gateway.env`. The controller also reads `gateway.env` in Lima mode because agent and internal gateway operations need shared tokens.

Important boundaries:

- `CONTROLLER_API_TOKEN`: admin access to the controller UI/API.
- `OPENCLAW_GATEWAY_TOKEN`: token for requests to the OpenClaw gateway.
- `GATEWAY_INTERNAL_TOKEN`: internal gateway/controller calls.
- `AGENT_API_TOKEN`: scoped token for sandboxed agent calls to controller endpoints.
- `snapshot.key`: age private key for snapshot decrypt and encrypted capture key recovery.

When `CONTROLLER_API_TOKEN` is unset, most controller endpoints are intentionally open for backward compatibility. That is convenient for local-only use and unsafe for exposed controllers.

The `/agent/system/*` endpoints are especially sensitive. They are gated by `AGENT_API_TOKEN` and reject scoped sub-agents, but successful calls take immediate effect inside the VM.

## Persistence

Snapshots are the recovery mechanism. In Lima mode, `lima_sync` pulls VM snapshots and state back to the host before pushing host changes into the VM. On a cold start, if the VM has snapshots but no state, the launcher restores the latest snapshot automatically.

OpenClaw dependencies and build output are not treated as durable state. They are rebuilt from the code checkout and package files.

## Non-Implemented Or Legacy Areas

GitHub PR promotion is not currently implemented in the controller routes, even though the installer can prompt for GitHub webhook settings. Legacy TODO docs were removed because they described plans rather than shipped behavior. See [issues-log.md](issues-log.md) for the current implementation gaps found during the documentation rewrite.
