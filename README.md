# ClawFactory
> **Status**: Work in progress (but looking promising).

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
│  │  approved/     working_repo/   secrets/    audit/       ││
│  │  (live config) (proposals)     (600)       (append)     ││
│  └──────────────────────────────────────────────────────────┘│
│                                                              │
│  [Kill Switch] ─────────────────────────────────────────────│
└──────────────────────────────────────────────────────────────┘

GitHub: your-org/{instance}-bot (fork of openclaw/openclaw)
         └── workspace/
             ├── SOUL.md, TOOLS.md, etc.
             ├── skills/
             └── memory/  ← agent memories backed up here
```

## Directory Structure

```
bot_repos/
├── bot1/
│   ├── approved/       # Live config (read-only at runtime)
│   ├── working_repo/   # Proposals (agent pushes branches here)
│   └── state/          # OpenClaw runtime state
├── bot2/
│   └── ...
```

## Components

| Component | Role | Listens On | Can Write To |
|-----------|------|------------|--------------|
| **Proxy** | Reverse proxy, localhost access | localhost:18789, :8080 | - |
| **Gateway** | Agent runtime (OpenClaw) | Docker network only | working_repo, memory |
| **Controller** | Authority & promotion | Docker network only | approved (via git) |

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
- GitHub organization (optional) - keeps bot repos organized under an org
- GitHub setup (forks openclaw/openclaw as {instance}-bot)

The installer generates two tokens:
- **Gateway token** - for authenticating to the OpenClaw gateway API
- **Controller token** - for accessing the Controller dashboard

### Option B: Manual Setup

```bash
# 1. Fork OpenClaw on GitHub
# Go to https://github.com/openclaw/openclaw and click Fork
# Name it: {instance}-bot (e.g., bot1-bot)
# Optional: Fork to an organization (e.g., my-bots-org/bot1-bot)

# 2. Create instance directory structure
mkdir -p bot_repos/bot1/{approved,working_repo,state}

# 3. Clone your fork (use your org or username)
git clone https://github.com/YOUR_ORG_OR_USERNAME/bot1-bot.git bot_repos/bot1/working_repo
git clone https://github.com/YOUR_ORG_OR_USERNAME/bot1-bot.git bot_repos/bot1/approved

# 4. Add config files to workspace/
mkdir -p bot_repos/bot1/working_repo/workspace
# Create SOUL.md, TOOLS.md, etc. in workspace/
git -C bot_repos/bot1/working_repo add workspace/
git -C bot_repos/bot1/working_repo commit -m "Add config files"
git -C bot_repos/bot1/working_repo push

# 5. Copy and edit secrets
mkdir -p secrets/bot1
cp secrets/gateway.env.example secrets/bot1/gateway.env
cp secrets/controller.env.example secrets/bot1/controller.env
# Edit with your values
chmod 600 secrets/bot1/*.env

# 6. Start
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

1. Bot edits files in `working_repo/workspace/`
2. Bot commits and pushes to proposal branch
3. Bot opens PR on GitHub
4. **Human merges PR** ← Authority checkpoint
5. GitHub webhook → Controller
6. Controller pulls to `approved/`
7. Gateway restarts with new config

### Offline (Local UI)

1. Bot commits to `working_repo/`
2. Human opens Controller dashboard (http://localhost:8080/controller?token=...)
3. **Human clicks Promote** ← Authority checkpoint
4. Controller pulls to `approved/`
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

1. Live config is immutable at runtime (read from approved/)
2. Bot cannot promote itself (requires human merge or UI action)
3. Gateway sandbox cannot escalate to host
4. Discord is not an authority signal
5. Kill switch always wins
6. Memory persists but requires explicit backup to GitHub

## Updating OpenClaw

Since bot repos are forks of OpenClaw, merge upstream updates:

```bash
cd bot_repos/bot1/working_repo
git remote add upstream https://github.com/openclaw/openclaw.git
git fetch upstream
git merge upstream/main
git push origin main
```

Repeat for approved/ if needed.

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
