# ClawFactory

ClawFactory is an operator-owned runtime for running OpenClaw bots without pretending the bot is a disposable process. It is a harness of harnesses: a practical shell around OpenClaw that keeps the bot's source, state, secrets, snapshots, logs, and emergency controls visible to the human who is responsible for it.

The project has a practical bias: make the bot easy to start, inspect, stop, update, and recover. OpenClaw moves quickly, experiments can wreck a good setup, and local security boundaries are easy to misunderstand. ClawFactory answers that with named instances, encrypted snapshots, controller visibility, and, in Lima mode, VMs all the way down.

## What It Runs

A ClawFactory instance is a named bot under `bot_repos/<instance>/` with matching secrets and snapshots:

- `bot_repos/<instance>/code`: the OpenClaw source checkout and workspace files.
- `bot_repos/<instance>/state`: OpenClaw runtime state, config, memory indexes, credentials, paired devices, and generated files.
- `secrets/<instance>`: env files and snapshot encryption keys.
- `snapshots/<instance>`: age-encrypted state backups.
- `audit`: controller audit events, traffic logs, scrub rules, and session cookies.

There are two runtime paths:

- Lima mode, selected by `SANDBOX_MODE=lima`, runs services directly inside a Linux VM with systemd. This is the current macOS path and the only path wired for Temporal and MITM TLS capture.
- Docker Compose mode runs `proxy`, `gateway`, `controller`, and `llm-proxy` containers on the host. It is still supported by the compose files and launcher.

## Quick Start

```bash
./install.sh
./clawfactory.sh -i <instance> start
./clawfactory.sh -i <instance> status
./clawfactory.sh -i <instance> controller
```

On macOS, the installer defaults toward Lima. If you already have an instance, `./clawfactory.sh bots` lists saved bots, configured secrets, snapshot availability, and ports.

For emergencies:

```bash
./killswitch.sh lock
```

In Lima mode, the killswitch stops the Lima VM. In Docker mode, it stops compose services and attempts a restrictive iptables lock when iptables exists.

The point is not to make the agent powerless. The point is to make the agent useful while keeping rollback, shutdown, and recovery boring.

## Main Commands

```bash
./clawfactory.sh -i <instance> start
./clawfactory.sh -i <instance> stop
./clawfactory.sh -i <instance> restart
./clawfactory.sh -i <instance> rebuild
./clawfactory.sh -i <instance> update
./clawfactory.sh -i <instance> logs gateway
./clawfactory.sh -i <instance> shell
./clawfactory.sh -i <instance> snapshot list
./clawfactory.sh -i <instance> snapshot create before-change
./clawfactory.sh -i <instance> snapshot restore latest
./clawfactory.sh -i <instance> openclaw onboard
```

See [docs/commands.md](docs/commands.md) for the full command reference.

## Operator UI

The controller is the operational dashboard. It exposes:

- gateway status, restart, rebuild, logs, pairing approvals, and config editing;
- encrypted snapshot create, list, rename, delete, restore, download, and browse/edit/save flows;
- plaintext LLM proxy logs in Docker mode and encrypted MITM traffic logs in Lima mode;
- scrub-rule management for captured text;
- Temporal workflow start/status/definition endpoints when Temporal is available.

The controller accepts `?token=...`, `Authorization: Bearer ...`, or a `clawfactory_session` cookie when `CONTROLLER_API_TOKEN` is configured. If no controller token is configured, most controller endpoints are open.

## Docs

- [Setup](docs/setup.md)
- [Commands](docs/commands.md)
- [Architecture](docs/architecture.md)
- [Sandboxing](docs/sandbox.md)
- [Snapshots](docs/snapshots.md)
- [Controller API](docs/controller-api.md)
- [API keys and secrets](docs/API-KEYS.md)
- [Temporal](docs/temporal.md)
- [Known issues log](docs/issues-log.md)

## Current Caveats

The docs now describe what the code does, not every plan the project has had. Some older planning ideas are not implemented, especially GitHub PR promotion endpoints and some tests that still refer to removed or unfinished routes. The tracked issues are in [docs/issues-log.md](docs/issues-log.md).
