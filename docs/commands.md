# Command Reference

Your main interface is `./clawfactory.sh`. Every command supports `-i <instance>` for targeting a specific agent in your fleet.

## Lifecycle

Bring agents online, take them offline, or cycle them.

```bash
./clawfactory.sh start              # Launch default instance
./clawfactory.sh stop               # Shut down default instance
./clawfactory.sh restart            # Cycle all services
./clawfactory.sh status             # Service health at a glance
./clawfactory.sh info               # Display instance name + auth tokens
./clawfactory.sh list               # Survey the entire fleet
```

## Diagnostics

Peek under the hood when you need to see what your agent is up to.

```bash
./clawfactory.sh logs [service]     # Tail live logs (gateway/proxy/controller/temporal/worker)
./clawfactory.sh shell [service]    # Drop into a container shell
./clawfactory.sh controller         # Print the Controller dashboard URL
./clawfactory.sh audit              # Review the recent audit trail
```

## Snapshots

Freeze-dry your agent's runtime state into an encrypted archive. Restore it anytime.

```bash
./clawfactory.sh snapshot create    # Capture current state
./clawfactory.sh snapshot list      # Browse available snapshots
./clawfactory.sh snapshot restore <file>  # Restore from archive
./clawfactory.sh snapshot keygen    # Generate encryption keys
```

See [Snapshots](snapshots.md) for the full picture.

## Fleet Operations

Run multiple agents side by side, each with their own identity, secrets, and sandbox.

```bash
./clawfactory.sh -i bot1 start     # Bring 'bot1' online
./clawfactory.sh -i bot1 stop      # Take 'bot1' offline
./clawfactory.sh -i bot1 info      # Show 'bot1' credentials
```

## Sandbox Control

Manage your containment layer depending on your platform.

```bash
# Sysbox (Linux)
./clawfactory.sh sandbox            # Check sandbox status
./clawfactory.sh sandbox enable     # Activate Sysbox isolation
./clawfactory.sh sandbox disable    # Drop back to standard containers

# Lima VM (macOS)
./clawfactory.sh lima setup         # Provision a Lima VM
./clawfactory.sh lima shell         # Shell into the VM
./clawfactory.sh lima status        # VM + service health
./clawfactory.sh lima teardown      # Tear it all down
```

See [Sandbox](sandbox.md) for architecture details.

## OpenClaw CLI

Talk directly to the OpenClaw runtime inside the sandbox — useful for first-time bot setup.

```bash
./clawfactory.sh openclaw           # Launch the OpenClaw interactive CLI
```

## Temporal Workflows

Temporal provides durable workflow orchestration with automatic retries, scheduling, and a web UI for visibility.

```bash
./clawfactory.sh logs temporal      # Tail Temporal server logs
./clawfactory.sh logs worker        # Tail Temporal worker logs
```

Access the Temporal UI:
- **Local:** `http://localhost:8082`
- **Tailnet:** `https://<hostname>:8444/`

Set up workflow schedules (run once after deployment):
```bash
# Inside the Lima VM
cd /srv/clawfactory/controller
python3 temporal_schedules.py
```

## Kill Switch

The nuclear option. Shuts down all services and severs network access immediately.

```bash
./killswitch.sh lock                # Hard stop — everything goes dark
./killswitch.sh restore             # Bring systems back online after review
```
