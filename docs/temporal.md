# Temporal

Temporal support is present, but it is mostly wired for Lima mode.

## Services

Lima setup creates:

- `clawfactory-temporal`: Temporal dev server using SQLite at `/srv/clawfactory/temporal/temporal.db`.
- `clawfactory-temporal-worker`: Python worker in `controller/temporal_worker.py`.

The controller connects to `TEMPORAL_HOST`, defaulting to:

```text
127.0.0.1:7233
```

The Temporal UI is proxied on host port `8082` in Lima mode.

Docker Compose mode does not define a Temporal service in `docker-compose.yml`.

## Built-In Workflows

`controller/temporal_worker.py` registers:

- `WeatherCheckWorkflow`: calls `wttr.in` for current weather.
- `PoetryResearchWorkflow`: triggers a research agent turn, waits two hours, then triggers a synthesis agent turn.
- `CustomWorkflow`: runs a JSON-defined sequence of `agent_turn`, `delay`, and `http` steps.

The worker uses task queue:

```text
clawfactory
```

Agent turns call the OpenClaw gateway endpoint:

```text
POST /api/cron/fire
```

with `{"job_id": "<agent_id>"}`.

## Controller Endpoints

```text
POST /temporal/start
GET  /temporal/workflows
GET  /temporal/workflow/{workflow_id}
```

Agent-scoped equivalents:

```text
POST /agent/temporal/start
GET  /agent/temporal/status/{workflow_id}
```

All return `503` if the controller cannot connect to Temporal.

## Workflow Definitions

Definitions are JSON files under:

```text
/srv/clawfactory/workflows
```

Controller endpoints:

```text
GET    /temporal/definitions
GET    /temporal/definition/{name}
POST   /temporal/definition
DELETE /temporal/definition/{name}
POST   /temporal/definition/{name}/run
POST   /agent/temporal/run/{name}
```

Example definition:

```json
{
  "name": "daily-check",
  "description": "Run an agent, wait, then call a URL",
  "steps": [
    {"type": "agent_turn", "agent_id": "daily-check"},
    {"type": "delay", "duration": "5m"},
    {"type": "http", "method": "GET", "url": "https://example.com/health", "timeout": 30}
  ]
}
```

Durations support `s`, `m`, and `h`.

## Schedule Helper

`controller/temporal_schedules.py` creates one hard-coded schedule:

```text
poetry-research-daily
```

It runs `PoetryResearchWorkflow` at `0 4 * * *`.

Run it inside an environment that can reach Temporal:

```bash
python3 controller/temporal_schedules.py
```

## Killswitch Behavior

The worker checks:

```text
/tmp/clawfactory-snapshot-sync/KILLSWITCH_<instance>
```

before firing agent turns. If the killswitch signal exists, the activity refuses to trigger the gateway.
