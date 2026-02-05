# ClawFactory
> **Status**: Work in progress - found a deeper architectural issue as of 4/2/26, basically we cant run docker in docker for subagents. this works single threaded, but cron jobs wont work until i take the gateway out of its own isolation.. which was half the security model, and would kill the multiple independent agents ecosystems idea..

Local-capable openclaw sandboxed agent manager. support for easily starting multiple local bots (with orchestration for remote bots possibly coming), current security features are snapshots, an external sandboxed controller gui so you dont need to use the control cli, git control of personality backups, a restore system, killswitch.. and more!

## Quick Start

```bash
git clone https://github.com/elimaine/clawfactory
cd clawfactory
./install.sh              # Interactive setup (prompts for secrets + instance name)
./clawfactory.sh start    # Start containers
./clawfactory.sh info     # Show instance info and access tokens
```

Access:
- **Gateway UI**: http://localhost:18789?token=YOUR_GATEWAY_TOKEN
- **Controller**: http://localhost:8080/controller?token=YOUR_CONTROLLER_TOKEN

The install script generates authentication tokens. Run `./clawfactory.sh info` to see them.

## Philosophy

> Chat is UI, GitHub is authority.
> The bot may propose, but can never silently promote or persist changes.

Supported channels: Discord, Telegram, Slack (more via OpenClaw extensions)

## Prerequisites

- **Docker** with Docker Compose
- **Git**
- **GitHub CLI** (`gh`) - authenticated with `gh auth login`
- API keys for your chosen providers - see [API Key Guide](docs/API-KEYS.md)

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                         Host VM                              │
│  ┌──────────────────────────────────────────────────────────┐│
│  │                    Docker Compose                        ││
│  │                                                          ││
│  │  ┌─────────┐    ┌───────────────┐    ┌──────────────┐   ││
│  │  │  Proxy  │───►│    Gateway    │    │  Controller  │   ││
│  │  │ (nginx) │    │   (OpenClaw)  │    │   (FastAPI)  │   ││
│  │  │         │───►│               │    │              │   ││
│  │  └────┬────┘    │ • Channels    │    │ • Webhooks   │   ││
│  │       │         │ • LLM calls   │    │ • Promotion  │   ││
│  │       │         │ • Sandbox     │    │ • Snapshots  │   ││
│  │  localhost      │ • Memory      │    │ • Pairing    │   ││
│  │  :18789/:8080   └───────────────┘    └──────────────┘   ││
│  └──────────────────────────────────────────────────────────┘│
│                                                              │
│  ┌──────────────────────────────────────────────────────────┐│
│  │                   Volumes                                ││
│  │  approved/     state/          snapshots/    secrets/   ││
│  │  (git repo)    (runtime)       (encrypted)   (600)      ││
│  └──────────────────────────────────────────────────────────┘│
│                                                              │
│  [Kill Switch] ─────────────────────────────────────────────│
└──────────────────────────────────────────────────────────────┘

GitHub: your-org/{instance}-bot (fork of openclaw/openclaw)
         └── workspace/
             ├── SOUL.md, TOOLS.md, etc.
             ├── skills/
             ├── memory/           ← memory markdown (git tracked)
             └── {instance}_save/  ← bot's declarative state
                 └── package.json  ← dependencies (installed on startup)
```

## Directory Structure

```
bot_repos/
├── bot1/
│   ├── approved/              # Git clone (bot pushes branches, PRs merged)
│   │   └── workspace/
│   │       ├── SOUL.md        # Bot personality
│   │       ├── skills/        # Bot skills
│   │       ├── memory/        # Memory markdown (git tracked)
│   │       └── bot1_save/     # Bot's declarative save state
│   │           └── package.json
│   └── state/                 # Runtime state (encrypted snapshots)
│       ├── memory/main.sqlite # Embeddings database
│       ├── openclaw.json      # Runtime config
│       ├── devices/           # Paired devices
│       ├── credentials/       # Allowlists
│       └── installed/         # npm packages (rebuilt, not snapshotted)

snapshots/
└── bot1/
    ├── snapshot-2026-02-03T12-00-00Z.tar.age
    └── latest.tar.age -> snapshot-...

