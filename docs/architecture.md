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
│  │                                                          ││
│  │                 ┌──────────────┐                        ││
│  │                 │  MITM Proxy │  (opt-in, off by default)││
│  │                 │  :8888      │                        ││
│  │                 │  iptables   │─► audit/traffic.enc.jsonl│
│  │                 │  redirect   │  (Fernet encrypted)     ││
│  │                 └──────────────┘                        ││
│  └──────────────────────────────────────────────────────────┘│
│                                                              │
│  ┌──────────────────────────────────────────────────────────┐│
│  │                   Volumes                                ││
│  │  code/         state/          snapshots/    secrets/   ││
│  │  (bot code)    (runtime)       (encrypted)   (600)      ││
│  └──────────────────────────────────────────────────────────┘│
│                                                              │
│  [Kill Switch] ─────────────────────────────────────────────│
└──────────────────────────────────────────────────────────────┘

code/   (OpenClaw source + workspace)
 └── workspace/
     ├── SOUL.md, TOOLS.md, etc.
     ├── skills/
     ├── memory/           ← memory markdown
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
| **Gateway** | The agent itself — runs OpenClaw | Internal network | code/, state/ |
| **LLM Proxy** | Logs outbound AI API calls | Internal :9090 | audit/traffic.jsonl |
| **MITM Proxy** | Transparent TLS capture (opt-in) | Internal :8888 | audit/traffic.enc.jsonl (encrypted) |
| **Controller** | Management & monitoring | Internal network | code/ (pull upstream), snapshots/ |

## Directory Layout

Every agent instance gets its own isolated directory tree:

```
bot_repos/
├── bot1/
│   ├── code/                  # Bot code directory (OpenClaw source)
│   │   └── workspace/
│   │       ├── SOUL.md        # Personality definition
│   │       ├── skills/        # Learned abilities
│   │       ├── memory/        # Memory logs
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

## Pulling Upstream Updates

Bot code directories are clones of OpenClaw. Pull in new releases via the Controller UI ("Pull Latest OpenClaw") or manually:

```bash
cd bot_repos/bot1/code
git pull upstream main
```
