# ClawFactory

> **Status**: Work in progress - major hurdles overcome! this is midstage looking good, but still unstable for new installs. i recommend installing with claude. this was built on latest osx on m4 apple silicon. 

## Yo Dogg I heard you like OpenClaw Agents
> so we put a VM in your VM, spun up 5 of them, and added a controller.

## Vms all the way down.
> The bot may propose, but can never silently promote or persist changes.

This project has 3 modes: 

- nested virtualization (recommended, osx apple silicon implemented)
- single layer virtualization, openclaw gateway sandboxed
- and no virtualization, with agents sandboxed through openclaw

Pulls from latest version of openclaw but you can also swap in your workspace.

Your agents talk through channels (Discord, Telegram, Slack, and more via OpenClaw extensions), but every meaningful change flows through the controller. Soft lock on bot promotion, need to be taught to push version proposals. No silent mutations should last long term. Humans hold the merge to long term state.

## Launch Sequence

```bash
git clone https://github.com/elimaine/clawfactory
cd clawfactory
./install.sh              # Interactive setup — provisions your first agent
./clawfactory.sh start    # Bring systems online
./clawfactory.sh info     # Display access credentials
```

Once online:
- **Gateway UI**: http://localhost:18789?token=YOUR_GATEWAY_TOKEN
- **Mission Control**: http://localhost:8080/controller?token=YOUR_CONTROLLER_TOKEN

The installer generates all auth tokens automatically. Run `./clawfactory.sh info` to reveal them.

## Requirements

- **Docker** with Docker Compose
- **Git**
- **GitHub CLI** (`gh`) — authenticated with `gh auth login`
- API keys for your chosen AI providers — see the [API Key Guide](docs/API-KEYS.md)

## How It Works

Three subsystems behind a reverse proxy, each with a distinct role:

| Subsystem | Function |
|-----------|----------|
| **Proxy** (nginx) | Front door — routes traffic on :18789 and :8080 |
| **Gateway** (OpenClaw) | The agent brain — channels, LLM calls, tool execution, memory |
| **Controller** (FastAPI) | Mission control — webhooks, promotion authority, snapshots, device pairing |

The bot proposes changes via git branches. Humans approve via PR merge or the Controller UI. The gateway cannot promote itself — ever.

## Containment Protocols

These invariants hold at all times, no exceptions:

1. Bots propose via git branches — they cannot merge to main
2. No self-promotion — requires human merge or explicit UI action
3. Sandbox boundaries are enforced (Sysbox on Linux, Lima VM on macOS)
4. Chat messages carry zero authority
5. The kill switch always wins
6. All sensitive state is encrypted at rest (age encryption)
7. Private keys never touch GitHub
8. Tool execution is isolated when sandbox mode is active

## Emergency Shutdown

If an agent goes sideways, pull the plug instantly:

```bash
./killswitch.sh lock      # Everything stops. Now.
./killswitch.sh restore   # Bring systems back after review
```

## Field Manual

- [Setup Guide](docs/setup.md) — Installation, secrets, remote access
- [Architecture](docs/architecture.md) — System diagrams, data flow, promotion pipeline
- [Commands](docs/commands.md) — Full CLI reference
- [Sandbox](docs/sandbox.md) — Containment modes (none, Sysbox, Lima VM)
- [Snapshots](docs/snapshots.md) — Encrypted backups, memory systems, bot state
- [Controller API](docs/controller-api.md) — Endpoints and the config editor
- [API Keys](docs/API-KEYS.md) — Provider key setup

## License

MIT
