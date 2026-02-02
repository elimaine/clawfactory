#!/usr/bin/env bash
set -euo pipefail

# ---------------------------
# ClawFactory Install Script
# ---------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SECRETS_DIR="${SCRIPT_DIR}/secrets"
BRAIN_DIR="${SCRIPT_DIR}/brain"

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
        prompt_text="${prompt_text} [${default}]"
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

    success "All dependencies satisfied"
}

# ============================================================
# Initialize Brain Repository
# ============================================================
init_brain() {
    info "Initializing brain repository..."

    mkdir -p "${BRAIN_DIR}"

    # Initialize bare repo if not exists
    if [[ ! -d "${BRAIN_DIR}/brain.git/HEAD" ]]; then
        git init --bare "${BRAIN_DIR}/brain.git"
        info "Created bare repository at brain/brain.git"
    fi

    # Create brain_ro if not exists
    if [[ ! -d "${BRAIN_DIR}/brain_ro" ]]; then
        mkdir -p "${BRAIN_DIR}/brain_ro"
        info "Created brain_ro directory"
    fi

    # Create brain_work if not exists
    if [[ ! -d "${BRAIN_DIR}/brain_work/.git" ]]; then
        mkdir -p "${BRAIN_DIR}/brain_work"
        cd "${BRAIN_DIR}/brain_work"
        git init
        git remote add origin "${BRAIN_DIR}/brain.git" 2>/dev/null || true

        # Create initial brain content
        cat > SOUL.md <<'EOF'
# Soul

You are a helpful AI assistant.

## Principles

1. Be helpful and honest
2. Respect user privacy
3. Admit when you don't know something
4. Follow the policies defined in this brain

## Capabilities

You can propose changes to your own configuration by creating commits.
These changes require human approval before they take effect.
EOF

        cat > policies.yml <<'EOF'
# Policies

allowed_actions:
  - read_files
  - write_proposals
  - create_commits
  - open_pull_requests

forbidden_actions:
  - direct_promotion
  - secret_access
  - network_escalation
  - docker_access
EOF

        git add -A
        git commit -m "Initial brain setup"
        git push -u origin main 2>/dev/null || git push --set-upstream origin main

        info "Created initial brain content"
        cd "${SCRIPT_DIR}"
    fi

    # Checkout to brain_ro
    cd "${BRAIN_DIR}/brain.git"
    local sha
    sha=$(git rev-parse main 2>/dev/null || echo "")
    if [[ -n "$sha" ]]; then
        git --work-tree "${BRAIN_DIR}/brain_ro" checkout main -- . 2>/dev/null || true
        info "Checked out main to brain_ro"
    fi

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

    # Check if secrets already exist
    if [[ -f "${SECRETS_DIR}/secrets.yml" ]]; then
        warn "secrets/secrets.yml already exists"
        read -p "Overwrite? [y/N]: " overwrite
        if [[ ! "$overwrite" =~ ^[Yy]$ ]]; then
            info "Keeping existing secrets"
            return
        fi
    fi

    echo ""
    echo "=== Mode Selection ==="
    echo "online  - GitHub PRs for promotion, Cloudflare for ingress"
    echo "offline - Local approval UI only"
    echo ""
    prompt MODE "Mode (online/offline)" "online"

    echo ""
    echo "=== Discord Configuration ==="
    prompt DISCORD_BOT_TOKEN "Discord bot token" "" true
    prompt DISCORD_USER_ID "Your Discord user ID (for DM allowlist)"

    if [[ "$MODE" == "online" ]]; then
        echo ""
        echo "=== GitHub Configuration ==="
        prompt GITHUB_WEBHOOK_SECRET "GitHub webhook secret" "" true
        prompt GITHUB_ALLOWED_ACTORS "Allowed merge actors (comma-separated)"

        echo ""
        echo "=== AI Provider ==="
        prompt ANTHROPIC_API_KEY "Anthropic API key" "" true
    else
        GITHUB_WEBHOOK_SECRET=""
        GITHUB_ALLOWED_ACTORS=""
        echo ""
        echo "=== AI Provider (optional for offline) ==="
        prompt ANTHROPIC_API_KEY "Anthropic API key (or leave empty for local LLM)" "" true
    fi

    # Write secrets.yml
    cat > "${SECRETS_DIR}/secrets.yml" <<EOF
# ClawFactory Secrets
# Generated: $(date -Iseconds)
# chmod 600 this file!

mode: ${MODE}

discord:
  bot_token: "${DISCORD_BOT_TOKEN}"
  allowed_user_ids:
    - "${DISCORD_USER_ID}"

github:
  webhook_secret: "${GITHUB_WEBHOOK_SECRET}"
  allowed_merge_actors: "${GITHUB_ALLOWED_ACTORS}"

anthropic:
  api_key: "${ANTHROPIC_API_KEY}"
EOF

    chmod 600 "${SECRETS_DIR}/secrets.yml"

    # Generate env files for containers
    cat > "${SECRETS_DIR}/gateway.env" <<EOF
# Gateway environment
DISCORD_BOT_TOKEN=${DISCORD_BOT_TOKEN}
ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
EOF

    cat > "${SECRETS_DIR}/controller.env" <<EOF
# Controller environment
GITHUB_WEBHOOK_SECRET=${GITHUB_WEBHOOK_SECRET}
ALLOWED_MERGE_ACTORS=${GITHUB_ALLOWED_ACTORS}
EOF

    chmod 600 "${SECRETS_DIR}"/*.env

    success "Secrets configured"
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
        echo "  logs [name]     Follow logs (gateway/runner/controller)"
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
brain/brain_ro/
brain/brain_work/

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
