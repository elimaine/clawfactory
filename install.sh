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
    cat > "$CONFIG_FILE" <<EOF
# ClawFactory Instance Configuration
INSTANCE_NAME="${INSTANCE_NAME:-}"
GITHUB_USERNAME="${GITHUB_USERNAME:-}"
GITHUB_ORG="${GITHUB_ORG:-}"
SANDBOX_ENABLED="${SANDBOX_ENABLED:-false}"
EOF

    # Also save to .env for docker-compose
    cat > "${SCRIPT_DIR}/.env" <<EOF
# Docker Compose environment (auto-generated)
INSTANCE_NAME=${INSTANCE_NAME:-clawfactory}
COMPOSE_PROJECT_NAME=clawfactory-${INSTANCE_NAME:-default}
GITHUB_USERNAME=${GITHUB_USERNAME:-}
GITHUB_ORG=${GITHUB_ORG:-}
SANDBOX_ENABLED=${SANDBOX_ENABLED:-false}
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
        DISCORD_BOT_TOKEN|ANTHROPIC_API_KEY|MOONSHOT_API_KEY|OPENAI_API_KEY|GEMINI_API_KEY|OLLAMA_API_KEY|OPENROUTER_API_KEY|BRAVE_API_KEY|ELEVENLABS_API_KEY|OPENCLAW_GATEWAY_TOKEN)
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
    require gh

    if ! docker info >/dev/null 2>&1; then
        die "Docker is not running. Please start Docker and try again."
    fi

    if ! gh auth status &>/dev/null; then
        die "GitHub CLI not authenticated. Run: gh auth login"
    fi

    # Check for Sysbox (optional, for sandbox support)
    SYSBOX_AVAILABLE=false
    if docker info 2>/dev/null | grep -qi sysbox; then
        SYSBOX_AVAILABLE=true
        success "Sysbox runtime detected (sandbox support available)"
    else
        info "Sysbox not detected (sandbox mode unavailable)"
        info "  Install Sysbox for agent sandbox support:"
        info "  https://github.com/nestybox/sysbox#installation"
    fi

    success "All dependencies satisfied"
}

# ============================================================
# Configure Sandbox Mode (requires Sysbox)
# ============================================================
configure_sandbox() {
    echo ""
    echo "=== Sandbox Mode ==="
    echo ""

    # Sysbox only works on Linux
    if [[ "$(uname)" == "Darwin" ]]; then
        echo "Sandbox mode is not available on macOS."
        echo "Sysbox (required for sandboxing) only supports Linux."
        echo ""
        echo "Tools will run directly on the gateway container."
        echo "For sandboxed execution, deploy on a Linux host."
        echo ""
        SANDBOX_ENABLED="false"
        return
    fi

    if [[ "$SYSBOX_AVAILABLE" != "true" ]]; then
        echo "Sysbox runtime is NOT installed."
        echo "Sandbox mode allows the agent to run tools in isolated Docker containers."
        echo ""
        echo "To enable sandbox mode later:"
        echo "  1. Install Sysbox: https://github.com/nestybox/sysbox#installation"
        echo "  2. Run: ./clawfactory.sh sandbox enable"
        echo "  3. Run: ./clawfactory.sh rebuild"
        echo ""
        SANDBOX_ENABLED="false"
        return
    fi

    echo "Sysbox is installed! You can enable sandbox mode."
    echo ""
    echo "Sandbox mode enables:"
    echo "  - Isolated Docker containers for tool execution"
    echo "  - Secure execution of untrusted code"
    echo "  - OpenClaw's native sandbox feature"
    echo ""
    echo "Without sandbox mode:"
    echo "  - Tools run directly on the gateway container"
    echo "  - Less isolation but simpler setup"
    echo ""

    read -p "Enable sandbox mode? [Y/n]: " enable_sandbox
    if [[ "$enable_sandbox" =~ ^[Nn]$ ]]; then
        SANDBOX_ENABLED="false"
        info "Sandbox mode disabled"
    else
        SANDBOX_ENABLED="true"
        success "Sandbox mode enabled"
    fi
}

