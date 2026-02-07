# Architecture

## The Big Picture

ClawFactory is a launch platform for autonomous agents. Three subsystems work together behind a reverse proxy: the Gateway runs the agent brain, the Controller enforces human authority, and the Proxy keeps everything accessible from localhost. Services can run as Docker containers or natively inside a Lima VM on macOS.

### Docker Mode (Default)

```
┌──────────────────────────────────────────────────────────────┐
│                         Host                                  │
│  ┌──────────────────────────────────────────────────────────┐│
│  │                    Docker Compose                        ││
│  │                                                          ││
│  │  ┌─────────┐    ┌───────────────┐    ┌──────────────┐   ││
│  │  │  Proxy  │───►│    Gateway    │    │  Controller  │   ││
│  │  │ (nginx) │    │   (OpenClaw)  │    │   (FastAPI)  │   ││
│  │  │         │───►│    │          │    │              │   ││
│  │  └────┬────┘    │    │ LLM calls│    │ • Webhooks   │   ││
│  │       │         │    ▼          │    │ • Promotion  │   ││
│  │       │         └────┼──────────┘    │ • Snapshots  │   ││
│  │  localhost           │               │ • Traffic UI │   ││
│  │  :18789/:8080   ┌────▼──────────┐    └──────────────┘   ││
│  │                 │  LLM Proxy   │         ▲              ││
│  │                 │  :9090       │         │ reads        ││
│  │                 │  ──► Anthropic│    audit/traffic.jsonl ││
│  │                 │  ──► OpenAI  │─────────┘              ││
│  │                 │  ──► Gemini  │                        ││
│  │                 └──────────────┘                        ││
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

### Lima Mode (macOS)

When `SANDBOX_MODE=lima`, the entire agent stack runs as systemd services inside a Lima VM with near-native VZ framework networking. Files sync from host to VM via rsync over SSH. No Docker overhead for the core services — Docker only runs inside the VM for OpenClaw's built-in tool sandbox. See [Sandbox](sandbox.md) for the full breakdown.

## Subsystems

Each subsystem has strict boundaries — they can only write to what they own.

| Subsystem | Role | Listens On | Write Access |
|-----------|------|------------|--------------|
| **Proxy** | Front door, routes all traffic | localhost:18789, :8080 | None |
| **Gateway** | The agent itself — runs OpenClaw | Internal network | approved/ (git branches), state/ |
| **LLM Proxy** | Logs outbound AI API calls | Internal :9090 | audit/traffic.jsonl |
| **Controller** | Human authority layer | Internal network | approved/ (git pull), snapshots/ |

## Directory Layout

Every agent instance gets its own isolated directory tree:

```
bot_repos/
├── bot1/
│   ├── approved/              # Git clone — the bot's codebase
│   │   └── workspace/
│   │       ├── SOUL.md        # Personality definition
│   │       ├── skills/        # Learned abilities
│   │       ├── memory/        # Memory logs (git tracked)
│   │       └── bot1_save/     # Bot's declarative save state
│   │           └── package.json
│   └── state/                 # Runtime state (lives in encrypted snapshots)
│       ├── memory/main.sqlite # Vector embeddings database
│       ├── openclaw.json      # Runtime configuration
│       ├── devices/           # Paired devices
│       ├── credentials/       # Access allowlists
│       └── installed/         # npm packages (rebuilt on start, not snapshotted)

snapshots/
└── bot1/
    ├── snapshot-2026-02-03T12-00-00Z.tar.age
    └── latest.tar.age -> snapshot-...

secrets/
├── bot1/
│   ├── gateway.env            # API keys, channel tokens
│   ├── controller.env         # Webhook secrets
│   └── snapshot.key           # Age encryption key
└── tokens.env                 # Token registry for the fleet
```

## Promotion Pipeline

The core security model: bots can *propose*, but only humans can *promote*.

### Online (via GitHub)

1. Bot edits files in `approved/workspace/`
2. Bot commits and pushes to a proposal branch
3. Bot opens a PR on GitHub
4. **Human merges the PR** — this is the authority checkpoint
5. GitHub webhook fires, Controller picks it up
6. Controller pulls main into `approved/`
7. Gateway restarts with the new configuration

### Offline (via Controller UI)

1. Bot commits to a feature branch in `approved/`
2. Human opens the Controller dashboard
3. **Human clicks Promote** — authority checkpoint
4. Controller pulls main
5. Gateway restarts

## Pulling Upstream Updates

Bot repos are forks of OpenClaw, so pulling in new OpenClaw releases is straightforward:

```bash
cd bot_repos/bot1/approved
git remote add upstream https://github.com/openclaw/openclaw.git
git fetch upstream
git merge upstream/main
git push origin main
```
