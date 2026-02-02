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
│  │  │  • Native sandbox     │  │  • Memory backup        │  ││
│  │  │  • Memory (Gemini)    │  │  • Approval UI          │  ││
│  │  └───────────────────────┘  └────────────┬────────────┘  ││
│  └──────────────────────────────────────────┼───────────────┘│
│                                             │                │
│  ┌──────────────────────────────────────────┼───────────────┐│
│  │                   Volumes                │                ││
│  │  brain_ro/    brain_work/    secrets/    audit/          ││
│  │  (approved)   (proposals)    (600)       (append)        ││
│  └──────────────────────────────────────────────────────────┘│
│                                                              │
│  [Kill Switch] ─────────────────────────────────────────────│
└──────────────────────────────────────────────────────────────┘

GitHub: your-username/sandyclaws-brain (fork of openclaw/openclaw)
         └── workspace/
             ├── SOUL.md, TOOLS.md, etc.
             ├── skills/
             └── memory/  ← agent memories backed up here
```

## Components

| Component | Role | Can Write To | Cannot Access |
|-----------|------|--------------|---------------|
| **Gateway** | Agent runtime (OpenClaw) | proposals, memory | secrets |
| **Controller** | Authority & promotion | brain_ro (via git pull) | - |

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

# 1. Fork OpenClaw on GitHub
# Go to https://github.com/openclaw/openclaw and click Fork
# Name it: sandyclaws-brain

# 2. Clone your fork locally
mkdir -p sandyclaws
git clone https://github.com/YOUR_USERNAME/sandyclaws-brain.git sandyclaws/brain_work
git clone https://github.com/YOUR_USERNAME/sandyclaws-brain.git sandyclaws/brain_ro

# 3. Add brain files to workspace/
mkdir -p sandyclaws/brain_work/workspace
# Create SOUL.md, TOOLS.md, etc. in workspace/
git -C sandyclaws/brain_work add workspace/ && git -C sandyclaws/brain_work commit -m "Add brain files" && git -C sandyclaws/brain_work push

# 4. Copy and edit secrets
cp secrets/gateway.env.example secrets/gateway.env
cp secrets/controller.env.example secrets/controller.env
# Edit with your values (Discord token, Anthropic key, Gemini key)
chmod 600 secrets/*.env

# 5. Start
docker compose up -d
```

### Verify

```bash
./clawfactory.sh status
./clawfactory.sh logs gateway
curl http://localhost:8080/status
```

## Secrets Configuration

| File | Purpose |
|------|---------|
| `secrets/secrets.yml` | Main config (mode, discord, github, anthropic, gemini) |
| `secrets/gateway.env` | Gateway container environment |
| `secrets/controller.env` | Controller container environment |

Required values:
- **Discord bot token** - from [Discord Developer Portal](https://discord.com/developers/applications)
- **Discord user ID** - right-click your name → Copy User ID
- **Anthropic API key** - from [Anthropic Console](https://console.anthropic.com/)
- **Gemini API key** - from [Google AI Studio](https://aistudio.google.com/app/apikey) (for memory embeddings)
- **GitHub webhook secret** - generate with `openssl rand -hex 32`

## Promotion Flow (Online)

1. Bot edits files in `sandyclaws/brain_work/workspace/`
2. Bot commits and pushes to proposal branch
3. Bot opens PR on GitHub
4. **Human merges PR** ← Authority checkpoint
5. GitHub webhook → Controller
6. Controller pulls main to `sandyclaws/brain_ro/`
7. Gateway restarts with new config

## Promotion Flow (Offline)

1. Bot commits to `sandyclaws/brain_work/`
2. Human opens Controller UI (http://localhost:8080/promote)
3. **Human clicks Promote** ← Authority checkpoint
4. Controller pulls to `sandyclaws/brain_ro/`
5. Gateway restarts

## Memory

Agent memory is stored as Markdown files:
- `workspace/memory/YYYY-MM-DD.md` - Daily logs
- `workspace/MEMORY.md` - Long-term curated memory

Memory persists across restarts. To backup to GitHub:
```bash
curl -X POST http://localhost:8080/memory/backup
```

The agent can also call this endpoint using the `memory-backup` skill.

## Controller Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/promote` | GET | Approval UI |
| `/promote` | POST | Promote specific SHA |
| `/promote/main` | POST | Pull latest main |
| `/memory/backup` | POST | Backup memory to GitHub |
| `/memory/status` | GET | List memory files |
| `/status` | GET | System status |
| `/health` | GET | Health check |

## Kill Switch

```bash
# Immediate containment - stops everything, drops network
./killswitch.sh lock

# Restore after incident
./killswitch.sh restore
```

## Safety Invariants

1. Active brain is immutable at runtime (read from brain_ro)
2. Bot cannot promote itself (requires human merge or UI action)
3. Gateway sandbox cannot escalate to host
4. Discord is not an authority signal
5. Kill switch always wins
6. Memory persists but requires explicit backup to GitHub

## Updating OpenClaw

Since brain_work/brain_ro are forks of OpenClaw, you can merge upstream updates:

```bash
cd sandyclaws/brain_work
git remote add upstream https://github.com/openclaw/openclaw.git
git fetch upstream
git merge upstream/main
git push origin main
```

## Documentation

- [SANDYCLAW-DESIGN.md](SANDYCLAW-DESIGN.md) - Full architecture specification
- [docs/TODO-cloudflare-zerotrust.md](docs/TODO-cloudflare-zerotrust.md) - Egress control setup
- [docs/TODO-expose-controller.md](docs/TODO-expose-controller.md) - Webhook ingress setup

## License

MIT
