# ClawFactory

> **Status**: Work in progress - i recommend installing with claude while this is unstable. this was built in latest osx on m5 apple silicon. nested virtualization isnt easy, but aside from renting a server its one if the best ways i found to massively reduces blast radious. you can also run this with just docker which gives it a smaller memory footprint but you lose a level of blast radious which could be everything the bot has access to.

A local-first launch platform for autonomous OpenClaw agents. Spin up sandboxed bots, wire them into Discord/Telegram/Slack, and let them loose — with you holding the leash. Encrypted snapshots, a mission control dashboard, git-backed personality versioning, instant restore, a kill switch for when things get weird, and full multi-agent fleet support.

## The Prime Directive

> Chat is UI, GitHub is authority.
> The bot may propose, but can never silently promote or persist changes.

Your agents talk through channels (Discord, Telegram, Slack, and more via OpenClaw extensions), but every meaningful change flows through git. No bot promotes itself. No silent mutations. Humans hold the merge button.

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
