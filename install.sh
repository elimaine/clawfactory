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
EOF

    # Also save to .env for docker-compose
    cat > "${SCRIPT_DIR}/.env" <<EOF
# Docker Compose environment (auto-generated)
INSTANCE_NAME=${INSTANCE_NAME:-clawfactory}
COMPOSE_PROJECT_NAME=clawfactory-${INSTANCE_NAME:-default}
GITHUB_USERNAME=${GITHUB_USERNAME:-}
GITHUB_ORG=${GITHUB_ORG:-}
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
        DISCORD_BOT_TOKEN|ANTHROPIC_API_KEY|GEMINI_API_KEY|OPENCLAW_GATEWAY_TOKEN)
            file="${SECRETS_DIR}/${instance}/gateway.env"
            ;;
        GITHUB_WEBHOOK_SECRET|ALLOWED_MERGE_ACTORS|CONTROLLER_API_TOKEN)
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

    success "All dependencies satisfied"
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

    # Clone working_repo if not exists
    if [[ ! -d "${BOT_REPOS_DIR}/${INSTANCE_NAME}/working_repo/.git" ]]; then
        info "Cloning fork to working_repo..."
        git clone "${fork_url}" "${BOT_REPOS_DIR}/${INSTANCE_NAME}/working_repo"
        success "Cloned to bot_repos/${INSTANCE_NAME}/working_repo"
    else
        success "working_repo already exists"
    fi

    # Clone approved if not exists
    if [[ ! -d "${BOT_REPOS_DIR}/${INSTANCE_NAME}/approved/.git" ]]; then
        info "Cloning fork to approved..."
        git clone "${fork_url}" "${BOT_REPOS_DIR}/${INSTANCE_NAME}/approved"
        success "Cloned to bot_repos/${INSTANCE_NAME}/approved"
    else
        success "approved already exists"
    fi

    # Create workspace config files if they don't exist
    if [[ ! -f "${BOT_REPOS_DIR}/${INSTANCE_NAME}/working_repo/workspace/SOUL.md" ]]; then
        info "Creating bot config files..."
        mkdir -p "${BOT_REPOS_DIR}/${INSTANCE_NAME}/working_repo/workspace/skills"
        mkdir -p "${BOT_REPOS_DIR}/${INSTANCE_NAME}/working_repo/workspace/memory"

        cat > "${BOT_REPOS_DIR}/${INSTANCE_NAME}/working_repo/workspace/SOUL.md" <<'EOF'
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
1. Write changes to `/workspace/proposals/workspace/`
2. Commit and push to create a proposal branch
3. Notify your operator for review
4. Wait for approval before changes take effect

See `skills/propose.md` for detailed instructions.

## Capabilities

- You can use all your configured tools and skills
- You can propose changes to SOUL.md, TOOLS.md, etc.
- Changes require human approval before they take effect
- Memory search is enabled for context recall
EOF

        cat > "${BOT_REPOS_DIR}/${INSTANCE_NAME}/working_repo/workspace/policies.yml" <<'EOF'
# Policies

allowed_actions:
  - read_files
  - write_proposals
  - create_commits
  - open_pull_requests
  - backup_memory

forbidden_actions:
  - direct_promotion
  - secret_access
  - network_escalation
  - docker_access
EOF

        cat > "${BOT_REPOS_DIR}/${INSTANCE_NAME}/working_repo/workspace/skills/propose.md" <<'EOF'
# Propose Changes Skill

When you need to modify your configuration, use the proposal workflow.

## How to Propose

1. Write changes to `/workspace/proposals/workspace/`
2. Commit and push: `git add . && git commit -m "Proposal: description" && git push origin HEAD:refs/heads/proposal/name`
3. Notify your operator for review
4. Wait for approval
EOF

        cat > "${BOT_REPOS_DIR}/${INSTANCE_NAME}/working_repo/workspace/skills/memory-backup.md" <<'EOF'
# Memory Backup Skill

Your memories persist across restarts. To backup to GitHub:

```bash
curl -X POST http://controller:8080/memory/backup
```

This commits memory files and pushes to GitHub for disaster recovery.
EOF

        cd "${BOT_REPOS_DIR}/${INSTANCE_NAME}/working_repo"
        git add workspace/
        git commit -m "Add ClawFactory bot config files"
        git push origin main
        cd "${SCRIPT_DIR}"

        success "Created bot config files"
    else
        success "Bot config files already exist"
    fi

    # Pull latest to approved
    cd "${BOT_REPOS_DIR}/${INSTANCE_NAME}/approved"
    git pull origin main 2>/dev/null || true
    cd "${SCRIPT_DIR}"

    success "Bot repository initialized"
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
    local saved_anthropic=$(load_env_value "ANTHROPIC_API_KEY")
    local saved_gemini=$(load_env_value "GEMINI_API_KEY")
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

        echo ""
        echo "=== AI Provider ==="
        if [[ -n "$saved_anthropic" ]]; then
            ANTHROPIC_API_KEY="$saved_anthropic"
            success "Anthropic API key (saved)"
        else
            prompt ANTHROPIC_API_KEY "Anthropic API key" "" true
        fi

        echo ""
        echo "=== Memory (Gemini Embeddings) ==="
        echo "For agent memory/search. Get a key at:"
        echo "  https://aistudio.google.com/app/apikey"
        echo ""
        if [[ -n "$saved_gemini" ]]; then
            GEMINI_API_KEY="$saved_gemini"
            success "Gemini API key (saved)"
        else
            prompt GEMINI_API_KEY "Gemini API key (optional, for memory)" "" true
        fi
    else
        GITHUB_USERNAME=""
        GITHUB_WEBHOOK_SECRET=""
        GITHUB_ALLOWED_ACTORS=""
        GITHUB_BOT_REPO=""
        echo ""
        echo "=== AI Provider (optional for offline) ==="
        if [[ -n "$saved_anthropic" ]]; then
            ANTHROPIC_API_KEY="$saved_anthropic"
            success "Anthropic API key (saved)"
        else
            prompt ANTHROPIC_API_KEY "Anthropic API key (or leave empty for local LLM)" "" true
        fi

        echo ""
        echo "=== Memory (Gemini Embeddings) ==="
        if [[ -n "$saved_gemini" ]]; then
            GEMINI_API_KEY="$saved_gemini"
            success "Gemini API key (saved)"
        else
            prompt GEMINI_API_KEY "Gemini API key (optional)" "" true
        fi
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

    cat > "${INSTANCE_SECRETS_DIR}/gateway.env" <<EOF
# Gateway environment for instance: ${INSTANCE_NAME}
DISCORD_BOT_TOKEN=${DISCORD_BOT_TOKEN}
ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
GEMINI_API_KEY=${GEMINI_API_KEY}

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
    cd "${BOT_REPOS_DIR}/${INSTANCE_NAME}/working_repo"
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
    echo "  cd bot_repos/${INSTANCE_NAME}/working_repo && git push -u origin main"
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
    init_bot_repo
    create_killswitch
    create_helper
    create_gitignore

    echo ""
    success "Installation complete!"
    echo ""
    echo "Instance: ${INSTANCE_NAME}"
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
