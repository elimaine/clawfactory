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
    rebuild)
        echo "Rebuilding ClawFactory [${INSTANCE_NAME}]..."
        ${COMPOSE_CMD} build --no-cache
        ${COMPOSE_CMD} up -d --force-recreate
        echo "✓ ClawFactory [${INSTANCE_NAME}] rebuilt and restarted"
        if [[ -n "$GATEWAY_TOKEN" ]]; then
            echo "  Gateway:    http://localhost:18789/?token=${GATEWAY_TOKEN}"
            echo "  Controller: http://localhost:8080/controller?token=${CONTROLLER_TOKEN}"
        else
            echo "  Gateway:    http://localhost:18789"
            echo "  Controller: http://localhost:8080/controller"
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
        echo "  logs [service]  Follow logs (gateway/proxy/controller)"
        echo "  shell [service] Open shell in container"
        echo "  controller      Show controller URL"
        echo "  audit           Show recent audit log"
        echo "  info            Show instance info and tokens"
        echo "  remote [fix]    Show/fix git remote URL"
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
