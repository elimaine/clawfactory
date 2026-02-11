#!/usr/bin/env bash
set -euo pipefail

# ---------------------------
# ClawFactory Install Script
# ---------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SECRETS_DIR="${SCRIPT_DIR}/secrets"
BOT_REPOS_DIR="${SCRIPT_DIR}/bot_repos"
CONFIG_FILE="${SCRIPT_DIR}/.clawfactory.conf"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info() { echo -e "${GREEN}→${NC} $*"; }
warn() { echo -e "${YELLOW}⚠${NC} $*"; }
error() { echo -e "${RED}✗${NC} $*" >&2; }
success() { echo -e "${GREEN}✓${NC} $*"; }

die() { error "$*"; exit 1; }

require() {
    command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"
}

# Validate instance name: lowercase alphanumeric and hyphens only, 1-32 chars, can't start/end with hyphen
validate_instance_name() {
    local name="$1"
    if [[ -z "$name" ]]; then
        echo "Instance name cannot be empty"
        return 1
    fi
    if [[ ${#name} -gt 32 ]]; then
        echo "Instance name must be 32 characters or less"
        return 1
    fi
    if [[ ! "$name" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]]; then
        if [[ "$name" =~ ^- ]] || [[ "$name" =~ -$ ]]; then
            echo "Instance name cannot start or end with a hyphen"
        elif [[ "$name" =~ [A-Z] ]]; then
            echo "Instance name must be lowercase"
        elif [[ "$name" =~ [^a-z0-9-] ]]; then
            echo "Instance name can only contain lowercase letters, numbers, and hyphens"
        else
            echo "Invalid instance name format"
        fi
        return 1
    fi
    return 0
}

# Load config file
load_config() {
    if [[ -f "$CONFIG_FILE" ]]; then
        source "$CONFIG_FILE"
    fi
}

# Save config file
save_config() {
    # Backward compat: migrate SANDBOX_ENABLED to SANDBOX_MODE
    if [[ -z "${SANDBOX_MODE:-}" ]]; then
        if [[ "${SANDBOX_ENABLED:-false}" == "true" ]]; then
            SANDBOX_MODE="sysbox"
        else
            SANDBOX_MODE="none"
        fi
    fi

    cat > "$CONFIG_FILE" <<EOF
# ClawFactory Instance Configuration
INSTANCE_NAME="${INSTANCE_NAME:-}"
GITHUB_USERNAME="${GITHUB_USERNAME:-}"
GITHUB_ORG="${GITHUB_ORG:-}"
SANDBOX_MODE="${SANDBOX_MODE:-none}"
EOF

    # Also save to .env for docker-compose
    # Map SANDBOX_MODE back to SANDBOX_ENABLED for docker-compose compat
    local sandbox_enabled="false"
    [[ "${SANDBOX_MODE:-none}" == "sysbox" ]] && sandbox_enabled="true"

    cat > "${SCRIPT_DIR}/.env" <<EOF
# Docker Compose environment (auto-generated)
INSTANCE_NAME=${INSTANCE_NAME:-clawfactory}
COMPOSE_PROJECT_NAME=clawfactory-${INSTANCE_NAME:-default}
GITHUB_USERNAME=${GITHUB_USERNAME:-}
GITHUB_ORG=${GITHUB_ORG:-}
SANDBOX_ENABLED=${sandbox_enabled}
SANDBOX_MODE=${SANDBOX_MODE:-none}
EOF
}

prompt() {
    local var_name="$1"
    local prompt_text="$2"
    local default="${3:-}"
    local secret="${4:-false}"

    if [[ -n "$default" ]]; then
        if [[ "$secret" == "true" ]]; then
            prompt_text="${prompt_text} [****saved****]"
        else
            prompt_text="${prompt_text} [${default}]"
        fi
    fi

    if [[ "$secret" == "true" ]]; then
        read -s -p "${prompt_text}: " value
        echo
    else
        read -p "${prompt_text}: " value
    fi

    value="${value:-$default}"
    eval "$var_name=\"\$value\""
}

# Load existing value from instance env files
load_env_value() {
    local key="$1"
    local instance="${2:-${INSTANCE_NAME:-}}"
    local file=""

    # Determine which file to check based on key
    case "$key" in
        DISCORD_BOT_TOKEN|TELEGRAM_BOT_TOKEN|SLACK_BOT_TOKEN|SLACK_APP_TOKEN|ANTHROPIC_API_KEY|MOONSHOT_API_KEY|OPENAI_API_KEY|GEMINI_API_KEY|OLLAMA_API_KEY|OPENROUTER_API_KEY|BRAVE_API_KEY|ELEVENLABS_API_KEY|OPENCLAW_GATEWAY_TOKEN)
            file="${SECRETS_DIR}/${instance}/gateway.env"
            ;;
        GITHUB_WEBHOOK_SECRET|ALLOWED_MERGE_ACTORS|CONTROLLER_API_TOKEN|GITHUB_TOKEN)
            file="${SECRETS_DIR}/${instance}/controller.env"
            ;;
        *)
            return
            ;;
    esac

    [[ -f "$file" ]] || return
    grep "^${key}=" "$file" 2>/dev/null | cut -d'=' -f2- | head -1
}

# ============================================================
# Pre-flight Checks
# ============================================================
preflight() {
    info "Checking dependencies..."
    require docker
    require git

    if ! docker info >/dev/null 2>&1; then
        die "Docker is not running. Please start Docker and try again."
    fi

    # GitHub CLI is optional - only needed for online mode
    GH_AVAILABLE=false
    if command -v gh >/dev/null 2>&1; then
        if gh auth status &>/dev/null; then
            GH_AVAILABLE=true
            success "GitHub CLI authenticated"
        else
            info "GitHub CLI installed but not authenticated"
            info "  Run 'gh auth login' to enable GitHub integration"
        fi
    else
        info "GitHub CLI not installed (GitHub integration unavailable)"
        info "  Install from: https://cli.github.com/"
    fi

    # Check for Sysbox (optional, for sandbox support on Linux)
    SYSBOX_AVAILABLE=false
    if docker info 2>/dev/null | grep -qi sysbox; then
        SYSBOX_AVAILABLE=true
        success "Sysbox runtime detected (sandbox support available)"
    else
        info "Sysbox not detected"
    fi

    # Check for Lima (optional, for Lima VM sandbox on macOS)
    LIMA_AVAILABLE=false
    if command -v limactl >/dev/null 2>&1; then
        LIMA_AVAILABLE=true
        success "Lima detected (VM sandbox available)"
    else
        info "Lima not detected"
    fi

    success "Core dependencies satisfied"
}

# ============================================================
# Configure Sandbox Mode
# ============================================================
configure_sandbox() {
    echo ""
    echo "=== Sandbox Mode ==="
    echo ""

    if [[ "$(uname)" == "Darwin" ]]; then
        # macOS: offer Lima VM or none
        echo "Sandbox options for macOS:"
        echo ""
        echo "  1) Lima VM (recommended) - services run in a Linux VM"
        echo "  2) None                  - tools run directly (Docker Compose)"
        echo ""

        if [[ "$LIMA_AVAILABLE" == "true" ]]; then
            info "Lima is installed"
        else
            info "Lima not installed - will be installed if you select Lima VM"
        fi
        echo ""

        read -p "Select sandbox mode [1]: " sandbox_choice
        sandbox_choice="${sandbox_choice:-1}"

        case "$sandbox_choice" in
            1)
                SANDBOX_MODE="lima"
                success "Sandbox mode: Lima VM"
                echo ""
                echo "Lima setup creates a Linux VM with all ClawFactory dependencies."
                echo "This takes several minutes on first run."
                echo ""
                read -p "Run Lima setup now? [Y/n]: " run_setup
                if [[ ! "$run_setup" =~ ^[Nn]$ ]]; then
                    bash "${SCRIPT_DIR}/sandbox/lima/setup.sh" setup
                else
                    echo ""
                    info "Skipped. Run later with: ./sandbox/lima/setup.sh"
                fi
                ;;
            *)
                SANDBOX_MODE="none"
                info "Sandbox mode: none (Docker Compose)"
                ;;
        esac
    else
        # Linux: offer Sysbox, Lima, or none
        echo "Sandbox options:"
        echo ""
        if [[ "$SYSBOX_AVAILABLE" == "true" ]]; then
            echo "  1) Sysbox (recommended)  - Docker-in-Docker with VM-like isolation"
        else
            echo "  1) Sysbox               - not installed (https://github.com/nestybox/sysbox)"
        fi
        echo "  2) Lima VM              - services run in a Linux VM"
        echo "  3) None                 - tools run directly (Docker Compose)"
        echo ""

        read -p "Select sandbox mode [1]: " sandbox_choice
        sandbox_choice="${sandbox_choice:-1}"

        case "$sandbox_choice" in
            1)
                if [[ "$SYSBOX_AVAILABLE" != "true" ]]; then
                    warn "Sysbox is not installed."
                    echo "  Install from: https://github.com/nestybox/sysbox#installation"
                    echo "  Then re-run this installer."
                    SANDBOX_MODE="none"
                else
                    SANDBOX_MODE="sysbox"
                    success "Sandbox mode: Sysbox"
                fi
                ;;
            2)
                SANDBOX_MODE="lima"
                success "Sandbox mode: Lima VM"
                echo ""
                read -p "Run Lima setup now? [Y/n]: " run_setup
                if [[ ! "$run_setup" =~ ^[Nn]$ ]]; then
                    bash "${SCRIPT_DIR}/sandbox/lima/setup.sh" setup
                else
                    echo ""
                    info "Skipped. Run later with: ./sandbox/lima/setup.sh"
                fi
                ;;
            *)
                SANDBOX_MODE="none"
                info "Sandbox mode: none"
                ;;
        esac
    fi
}