secrets/
├── bot1/
│   ├── gateway.env            # API keys
│   ├── controller.env         # Webhook secrets
│   └── snapshot.key           # Age encryption key
└── tokens.env                 # Token registry
```

## Components

| Component | Role | Listens On | Can Write To |
|-----------|------|------------|--------------|
| **Proxy** | Reverse proxy, localhost access | localhost:18789, :8080 | - |
| **Gateway** | Agent runtime (OpenClaw) | Docker network only | approved/ (git branches), state/ |
| **Controller** | Authority & promotion | Docker network only | approved/ (git pull), snapshots/ |

## Setup Options

### Option A: Interactive Setup (Recommended)

```bash
./install.sh
```

This prompts for:
- **Instance name** (e.g., `bot1`, `prod-agent`) - used for container naming
- Channel tokens (Discord, Telegram, Slack)
- **AI Providers** (select one or more):
  - `1` Anthropic (Claude) - Recommended primary
  - `2` Kimi-K2 (Moonshot) - High-performance alternative
  - `3` OpenAI (GPT-4)
  - `4` Google (Gemini) - Also used for memory embeddings
  - `5` Ollama (Local) - Run models locally
  - `6` OpenRouter - Multi-provider gateway
- GitHub organization (optional) - keeps bot repos organized under an org
- GitHub setup (forks openclaw/openclaw as {instance}-bot)

Example: Enter `1 4 5` to use Anthropic + Gemini + Ollama.

The installer generates:
- **Gateway token** - for authenticating to the OpenClaw gateway API
- **Controller token** - for accessing the Controller dashboard
- **Snapshot key** - age keypair for encrypted state snapshots

### Option B: Manual Setup

```bash
# 1. Fork OpenClaw on GitHub
# Go to https://github.com/openclaw/openclaw and click Fork
# Name it: {instance}-bot (e.g., bot1-bot)
# Optional: Fork to an organization (e.g., my-bots-org/bot1-bot)

# 2. Create instance directory structure
mkdir -p bot_repos/bot1/{approved,state}

# 3. Clone your fork
git clone https://github.com/YOUR_ORG_OR_USERNAME/bot1-bot.git bot_repos/bot1/approved

# 4. Add config files to workspace/
mkdir -p bot_repos/bot1/approved/workspace/bot1_save
# Create SOUL.md, TOOLS.md, etc. in workspace/
git -C bot_repos/bot1/approved add workspace/
git -C bot_repos/bot1/approved commit -m "Add config files"
git -C bot_repos/bot1/approved push

