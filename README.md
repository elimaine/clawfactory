# ClawFactory

Local-first autonomous agent runtime with hard separation between proposal and authority.

## Philosophy

> Discord is UI, GitHub is authority.
> The bot may propose, but can never silently promote or persist changes.

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                         Host VM                               │
│  ┌──────────────────────────────────────────────────────────┐│
│  │                    Docker Compose                         ││
│  │  ┌───────────────────────┐  ┌─────────────────────────┐  ││
│  │  │        Gateway        │  │       Controller        │  ││
│  │  │       (OpenClaw)      │  │    (Python/FastAPI)     │  ││
│  │  │                       │  │                         │  ││
│  │  │  • Discord            │  │  • GitHub webhooks      │  ││
│  │  │  • LLM calls          │  │  • Promotion logic      │  ││
│  │  │  • Native sandbox     │  │  • Approval UI          │  ││
│  │  │  • Tool execution     │  │                         │  ││
│  │  └───────────────────────┘  └────────────┬────────────┘  ││
│  └──────────────────────────────────────────┼───────────────┘│
│                                             │                │
│  ┌──────────────────────────────────────────┼───────────────┐│
│  │                   Volumes                │                ││
│  │  brain_ro/    brain_work/    secrets/    audit/          ││
│  │  (read-only)  (proposals)    (600)       (append)        ││
│  └──────────────────────────────────────────────────────────┘│
│                                                              │
│  [Kill Switch] ─────────────────────────────────────────────│
└──────────────────────────────────────────────────────────────┘
```

## Components

| Component | Role | Can Write To | Cannot Access |
|-----------|------|--------------|---------------|
| **Gateway** | Agent runtime (OpenClaw) | brain_work (via sandbox) | secrets |
| **Controller** | Authority & promotion | brain_ro (promotion only) | - |

## Quick Start

### Option A: Interactive Setup

```bash
git clone https://github.com/elimaine/clawfactory
cd clawfactory
./install.sh              # Prompts for all secrets interactively
./clawfactory.sh start    # Start containers
```

### Option B: Manual Setup

```bash
git clone https://github.com/elimaine/clawfactory
cd clawfactory

# 1. Copy example secrets
cp secrets/secrets.yml.example secrets/secrets.yml
cp secrets/gateway.env.example secrets/gateway.env
cp secrets/controller.env.example secrets/controller.env

# 2. Edit with your values
vim secrets/secrets.yml
vim secrets/gateway.env
vim secrets/controller.env

# 3. Lock down permissions
chmod 600 secrets/secrets.yml secrets/gateway.env secrets/controller.env

# 4. Initialize brain repository
mkdir -p sandyclaws/{brain.git,brain_ro,brain_work}
git init --bare sandyclaws/brain.git
cd sandyclaws/brain_work && git init && git remote add origin ../brain.git

# 5. Start
docker compose up -d
```

### Verify

```bash
./clawfactory.sh status
./clawfactory.sh logs gateway
```

## Secrets Configuration

| File | Purpose |
|------|---------|
| `secrets/secrets.yml` | Main config (mode, discord, github, anthropic) |
| `secrets/gateway.env` | Gateway container environment |
| `secrets/controller.env` | Controller container environment |

Required values:
- **Discord bot token** - from [Discord Developer Portal](https://discord.com/developers/applications)
- **Discord user ID** - right-click your name → Copy User ID
- **Anthropic API key** - from [Anthropic Console](https://console.anthropic.com/)
- **GitHub webhook secret** - generate with `openssl rand -hex 32`

## Promotion Flow (Online)

1. Bot edits `sandyclaws/brain_work/`
2. Bot creates branch + commit
3. Bot opens PR on GitHub
4. **Human merges PR** ← Authority checkpoint
5. GitHub webhook → Controller
6. Controller promotes to `sandyclaws/brain_ro/`
7. Gateway restarts with new config

## Promotion Flow (Offline)

1. Bot commits to `sandyclaws/brain_work/`
2. Bot DMs diff + SHA to human
3. Human opens Controller UI (Tailscale)
4. **Human clicks Promote** ← Authority checkpoint
5. Controller promotes to `sandyclaws/brain_ro/`
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
3. Gateway sandbox cannot escalate to host
4. Discord is not an authority signal
5. Kill switch always wins
6. Offline mode remains functional

## Documentation

- [SANDYCLAW-DESIGN.md](SANDYCLAW-DESIGN.md) - Full architecture specification
- [docs/setup.md](docs/setup.md) - Detailed setup guide
- [docs/secrets.md](docs/secrets.md) - Secrets configuration

## License

MIT
