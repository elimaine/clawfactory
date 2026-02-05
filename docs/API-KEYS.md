# API Key Acquisition Guide

This guide walks you through obtaining each API key needed for a fresh ClawFactory install.

## Minimal Recommended Setup

1. **Channel token** (Discord, Telegram, or Slack)
2. **Kimi K2** or **Anthropic** (primary model)
3. **Ollama** (local embeddings, free) or **OpenAI** (remote embeddings)


## Channel Tokens (At least one required)

### Discord Bot Token

1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Click **New Application** → name it (e.g., "MyBot")
3. Go to **Bot** in the left sidebar
4. Click **Reset Token** → **Yes, do it!**
5. Copy the token (you won't see it again)
6. Under **Privileged Gateway Intents**, enable:
   - **Message Content Intent** (required for reading messages)
   - **Server Members Intent** (optional, for member lists)

**Token format**: `MTIzNDU2Nzg5...` (long base64 string)

#### Required OAuth2 Scopes

| Scope | Required | Purpose |
|-------|----------|---------|
| `bot` | Yes | Main bot functionality |
| `applications.commands` | Optional | Slash commands support |

The other scopes (`guilds`, `rpc`, `presences`, etc.) are for user OAuth apps, not bots. For a standard bot, you only need `bot` and optionally `applications.commands`.

#### Bot Permissions

When generating the invite URL, select these permissions:

| Permission | Bit Value | Purpose |
|------------|-----------|---------|
| View Channels | 1024 | See channels the bot can access |
| Send Messages | 2048 | Reply to users |
| Embed Links | 16384 | Rich message formatting |
| Attach Files | 32768 | Send images/files |
| Read Message History | 65536 | Context for conversations |
| Add Reactions | 64 | React to messages |
| Use External Emoji | 262144 | Custom emoji support |
| Create Public Threads | 34359738368 | Thread conversations |
| Send Messages in Threads | 274877906944 | Reply in threads |
| Manage Threads | 17179869184 | Archive/manage threads |

#### Generating the Invite URL

1. Go to **OAuth2** → **URL Generator** in the Developer Portal
2. Select scopes: `bot` + `applications.commands`
3. Select the permissions listed above
4. Copy the generated URL
5. Open the URL to invite the bot to **your own server**

#### Security: Bot Pairing and Server Control

**Important**: Only add the bot to servers you control.

OpenClaw uses a **pairing system** for security:
- When the bot joins a server, it doesn't automatically respond to everyone
- Users must be **paired** (authorized) before the bot will interact with them
- Pairing is managed through the Controller UI or via DM pairing codes

**Why this matters**:
- If you add the bot to a server you don't control, the server owner/admins could potentially interact with your bot
- The pairing system prevents unauthorized access, but it's still best practice to only deploy to servers you own
- For public bots serving multiple communities, use the allowlist/denylist features in `openclaw.json`

To pair a user:
1. Go to Controller UI → **Devices** section
2. Approve pending pairing requests, or
3. Have the user DM the bot with the pairing code shown in the Controller

---

### Telegram Bot Token

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot`
3. Follow prompts to name your bot
4. Copy the token BotFather gives you

**Token format**: `123456789:ABCdefGHI...`

**Note**: Use `/setprivacy` with BotFather to enable group messages if needed.

---

### Slack Bot Token

1. Go to [Slack API Apps](https://api.slack.com/apps)
2. Click **Create New App** → **From scratch**
3. Name it and select workspace
4. Go to **OAuth & Permissions**
5. Add Bot Token Scopes:
   - `chat:write`
   - `channels:history`
   - `groups:history`
   - `im:history`
   - `app_mentions:read`
6. Click **Install to Workspace**
7. Copy the **Bot User OAuth Token**

**Token format**: `xoxb-...`

---

## AI Provider Keys (Choose at least one)

### Anthropic (Claude) - note (best model, expensive)

Best for: Primary model, high-quality reasoning

1. Go to [Anthropic Console](https://console.anthropic.com/)
2. Sign up or log in
3. Go to **Settings** → **API Keys**
4. Click **Create Key**
5. Name it (e.g., "ClawFactory")
6. Copy the key

**Key format**: `sk-ant-api03-...`

**Pricing**: ~$3/M input tokens, ~$15/M output tokens (Claude Sonnet)

---

### Kimi K2.5 (Moonshot AI) - Recommended (high quality, 8x cheaper than Claude!)

Best for: High-performance alternative, 256k context window, agentic tasks

1. Go to [Moonshot Platform](https://platform.moonshot.ai/)
2. Sign up and verify your account
3. Go to **Console** → **API Keys**
4. Click **Create new API key**
5. Copy the key

**Key format**: `sk-...` (standard format)

**Pricing**: ~$0.60/M input, ~$2.50/M output - significantly cheaper than Anthropic

---

### OpenAI (GPT-4) - note: you want one of these for memory embeddings, unless you want to do local.

Best for: GPT models, widely compatible

1. Go to [OpenAI Platform](https://platform.openai.com/)
2. Sign up or log in
3. Go to **API Keys** (left sidebar)
4. Click **Create new secret key**
5. Name it and copy the key

**Key format**: `sk-proj-...` or `sk-...`

**Pricing**: ~$2.50/M input, ~$10/M output (GPT-4o)

---

### OpenRouter

Best for: Access to multiple providers through one API

1. Go to [OpenRouter](https://openrouter.ai/)
2. Sign up or log in
3. Go to **Keys** (left sidebar)
4. Click **Create Key**
5. Set spending limit (recommended)
6. Copy the key

**Key format**: `sk-or-v1-...`

**Pricing**: Pass-through pricing + small markup

---

### Ollama (Local Models)

Best for: Running models locally, no API costs, privacy

1. Install Ollama:
   ```bash
   # macOS
   brew install ollama

   # Linux
   curl -fsSL https://ollama.ai/install.sh | sh
   ```

2. Start Ollama:
   ```bash
   ollama serve
   ```

3. Pull models:
   ```bash
   ollama pull llama3.2        # General purpose
   ollama pull nomic-embed-text # For embeddings
   ollama pull qwen2.5:32b      # Larger model
   ```

**No API key needed** - uses `ollama-local` as placeholder

**Note**: For Docker, Ollama connects via `host.docker.internal:11434`

---

## Optional Keys

### GitHub Personal Access Token

Only needed for automatic webhook configuration during install.

1. Go to [GitHub Tokens (Classic)](https://github.com/settings/tokens/new)
2. Note: Must be **Classic** token, not fine-grained
3. Set description: "ClawFactory"
4. Select scopes:
   - `repo` (full control of private repos)
   - `admin:repo_hook` (manage webhooks)
   - `admin:org` (only if using an organization)
5. Click **Generate token**
6. Copy the token

**Token format**: `ghp_...` (classic) or `github_pat_...` (fine-grained, won't work)

**Note**: If you skip this, you can manually configure webhooks later.

---

## Environment Variables Summary

After obtaining keys, they go in `secrets/<instance>/gateway.env`:

```bash
# Channels (at least one)
DISCORD_BOT_TOKEN=your_discord_token
TELEGRAM_BOT_TOKEN=your_telegram_token
SLACK_BOT_TOKEN=xoxb-your-slack-token

# AI Providers (at least one)
ANTHROPIC_API_KEY=sk-ant-api03-...
KIMI_API_KEY=sk-...
OPENAI_API_KEY=sk-proj-...
GEMINI_API_KEY=AIza...
OPENROUTER_API_KEY=sk-or-v1-...

# Ollama (local)
OLLAMA_API_KEY=ollama-local
OLLAMA_BASE_URL=http://host.docker.internal:11434/v1

# Generated by installer
OPENCLAW_GATEWAY_TOKEN=<auto-generated>
```

---

## Recommended Setup

1. **Channel token** (Discord, Telegram, or Slack)
2. **Kimi K2** or **Anthropic** (primary model)
3. **Ollama** (local embeddings, free) or **OpenAI** (remote embeddings)

---

## Troubleshooting

### "Invalid API key" errors
- Check for extra whitespace when copying
- Ensure the key hasn't been revoked
- Verify you're using the correct key type (e.g., classic GitHub token)

### Ollama connection issues
- Ensure `ollama serve` is running
- Check if port 11434 is accessible
- For Docker: verify `host.docker.internal` resolves

### Rate limits
- Start with lower usage models during testing
- Consider OpenRouter for automatic fallbacks
- Add multiple providers for redundancy

---

## Cost Estimation

Rough monthly costs for moderate usage (~100k messages):

| Provider | Estimated Cost |
|----------|----------------|
| Anthropic (Claude Sonnet) | $10-30/month |
| OpenAI (GPT-4o) | $10-25/month |
| Gemini (Flash) | $1-5/month |
| Ollama | $0 (hardware costs only) |
| OpenRouter | Varies by model |

Actual costs depend heavily on message length, thinking mode, and usage patterns.
