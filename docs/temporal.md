# Temporal Workflow Orchestration

Temporal provides durable execution for agentic workflows — automatic retries, crash-resilient scheduling, and a web UI for real-time visibility. It replaces fragile cron-based workflows with first-class orchestration.

## Why Temporal?

Cron jobs fire-and-forget. If a step fails, you get a log entry and nothing else. Temporal gives you:

- **Automatic retries** with configurable backoff (e.g., 30s initial, 5min max, 3 attempts)
- **Durable execution** — workflows survive server restarts and pick up where they left off
- **Multi-step sequencing** — chain activities with waits, conditionals, and error handling
- **Visibility** — every workflow execution is browsable in the Temporal UI with full history
- **Scheduling** — cron-like schedules with the Temporal scheduler (replaces OpenClaw cron jobs)

## Architecture

```
┌─────────────────────────────────────────────────┐
│  Lima VM                                         │
│                                                  │
│  clawfactory-temporal.service                    │
│  └── temporal server start-dev                   │
│      ├── gRPC :7233 (internal)                   │
│      ├── HTTP :7243 (internal)                   │
│      ├── UI   :8233 (internal)                   │
│      └── SQLite /srv/clawfactory/temporal/       │
│                                                  │
│  clawfactory-temporal-worker.service             │
│  └── python3 temporal_worker.py                  │
│      ├── Connects to Temporal gRPC               │
│      ├── Registers workflows + activities        │
│      └── Calls gateway HTTP API for agent turns  │
│                                                  │
│  nginx :8082 ──► proxy to Temporal UI :8233      │
└─────────────────────────────────────────────────┘

macOS host:
  SSH tunnel: localhost:8082 ──► VM:8082
  Tailscale:  https://<hostname>:8444/ ──► VM:8082
```

## Port Assignments

| Service | VM Port | External Access |
|---------|---------|-----------------|
| Temporal gRPC | 7233 | Internal only |
| Temporal HTTP | 7243 | Internal only |
| Temporal UI | 8233 | Internal only |
| nginx (Temporal UI proxy) | 8082 | localhost:8082 |
| Tailscale HTTPS (Temporal) | — | :8444 |

## Files

| File | Purpose |
|------|---------|
| `controller/temporal_worker.py` | Worker process — registers workflows and activities |
| `controller/temporal_schedules.py` | One-time script to create Temporal schedules |
| `controller/requirements.txt` | Includes `temporalio>=1.9.0` |
| `sandbox/lima/setup.sh` | Installs Temporal CLI, creates systemd units |
| `sandbox/lima/vm.sh` | Manages Temporal services lifecycle |

## Usage

### Viewing the Temporal UI

After starting services with `./clawfactory.sh -i <instance> start`:

- **Local:** http://localhost:8082
- **Tailnet:** https://\<hostname\>:8444/

The UI shows:
- **Workflows** — active and completed workflow executions
- **Schedules** — configured cron-like schedules
- **History** — step-by-step execution timeline for each workflow run

### Logs

```bash
./clawfactory.sh logs temporal    # Temporal server logs
./clawfactory.sh logs worker      # Temporal worker logs
```

### Setting Up Schedules

After the first deployment, create the workflow schedules:

```bash
# SSH into the Lima VM
limactl shell clawfactory

# Run the schedule setup
cd /srv/clawfactory/controller
python3 temporal_schedules.py
```

This creates:
- `poetry-research-daily` — fires the `PoetryResearchWorkflow` at 4am daily

### Manual Workflow Execution

Trigger a workflow manually from the Temporal UI:
1. Open http://localhost:8082
2. Click "Start Workflow"
3. Select `PoetryResearchWorkflow`
4. Use task queue `clawfactory`
5. Click "Start"

Or via the Temporal CLI inside the VM:
```bash
temporal workflow start \
  --type PoetryResearchWorkflow \
  --task-queue clawfactory \
  --workflow-id poetry-research-manual
```

## Proof of Concept: PoetryResearchWorkflow

The initial workflow migrated to Temporal:

1. **Phase 1** — Triggers `poetry-research-execute` agent turn via gateway API
2. **Wait** — 2-hour delay for research to settle
3. **Phase 2** — Triggers `poetry-research-synthesize` agent turn

Each phase retries up to 3 times with 30s→5min exponential backoff.

## Adding New Workflows

1. Define the workflow class and activities in `temporal_worker.py`
2. Register workflows/activities in the `Worker()` constructor
3. Optionally add a schedule in `temporal_schedules.py`
4. Sync and restart: `./clawfactory.sh -i <instance> rebuild`

## Data Storage

Temporal runs in dev mode with SQLite storage at `/srv/clawfactory/temporal/temporal.db`. This is suitable for single-node deployments. For production use with multiple workers, switch to PostgreSQL or MySQL.
