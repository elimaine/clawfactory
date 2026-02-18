# Setup Guide

## Option A: Interactive Setup (Recommended)

The fastest way to get your first agent online. The installer walks you through everything.

```bash
./install.sh
```

It'll ask for:
- **Instance name** (e.g., `bot1`, `recon-agent`) — this becomes your agent's identity
- **Channel tokens** — Discord, Telegram, Slack (connect one or all)
- **AI Providers** — pick your agent's brain(s):
  - `1` Anthropic (Claude) — Recommended primary
  - `2` Kimi-K2 (Moonshot) — High-performance alternative
  - `3` OpenAI (GPT-4)
  - `4` Google (Gemini) — Also powers memory embeddings
  - `5` Ollama (Local) — Run models on your own hardware
  - `6` OpenRouter — Multi-provider gateway
  - `7` Brave Search — Web search capabilities
  - `8` ElevenLabs — Voice synthesis
- **GitHub org** (optional) — keeps your fleet's repos organized under one roof
- **GitHub setup** — auto-forks openclaw/openclaw as `{instance}-bot`

Example: Enter `1 4 7` to wire up Anthropic + Gemini + Brave Search.

The installer generates three credentials for you:
- **Gateway token** — authenticates to the OpenClaw gateway API
- **Controller token** — grants access to the Mission Control dashboard
- **Snapshot key** — an age keypair for encrypting state snapshots

## Option B: Manual Setup

For those who prefer to wire things up by hand.

```bash
# 1. Fork OpenClaw on GitHub
# Go to https://github.com/openclaw/openclaw and click Fork
# Name it: {instance}-bot (e.g., bot1-bot)
# Optional: Fork to an organization (e.g., my-bots-org/bot1-bot)

# 2. Create the instance directory structure
mkdir -p bot_repos/bot1/{code,state}

# 3. Clone your fork
git clone https://github.com/YOUR_ORG_OR_USERNAME/bot1-bot.git bot_repos/bot1/code

# 4. Set up the workspace
mkdir -p bot_repos/bot1/code/workspace/bot1_save
# Create SOUL.md, TOOLS.md, etc. in workspace/
git -C bot_repos/bot1/code add workspace/
git -C bot_repos/bot1/code commit -m "Add config files"
git -C bot_repos/bot1/code push

# 5. Configure secrets
mkdir -p secrets/bot1
cp secrets/gateway.env.example secrets/bot1/gateway.env
cp secrets/controller.env.example secrets/bot1/controller.env
# Edit with your API keys and tokens
chmod 600 secrets/bot1/*.env

# 6. Generate snapshot encryption key
./clawfactory.sh snapshot keygen

# 7. Launch
./clawfactory.sh start
```

## Secrets

Every agent gets its own isolated secrets vault. No cross-contamination between instances.

```
secrets/
├── bot1/
│   ├── gateway.env      # Channel tokens, AI provider keys
│   ├── controller.env   # GitHub webhook secret, auth tokens
│   └── snapshot.key     # Age private key for encrypted snapshots
├── bot2/
│   └── ...
└── tokens.env           # Master token registry for the fleet
```

| File | What's Inside |
|------|---------------|
| `gateway.env` | Channel tokens (Discord/Telegram/Slack), AI provider API keys, gateway auth token |
| `controller.env` | GitHub webhook secret, Controller dashboard auth token |
| `snapshot.key` | Age private key — the encryption key for your agent's state backups |
| `tokens.env` | Token registry mapping instances to their credentials |
| `.clawfactory.conf` | Instance configuration (`SANDBOX_MODE`, ports, etc.) |

Tokens are auto-generated during install. To reveal them anytime:
```bash
./clawfactory.sh info
./clawfactory.sh -i bot1 info   # For a specific agent
```

For detailed API key instructions, see [API Keys](API-KEYS.md).

## Remote Access

### Tailscale (Recommended)

The simplest way to reach your agents from anywhere on your tailnet.

```bash
# Expose gateway, controller, and Temporal on your tailnet
tailscale serve --bg --set-path /<instance> http://127.0.0.1:18789
tailscale serve --bg --set-path /<instance>/controller http://127.0.0.1:8080
tailscale serve --bg --https=8444 --set-path / http://127.0.0.1:8082
```

Then access via `https://your-machine.tailnet.ts.net/<instance>/` from any device on your network. Temporal UI is available at `https://your-machine.tailnet.ts.net:8444/`.

### Cloudflare Zero Trust

For public-facing access with proper authentication, check `todo/cloudflare/` for Cloudflare tunnel setup guides.
