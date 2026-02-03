# ClawFactory

Local-first autonomous agent runtime with hard separation between proposal and authority.

> **Status**: Work in progress. The bot can propose changes but requires human approval (via GitHub PR or Controller UI) to promote them.

## Quick Start

```bash
git clone https://github.com/elimaine/clawfactory
cd clawfactory
./install.sh              # Interactive setup (prompts for secrets + instance name)
./clawfactory.sh start    # Start containers
./clawfactory.sh info     # Show instance info and access tokens
```

Access:
- **Gateway UI**: http://localhost:18789
- **Controller**: http://localhost:8080/controller?token=YOUR_TOKEN

The install script generates authentication tokens. Run `./clawfactory.sh info` to see them.

## Philosophy

> Discord is UI, GitHub is authority.
> The bot may propose, but can never silently promote or persist changes.

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
│  │  └────┬────┘    │ • Discord     │    │ • Webhooks   │   ││
│  │       │         │ • LLM calls   │    │ • Promotion  │   ││
│  │       │         │ • Sandbox     │    │ • Memory     │   ││
│  │  localhost      │ • Memory      │    │   backup     │   ││
│  │  :18789/:8080   └───────────────┘    └──────────────┘   ││
│  └──────────────────────────────────────────────────────────┘│
│                                                              │
│  ┌──────────────────────────────────────────────────────────┐│
│  │                   Volumes                                ││
│  │  brain_ro/    brain_work/    secrets/    audit/         ││
│  │  (approved)   (proposals)    (600)       (append)       ││
│  └──────────────────────────────────────────────────────────┘│
│                                                              │
│  [Kill Switch] ─────────────────────────────────────────────│
└──────────────────────────────────────────────────────────────┘

GitHub: your-username/{instance}-brain (fork of openclaw/openclaw)
         └── workspace/
             ├── SOUL.md, TOOLS.md, etc.
             ├── skills/
             └── memory/  ← agent memories backed up here
```

## Components

| Component | Role | Listens On | Can Write To |
|-----------|------|------------|--------------|
| **Proxy** | Reverse proxy, localhost access | localhost:18789, :8080 | - |
| **Gateway** | Agent runtime (OpenClaw) | Docker network only | proposals, memory |
| **Controller** | Authority & promotion | Docker network only | brain_ro (via git) |

## Setup Options

### Option A: Interactive Setup (Recommended)

```bash
./install.sh
```

This prompts for:
- **Instance name** (e.g., `bot1`, `prod-agent`) - used for container naming and multi-instance support
- Discord bot token
- Anthropic API key
- Gemini API key (for memory embeddings)
- GitHub setup (forks openclaw/openclaw as {instance}-brain)

The installer generates two tokens:
- **Gateway token** - for authenticating to the OpenClaw gateway API
- **Controller token** - for accessing the Controller dashboard

### Option B: Manual Setup

```bash
# 1. Fork OpenClaw on GitHub
# Go to https://github.com/openclaw/openclaw and click Fork
# Name it: {instance}-brain (e.g., bot1-brain)

# 2. Clone your fork locally
mkdir -p data
git clone https://github.com/YOUR_USERNAME/bot1-brain.git data/brain_work
git clone https://github.com/YOUR_USERNAME/bot1-brain.git data/brain_ro

# 3. Add brain files to workspace/
mkdir -p data/brain_work/workspace
# Create SOUL.md, TOOLS.md, etc. in workspace/
git -C data/brain_work add workspace/
git -C data/brain_work commit -m "Add brain files"
git -C data/brain_work push

# 4. Copy and edit secrets
cp secrets/gateway.env.example secrets/gateway.env
cp secrets/controller.env.example secrets/controller.env
# Edit with your values
chmod 600 secrets/*.env

# 5. Start
./clawfactory.sh start
```

## Secrets Configuration

Each instance has its own secrets folder:

```
secrets/
├── bot1/
│   ├── gateway.env      # Discord token, Anthropic key, Gemini key
│   └── controller.env   # GitHub webhook secret, tokens
├── bot2/
│   ├── gateway.env
│   └── controller.env
└── tokens.env           # Token registry for all instances
```

| File | Contents |
|------|----------|
| `secrets/<instance>/gateway.env` | Discord token, Anthropic key, Gemini key, Gateway token |
| `secrets/<instance>/controller.env` | GitHub webhook secret, Controller token, Gateway token |
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

# Multi-instance support
./clawfactory.sh -i bot1 start      # Start 'bot1' instance
./clawfactory.sh -i bot1 stop       # Stop 'bot1' instance
./clawfactory.sh -i bot1 info       # Show 'bot1' info and tokens
```

## Promotion Flow

### Online (GitHub)

1. Bot edits files in `brain_work/workspace/`
2. Bot commits and pushes to proposal branch
3. Bot opens PR on GitHub
4. **Human merges PR** ← Authority checkpoint
5. GitHub webhook → Controller
6. Controller pulls to `brain_ro/`
7. Gateway restarts with new config

### Offline (Local UI)

1. Bot commits to `brain_work/`
2. Human opens Controller dashboard (http://localhost:8080/controller?token=...)
3. **Human clicks Promote** ← Authority checkpoint
4. Controller pulls to `brain_ro/`
5. Gateway restarts

## Memory

Agent memory is stored as Markdown files:
- `workspace/memory/YYYY-MM-DD.md` - Daily logs
- `workspace/MEMORY.md` - Long-term curated memory

Memory persists across restarts. To backup to GitHub:
```bash
curl -X POST http://localhost:8080/memory/backup
```

The agent can also trigger this via the `memory-backup` skill.

## Controller API

All endpoints require authentication via `?token=` query param or session cookie.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/controller` | GET | Dashboard UI |
| `/controller` | POST | Promote specific SHA |
| `/controller/promote-main` | POST | Pull latest main & restart |
| `/memory/backup` | POST | Backup memory to GitHub |
| `/memory/status` | GET | List memory files |
| `/status` | GET | System status |
| `/health` | GET | Health check |
| `/audit` | GET | Get audit log entries |
| `/gateway/devices` | GET | List pending/paired devices |
| `/gateway/devices/approve` | POST | Approve device pairing |
| `/gateway/devices/reject` | POST | Reject device pairing |
| `/gateway/pairing/{channel}` | GET | List DM pairing requests |
| `/gateway/pairing/approve` | POST | Approve DM pairing code |
| `/gateway/security-audit` | GET | Run OpenClaw security audit |

## Kill Switch

```bash
./killswitch.sh lock      # Stop everything, drop network
./killswitch.sh restore   # Restore after incident
```

## Safety Invariants

1. Active brain is immutable at runtime (read from brain_ro)
2. Bot cannot promote itself (requires human merge or UI action)
3. Gateway sandbox cannot escalate to host
4. Discord is not an authority signal
5. Kill switch always wins
6. Memory persists but requires explicit backup to GitHub

## Updating OpenClaw

Since brain repos are forks of OpenClaw, merge upstream updates:

```bash
cd data/brain_work
git remote add upstream https://github.com/openclaw/openclaw.git
git fetch upstream
git merge upstream/main
git push origin main
```

Repeat for brain_ro if needed.

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
