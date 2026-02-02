# ClawFactory

Local-first autonomous agent runtime with hard separation between proposal and authority.

## Philosophy

> Discord is UI, GitHub is authority.
> The bot may propose, but can never silently promote or persist changes.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         Host VM                                  │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │                    Docker Compose                            ││
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  ││
│  │  │   Gateway   │  │   Runner    │  │     Controller      │  ││
│  │  │  (OpenClaw) │◄─┤  (Python)   │  │  (Python/FastAPI)   │  ││
│  │  │             │  │             │  │                     │  ││
│  │  │ • Discord   │  │ • Tools     │  │ • GitHub webhooks   │  ││
│  │  │ • LLM calls │  │ • Git       │  │ • Promotion logic   │  ││
│  │  │ • Read-only │  │ • Proposals │  │ • Approval UI       │  ││
│  │  │   brain     │  │   only      │  │                     │  ││
│  │  └──────┬──────┘  └──────┬──────┘  └──────────┬──────────┘  ││
│  │         │    Unix Socket │                     │             ││
│  │         └────────────────┘                     │             ││
│  └────────────────────────────────────────────────┼─────────────┘│
│                                                   │              │
│  ┌────────────────────────────────────────────────┼─────────────┐│
│  │                    Volumes                     │             ││
│  │  brain_ro/     brain_work/     secrets/     audit/          ││
│  │  (read-only)   (proposals)     (600)        (append)        ││
│  └──────────────────────────────────────────────────────────────┘│
│                                                                  │
│  [Kill Switch] ─────────────────────────────────────────────────│
└──────────────────────────────────────────────────────────────────┘
```

## Components

| Component | Role | Can Write To | Cannot Access |
|-----------|------|--------------|---------------|
| **Gateway** | Agent runtime (OpenClaw) | Nothing | brain_work, secrets |
| **Runner** | Tool execution | brain_work only | brain_ro, Docker socket |
| **Controller** | Authority & promotion | brain_ro (promotion only) | - |

## Quick Start

```bash
# 1. Clone and setup
git clone https://github.com/elimaine/clawfactory
cd clawfactory
./install.sh

# 2. Edit secrets (install.sh will prompt, or edit manually)
vim secrets/secrets.yml

# 3. Start
docker compose up -d

# 4. Check status
docker compose ps
./clawfactory.sh status
```

## Promotion Flow (Online)

1. Bot edits `brain_work/`
2. Bot creates branch + commit
3. Bot opens PR on GitHub
4. **Human merges PR** ← Authority checkpoint
5. GitHub webhook → Controller
6. Controller promotes to `brain_ro/`
7. Gateway restarts with new config

## Promotion Flow (Offline)

1. Bot commits to `brain_work/`
2. Bot DMs diff + SHA to human
3. Human opens Controller UI (Tailscale)
4. **Human clicks Promote** ← Authority checkpoint
5. Controller promotes to `brain_ro/`
6. Gateway restarts

## Kill Switch

```bash
# Immediate containment - stops everything, drops network
./killswitch.sh lock

# Restore after incident
./killswitch.sh restore
```

## Safety Invariants

1. Active brain is immutable at runtime
2. Bot cannot promote itself
3. Runner cannot escalate to host
4. Discord is not an authority signal
5. Kill switch always wins
6. Offline mode remains functional

## Documentation

- [SANDYCLAW-DESIGN.md](SANDYCLAW-DESIGN.md) - Full architecture specification
- [docs/setup.md](docs/setup.md) - Detailed setup guide
- [docs/secrets.md](docs/secrets.md) - Secrets configuration

## License

MIT