# ============================================================
# Initialize Bot Repository (GitHub Fork or Local)
# ============================================================
init_bot_repo() {
    info "Initializing bot repository..."

    mkdir -p "${BOT_REPOS_DIR}/${INSTANCE_NAME}"
    mkdir -p "${BOT_REPOS_DIR}/${INSTANCE_NAME}/state"

    # Check if code/ already exists with OpenClaw source code
    if [[ -f "${BOT_REPOS_DIR}/${INSTANCE_NAME}/code/Dockerfile" ]] || \
       [[ -f "${BOT_REPOS_DIR}/${INSTANCE_NAME}/code/package.json" ]]; then
        success "Using existing bot_repos/${INSTANCE_NAME}/code (OpenClaw source found)"
        # Initialize as git repo if not already
        if [[ ! -d "${BOT_REPOS_DIR}/${INSTANCE_NAME}/code/.git" ]]; then
            info "Initializing git repo..."
            cd "${BOT_REPOS_DIR}/${INSTANCE_NAME}/code"
            git init
            git add -A
            git commit -m "Initial commit from existing source" 2>/dev/null || true
            cd "${SCRIPT_DIR}"
        fi
    else
        # No existing source - try GitHub or manual setup
        # Determine repo owner (org or username)
        local repo_owner="${GITHUB_REPO_OWNER:-${GITHUB_ORG:-${GITHUB_USERNAME}}}"
        if [[ -z "$repo_owner" ]] && [[ "$GH_AVAILABLE" == "true" ]]; then
            # Try to get from gh auth
            repo_owner=$(gh api user --jq '.login' 2>/dev/null || echo "")
        fi

        if [[ -z "$repo_owner" ]]; then
            warn "No existing source in bot_repos/${INSTANCE_NAME}/code/"
            echo ""
            echo "To set up OpenClaw source, clone OpenClaw manually:"
            echo ""
            echo "  git clone https://github.com/openclaw/openclaw.git bot_repos/${INSTANCE_NAME}/code"
            echo ""
            echo "Or copy from an existing bot:"
            echo ""
            echo "  cp -r bot_repos/existing_bot/code bot_repos/${INSTANCE_NAME}/code"
            echo ""
            if [[ "$GH_AVAILABLE" != "true" ]]; then
                echo "To enable GitHub forking, install and authenticate GitHub CLI:"
                echo "  1. Install: https://cli.github.com/"
                echo "  2. Authenticate: gh auth login"
                echo ""
            fi
            return 0
        fi

        local bot_repo_name="${INSTANCE_NAME}-bot"
        local fork_repo="${repo_owner}/${bot_repo_name}"
        local fork_url="https://github.com/${fork_repo}.git"

        # Check if fork exists, if not create it
        if ! gh repo view "${fork_repo}" &>/dev/null; then
            info "Forking openclaw/openclaw as ${bot_repo_name} under ${repo_owner}..."
            if [[ -n "$GITHUB_ORG" && "$GITHUB_ORG" == "$repo_owner" ]]; then
                # Fork to organization
                gh repo fork openclaw/openclaw --clone=false --fork-name "${bot_repo_name}" --org "${GITHUB_ORG}"
            else
                # Fork to personal account
                gh repo fork openclaw/openclaw --clone=false --fork-name "${bot_repo_name}"
            fi
            success "Created fork: ${fork_repo}"
        else
            success "Fork exists: ${fork_repo}"
        fi

        # Clone code dir if not exists
        if [[ ! -d "${BOT_REPOS_DIR}/${INSTANCE_NAME}/code/.git" ]]; then
            info "Cloning fork to code dir..."
            git clone "${fork_url}" "${BOT_REPOS_DIR}/${INSTANCE_NAME}/code"
            success "Cloned to bot_repos/${INSTANCE_NAME}/code"
        else
            success "Code dir already exists"
        fi
    fi

    # Create state directory
    mkdir -p "${BOT_REPOS_DIR}/${INSTANCE_NAME}/state"

    # Create workspace config files if they don't exist
    if [[ ! -f "${BOT_REPOS_DIR}/${INSTANCE_NAME}/code/workspace/SOUL.md" ]]; then
        info "Creating bot config files..."
        mkdir -p "${BOT_REPOS_DIR}/${INSTANCE_NAME}/code/workspace/skills"
        mkdir -p "${BOT_REPOS_DIR}/${INSTANCE_NAME}/code/workspace/memory"
        mkdir -p "${BOT_REPOS_DIR}/${INSTANCE_NAME}/code/workspace/${INSTANCE_NAME}_save"

        cat > "${BOT_REPOS_DIR}/${INSTANCE_NAME}/code/workspace/SOUL.md" <<'EOF'
# Soul

You are a helpful AI assistant running in the ClawFactory secure environment.

## Principles

1. Be helpful and honest
2. Respect user privacy
3. Admit when you don't know something
4. Follow the policies defined in your config

## Workspace

Your workspace files live in `/workspace/code/workspace/`.
Configuration changes are managed by your operator via the controller UI.

## Capabilities

- You can use all your configured tools and skills
- Memory search is enabled for context recall
- Your save state goes in the {instance}_save/ directory
- Use encrypted snapshots for full state backup
EOF

        cat > "${BOT_REPOS_DIR}/${INSTANCE_NAME}/code/workspace/policies.yml" <<'EOF'
# Policies

allowed_actions:
  - read_files
  - write_files
  - backup_memory
  - create_snapshots

forbidden_actions:
  - secret_access
  - network_escalation
  - docker_access
EOF

        cat > "${BOT_REPOS_DIR}/${INSTANCE_NAME}/code/workspace/skills/self-management.md" <<EOF
# Self-Management Skill

This skill covers how you manage your own state, memories, and configuration.

## Architecture Overview

Your state is split into two categories:

1. **Code** (in \`code/workspace/\`) - Your workspace files
   - Memory markdown files (\`memory/*.md\`)
   - Configuration (SOUL.md, skills/, policies.yml)
   - Your save directory (\`${INSTANCE_NAME}_save/\`)

2. **Encrypted snapshots** (in \`state/\`) - Backed up via encryption
   - Embeddings database (\`memory/main.sqlite\`)
   - Runtime config (\`openclaw.json\`)
   - Paired devices and credentials

## Encrypted Snapshots

For full state backup (including embeddings):

\`\`\`bash
curl -X POST http://controller:8080/internal/snapshot
\`\`\`

List existing snapshots:
\`\`\`bash
curl http://controller:8080/internal/snapshot
\`\`\`

## Your Save Directory

You have a persistent save directory at \`/workspace/code/workspace/${INSTANCE_NAME}_save/\`.

Add npm dependencies to \`package.json\` there - they install on container startup.

## Status Endpoints

Check system health:
\`\`\`bash
curl http://controller:8080/health
\`\`\`
EOF

        # Create initial package.json for bot save state
        cat > "${BOT_REPOS_DIR}/${INSTANCE_NAME}/code/workspace/${INSTANCE_NAME}_save/package.json" <<EOF
{
  "name": "${INSTANCE_NAME}-save",
  "version": "1.0.0",
  "description": "Declarative dependencies for ${INSTANCE_NAME}",
  "dependencies": {}
}
EOF

        cd "${BOT_REPOS_DIR}/${INSTANCE_NAME}/code"
        git add workspace/
        git commit -m "Add ClawFactory bot config files"
        git push origin main
        cd "${SCRIPT_DIR}"

        success "Created bot config files"
    else
        success "Bot config files already exist"
    fi

    success "Bot repository initialized"
}

# ============================================================
# Create Initial State Config (openclaw.json)
# ============================================================
create_state_config() {
    local config_file="${BOT_REPOS_DIR}/${INSTANCE_NAME}/state/openclaw.json"

    # Skip if config already exists
    if [[ -f "$config_file" ]]; then
        success "State config already exists"
        return
    fi

    info "Creating initial state config..."

    # Build memory search config based on selected provider
    local memory_search_json=""
    if [[ "${MEMORY_SEARCH_ENABLED:-false}" == "true" ]]; then
        case "${MEMORY_SEARCH_PROVIDER:-}" in
            ollama)
                memory_search_json='"memorySearch": {
        "enabled": true,
        "provider": "openai",
        "remote": {
          "baseUrl": "http://host.docker.internal:11434/v1",
          "apiKey": "ollama-local"
        },
        "model": "nomic-embed-text"
      }'
                ;;
            openai)
                memory_search_json='"memorySearch": {
        "enabled": true,
        "provider": "openai",
        "model": "text-embedding-3-small"
      }'
                ;;
        esac
    fi

    # Build primary model config
    local model_config=""
    if [[ -n "${PRIMARY_MODEL:-}" ]]; then
        model_config='"model": {
        "primary": "'"${PRIMARY_MODEL}"'"
      }'
    fi

    # Build sandbox config based on SANDBOX_MODE
    local sandbox_mode="off"
    if [[ "${SANDBOX_MODE:-none}" != "none" ]]; then
        sandbox_mode="non-main"  # Sandbox non-main sessions (default safe mode)
    fi

    # Build tools config for web search (Brave)
    local tools_json=""
    if [[ -n "${BRAVE_API_KEY:-}" ]]; then
        tools_json='"tools": {
    "web": {
      "search": {
        "provider": "brave",
        "maxResults": 5,
        "timeoutSeconds": 30
      }
    }
  },'
    fi

    # Create the config file
    cat > "$config_file" <<EOF
{
  "meta": {
    "lastTouchedVersion": "2026.1.0",
    "lastTouchedAt": "$(date -u +%Y-%m-%dT%H:%M:%S.000Z)"
  },
  ${tools_json}
  "agents": {
    "defaults": {
      ${model_config}${model_config:+,}
      ${memory_search_json}${memory_search_json:+,}
      "workspace": "/home/node/.openclaw/workspace",
      "maxConcurrent": 4,
      "sandbox": {
        "mode": "${sandbox_mode}",
        "scope": "session",
        "workspaceAccess": "none"
      }
    }
  },
  "gateway": {
    "port": 18789,
    "mode": "local",
    "bind": "loopback"
  }
}
EOF

    # Clean up any double commas or trailing commas (simple cleanup)
    if command -v python3 >/dev/null 2>&1; then
        python3 -c "
import json
with open('$config_file', 'r') as f:
    try:
        data = json.load(f)
        with open('$config_file', 'w') as f:
            json.dump(data, f, indent=2)
    except:
        pass
" 2>/dev/null || true
    fi

    success "Created initial state config with memory search"
}

# ============================================================
# AI Provider Selection Menu
# ============================================================
configure_ai_providers() {
    echo ""
    echo "=== AI Provider Selection ==="
    echo ""
    echo "Select which AI providers to configure:"
    echo ""
    echo "  1) Anthropic (Claude)      - Recommended primary model"
    echo "  2) Kimi K2.5 (Moonshot)    - High-performance alternative"
    echo "  3) OpenAI (GPT-4)          - GPT models"
    echo "  4) Google (Gemini)         - Gemini models"
    echo "  5) Ollama (Local)          - Run models locally"
    echo "  6) OpenRouter              - Multi-provider gateway"
    echo "  7) Brave Search            - Web search API"
    echo "  8) ElevenLabs              - Text-to-speech API"
    echo ""
    echo "Enter numbers separated by spaces (e.g., '1 5 7' for Anthropic + Ollama + Brave)"
    echo "Press Enter for default: Anthropic only"
    echo ""

    read -p "Providers [1]: " provider_selection
    provider_selection="${provider_selection:-1}"

    # Initialize all provider keys as empty
    ANTHROPIC_API_KEY=""
    MOONSHOT_API_KEY=""
    OPENAI_API_KEY=""
    GEMINI_API_KEY=""
    OLLAMA_ENABLED=""
    OPENROUTER_API_KEY=""
    BRAVE_API_KEY=""
    ELEVENLABS_API_KEY=""

    # Load any existing values
    local saved_anthropic=$(load_env_value "ANTHROPIC_API_KEY")
    local saved_kimi=$(load_env_value "MOONSHOT_API_KEY")
    local saved_openai=$(load_env_value "OPENAI_API_KEY")
    local saved_gemini=$(load_env_value "GEMINI_API_KEY")
    local saved_ollama=$(load_env_value "OLLAMA_API_KEY")
    local saved_openrouter=$(load_env_value "OPENROUTER_API_KEY")
    local saved_brave=$(load_env_value "BRAVE_API_KEY")
    local saved_elevenlabs=$(load_env_value "ELEVENLABS_API_KEY")

    for provider in $provider_selection; do
        case "$provider" in
            1)
                echo ""
                echo "--- Anthropic (Claude) ---"
                echo "Get your API key at: https://console.anthropic.com/settings/keys"
                if [[ -n "$saved_anthropic" ]]; then
                    ANTHROPIC_API_KEY="$saved_anthropic"
                    success "Anthropic API key (saved)"
                else
                    prompt ANTHROPIC_API_KEY "Anthropic API key" "" true
                fi
                ;;
            2)
                echo ""
                echo "--- Kimi K2.5 (Moonshot AI) ---"
                echo "Get your API key at: https://platform.moonshot.ai/console/api-keys"
                echo "Model: kimi-k2.5 (256k context)"
                if [[ -n "$saved_kimi" ]]; then
                    MOONSHOT_API_KEY="$saved_kimi"
                    success "Kimi API key (saved)"
                else
                    prompt MOONSHOT_API_KEY "Kimi/Moonshot API key" "" true
                fi
                ;;
            3)
                echo ""
                echo "--- OpenAI (GPT-4) ---"
                echo "Get your API key at: https://platform.openai.com/api-keys"
                if [[ -n "$saved_openai" ]]; then
                    OPENAI_API_KEY="$saved_openai"
                    success "OpenAI API key (saved)"
                else
                    prompt OPENAI_API_KEY "OpenAI API key" "" true
                fi
                ;;
            4)
                echo ""
                echo "--- Google (Gemini) ---"
                echo "Get your API key at: https://aistudio.google.com/app/apikey"
                echo "Used for: Gemini models"
                if [[ -n "$saved_gemini" ]]; then
                    GEMINI_API_KEY="$saved_gemini"
                    success "Gemini API key (saved)"
                else
                    prompt GEMINI_API_KEY "Gemini API key" "" true
                fi
                ;;
            5)
                echo ""
                echo "--- Ollama (Local Models) ---"
                echo "Run models locally with Ollama: https://ollama.ai"
                echo "Make sure Ollama is running on your machine."
                OLLAMA_ENABLED="true"
                if [[ -n "$saved_ollama" ]]; then
                    success "Ollama enabled (saved)"
                else
                    success "Ollama will be configured"
                fi
                ;;
            6)
                echo ""
                echo "--- OpenRouter ---"
                echo "Get your API key at: https://openrouter.ai/keys"
                echo "Access multiple providers through one API"
                if [[ -n "$saved_openrouter" ]]; then
                    OPENROUTER_API_KEY="$saved_openrouter"
                    success "OpenRouter API key (saved)"
                else
                    prompt OPENROUTER_API_KEY "OpenRouter API key" "" true
                fi
                ;;
            7)
                echo ""
                echo "--- Brave Search ---"
                echo "Get your API key at: https://brave.com/search/api/"
                echo "Used for: Web search tool (search the internet)"
                if [[ -n "$saved_brave" ]]; then
                    BRAVE_API_KEY="$saved_brave"
                    success "Brave API key (saved)"
                else
                    prompt BRAVE_API_KEY "Brave Search API key" "" true
                fi
                ;;
            8)
                echo ""
                echo "--- ElevenLabs ---"
                echo "Get your API key at: https://elevenlabs.io/app/settings/api-keys"
                echo "Used for: High-quality text-to-speech"
                if [[ -n "$saved_elevenlabs" ]]; then
                    ELEVENLABS_API_KEY="$saved_elevenlabs"
                    success "ElevenLabs API key (saved)"
                else
                    prompt ELEVENLABS_API_KEY "ElevenLabs API key" "" true
                fi
                ;;
            *)
                warn "Unknown provider: $provider (skipping)"
                ;;
        esac
    done

    # Determine primary model based on what's configured
    echo ""
    if [[ -n "$ANTHROPIC_API_KEY" ]]; then
        PRIMARY_MODEL="anthropic/claude-sonnet-4-20250514"
        info "Primary model: Claude Sonnet 4 (Anthropic)"
    elif [[ -n "$MOONSHOT_API_KEY" ]]; then
        PRIMARY_MODEL="moonshot/kimi-k2.5"
        info "Primary model: Kimi K2.5 (Moonshot)"
    elif [[ -n "$OPENAI_API_KEY" ]]; then
        PRIMARY_MODEL="openai/gpt-4o"
        info "Primary model: GPT-4o (OpenAI)"
    elif [[ -n "$GEMINI_API_KEY" ]]; then
        PRIMARY_MODEL="google/gemini-2.0-flash"
        info "Primary model: Gemini 2.0 Flash (Google)"
    elif [[ -n "$OPENROUTER_API_KEY" ]]; then
        PRIMARY_MODEL="openrouter/anthropic/claude-sonnet-4"
        info "Primary model: Claude Sonnet 4 via OpenRouter"
    elif [[ -n "$OLLAMA_ENABLED" ]]; then
        PRIMARY_MODEL="ollama/llama3.2"
        info "Primary model: Llama 3.2 (Ollama local)"
    else
        PRIMARY_MODEL=""
        warn "No AI provider configured - bot will not be functional"
    fi

    success "AI providers configured"

    # Configure vector memory after AI providers
    configure_vector_memory
}

# ============================================================
# Configure Vector Memory (Embeddings)
# ============================================================
configure_vector_memory() {
    echo ""
    echo "=== Vector Memory Search ==="
    echo ""
    echo "Vector memory enables semantic search over conversation history."
    echo "This requires an embedding model to convert text to vectors."
    echo ""

    read -p "Enable vector memory search? [Y/n]: " enable_memory
    if [[ "$enable_memory" =~ ^[Nn]$ ]]; then
        MEMORY_SEARCH_ENABLED="false"
        warn "Vector memory disabled"
        return
    fi

    echo ""
    echo "Select embedding provider:"
    echo ""
    echo "  1) OpenClaw (Local)    - Built-in embeddings, no external service needed"
    echo "  2) Ollama (Local)      - nomic-embed-text, free, runs locally"
    echo "  3) OpenAI              - text-embedding-3-small, high quality"
    echo ""

    read -p "Select provider [1]: " embed_selection
    embed_selection="${embed_selection:-1}"

    case "$embed_selection" in
        1)
            MEMORY_SEARCH_ENABLED="true"
            MEMORY_SEARCH_PROVIDER="openclaw"
            MEMORY_SEARCH_MODEL="local"
            success "Vector memory: OpenClaw (local embeddings)"
            ;;
        2)
            MEMORY_SEARCH_ENABLED="true"
            MEMORY_SEARCH_PROVIDER="ollama"
            MEMORY_SEARCH_MODEL="nomic-embed-text"
            MEMORY_SEARCH_BASE_URL="http://host.docker.internal:11434/v1"
            MEMORY_SEARCH_API_KEY="ollama-local"
            success "Vector memory: Ollama (nomic-embed-text)"
            echo ""
            info "Make sure to pull the embedding model:"
            echo "  ollama pull nomic-embed-text"
            ;;
        3)
            # Prompt for OpenAI key if not already set
            if [[ -z "${OPENAI_API_KEY:-}" ]]; then
                echo ""
                echo "OpenAI API key required for embeddings."
                echo "Get one at: https://platform.openai.com/api-keys"
                echo ""
                read -p "Enter OpenAI API key: " openai_key
                if [[ -z "$openai_key" ]]; then
                    warn "No API key provided, disabling vector memory"
                    MEMORY_SEARCH_ENABLED="false"
                    return
                fi
                OPENAI_API_KEY="$openai_key"
            fi
            MEMORY_SEARCH_ENABLED="true"
            MEMORY_SEARCH_PROVIDER="openai"
            MEMORY_SEARCH_MODEL="text-embedding-3-small"
            MEMORY_SEARCH_API_KEY="$OPENAI_API_KEY"
            success "Vector memory: OpenAI (text-embedding-3-small)"
            ;;
        *)
            warn "Invalid selection, using OpenClaw local embeddings"
            MEMORY_SEARCH_ENABLED="true"
            MEMORY_SEARCH_PROVIDER="openclaw"
            MEMORY_SEARCH_MODEL="local"
            ;;
    esac
}

# ============================================================
# Configure Secrets
# ============================================================
configure_secrets() {
    info "Configuring secrets..."

    mkdir -p "${SECRETS_DIR}"
    chmod 700 "${SECRETS_DIR}"

    # Load existing config
    load_config

    # Instance name configuration
    echo ""
    echo "=== Instance Name ==="
    echo "This identifies your ClawFactory instance (e.g., 'bot1', 'bot2', 'prod-agent')"
    echo "Used for container names and token storage."
    echo ""

    local default_instance="${INSTANCE_NAME:-bot1}"

    while true; do
        prompt INSTANCE_NAME "Instance name" "$default_instance"
        local validation_error
        if validation_error=$(validate_instance_name "$INSTANCE_NAME"); then
            break
        else
            error "$validation_error"
            echo "Please try again."
        fi
    done
    save_config
    success "Instance name: $INSTANCE_NAME"

    # Check for existing secrets for this instance
    local INSTANCE_SECRETS_DIR="${SECRETS_DIR}/${INSTANCE_NAME}"
    local USE_SAVED_SECRETS="false"

    if [[ -f "${INSTANCE_SECRETS_DIR}/gateway.env" ]]; then
        echo ""
        echo "=== Existing Configuration Found ==="
        echo "Found saved secrets for instance '${INSTANCE_NAME}'"
        echo ""

        # Show what's configured
        local has_discord=$(grep -q "DISCORD_BOT_TOKEN=" "${INSTANCE_SECRETS_DIR}/gateway.env" 2>/dev/null && echo "yes" || echo "no")
        local has_anthropic=$(grep -q "ANTHROPIC_API_KEY=" "${INSTANCE_SECRETS_DIR}/gateway.env" 2>/dev/null && echo "yes" || echo "no")
        local has_moonshot=$(grep -q "MOONSHOT_API_KEY=" "${INSTANCE_SECRETS_DIR}/gateway.env" 2>/dev/null && echo "yes" || echo "no")
        local has_openai=$(grep -q "OPENAI_API_KEY=" "${INSTANCE_SECRETS_DIR}/gateway.env" 2>/dev/null && echo "yes" || echo "no")

        echo "Saved configuration:"
        [[ "$has_discord" == "yes" ]] && echo "  - Discord bot token"
        [[ "$has_anthropic" == "yes" ]] && echo "  - Anthropic API key"
        [[ "$has_moonshot" == "yes" ]] && echo "  - Moonshot/Kimi API key"
        [[ "$has_openai" == "yes" ]] && echo "  - OpenAI API key"
        echo ""

        read -p "Use saved configuration? [Y/n]: " use_saved
        if [[ ! "$use_saved" =~ ^[Nn]$ ]]; then
            USE_SAVED_SECRETS="true"
            success "Using saved configuration"
        else
            info "Starting fresh configuration"
        fi
    fi

    # Load any existing values from env files as defaults
    local saved_discord_token=$(load_env_value "DISCORD_BOT_TOKEN")
    local saved_github_webhook=$(load_env_value "GITHUB_WEBHOOK_SECRET")
    local saved_github_actors=$(load_env_value "ALLOWED_MERGE_ACTORS")

    # GitHub username can be derived from actors
    local saved_github_username="${saved_github_actors%%,*}"

    # If using saved secrets, skip configuration prompts
    if [[ "$USE_SAVED_SECRETS" == "true" ]]; then
        # Load all saved values
        DISCORD_BOT_TOKEN=$(load_env_value "DISCORD_BOT_TOKEN")
        TELEGRAM_BOT_TOKEN=$(load_env_value "TELEGRAM_BOT_TOKEN")
        SLACK_BOT_TOKEN=$(load_env_value "SLACK_BOT_TOKEN")
        SLACK_APP_TOKEN=$(load_env_value "SLACK_APP_TOKEN")
        ANTHROPIC_API_KEY=$(load_env_value "ANTHROPIC_API_KEY")
        MOONSHOT_API_KEY=$(load_env_value "MOONSHOT_API_KEY")
        OPENAI_API_KEY=$(load_env_value "OPENAI_API_KEY")
        GEMINI_API_KEY=$(load_env_value "GEMINI_API_KEY")
        OLLAMA_ENABLED=$(load_env_value "OLLAMA_API_KEY")
        [[ -n "$OLLAMA_ENABLED" ]] && OLLAMA_ENABLED="true"
        OPENROUTER_API_KEY=$(load_env_value "OPENROUTER_API_KEY")
        BRAVE_API_KEY=$(load_env_value "BRAVE_API_KEY")
        ELEVENLABS_API_KEY=$(load_env_value "ELEVENLABS_API_KEY")

        # Set primary model based on what's configured
        if [[ -n "$ANTHROPIC_API_KEY" ]]; then
            PRIMARY_MODEL="anthropic/claude-sonnet-4-20250514"
        elif [[ -n "$MOONSHOT_API_KEY" ]]; then
            PRIMARY_MODEL="moonshot/kimi-k2.5"
        elif [[ -n "$OPENAI_API_KEY" ]]; then
            PRIMARY_MODEL="openai/gpt-4o"
        elif [[ -n "$OLLAMA_ENABLED" ]]; then
            PRIMARY_MODEL="ollama/llama3.2"
        fi

        # Default mode to offline
        MODE="offline"

        # Load memory settings - default to enabled with openclaw
        MEMORY_SEARCH_ENABLED="true"
        MEMORY_SEARCH_PROVIDER="openclaw"
        MEMORY_SEARCH_MODEL="local"

        # Set defaults for GitHub (offline mode when using saved)
        GITHUB_USERNAME=""
        GITHUB_WEBHOOK_SECRET=""
        GITHUB_ALLOWED_ACTORS=""
        GITHUB_BOT_REPO=""
    fi

    # If using saved secrets, skip configuration prompts
    if [[ "$USE_SAVED_SECRETS" == "true" ]]; then
        # Jump directly to token generation (after the else block below)
        :
    else

    # GitHub Integration - only offer if GitHub CLI is available and authenticated
    if [[ "$GH_AVAILABLE" == "true" ]]; then
        echo ""
        echo "=== GitHub Integration ==="
        echo "Configure GitHub for PR-based promotion workflow?"
        echo ""
        echo "  no  - Use Controller UI only (recommended)"
        echo "  yes - Configure GitHub webhooks and PR workflow"
        echo ""
        warn "Note: GitHub integration is not fully implemented yet."
        echo ""
        read -p "Configure GitHub? [y/N]: " configure_github
        if [[ "$configure_github" =~ ^[Yy]$ ]]; then
            MODE="online"
        else
            MODE="offline"
        fi
    else
        echo ""
        info "Running in local mode (GitHub CLI not available)"
        MODE="offline"
    fi

    echo ""
    echo "=== Channel Configuration ==="
    echo "Select which chat channels to configure:"
    echo ""
    echo "  1) Discord    - Discord bot"
    echo "  2) Telegram   - Telegram bot"
    echo "  3) Slack      - Slack app"
    echo ""
    echo "Enter numbers separated by spaces (e.g., '1 2' for Discord + Telegram)"
    echo ""

    read -p "Channels [1]: " channel_selection
    channel_selection="${channel_selection:-1}"

    # Initialize channel tokens
    DISCORD_BOT_TOKEN=""
    TELEGRAM_BOT_TOKEN=""
    SLACK_BOT_TOKEN=""
    SLACK_APP_TOKEN=""

    # Load saved values
    local saved_discord_token=$(load_env_value "DISCORD_BOT_TOKEN")
    local saved_telegram_token=$(load_env_value "TELEGRAM_BOT_TOKEN")
    local saved_slack_bot_token=$(load_env_value "SLACK_BOT_TOKEN")
    local saved_slack_app_token=$(load_env_value "SLACK_APP_TOKEN")

    for channel in $channel_selection; do
        case "$channel" in
            1)
                echo ""
                echo "--- Discord ---"
                echo "Create a bot at: https://discord.com/developers/applications"
                if [[ -n "$saved_discord_token" ]]; then
                    DISCORD_BOT_TOKEN="$saved_discord_token"
                    success "Discord bot token (saved)"
                else
                    prompt DISCORD_BOT_TOKEN "Discord bot token" "" true
                fi
                ;;
            2)
                echo ""
                echo "--- Telegram ---"
                echo "Create a bot via @BotFather on Telegram"
                if [[ -n "$saved_telegram_token" ]]; then
                    TELEGRAM_BOT_TOKEN="$saved_telegram_token"
                    success "Telegram bot token (saved)"
                else
                    prompt TELEGRAM_BOT_TOKEN "Telegram bot token" "" true
                fi
                ;;
            3)
                echo ""
                echo "--- Slack ---"
                echo "Create an app at: https://api.slack.com/apps"
                if [[ -n "$saved_slack_bot_token" ]]; then
                    SLACK_BOT_TOKEN="$saved_slack_bot_token"
                    SLACK_APP_TOKEN="$saved_slack_app_token"
                    success "Slack tokens (saved)"
                else
                    prompt SLACK_BOT_TOKEN "Slack bot token (xoxb-...)" "" true
                    prompt SLACK_APP_TOKEN "Slack app token (xapp-...)" "" true
                fi
                ;;
            *)
                warn "Unknown channel: $channel (skipping)"
                ;;
        esac
    done

    if [[ "$MODE" == "online" ]]; then
        echo ""
        echo "=== GitHub Configuration ==="
        if [[ -n "$saved_github_username" ]]; then
            GITHUB_USERNAME="$saved_github_username"
            success "GitHub username: $GITHUB_USERNAME (saved)"
        else
            prompt GITHUB_USERNAME "Your GitHub username"
        fi

        # GitHub org configuration
        echo ""
        echo "Bot repos can be stored under a GitHub organization."
        echo "This keeps them organized (e.g., my-bots-org/bot1-bot, my-bots-org/bot2-bot)"
        echo "Leave empty to use your personal account (${GITHUB_USERNAME})"
        echo ""

        # Load saved org if exists
        local saved_github_org=""
        if [[ -f "$CONFIG_FILE" ]]; then
            saved_github_org=$(grep "^GITHUB_ORG=" "$CONFIG_FILE" 2>/dev/null | cut -d'=' -f2- | tr -d '"')
        fi

        if [[ -n "$saved_github_org" ]]; then
            GITHUB_ORG="$saved_github_org"
            success "GitHub org: $GITHUB_ORG (saved)"
        else
            prompt GITHUB_ORG "GitHub organization (or leave empty for personal)" ""
        fi

        # Determine the repo owner (org or username)
        if [[ -n "$GITHUB_ORG" ]]; then
            GITHUB_REPO_OWNER="$GITHUB_ORG"
            info "Bot repos will be created under: $GITHUB_ORG"
        else
            GITHUB_REPO_OWNER="$GITHUB_USERNAME"
            info "Bot repos will be created under: $GITHUB_USERNAME"
        fi

        if [[ -n "$saved_github_webhook" ]]; then
            GITHUB_WEBHOOK_SECRET="$saved_github_webhook"
            GITHUB_BOT_REPO="${INSTANCE_NAME}-bot"
            success "GitHub webhook secret (saved)"
            success "Bot repo: ${GITHUB_REPO_OWNER}/${GITHUB_BOT_REPO}"
            AUTO_GITHUB=false
        else
            echo ""
            echo "Do you want to automatically configure GitHub webhooks?"
            echo "This requires a classic GitHub personal access token (not fine-grained)."
            echo "Scopes needed: 'repo' and 'admin:repo_hook'"
            if [[ -n "$GITHUB_ORG" ]]; then
                echo "For org repos, you also need 'admin:org' scope."
            fi
            echo ""
            echo "Create a classic token here:"
            echo "  https://github.com/settings/tokens/new?scopes=repo,admin:repo_hook&description=ClawFactory"
            echo ""
            echo "(If that page shows 'Fine-grained tokens', click 'Tokens (classic)' in the left sidebar)"
            echo ""
            read -p "Auto-configure GitHub? [y/N]: " auto_github

            if [[ "$auto_github" =~ ^[Yy]$ ]]; then
                prompt GITHUB_TOKEN "GitHub personal access token" "" true
                prompt GITHUB_BOT_REPO "Bot repository name (will be created if doesn't exist)" "${INSTANCE_NAME}-bot"

                GITHUB_WEBHOOK_SECRET=$(openssl rand -hex 32)
                info "Generated webhook secret automatically"

                AUTO_GITHUB=true
            else
                AUTO_GITHUB=false
                GITHUB_TOKEN=""
                GITHUB_BOT_REPO="${INSTANCE_NAME}-bot"
                echo ""
                echo "Manual webhook setup required."
                echo ""
                echo "The webhook secret is a shared secret between GitHub and ClawFactory."
                echo "It verifies that webhook requests actually come from GitHub."
                echo ""
                echo "Generate one with:  openssl rand -hex 32"
                echo ""
                echo "After setup, add this secret to your bot repo's webhook settings:"
                echo "  https://github.com/${GITHUB_REPO_OWNER}/${GITHUB_BOT_REPO}/settings/hooks/new"
                echo ""
                echo "Webhook settings:"
                echo "  Payload URL: https://your-controller/webhook/github"
                echo "  Content type: application/json"
                echo "  Secret: (paste the secret you enter below)"
                echo "  Events: Pull requests only"
                echo ""
                prompt GITHUB_WEBHOOK_SECRET "GitHub webhook secret" "" true
            fi
        fi

        GITHUB_ALLOWED_ACTORS="${GITHUB_USERNAME}"

        configure_ai_providers
    else
        GITHUB_USERNAME=""
        GITHUB_WEBHOOK_SECRET=""
        GITHUB_ALLOWED_ACTORS=""
        GITHUB_BOT_REPO=""
        configure_ai_providers
    fi

    fi  # End of USE_SAVED_SECRETS else block

    # Generate or load API tokens (instance-specific)
    info "Configuring API tokens for instance '${INSTANCE_NAME}'..."

    local TOKEN_FILE="${SECRETS_DIR}/tokens.env"
    local GATEWAY_TOKEN_VAR="${INSTANCE_NAME}_gateway_token"
    local CONTROLLER_TOKEN_VAR="${INSTANCE_NAME}_controller_token"

    # Load existing tokens if they exist
    local GATEWAY_TOKEN=""
    local CONTROLLER_TOKEN=""
    if [[ -f "$TOKEN_FILE" ]]; then
        source "$TOKEN_FILE"
        # Use indirect variable reference to get instance-specific tokens
        GATEWAY_TOKEN="${!GATEWAY_TOKEN_VAR:-}"
        CONTROLLER_TOKEN="${!CONTROLLER_TOKEN_VAR:-}"
    fi

    # Generate new tokens if not found
    if [[ -z "$GATEWAY_TOKEN" ]]; then
        GATEWAY_TOKEN=$(openssl rand -hex 32)
        success "Generated new gateway token for ${INSTANCE_NAME}"
    else
        success "Using existing gateway token for ${INSTANCE_NAME}"
    fi

    if [[ -z "$CONTROLLER_TOKEN" ]]; then
        CONTROLLER_TOKEN=$(openssl rand -hex 32)
        success "Generated new controller token for ${INSTANCE_NAME}"
    else
        success "Using existing controller token for ${INSTANCE_NAME}"
    fi

    # Save tokens to tokens.env (append/update instance-specific tokens)
    # Filter out old tokens for this instance, then append new ones
    local temp_file="${TOKEN_FILE}.tmp"

    # Start with header
    cat > "$temp_file" <<EOF
# ClawFactory API Tokens
# Generated tokens for each instance (do not edit manually)
# Format: {instance}_gateway_token, {instance}_controller_token
EOF

    # Copy existing tokens for OTHER instances (not this one)
    if [[ -f "$TOKEN_FILE" ]]; then
        grep -v "^#" "$TOKEN_FILE" | grep -v "^${INSTANCE_NAME}_" | grep -v "^$" >> "$temp_file" 2>/dev/null || true
    fi

    # Add current instance tokens
    echo "${INSTANCE_NAME}_gateway_token=${GATEWAY_TOKEN}" >> "$temp_file"
    echo "${INSTANCE_NAME}_controller_token=${CONTROLLER_TOKEN}" >> "$temp_file"

    mv "$temp_file" "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE"

    # Generate env files for containers in instance-specific folder
    local INSTANCE_SECRETS_DIR="${SECRETS_DIR}/${INSTANCE_NAME}"
    mkdir -p "${INSTANCE_SECRETS_DIR}"
    chmod 700 "${INSTANCE_SECRETS_DIR}"

    # Build gateway.env with configured channels and providers
    cat > "${INSTANCE_SECRETS_DIR}/gateway.env" <<EOF
# Gateway environment for instance: ${INSTANCE_NAME}

# Chat Channels (only non-empty tokens are used)
EOF

    # Add channel tokens if configured
    [[ -n "${DISCORD_BOT_TOKEN:-}" ]] && echo "DISCORD_BOT_TOKEN=${DISCORD_BOT_TOKEN}" >> "${INSTANCE_SECRETS_DIR}/gateway.env"
    [[ -n "${TELEGRAM_BOT_TOKEN:-}" ]] && echo "TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}" >> "${INSTANCE_SECRETS_DIR}/gateway.env"
    [[ -n "${SLACK_BOT_TOKEN:-}" ]] && echo "SLACK_BOT_TOKEN=${SLACK_BOT_TOKEN}" >> "${INSTANCE_SECRETS_DIR}/gateway.env"
    [[ -n "${SLACK_APP_TOKEN:-}" ]] && echo "SLACK_APP_TOKEN=${SLACK_APP_TOKEN}" >> "${INSTANCE_SECRETS_DIR}/gateway.env"

    echo "" >> "${INSTANCE_SECRETS_DIR}/gateway.env"
    echo "# AI Providers (only non-empty keys are used)" >> "${INSTANCE_SECRETS_DIR}/gateway.env"

    # Add each provider key if configured
    [[ -n "${ANTHROPIC_API_KEY:-}" ]] && echo "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}" >> "${INSTANCE_SECRETS_DIR}/gateway.env"
    [[ -n "${MOONSHOT_API_KEY:-}" ]] && echo "MOONSHOT_API_KEY=${MOONSHOT_API_KEY}" >> "${INSTANCE_SECRETS_DIR}/gateway.env"
    [[ -n "${OPENAI_API_KEY:-}" ]] && echo "OPENAI_API_KEY=${OPENAI_API_KEY}" >> "${INSTANCE_SECRETS_DIR}/gateway.env"
    [[ -n "${GEMINI_API_KEY:-}" ]] && echo "GEMINI_API_KEY=${GEMINI_API_KEY}" >> "${INSTANCE_SECRETS_DIR}/gateway.env"
    [[ -n "${OPENROUTER_API_KEY:-}" ]] && echo "OPENROUTER_API_KEY=${OPENROUTER_API_KEY}" >> "${INSTANCE_SECRETS_DIR}/gateway.env"
    [[ -n "${BRAVE_API_KEY:-}" ]] && echo "BRAVE_API_KEY=${BRAVE_API_KEY}" >> "${INSTANCE_SECRETS_DIR}/gateway.env"
    # Note: ELEVENLABS_API_KEY not written here - add manually if needed

    # Ollama configuration
    if [[ -n "${OLLAMA_ENABLED:-}" ]]; then
        cat >> "${INSTANCE_SECRETS_DIR}/gateway.env" <<EOF

# Ollama (local LLM)
OLLAMA_API_KEY=ollama-local
OLLAMA_BASE_URL=http://host.docker.internal:11434/v1
EOF
    fi

    # Add gateway token
    cat >> "${INSTANCE_SECRETS_DIR}/gateway.env" <<EOF

# Gateway API token (for authenticating requests TO gateway)
OPENCLAW_GATEWAY_TOKEN=${GATEWAY_TOKEN}
EOF

    # Get git user config from host (for merge commits in controller)
    local git_user_name git_user_email
    git_user_name=$(git config --global user.name 2>/dev/null || echo "ClawFactory")
    git_user_email=$(git config --global user.email 2>/dev/null || echo "bot@clawfactory.local")

    cat > "${INSTANCE_SECRETS_DIR}/controller.env" <<EOF
# Controller environment for instance: ${INSTANCE_NAME}
GITHUB_WEBHOOK_SECRET=${GITHUB_WEBHOOK_SECRET}
ALLOWED_MERGE_ACTORS=${GITHUB_ALLOWED_ACTORS}

# Controller's own API token (for authenticating requests TO controller)
CONTROLLER_API_TOKEN=${CONTROLLER_TOKEN}

# Gateway token (for controller to call gateway API)
OPENCLAW_GATEWAY_TOKEN=${GATEWAY_TOKEN}

# Instance name
INSTANCE_NAME=${INSTANCE_NAME}

# Git user config (for merge commits)
GIT_USER_NAME=${git_user_name}
GIT_USER_EMAIL=${git_user_email}
EOF

    # Add GITHUB_TOKEN if provided (for bot push capability)
    if [[ -n "${GITHUB_TOKEN:-}" ]]; then
        echo "" >> "${INSTANCE_SECRETS_DIR}/controller.env"
        echo "# GitHub token for pushing proposal branches" >> "${INSTANCE_SECRETS_DIR}/controller.env"
        echo "GITHUB_TOKEN=${GITHUB_TOKEN}" >> "${INSTANCE_SECRETS_DIR}/controller.env"
    fi

    # Generate snapshot encryption key if age is available
    if command -v age-keygen &>/dev/null; then
        if [[ ! -f "${INSTANCE_SECRETS_DIR}/snapshot.key" ]]; then
            info "Generating snapshot encryption key..."
            age-keygen -o "${INSTANCE_SECRETS_DIR}/snapshot.key" 2>"${INSTANCE_SECRETS_DIR}/snapshot.pub"
            chmod 600 "${INSTANCE_SECRETS_DIR}/snapshot.key"
            chmod 644 "${INSTANCE_SECRETS_DIR}/snapshot.pub"
            success "Snapshot encryption key generated"
        fi
    else
        info "age not installed on host - key will be auto-generated in container on first snapshot"
    fi

    chmod 600 "${INSTANCE_SECRETS_DIR}"/*.env

    # Auto-configure GitHub if requested
    if [[ "${AUTO_GITHUB:-false}" == "true" ]] && [[ -n "${GITHUB_TOKEN:-}" ]]; then
        configure_github_auto
    fi

    # Update config with GITHUB_USERNAME for docker-compose
    save_config

    success "Secrets configured"
}

# ============================================================
# Auto-configure GitHub (repo + webhook)
# ============================================================
configure_github_auto() {
    info "Configuring GitHub automatically..."

    local api_base="https://api.github.com"
    local auth_header="Authorization: Bearer ${GITHUB_TOKEN}"
    local repo_owner="${GITHUB_REPO_OWNER:-${GITHUB_ORG:-${GITHUB_USERNAME}}}"

    # Check if repo exists
    local repo_check
    repo_check=$(curl -s -o /dev/null -w "%{http_code}" \
        -H "$auth_header" \
        "${api_base}/repos/${repo_owner}/${GITHUB_BOT_REPO}")

    if [[ "$repo_check" == "404" ]]; then
        info "Creating repository ${repo_owner}/${GITHUB_BOT_REPO}..."
        local create_result

        if [[ -n "$GITHUB_ORG" && "$GITHUB_ORG" == "$repo_owner" ]]; then
            # Create in organization
            create_result=$(curl -s -X POST \
                -H "$auth_header" \
                -H "Content-Type: application/json" \
                "${api_base}/orgs/${GITHUB_ORG}/repos" \
                -d "{\"name\":\"${GITHUB_BOT_REPO}\",\"private\":true,\"description\":\"ClawFactory bot repository\"}")
        else
            # Create in personal account
            create_result=$(curl -s -X POST \
                -H "$auth_header" \
                -H "Content-Type: application/json" \
                "${api_base}/user/repos" \
                -d "{\"name\":\"${GITHUB_BOT_REPO}\",\"private\":true,\"description\":\"ClawFactory bot repository\"}")
        fi

        if echo "$create_result" | grep -q '"id"'; then
            success "Created repository"
        else
            warn "Failed to create repository: $create_result"
            return 1
        fi
    else
        info "Repository already exists"
    fi

    # Create webhook
    info "Creating webhook..."
    echo ""
    echo "Enter your Controller's public URL (where GitHub will send webhooks)."
    echo "Example: https://clawfactory.yourdomain.com"
    prompt CONTROLLER_URL "Controller URL"

    local webhook_result
    webhook_result=$(curl -s -X POST \
        -H "$auth_header" \
        -H "Content-Type: application/json" \
        "${api_base}/repos/${repo_owner}/${GITHUB_BOT_REPO}/hooks" \
        -d "{
            \"name\": \"web\",
            \"active\": true,
            \"events\": [\"pull_request\"],
            \"config\": {
                \"url\": \"${CONTROLLER_URL}/webhook/github\",
                \"content_type\": \"json\",
                \"secret\": \"${GITHUB_WEBHOOK_SECRET}\",
                \"insecure_ssl\": \"0\"
            }
        }")

    if echo "$webhook_result" | grep -q '"id"'; then
        success "Created webhook"
    else
        warn "Failed to create webhook (may already exist): $webhook_result"
    fi

    # Update local repo to push to GitHub
    info "Configuring local repo to push to GitHub..."
    cd "${BOT_REPOS_DIR}/${INSTANCE_NAME}/code"
    git remote set-url origin "https://github.com/${repo_owner}/${GITHUB_BOT_REPO}.git" 2>/dev/null || \
        git remote add origin "https://github.com/${repo_owner}/${GITHUB_BOT_REPO}.git"

    cd "${SCRIPT_DIR}"

    echo ""
    success "GitHub configured!"
    echo ""
    echo "Your bot repo: https://github.com/${repo_owner}/${GITHUB_BOT_REPO}"
    echo ""
    echo "Note: You may need to push the initial bot content to GitHub:"
    echo "  cd bot_repos/${INSTANCE_NAME}/code && git push -u origin main"
    echo ""
}

# ============================================================
# Create Kill Switch
# ============================================================
create_killswitch() {
    info "Creating kill switch script..."

    cat > "${SCRIPT_DIR}/killswitch.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IPTABLES_BACKUP="${SCRIPT_DIR}/.iptables.backup"

case "${1:-}" in
    lock)
        echo "🔒 KILL SWITCH ACTIVATED"
        echo ""

        # Stop Docker stack
        echo "Stopping containers..."
        cd "${SCRIPT_DIR}"
        docker compose down --timeout 5 || true

        # Save current iptables (macOS doesn't use iptables)
        if command -v iptables >/dev/null 2>&1; then
            echo "Saving firewall rules..."
            iptables-save > "${IPTABLES_BACKUP}" 2>/dev/null || true

            echo "Applying restrictive firewall..."
            iptables -F
            iptables -X
            iptables -P INPUT DROP
            iptables -P FORWARD DROP
            iptables -P OUTPUT DROP
            iptables -A INPUT -i lo -j ACCEPT
            iptables -A OUTPUT -o lo -j ACCEPT
            # Allow established connections (for SSH)
            iptables -A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
            iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
        else
            echo "Note: iptables not available (macOS?). Network not locked."
        fi

        echo ""
        echo "✓ System locked. All containers stopped."
        echo "  Run './killswitch.sh restore' to restore."
        ;;

    restore)
        echo "🔓 Restoring system..."

        # Restore iptables
        if [[ -f "${IPTABLES_BACKUP}" ]] && command -v iptables >/dev/null 2>&1; then
            echo "Restoring firewall rules..."
            iptables-restore < "${IPTABLES_BACKUP}"
            rm -f "${IPTABLES_BACKUP}"
        fi

        # Restart Docker stack
        echo "Starting containers..."
        cd "${SCRIPT_DIR}"
        docker compose up -d

        echo ""
        echo "✓ System restored."
        ;;

    *)
        echo "Usage: ./killswitch.sh [lock|restore]"
        echo ""
        echo "  lock    - Stop everything, lock down network"
        echo "  restore - Restore normal operation"
        ;;
esac
EOF

    chmod +x "${SCRIPT_DIR}/killswitch.sh"
    success "Kill switch created"
}

# ============================================================
# Create Helper Script
# ============================================================
create_helper() {
    info "Creating clawfactory.sh helper..."

    cat > "${SCRIPT_DIR}/clawfactory.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load default instance configuration
if [[ -f "${SCRIPT_DIR}/.clawfactory.conf" ]]; then
    source "${SCRIPT_DIR}/.clawfactory.conf"
fi

# Parse -i/--instance flag
while [[ $# -gt 0 ]]; do
    case "$1" in
        -i|--instance)
            INSTANCE_NAME="$2"
            shift 2
            ;;
        -i=*|--instance=*)
            INSTANCE_NAME="${1#*=}"
            shift
            ;;
        *)
            break
            ;;
    esac
done

INSTANCE_NAME="${INSTANCE_NAME:-default}"
export INSTANCE_NAME
export COMPOSE_PROJECT_NAME="clawfactory-${INSTANCE_NAME}"

COMPOSE_CMD="docker compose -f ${SCRIPT_DIR}/docker-compose.yml"
CONTAINER_PREFIX="clawfactory-${INSTANCE_NAME}"

# Load tokens for URLs
TOKEN_FILE="${SCRIPT_DIR}/secrets/tokens.env"
GATEWAY_TOKEN=""
CONTROLLER_TOKEN=""
if [[ -f "$TOKEN_FILE" ]]; then
    source "$TOKEN_FILE"
    gw_var="${INSTANCE_NAME}_gateway_token"
    ctrl_var="${INSTANCE_NAME}_controller_token"
    GATEWAY_TOKEN="${!gw_var:-}"
    CONTROLLER_TOKEN="${!ctrl_var:-}"
fi

case "${1:-help}" in
    start)
        ${COMPOSE_CMD} up -d
        echo "✓ ClawFactory [${INSTANCE_NAME}] started"
        if [[ -n "$GATEWAY_TOKEN" ]]; then
            echo "  Gateway:    http://localhost:18789/?token=${GATEWAY_TOKEN}"
            echo "  Controller: http://localhost:8080/controller?token=${CONTROLLER_TOKEN}"
        else
            echo "  Gateway:    http://localhost:18789"
            echo "  Controller: http://localhost:8080/controller"
        fi
        ;;
    stop)
        ${COMPOSE_CMD} down
        echo "✓ ClawFactory [${INSTANCE_NAME}] stopped"
        ;;
    restart)
        ${COMPOSE_CMD} up -d --force-recreate
        echo "✓ ClawFactory [${INSTANCE_NAME}] restarted"
        ;;
    status)
        ${COMPOSE_CMD} ps -a
        ;;
    logs)
        container="${2:-gateway}"
        docker logs -f "${CONTAINER_PREFIX}-${container}"
        ;;
    shell)
        container="${2:-gateway}"
        docker exec -it "${CONTAINER_PREFIX}-${container}" /bin/bash
        ;;
    controller)
        echo "Controller UI for [${INSTANCE_NAME}]:"
        if [[ -n "$CONTROLLER_TOKEN" ]]; then
            echo "http://127.0.0.1:8080/controller?token=${CONTROLLER_TOKEN}"
        else
            echo "http://127.0.0.1:8080/controller"
        fi
        ;;
    audit)
        curl -s http://127.0.0.1:8080/audit | jq '.entries[-10:]'
        ;;
    info)
        echo "Instance: ${INSTANCE_NAME}"
        echo "Containers: ${CONTAINER_PREFIX}-{gateway,controller,proxy}"
        # Show tokens if available
        if [[ -f "${SCRIPT_DIR}/secrets/tokens.env" ]]; then
            source "${SCRIPT_DIR}/secrets/tokens.env"
            gw_var="${INSTANCE_NAME}_gateway_token"
            ctrl_var="${INSTANCE_NAME}_controller_token"
            echo ""
            echo "Gateway token:    ${!gw_var:-<not set>}"
            echo "Controller token: ${!ctrl_var:-<not set>}"
        fi
        ;;
    list)
        echo "Configured instances:"
        if [[ -f "${SCRIPT_DIR}/secrets/tokens.env" ]]; then
            grep '_gateway_token=' "${SCRIPT_DIR}/secrets/tokens.env" 2>/dev/null | sed 's/_gateway_token=.*//' | sort -u | sed 's/^/  /'
        elif [[ -f "${SCRIPT_DIR}/.clawfactory.conf" ]]; then
            source "${SCRIPT_DIR}/.clawfactory.conf"
            echo "  ${INSTANCE_NAME:-default}"
        else
            echo "  (none - run install.sh first)"
        fi
        echo ""
        echo "Running containers:"
        docker ps --filter "name=clawfactory-" --format "table {{.Names}}\t{{.Status}}" 2>/dev/null || echo "  (none)"
        ;;
    *)
        echo "ClawFactory - Agent Runtime [${INSTANCE_NAME}]"
        echo ""
        echo "Usage: ./clawfactory.sh [-i <instance>] <command>"
        echo ""
        echo "Options:"
        echo "  -i, --instance <name>   Specify instance (default: from .clawfactory.conf)"
        echo ""
        echo "Commands:"
        echo "  start           Start containers"
        echo "  stop            Stop all containers"
        echo "  restart         Restart all containers"
        echo "  status          Show container status"
        echo "  logs [service]  Follow logs (gateway/proxy/controller)"
        echo "  shell [service] Open shell in container"
        echo "  controller      Show controller URL"
        echo "  audit           Show recent audit log"
        echo "  info            Show instance info and tokens"
        echo "  list            List all instances and running containers"
        echo ""
        echo "Examples:"
        echo "  ./clawfactory.sh start              # Start default instance"
        echo "  ./clawfactory.sh -i bot1 start      # Start 'bot1' instance"
        echo "  ./clawfactory.sh -i bot1 stop       # Stop 'bot1' instance"
        echo "  ./clawfactory.sh list               # List all instances"
        echo ""
        echo "Local access:"
        if [[ -n "$GATEWAY_TOKEN" ]]; then
            echo "  Gateway:    http://localhost:18789/?token=\${GATEWAY_TOKEN}"
            echo "  Controller: http://localhost:8080/controller?token=\${CONTROLLER_TOKEN}"
            echo ""
            echo "Run './clawfactory.sh info' to see your tokens"
        else
            echo "  Gateway:    http://localhost:18789"
            echo "  Controller: http://localhost:8080/controller"
        fi
        ;;
esac
EOF

    chmod +x "${SCRIPT_DIR}/clawfactory.sh"
    success "Helper script created"
}

# ============================================================
# Create .gitignore
# ============================================================
create_gitignore() {
    cat > "${SCRIPT_DIR}/.gitignore" <<'EOF'
# Secrets (NEVER commit)
secrets/
*.env

# Runtime state
audit/
bot_repos/

# OS
.DS_Store

# Backups
*.backup
.iptables.backup
EOF

    success "Created .gitignore"
}

# ============================================================
# Main
# ============================================================
main() {
    echo ""
    echo "╔═══════════════════════════════════════╗"
    echo "║       ClawFactory Installer           ║"
    echo "╚═══════════════════════════════════════╝"
    echo ""

    preflight
    configure_secrets
    configure_sandbox
    init_bot_repo
    create_state_config
    create_killswitch
    create_helper
    create_gitignore

    # Save final config with sandbox setting
    save_config

    echo ""
    success "Installation complete!"
    echo ""
    echo "Instance: ${INSTANCE_NAME}"
    case "${SANDBOX_MODE:-none}" in
        sysbox)      echo "Sandbox: Sysbox (Docker-in-Docker isolation)" ;;
        lima)        echo "Sandbox: Lima VM" ;;
        *)           echo "Sandbox: none (tools run directly on gateway)" ;;
    esac
    echo ""
    echo "Next steps:"
    echo "  1. Run: ./clawfactory.sh start"
    echo "  2. Check: ./clawfactory.sh status"
    echo "  3. View tokens: ./clawfactory.sh info"
    echo ""
    echo "Access:"
    echo "  Gateway:    http://localhost:18789"
    echo "  Controller: http://localhost:8080/controller?token=<your-token>"
    echo ""
    echo "For emergencies: ./killswitch.sh lock"
    echo ""
}

main "$@"
