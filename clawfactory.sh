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

# Check if router is running
router_running() {
    docker ps --filter "name=clawfactory-router" --format "{{.Names}}" 2>/dev/null | grep -q "clawfactory-router"
}

# Check if hosts entry exists for an instance
hosts_entry_exists() {
    local instance="$1"
    grep -q "${instance}\.local" /etc/hosts 2>/dev/null
}

# Get all configured instances
get_all_instances() {
    local instances=""
    if [[ -d "${SCRIPT_DIR}/secrets" ]]; then
        for d in "${SCRIPT_DIR}"/secrets/*/; do
            if [[ -d "$d" ]]; then
                local name=$(basename "$d")
                instances="$instances $name"
            fi
        done
    fi
    echo "$instances"
}

# Get instances missing from hosts file
get_missing_hosts() {
    local missing=""
    for instance in $(get_all_instances); do
        if ! hosts_entry_exists "$instance"; then
            missing="$missing ${instance}.local"
        fi
    done
    echo "$missing"
}

# Prompt user to add hosts entries
prompt_add_hosts() {
    local missing=$(get_missing_hosts)
    if [[ -z "${missing// }" ]]; then
        return 0  # All entries exist
    fi

    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Hosts file setup needed for multi-instance access"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo "  ClawFactory uses subdomains to run multiple bots at once:"
    echo "    - http://sandy.local:8080/controller"
    echo "    - http://testbot.local:8080/controller"
    echo ""
    echo "  Missing entries:${missing}"
    echo ""
    echo "  This requires adding a line to /etc/hosts (one-time setup)."
    echo ""

    # Check if running interactively
    if [[ -t 0 ]]; then
        read -p "  Add hosts entries now? [y/N] " -n 1 -r
        echo ""
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            add_hosts_entries
        else
            echo ""
            echo "  To add manually later, run:"
            echo "    ./clawfactory.sh hosts add"
            echo ""
        fi
    else
        echo "  Run './clawfactory.sh hosts add' to set this up."
        echo ""
    fi
}

# Add hosts entries with sudo
add_hosts_entries() {
    local missing=$(get_missing_hosts)
    if [[ -z "${missing// }" ]]; then
        echo "  All hosts entries already exist."
        return 0
    fi

    local hosts_line="127.0.0.1${missing}"

    echo ""
    echo "  Adding to /etc/hosts:"
    echo "    ${hosts_line}"
    echo ""

    # Use sudo to append to hosts file
    if echo "$hosts_line" | sudo tee -a /etc/hosts > /dev/null; then
        echo "  ✓ Hosts entries added successfully!"
        echo ""
        echo "  You can now access:"
        for instance in $(get_all_instances); do
            echo "    http://${instance}.local:8080/controller"
        done
        echo ""
    else
        echo "  ✗ Failed to add hosts entries."
        echo "  Try manually: echo '${hosts_line}' | sudo tee -a /etc/hosts"
    fi
}

case "${1:-help}" in
    router)
        case "${2:-status}" in
            start)
                docker compose -f "${SCRIPT_DIR}/router/docker-compose.yml" up -d
                echo "✓ Router started"
                echo ""
                echo "Add to /etc/hosts:"
                echo "  127.0.0.1 ${INSTANCE_NAME}.local"
                echo ""
                echo "Then access: http://${INSTANCE_NAME}.local:8080/controller"
                ;;
            stop)
                docker compose -f "${SCRIPT_DIR}/router/docker-compose.yml" down
                echo "✓ Router stopped"
                ;;
            *)
                if router_running; then
                    echo "Router: running"
                else
                    echo "Router: stopped"
                    echo "  Start with: ./clawfactory.sh router start"
                fi
                ;;
        esac
        ;;
    hosts)
        case "${2:-show}" in
            add)
                add_hosts_entries
                ;;
            check)
                missing=$(get_missing_hosts)
                if [[ -z "${missing// }" ]]; then
                    echo "✓ All hosts entries are configured"
                else
                    echo "Missing hosts entries:${missing}"
                    echo ""
                    echo "Run './clawfactory.sh hosts add' to fix"
                fi
                ;;
            *)
                # Show current status and instructions
                echo "Hosts file status:"
                echo ""
                for instance in $(get_all_instances); do
                    if hosts_entry_exists "$instance"; then
                        echo "  ✓ ${instance}.local"
                    else
                        echo "  ✗ ${instance}.local (missing)"
                    fi
                done
                echo ""
                missing=$(get_missing_hosts)
                if [[ -n "${missing// }" ]]; then
                    echo "To add missing entries:"
                    echo "  ./clawfactory.sh hosts add"
                    echo ""
                    echo "Or manually:"
                    echo "  echo '127.0.0.1${missing}' | sudo tee -a /etc/hosts"
                else
                    echo "All instances are configured!"
                fi
                ;;
        esac
        ;;
    start)
        # Ensure router is running first
        if ! router_running; then
            echo "Starting router for subdomain access..."
            docker compose -f "${SCRIPT_DIR}/router/docker-compose.yml" up -d
        fi

        ${COMPOSE_CMD} up -d
        echo "✓ ClawFactory [${INSTANCE_NAME}] started"

        if [[ -n "$CONTROLLER_TOKEN" ]]; then
            echo "  Gateway:    http://${INSTANCE_NAME}.local:18789/?token=${GATEWAY_TOKEN}"
            echo "  Controller: http://${INSTANCE_NAME}.local:8080/controller?token=${CONTROLLER_TOKEN}"
        else
            echo "  Gateway:    http://${INSTANCE_NAME}.local:18789"
            echo "  Controller: http://${INSTANCE_NAME}.local:8080/controller"
        fi

        # Check if hosts entry exists for this instance
        if ! hosts_entry_exists "$INSTANCE_NAME"; then
            prompt_add_hosts
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
    rebuild)
        echo "Rebuilding ClawFactory [${INSTANCE_NAME}]..."
        ${COMPOSE_CMD} build --no-cache
        ${COMPOSE_CMD} up -d --force-recreate
        echo "✓ ClawFactory [${INSTANCE_NAME}] rebuilt and restarted"
        if router_running; then
            if [[ -n "$CONTROLLER_TOKEN" ]]; then
                echo "  Gateway:    http://${INSTANCE_NAME}.local:18789/?token=${GATEWAY_TOKEN}"
                echo "  Controller: http://${INSTANCE_NAME}.local:8080/controller?token=${CONTROLLER_TOKEN}"
            else
                echo "  Gateway:    http://${INSTANCE_NAME}.local:18789"
                echo "  Controller: http://${INSTANCE_NAME}.local:8080/controller"
            fi
        else
            echo ""
            echo "  Note: Router not running. Start it for subdomain access:"
            echo "    ./clawfactory.sh router start"
        fi
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
        if router_running; then
            if [[ -n "$CONTROLLER_TOKEN" ]]; then
                echo "http://${INSTANCE_NAME}.local:8080/controller?token=${CONTROLLER_TOKEN}"
            else
                echo "http://${INSTANCE_NAME}.local:8080/controller"
            fi
        else
            echo "Router not running. Start it first:"
            echo "  ./clawfactory.sh router start"
        fi
        ;;
    audit)
        if router_running; then
            curl -s "http://${INSTANCE_NAME}.local:8080/controller/audit" | jq '.entries[-10:]'
        else
            echo "Router not running. Start it first:"
            echo "  ./clawfactory.sh router start"
        fi
        ;;
    info)
        echo "Instance: ${INSTANCE_NAME}"
        echo "Containers: ${CONTAINER_PREFIX}-{gateway,controller}"
        echo "URL: http://${INSTANCE_NAME}.local:8080/controller"
        if router_running; then
            echo "Router: running"
        else
            echo "Router: stopped (run './clawfactory.sh router start')"
        fi
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
    remote)
        REPO_DIR="${SCRIPT_DIR}/bot_repos/${INSTANCE_NAME}/approved"
        if [[ ! -d "$REPO_DIR" ]]; then
            echo "Error: Instance directory not found: $REPO_DIR"
            exit 1
        fi

        # Load GitHub config from .env (save instance name first)
        SAVED_INSTANCE="${INSTANCE_NAME}"
        if [[ -f "${SCRIPT_DIR}/.env" ]]; then
            source "${SCRIPT_DIR}/.env"
        fi
        INSTANCE_NAME="${SAVED_INSTANCE}"
        GITHUB_OWNER="${GITHUB_ORG:-${GITHUB_USERNAME:-}}"

        current_remote=$(git -C "$REPO_DIR" remote get-url origin 2>/dev/null || echo "")
        echo "Instance: ${INSTANCE_NAME}"
        echo "Current remote: ${current_remote:-<not set>}"

        if [[ "${2:-}" == "fix" || "${2:-}" == "set" ]]; then
            if [[ -z "$GITHUB_OWNER" ]]; then
                echo ""
                echo "Error: No GITHUB_ORG or GITHUB_USERNAME in .env"
                echo "Set one in ${SCRIPT_DIR}/.env first"
                exit 1
            fi
            expected_remote="https://github.com/${GITHUB_OWNER}/${INSTANCE_NAME}-bot.git"
            echo "Setting remote to: $expected_remote"
            git -C "$REPO_DIR" remote set-url origin "$expected_remote" 2>/dev/null || \
                git -C "$REPO_DIR" remote add origin "$expected_remote"
            echo "✓ Remote updated"
        else
            if [[ -n "$GITHUB_OWNER" ]]; then
                expected_remote="https://github.com/${GITHUB_OWNER}/${INSTANCE_NAME}-bot.git"
                echo "Expected remote: $expected_remote"
                # Strip credentials from current remote for comparison
                current_clean=$(echo "$current_remote" | sed 's|https://[^@]*@|https://|')
                if [[ "$current_clean" != "$expected_remote" && "$current_clean" != "${expected_remote%.git}" ]]; then
                    echo ""
                    echo "⚠ Remote mismatch! Run './clawfactory.sh -i ${INSTANCE_NAME} remote fix' to correct"
                else
                    echo "✓ Remote is correct"
                fi
            fi
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
        echo "  rebuild         Rebuild images and restart"
        echo "  status          Show container status"
        echo "  logs [service]  Follow logs (gateway/controller)"
        echo "  shell [service] Open shell in container"
        echo "  controller      Show controller URL"
        echo "  audit           Show recent audit log"
        echo "  info            Show instance info and tokens"
        echo "  remote [fix]    Show/fix git remote URL"
        echo "  list            List all instances and running containers"
        echo "  router [start|stop]  Manage subdomain router"
        echo "  hosts [add|check]    Manage /etc/hosts entries"
        echo ""
        echo "Examples:"
        echo "  ./clawfactory.sh router start       # Start router (first time)"
        echo "  ./clawfactory.sh hosts              # Show hosts file entry"
        echo "  ./clawfactory.sh -i sandy start     # Start 'sandy' instance"
        echo "  ./clawfactory.sh -i testbot start   # Start 'testbot' instance"
        echo "  ./clawfactory.sh list               # List all instances"
        echo ""
        echo "Multi-instance access (via subdomains):"
        echo "  1. ./clawfactory.sh router start"
        echo "  2. Add to /etc/hosts: 127.0.0.1 sandy.local testbot.local"
        echo "  3. Access: http://sandy.local:8080/controller"
        echo ""
        echo "Run './clawfactory.sh info' to see tokens"
        ;;
esac
