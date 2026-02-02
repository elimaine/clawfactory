#!/usr/bin/env bash
set -euo pipefail

# ---------------------------
# ClawFactory Install Script
# ---------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SECRETS_DIR="${SCRIPT_DIR}/secrets"
SANDYCLAWS_DIR="${SCRIPT_DIR}/sandyclaws"

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

# Load existing value from secrets.yml using grep/sed (no yq dependency)
load_secret() {
    local key="$1"
    local file="${SECRETS_DIR}/secrets.yml"
    [[ -f "$file" ]] || return

    # Handle nested keys like discord.bot_token
    if [[ "$key" == *"."* ]]; then
        local parent="${key%%.*}"
        local child="${key#*.}"
        # Simple extraction - looks for "  child: value" after "parent:"
        sed -n "/^${parent}:/,/^[a-z]/p" "$file" 2>/dev/null | \
            grep "^  ${child}:" | \
            sed 's/.*: *"\{0,1\}\([^"]*\)"\{0,1\}/\1/' | \
            head -1
    else
        grep "^${key}:" "$file" 2>/dev/null | \
            sed 's/.*: *"\{0,1\}\([^"]*\)"\{0,1\}/\1/' | \
            head -1
    fi
}

# Save current secrets state
save_secrets() {
    mkdir -p "${SECRETS_DIR}"
    chmod 700 "${SECRETS_DIR}"

    cat > "${SECRETS_DIR}/secrets.yml" <<EOF
# ClawFactory Secrets
# Generated: $(date -Iseconds)
# chmod 600 this file!

mode: ${MODE:-}

discord:
  bot_token: "${DISCORD_BOT_TOKEN:-}"
  allowed_user_ids:
    - "${DISCORD_USER_ID:-}"

github:
  username: "${GITHUB_USERNAME:-}"
  webhook_secret: "${GITHUB_WEBHOOK_SECRET:-}"
  allowed_merge_actors: "${GITHUB_ALLOWED_ACTORS:-}"
  brain_repo: "${GITHUB_BRAIN_REPO:-}"

anthropic:
  api_key: "${ANTHROPIC_API_KEY:-}"

gemini:
  api_key: "${GEMINI_API_KEY:-}"
EOF
    chmod 600 "${SECRETS_DIR}/secrets.yml"
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
# Initialize Brain Repository (GitHub Fork)
# ============================================================
init_brain() {
    info "Initializing sandyclaws brain repository..."

    mkdir -p "${SANDYCLAWS_DIR}"
    mkdir -p "${SANDYCLAWS_DIR}/openclaw-home"

    local fork_repo="${GITHUB_USERNAME}/sandyclaws-brain"
    local fork_url="https://github.com/${fork_repo}.git"

    # Check if fork exists, if not create it
    if ! gh repo view "${fork_repo}" &>/dev/null; then
        info "Forking openclaw/openclaw as sandyclaws-brain..."
        gh repo fork openclaw/openclaw --clone=false --fork-name sandyclaws-brain
        success "Created fork: ${fork_repo}"
    else
        success "Fork exists: ${fork_repo}"
    fi

    # Clone brain_work if not exists
    if [[ ! -d "${SANDYCLAWS_DIR}/brain_work/.git" ]]; then
        info "Cloning fork to brain_work..."
        git clone "${fork_url}" "${SANDYCLAWS_DIR}/brain_work"
        success "Cloned to sandyclaws/brain_work"
    else
        success "brain_work already exists"
    fi

    # Clone brain_ro if not exists
    if [[ ! -d "${SANDYCLAWS_DIR}/brain_ro/.git" ]]; then
        info "Cloning fork to brain_ro..."
        git clone "${fork_url}" "${SANDYCLAWS_DIR}/brain_ro"
        success "Cloned to sandyclaws/brain_ro"
    else
        success "brain_ro already exists"
    fi

    # Create workspace/brain files if they don't exist
    if [[ ! -f "${SANDYCLAWS_DIR}/brain_work/workspace/SOUL.md" ]]; then
        info "Creating brain workspace files..."
        mkdir -p "${SANDYCLAWS_DIR}/brain_work/workspace/skills"
        mkdir -p "${SANDYCLAWS_DIR}/brain_work/workspace/memory"

        cat > "${SANDYCLAWS_DIR}/brain_work/workspace/SOUL.md" <<'EOF'
# Soul

You are a helpful AI assistant running in the ClawFactory secure environment.

## Principles

1. Be helpful and honest
2. Respect user privacy
3. Admit when you don't know something
4. Follow the policies defined in this brain
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

        cat > "${SANDYCLAWS_DIR}/brain_work/workspace/policies.yml" <<'EOF'
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

        cat > "${SANDYCLAWS_DIR}/brain_work/workspace/skills/propose.md" <<'EOF'
# Propose Changes Skill

When you need to modify your configuration, use the proposal workflow.

## How to Propose

1. Write changes to `/workspace/proposals/workspace/`
2. Commit and push: `git add . && git commit -m "Proposal: description" && git push origin HEAD:refs/heads/proposal/name`
3. Notify your operator for review
4. Wait for approval
EOF

        cat > "${SANDYCLAWS_DIR}/brain_work/workspace/skills/memory-backup.md" <<'EOF'
# Memory Backup Skill

Your memories persist across restarts. To backup to GitHub:

```bash
curl -X POST http://controller:8080/memory/backup
```

This commits memory files and pushes to GitHub for disaster recovery.
EOF

        cd "${SANDYCLAWS_DIR}/brain_work"
        git add workspace/
        git commit -m "Add ClawFactory brain workspace files"
        git push origin main
        cd "${SCRIPT_DIR}"

        success "Created brain workspace files"
    else
        success "Brain workspace files already exist"
    fi

    # Pull latest to brain_ro
    cd "${SANDYCLAWS_DIR}/brain_ro"
    git pull origin main 2>/dev/null || true
    cd "${SCRIPT_DIR}"

    success "Brain repository initialized"
}

# ============================================================
# Configure Secrets
# ============================================================
configure_secrets() {
    info "Configuring secrets..."

    mkdir -p "${SECRETS_DIR}"
    chmod 700 "${SECRETS_DIR}"

    # Load any existing values as defaults
    local saved_mode=$(load_secret "mode")
    local saved_discord_token=$(load_secret "discord.bot_token")
    local saved_discord_user=$(load_secret "discord.allowed_user_ids")
    local saved_github_username=$(load_secret "github.username")
    local saved_github_webhook=$(load_secret "github.webhook_secret")
    local saved_github_repo=$(load_secret "github.brain_repo")
    local saved_anthropic=$(load_secret "anthropic.api_key")
    local saved_gemini=$(load_secret "gemini.api_key")

    # Clean up array notation from user ID
    saved_discord_user="${saved_discord_user#- }"

    # Track if we have all required secrets
    local missing_secrets=false

    echo ""
    echo "=== Mode Selection ==="
    if [[ -n "$saved_mode" ]]; then
        MODE="$saved_mode"
        success "Mode: $MODE (saved)"
    else
        echo "online  - GitHub PRs for promotion, Cloudflare for ingress"
        echo "offline - Local approval UI only"
        echo ""
        prompt MODE "Mode (online/offline)" "online"
        save_secrets
    fi

    echo ""
    echo "=== Discord Configuration ==="
    if [[ -n "$saved_discord_token" ]]; then
        DISCORD_BOT_TOKEN="$saved_discord_token"
        success "Discord bot token (saved)"
    else
        prompt DISCORD_BOT_TOKEN "Discord bot token" "" true
        save_secrets
    fi

    if [[ -n "$saved_discord_user" ]]; then
        DISCORD_USER_ID="$saved_discord_user"
        success "Discord user ID: $DISCORD_USER_ID (saved)"
    else
        prompt DISCORD_USER_ID "Your Discord user ID (for DM allowlist)"
        save_secrets
    fi

    if [[ "$MODE" == "online" ]]; then
        echo ""
        echo "=== GitHub Configuration ==="
        if [[ -n "$saved_github_username" ]]; then
            GITHUB_USERNAME="$saved_github_username"
            success "GitHub username: $GITHUB_USERNAME (saved)"
        else
            echo ""
            prompt GITHUB_USERNAME "Your GitHub username"
            save_secrets
        fi

        if [[ -n "$saved_github_webhook" ]]; then
            GITHUB_WEBHOOK_SECRET="$saved_github_webhook"
            GITHUB_BRAIN_REPO="${saved_github_repo:-sandyclaws-brain}"
            success "GitHub webhook secret (saved)"
            success "Brain repo: $GITHUB_BRAIN_REPO (saved)"
            AUTO_GITHUB=false
        else
            echo ""
            echo "Do you want to automatically configure GitHub webhooks?"
            echo "This requires a classic GitHub personal access token (not fine-grained)."
            echo "Scopes needed: 'repo' and 'admin:repo_hook'"
            echo ""
            echo "Create a classic token here:"
            echo "  https://github.com/settings/tokens/new?scopes=repo,admin:repo_hook&description=ClawFactory"
            echo ""
            echo "(If that page shows 'Fine-grained tokens', click 'Tokens (classic)' in the left sidebar)"
            echo ""
            read -p "Auto-configure GitHub? [y/N]: " auto_github

            if [[ "$auto_github" =~ ^[Yy]$ ]]; then
                prompt GITHUB_TOKEN "GitHub personal access token" "" true
                prompt GITHUB_BRAIN_REPO "Brain repository name (will be created if doesn't exist)" "${saved_github_repo:-sandyclaws-brain}"
                save_secrets

                GITHUB_WEBHOOK_SECRET=$(openssl rand -hex 32)
                info "Generated webhook secret automatically"
                save_secrets

                AUTO_GITHUB=true
            else
                AUTO_GITHUB=false
                GITHUB_TOKEN=""
                GITHUB_BRAIN_REPO="${saved_github_repo:-sandyclaws-brain}"
                echo ""
                echo "Manual webhook setup required."
                echo ""
                echo "The webhook secret is a shared secret between GitHub and ClawFactory."
                echo "It verifies that webhook requests actually come from GitHub."
                echo ""
                echo "Generate one with:  openssl rand -hex 32"
                echo ""
                echo "After setup, add this secret to your brain repo's webhook settings:"
                echo "  https://github.com/${GITHUB_USERNAME}/${GITHUB_BRAIN_REPO}/settings/hooks/new"
                echo ""
                echo "Webhook settings:"
                echo "  Payload URL: https://your-controller/webhook/github"
                echo "  Content type: application/json"
                echo "  Secret: (paste the secret you enter below)"
                echo "  Events: Pull requests only"
                echo ""
                prompt GITHUB_WEBHOOK_SECRET "GitHub webhook secret" "" true
                save_secrets
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
            save_secrets
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
            save_secrets
        fi
    else
        GITHUB_USERNAME=""
        GITHUB_WEBHOOK_SECRET=""
        GITHUB_ALLOWED_ACTORS=""
        GITHUB_BRAIN_REPO=""
        echo ""
        echo "=== AI Provider (optional for offline) ==="
        if [[ -n "$saved_anthropic" ]]; then
            ANTHROPIC_API_KEY="$saved_anthropic"
            success "Anthropic API key (saved)"
        else
            prompt ANTHROPIC_API_KEY "Anthropic API key (or leave empty for local LLM)" "" true
            save_secrets
        fi

        echo ""
        echo "=== Memory (Gemini Embeddings) ==="
        if [[ -n "$saved_gemini" ]]; then
            GEMINI_API_KEY="$saved_gemini"
            success "Gemini API key (saved)"
        else
            prompt GEMINI_API_KEY "Gemini API key (optional)" "" true
            save_secrets
        fi
    fi

    # Generate env files for containers
    cat > "${SECRETS_DIR}/gateway.env" <<EOF
# Gateway environment
DISCORD_BOT_TOKEN=${DISCORD_BOT_TOKEN}
ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
GEMINI_API_KEY=${GEMINI_API_KEY}
EOF

    cat > "${SECRETS_DIR}/controller.env" <<EOF
# Controller environment
GITHUB_WEBHOOK_SECRET=${GITHUB_WEBHOOK_SECRET}
ALLOWED_MERGE_ACTORS=${GITHUB_ALLOWED_ACTORS}
EOF

    chmod 600 "${SECRETS_DIR}"/*.env

    # Auto-configure GitHub if requested
    if [[ "${AUTO_GITHUB:-false}" == "true" ]] && [[ -n "${GITHUB_TOKEN:-}" ]]; then
        configure_github_auto
    fi

    success "Secrets configured"
}

# ============================================================
# Auto-configure GitHub (repo + webhook)
# ============================================================
configure_github_auto() {
    info "Configuring GitHub automatically..."

    local api_base="https://api.github.com"
    local auth_header="Authorization: Bearer ${GITHUB_TOKEN}"

    # Check if repo exists
    local repo_check
    repo_check=$(curl -s -o /dev/null -w "%{http_code}" \
        -H "$auth_header" \
        "${api_base}/repos/${GITHUB_USERNAME}/${GITHUB_BRAIN_REPO}")

    if [[ "$repo_check" == "404" ]]; then
        info "Creating repository ${GITHUB_USERNAME}/${GITHUB_BRAIN_REPO}..."
        local create_result
        create_result=$(curl -s -X POST \
            -H "$auth_header" \
            -H "Content-Type: application/json" \
            "${api_base}/user/repos" \
            -d "{\"name\":\"${GITHUB_BRAIN_REPO}\",\"private\":true,\"description\":\"SandyClaws brain repository\"}")

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
        "${api_base}/repos/${GITHUB_USERNAME}/${GITHUB_BRAIN_REPO}/hooks" \
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

    # Update local brain to push to GitHub
    info "Configuring local brain to push to GitHub..."
    cd "${SANDYCLAWS_DIR}/brain_work"
    git remote set-url origin "https://github.com/${GITHUB_USERNAME}/${GITHUB_BRAIN_REPO}.git" 2>/dev/null || \
        git remote add origin "https://github.com/${GITHUB_USERNAME}/${GITHUB_BRAIN_REPO}.git"

    # Update bare repo remote too
    cd "${SANDYCLAWS_DIR}/brain.git"
    git remote add github "https://github.com/${GITHUB_USERNAME}/${GITHUB_BRAIN_REPO}.git" 2>/dev/null || true

    cd "${SCRIPT_DIR}"

    echo ""
    success "GitHub configured!"
    echo ""
    echo "Your brain repo: https://github.com/${GITHUB_USERNAME}/${GITHUB_BRAIN_REPO}"
    echo "Webhook URL: ${CONTROLLER_URL}/webhook/github"
    echo ""
    echo "Note: You may need to push the initial brain content to GitHub:"
    echo "  cd sandyclaws/brain_work && git push -u origin main"
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

case "${1:-help}" in
    start)
        docker compose -f "${SCRIPT_DIR}/docker-compose.yml" up -d
        echo "✓ ClawFactory started"
        ;;
    stop)
        docker compose -f "${SCRIPT_DIR}/docker-compose.yml" down
        echo "✓ ClawFactory stopped"
        ;;
    restart)
        docker compose -f "${SCRIPT_DIR}/docker-compose.yml" restart
        ;;
    status)
        docker compose -f "${SCRIPT_DIR}/docker-compose.yml" ps
        ;;
    logs)
        container="${2:-gateway}"
        docker logs -f "clawfactory-${container}"
        ;;
    shell)
        container="${2:-gateway}"
        docker exec -it "clawfactory-${container}" /bin/bash
        ;;
    promote)
        echo "Opening promotion UI..."
        echo "http://127.0.0.1:8080/promote"
        ;;
    audit)
        curl -s http://127.0.0.1:8080/audit | jq '.entries[-10:]'
        ;;
    *)
        echo "ClawFactory - Agent Runtime"
        echo ""
        echo "Usage: ./clawfactory.sh <command>"
        echo ""
        echo "Commands:"
        echo "  start           Start all containers"
        echo "  stop            Stop all containers"
        echo "  restart         Restart all containers"
        echo "  status          Show container status"
        echo "  logs [name]     Follow logs (gateway/controller)"
        echo "  shell [name]    Open shell in container"
        echo "  promote         Open promotion UI"
        echo "  audit           Show recent audit log"
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
sandyclaws/brain_ro/
sandyclaws/brain_work/

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
    init_brain
    configure_secrets
    create_killswitch
    create_helper
    create_gitignore

    echo ""
    success "Installation complete!"
    echo ""
    echo "Next steps:"
    echo "  1. Review secrets/secrets.yml"
    echo "  2. Run: ./clawfactory.sh start"
    echo "  3. Check: ./clawfactory.sh status"
    echo ""
    echo "For emergencies: ./killswitch.sh lock"
    echo ""
}

main "$@"