# 5. Copy and edit secrets
mkdir -p secrets/bot1
cp secrets/gateway.env.example secrets/bot1/gateway.env
cp secrets/controller.env.example secrets/bot1/controller.env
# Edit with your values
chmod 600 secrets/bot1/*.env

# 6. Generate snapshot encryption key
./clawfactory.sh snapshot keygen

# 7. Start
./clawfactory.sh start
```

## Secrets Configuration

Each instance has its own secrets folder:

```
secrets/
├── bot1/
│   ├── gateway.env      # Channel tokens, AI provider keys
│   ├── controller.env   # GitHub webhook secret, tokens
│   └── snapshot.key     # Age private key (for encrypted snapshots)
├── bot2/
│   └── ...
└── tokens.env           # Token registry for all instances
```

| File | Contents |
|------|----------|
| `secrets/<instance>/gateway.env` | Channel tokens (Discord/Telegram/Slack), AI provider keys, Gateway token |
| `secrets/<instance>/controller.env` | GitHub webhook secret, Controller token |
| `secrets/<instance>/snapshot.key` | Age private key for encrypted snapshots |
| `secrets/tokens.env` | Token registry for all instances |
| `.clawfactory.conf` | Default instance name |

Tokens are auto-generated by `install.sh`. To view them:
```bash
./clawfactory.sh info
./clawfactory.sh -i bot1 info   # For specific instance
```

## Commands

```bash
./clawfactory.sh start              # Start default instance
./clawfactory.sh stop               # Stop default instance
./clawfactory.sh restart            # Restart containers
./clawfactory.sh status             # Show container status
./clawfactory.sh info               # Show instance name and tokens
./clawfactory.sh list               # List all instances and containers
./clawfactory.sh logs [service]     # Follow logs (gateway/proxy/controller)
./clawfactory.sh shell [service]    # Shell into container
./clawfactory.sh controller         # Show controller URL
./clawfactory.sh audit              # Show recent audit log

# Snapshots (encrypted state backup)
./clawfactory.sh snapshot create    # Create encrypted snapshot
./clawfactory.sh snapshot list      # List available snapshots
./clawfactory.sh snapshot restore <file>  # Restore from snapshot
./clawfactory.sh snapshot keygen    # Generate encryption keys

# Multi-instance support
./clawfactory.sh -i bot1 start      # Start 'bot1' instance
./clawfactory.sh -i bot1 stop       # Stop 'bot1' instance
./clawfactory.sh -i bot1 info       # Show 'bot1' info and tokens
```

## Promotion Flow

### Online (GitHub)

1. Bot edits files in `approved/workspace/`
2. Bot commits and pushes to proposal branch
3. Bot opens PR on GitHub
4. **Human merges PR** ← Authority checkpoint
5. GitHub webhook → Controller
6. Controller pulls main to `approved/`
7. Gateway restarts with new config

### Offline (Local UI)

1. Bot commits to `approved/` (feature branch)
2. Human opens Controller dashboard (http://localhost:8080/controller?token=...)
3. **Human clicks Promote** ← Authority checkpoint
4. Controller pulls main
5. Gateway restarts

## Memory & Embeddings

Memory is stored in `state/` and backed up via encrypted snapshots.

| Type | Location | Backup |
|------|----------|--------|
| Daily logs | `state/workspace/memory/YYYY-MM-DD.md` | Encrypted snapshots |
| Long-term memory | `state/workspace/MEMORY.md` | Encrypted snapshots |
| Vector embeddings | `state/memory/main.sqlite` | Encrypted snapshots |

Memory stays in state/ (not git) because:
- Vector DB requires real files for indexing
- Frequent changes would create noisy git history
- Encrypted snapshots keep content private

## Encrypted Snapshots

Snapshots capture runtime state that isn't in git:
- Embeddings database (`memory/main.sqlite`)
- Configuration (`openclaw.json`)
- Paired devices and credentials
- Device identity keys

**NOT included** (rebuilt from git):
- `installed/` - npm packages from `{instance}_save/package.json`

### Usage

```bash
# Generate encryption key (once per instance)
./clawfactory.sh snapshot keygen

# Create snapshot
./clawfactory.sh snapshot create

# List snapshots
./clawfactory.sh snapshot list

# Restore (stop gateway first!)
./clawfactory.sh stop
./clawfactory.sh snapshot restore latest
./clawfactory.sh start
```

The bot can also trigger snapshots via the Controller API:
```bash
curl -X POST http://localhost:8080/snapshot
```

## Bot Save State

The `{instance}_save/` directory holds declarative state that the bot wants persisted:

```
workspace/{instance}_save/
├── package.json        # npm dependencies (installed on container start)
├── config.json         # Bot-specific configuration
└── tools/              # Scripts the bot created
```

Changes to `{instance}_save/` go through the normal PR flow:
1. Bot edits files
2. Bot commits and pushes branch
3. Bot opens PR
4. Human merges
5. Gateway restarts and installs any new packages

## Controller API

All endpoints require authentication via `?token=` query param or session cookie.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/controller` | GET | Dashboard UI |
| `/controller` | POST | Promote specific SHA |
| `/controller/promote-main` | POST | Pull latest main & restart |
| `/memory/backup` | POST | Backup memory to GitHub |
| `/memory/status` | GET | Memory files and embeddings status |
| `/snapshot` | POST | Create encrypted snapshot |
| `/snapshot` | GET | List available snapshots |
| `/status` | GET | System status |
| `/health` | GET | Health check |
| `/audit` | GET | Get audit log entries |
| `/gateway/restart` | POST | Restart the gateway container |
| `/gateway/config` | GET | Get gateway openclaw.json + Ollama models |
| `/gateway/config` | POST | Save config & restart gateway |
| `/gateway/config/validate` | GET | Validate config against OpenClaw schema |
| `/gateway/devices` | GET | List pending/paired devices |
| `/gateway/devices/approve` | POST | Approve device pairing |
| `/gateway/devices/reject` | POST | Reject device pairing |
| `/gateway/pairing/{channel}` | GET | List DM pairing requests |
| `/gateway/pairing/approve` | POST | Approve DM pairing code |
| `/gateway/security-audit` | GET | Run OpenClaw security audit |

### Config Editor

The Controller dashboard includes a Gateway Config editor with:

- **Load/Save** - Edit `openclaw.json` directly, saves and restarts gateway
- **Validation** - Validates config against OpenClaw schema before saving
- **Ollama detection** - Auto-discovers local Ollama models with click-to-add
- **RAM-based context** - Calculates safe context windows based on available RAM
- **Error navigation** - Clickable JSON errors with "Open in VS Code" and "Jump to line" links
- **Live validation** - Shows JSON syntax errors as you type

## Kill Switch

```bash
./killswitch.sh lock      # Stop everything, drop network
./killswitch.sh restore   # Restore after incident
```

## Safety Invariants

1. Bot proposes changes via git branches, cannot merge to main itself
2. Bot cannot promote itself (requires human merge or UI action)
3. Gateway sandbox cannot escalate to host
4. Chat messages are not authority signals
5. Kill switch always wins
6. Sensitive state is encrypted (snapshots use age encryption)
7. Private keys never go to GitHub

## Updating OpenClaw

Since bot repos are forks of OpenClaw, merge upstream updates:

```bash
cd bot_repos/bot1/approved
git remote add upstream https://github.com/openclaw/openclaw.git
git fetch upstream
git merge upstream/main
git push origin main
```

## Remote Access

### Tailscale (Recommended for personal use)

```bash
# Expose gateway and controller on your tailnet
tailscale serve --bg --set-path /<instance> http://127.0.0.1:18789
tailscale serve --bg --set-path /<instance>/controller http://127.0.0.1:8080
```

Access via `https://your-machine.tailnet.ts.net/<instance>/`

### Cloudflare Zero Trust

For public access with authentication, see `todo/cloudflare/` for Cloudflare tunnel setup.

## Documentation

- [DESIGN.md](DESIGN.md) - Full architecture specification

## License

MIT