# ============================================================
# Initialize Bot Repository (GitHub Fork)
# ============================================================
init_bot_repo() {
    info "Initializing bot repository..."

    mkdir -p "${BOT_REPOS_DIR}/${INSTANCE_NAME}"
    mkdir -p "${BOT_REPOS_DIR}/${INSTANCE_NAME}/state"

    # Determine repo owner (org or username)
    local repo_owner="${GITHUB_REPO_OWNER:-${GITHUB_ORG:-${GITHUB_USERNAME}}}"
    if [[ -z "$repo_owner" ]]; then
        # Try to get from gh auth
        repo_owner=$(gh api user --jq '.login' 2>/dev/null || echo "")
    fi

    if [[ -z "$repo_owner" ]]; then
        warn "GitHub not configured yet, skipping repo initialization"
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

    # Clone approved if not exists (single clone - bot pushes branches here)
    if [[ ! -d "${BOT_REPOS_DIR}/${INSTANCE_NAME}/approved/.git" ]]; then
        info "Cloning fork to approved..."
        git clone "${fork_url}" "${BOT_REPOS_DIR}/${INSTANCE_NAME}/approved"
        success "Cloned to bot_repos/${INSTANCE_NAME}/approved"
    else
        success "approved already exists"
    fi

    # Create state directory
    mkdir -p "${BOT_REPOS_DIR}/${INSTANCE_NAME}/state"

    # Create workspace config files if they don't exist
    if [[ ! -f "${BOT_REPOS_DIR}/${INSTANCE_NAME}/approved/workspace/SOUL.md" ]]; then
        info "Creating bot config files..."
        mkdir -p "${BOT_REPOS_DIR}/${INSTANCE_NAME}/approved/workspace/skills"
        mkdir -p "${BOT_REPOS_DIR}/${INSTANCE_NAME}/approved/workspace/memory"
        mkdir -p "${BOT_REPOS_DIR}/${INSTANCE_NAME}/approved/workspace/${INSTANCE_NAME}_save"

        cat > "${BOT_REPOS_DIR}/${INSTANCE_NAME}/approved/workspace/SOUL.md" <<'EOF'
# Soul

You are a helpful AI assistant running in the ClawFactory secure environment.

## Principles

1. Be helpful and honest
2. Respect user privacy
3. Admit when you don't know something
4. Follow the policies defined in your config
5. Use the proposal workflow for configuration changes

## Workspace Security

Your workspace is version-controlled. To modify your configuration:
1. Write changes to `/workspace/approved/workspace/`
2. Create a branch, commit, and push
3. Open a PR for review
4. Wait for approval before changes take effect

See `skills/propose.md` for detailed instructions.

## Capabilities

- You can use all your configured tools and skills
- You can propose changes to SOUL.md, TOOLS.md, etc.
- Changes require human approval before they take effect
- Memory search is enabled for context recall
- Your save state goes in the {instance}_save/ directory
EOF

        cat > "${BOT_REPOS_DIR}/${INSTANCE_NAME}/approved/workspace/policies.yml" <<'EOF'
# Policies

allowed_actions:
  - read_files
  - write_proposals
  - create_commits
  - open_pull_requests
  - backup_memory
  - create_snapshots

forbidden_actions:
  - direct_promotion
  - secret_access
  - network_escalation
  - docker_access
EOF

        cat > "${BOT_REPOS_DIR}/${INSTANCE_NAME}/approved/workspace/skills/propose.md" <<'EOF'
# Propose Changes Skill

When you need to modify your configuration, use the proposal workflow.

## How to Propose

1. Write changes to `/workspace/approved/workspace/`
2. Create a branch: `git checkout -b proposal/my-change`
3. Commit and push: `git add . && git commit -m "Proposal: description" && git push origin proposal/my-change`
4. Open a PR on GitHub
5. Notify your operator for review
6. Wait for approval
EOF

        cat > "${BOT_REPOS_DIR}/${INSTANCE_NAME}/approved/workspace/skills/self-management.md" <<EOF
# Self-Management Skill

This skill covers how you manage your own state, memories, and configuration.

## Architecture Overview

Your state is split into two categories:

1. **Git-tracked** (in \`approved/workspace/\`) - Goes through PR approval
   - Memory markdown files (\`memory/*.md\`)
   - Configuration (SOUL.md, skills/, policies.yml)
   - Your save directory (\`${INSTANCE_NAME}_save/\`)

2. **Encrypted snapshots** (in \`state/\`) - Not in git, backed up via encryption
   - Embeddings database (\`memory/main.sqlite\`)
   - Runtime config (\`openclaw.json\`)
   - Paired devices and credentials

## Memory Backup

To save memories to GitHub:

\`\`\`bash
curl -X POST http://controller:8080/memory/backup
\`\`\`

## Encrypted Snapshots

For full state backup (including embeddings):

\`\`\`bash
curl -X POST http://controller:8080/snapshot
\`\`\`

## Your Save Directory

You have a persistent save directory at \`/workspace/approved/workspace/${INSTANCE_NAME}_save/\`.

Add npm dependencies to \`package.json\` there - they install on container startup.

## Proposing Changes

1. Write changes to \`/workspace/approved/workspace/\`
2. Create a branch, commit, push
3. Open a PR for approval
4. Changes take effect after merge and restart

You cannot merge your own PRs - this requires human approval.
EOF

        # Create initial package.json for bot save state
        cat > "${BOT_REPOS_DIR}/${INSTANCE_NAME}/approved/workspace/${INSTANCE_NAME}_save/package.json" <<EOF
{
  "name": "${INSTANCE_NAME}-save",
  "version": "1.0.0",
  "description": "Declarative dependencies for ${INSTANCE_NAME}",
  "dependencies": {}
}
EOF

        cd "${BOT_REPOS_DIR}/${INSTANCE_NAME}/approved"
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
            gemini)
                memory_search_json='"memorySearch": {
        "enabled": true,
        "provider": "gemini",
        "model": "text-embedding-004"
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

    # Build sandbox config based on SANDBOX_ENABLED
    local sandbox_mode="off"
    if [[ "${SANDBOX_ENABLED:-false}" == "true" ]]; then
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
    echo "  2) Kimi-K2 (Moonshot)      - High-performance alternative"
    echo "  3) OpenAI (GPT-4)          - GPT models"
    echo "  4) Google (Gemini)         - Gemini models + embeddings"
    echo "  5) Ollama (Local)          - Run models locally"
    echo "  6) OpenRouter              - Multi-provider gateway"
    echo "  7) Brave Search            - Web search API"
    echo "  8) ElevenLabs              - Text-to-speech API"
    echo ""
    echo "Enter numbers separated by spaces (e.g., '1 4 7' for Anthropic + Gemini + Brave)"
    echo "Press Enter for default: Anthropic + Gemini embeddings"
    echo ""

    read -p "Providers [1 4]: " provider_selection
    provider_selection="${provider_selection:-1 4}"

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
                echo "--- Kimi-K2 (Moonshot AI) ---"
                echo "Get your API key at: https://platform.moonshot.cn/console/api-keys"
                echo "Model: kimi-k2-0905-preview (256k context)"
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
                echo "Used for: Gemini models + memory embeddings"
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
        PRIMARY_MODEL="moonshot/kimi-k2-0905-preview"
        info "Primary model: Kimi K2 (Moonshot)"
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

    # Build list of available embedding providers based on configured keys
    local available_providers=()
    local provider_descriptions=()

    if [[ -n "${OLLAMA_ENABLED:-}" ]]; then
        available_providers+=("ollama")
        provider_descriptions+=("1) Ollama (Local)        - nomic-embed-text, free, runs locally")
    fi
    if [[ -n "${OPENAI_API_KEY:-}" ]]; then
        available_providers+=("openai")
        provider_descriptions+=("2) OpenAI                - text-embedding-3-small, high quality")
    fi

    if [[ ${#available_providers[@]} -eq 0 ]]; then
        echo "No embedding providers available."
        echo "Vector memory requires Ollama, Gemini, or OpenAI."
        echo ""
        read -p "Skip vector memory setup? [Y/n]: " skip_memory
        if [[ ! "$skip_memory" =~ ^[Nn]$ ]]; then
            MEMORY_SEARCH_ENABLED="false"
            warn "Vector memory disabled (no embedding provider)"
            return
        fi
        echo ""
        echo "Configure an embedding provider:"
        echo "  - Ollama: Install from https://ollama.ai, then run 'ollama pull nomic-embed-text'"
        echo "  - Gemini: Get API key from https://aistudio.google.com/app/apikey"
        echo "  - OpenAI: Get API key from https://platform.openai.com/api-keys"
        echo ""
        MEMORY_SEARCH_ENABLED="false"
        return
    fi

    echo "Available embedding providers (based on your configured keys):"
    echo ""
    for desc in "${provider_descriptions[@]}"; do
        echo "  $desc"
    done
    echo ""
    echo "  0) Disable              - Skip vector memory"
    echo ""

    # Default to first available provider
    local default_provider="${available_providers[0]}"
    local default_num="1"
    [[ "$default_provider" == "gemini" ]] && default_num="2"
    [[ "$default_provider" == "openai" ]] && default_num="3"

    read -p "Select embedding provider [$default_num]: " embed_selection
    embed_selection="${embed_selection:-$default_num}"

    case "$embed_selection" in
        0)
            MEMORY_SEARCH_ENABLED="false"
            MEMORY_SEARCH_PROVIDER=""
            MEMORY_SEARCH_MODEL=""
            warn "Vector memory disabled"
            ;;
        1)
            if [[ " ${available_providers[*]} " =~ " ollama " ]]; then
                MEMORY_SEARCH_ENABLED="true"
                MEMORY_SEARCH_PROVIDER="ollama"
                MEMORY_SEARCH_MODEL="nomic-embed-text"
                MEMORY_SEARCH_BASE_URL="http://host.docker.internal:11434/v1"
                MEMORY_SEARCH_API_KEY="ollama-local"
                success "Vector memory: Ollama (nomic-embed-text)"
                echo ""
                info "Make sure to pull the embedding model:"
                echo "  ollama pull nomic-embed-text"
            else
                warn "Ollama not configured, skipping"
                MEMORY_SEARCH_ENABLED="false"
            fi
            ;;
        2)
            if [[ " ${available_providers[*]} " =~ " gemini " ]]; then
                MEMORY_SEARCH_ENABLED="true"
                MEMORY_SEARCH_PROVIDER="gemini"
                MEMORY_SEARCH_MODEL="text-embedding-004"
                success "Vector memory: Gemini (text-embedding-004)"
            else
                warn "Gemini not configured, skipping"
                MEMORY_SEARCH_ENABLED="false"
            fi
            ;;
        3)
            if [[ " ${available_providers[*]} " =~ " openai " ]]; then
                MEMORY_SEARCH_ENABLED="true"
                MEMORY_SEARCH_PROVIDER="openai"
                MEMORY_SEARCH_MODEL="text-embedding-3-small"
                success "Vector memory: OpenAI (text-embedding-3-small)"
            else
                warn "OpenAI not configured, skipping"
                MEMORY_SEARCH_ENABLED="false"
            fi
            ;;
        *)
            warn "Invalid selection, disabling vector memory"
            MEMORY_SEARCH_ENABLED="false"
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

    # Try to derive default from directory name
    local dir_name=$(basename "$SCRIPT_DIR")
    local default_instance="${INSTANCE_NAME:-}"
    if [[ -z "$default_instance" ]]; then
        # Sanitize directory name as default
        default_instance=$(echo "$dir_name" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-' '-' | sed 's/^-//;s/-$//' | cut -c1-32)
        [[ -z "$default_instance" ]] && default_instance="clawfactory"
    fi

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

    # Load any existing values from env files as defaults
    local saved_discord_token=$(load_env_value "DISCORD_BOT_TOKEN")
    local saved_github_webhook=$(load_env_value "GITHUB_WEBHOOK_SECRET")
    local saved_github_actors=$(load_env_value "ALLOWED_MERGE_ACTORS")

    # GitHub username can be derived from actors
    local saved_github_username="${saved_github_actors%%,*}"

    echo ""
    echo "=== Mode Selection ==="
    echo "online  - GitHub PRs for promotion, webhooks for updates"
    echo "offline - Local approval UI only"
    echo ""
    prompt MODE "Mode (online/offline)" "online"

    echo ""
    echo "=== Discord Configuration ==="
    if [[ -n "$saved_discord_token" ]]; then
        DISCORD_BOT_TOKEN="$saved_discord_token"
        success "Discord bot token (saved)"
    else
        prompt DISCORD_BOT_TOKEN "Discord bot token" "" true
    fi

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
    # First, load all existing tokens
    declare -A all_tokens
    if [[ -f "$TOKEN_FILE" ]]; then
        while IFS='=' read -r key value; do
            [[ -z "$key" || "$key" =~ ^# ]] && continue
            all_tokens["$key"]="$value"
        done < "$TOKEN_FILE"
    fi

    # Update with current instance tokens
    all_tokens["${INSTANCE_NAME}_gateway_token"]="$GATEWAY_TOKEN"
    all_tokens["${INSTANCE_NAME}_controller_token"]="$CONTROLLER_TOKEN"

    # Write all tokens back
    cat > "$TOKEN_FILE" <<EOF
# ClawFactory API Tokens
# Generated tokens for each instance (do not edit manually)
# Format: {instance}_gateway_token, {instance}_controller_token
EOF
    for key in "${!all_tokens[@]}"; do
        echo "${key}=${all_tokens[$key]}" >> "$TOKEN_FILE"
    done
    chmod 600 "$TOKEN_FILE"

    # Generate env files for containers in instance-specific folder
    local INSTANCE_SECRETS_DIR="${SECRETS_DIR}/${INSTANCE_NAME}"
    mkdir -p "${INSTANCE_SECRETS_DIR}"
    chmod 700 "${INSTANCE_SECRETS_DIR}"

    # Build gateway.env with configured providers
    cat > "${INSTANCE_SECRETS_DIR}/gateway.env" <<EOF
# Gateway environment for instance: ${INSTANCE_NAME}
DISCORD_BOT_TOKEN=${DISCORD_BOT_TOKEN}

# AI Providers (only non-empty keys are used)
EOF

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
EOF

    # Add GITHUB_TOKEN if provided (for bot push capability)
    if [[ -n "${GITHUB_TOKEN:-}" ]]; then
        echo "" >> "${INSTANCE_SECRETS_DIR}/controller.env"
        echo "# GitHub token for pushing proposal branches" >> "${INSTANCE_SECRETS_DIR}/controller.env"
        echo "GITHUB_TOKEN=${GITHUB_TOKEN}" >> "${INSTANCE_SECRETS_DIR}/controller.env"
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
    cd "${BOT_REPOS_DIR}/${INSTANCE_NAME}/approved"
    git remote set-url origin "https://github.com/${repo_owner}/${GITHUB_BOT_REPO}.git" 2>/dev/null || \
        git remote add origin "https://github.com/${repo_owner}/${GITHUB_BOT_REPO}.git"

    cd "${SCRIPT_DIR}"

    echo ""
    success "GitHub configured!"
    echo ""
    echo "Your bot repo: https://github.com/${repo_owner}/${GITHUB_BOT_REPO}"
    echo "Webhook URL: ${CONTROLLER_URL}/webhook/github"
    echo ""
    echo "Note: You may need to push the initial bot content to GitHub:"
    echo "  cd bot_repos/${INSTANCE_NAME}/approved && git push -u origin main"
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

case "${1:-help}" in
    start)
        ${COMPOSE_CMD} up -d
        echo "✓ ClawFactory [${INSTANCE_NAME}] started"
        echo "  Gateway:    http://localhost:18789"
        echo "  Controller: http://localhost:8080/controller"
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
        echo "http://127.0.0.1:8080/controller"
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
        echo "  Gateway:    http://localhost:18789"
        echo "  Controller: http://localhost:8080/controller"
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
    if [[ "${SANDBOX_ENABLED:-false}" == "true" ]]; then
        echo "Sandbox: enabled (Sysbox)"
    else
        echo "Sandbox: disabled"
    fi
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
