#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load default instance configuration
if [[ -f "${SCRIPT_DIR}/.clawfactory.conf" ]]; then
    source "${SCRIPT_DIR}/.clawfactory.conf"
fi

# Backward compat: SANDBOX_ENABLED=true -> SANDBOX_MODE=sysbox
if [[ -z "${SANDBOX_MODE:-}" ]]; then
    if [[ "${SANDBOX_ENABLED:-false}" == "true" ]]; then
        SANDBOX_MODE="sysbox"
    else
        SANDBOX_MODE="none"
    fi
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

# Load instance-specific ports (for multi-instance support)
CONTROLLER_ENV="${SCRIPT_DIR}/secrets/${INSTANCE_NAME}/controller.env"
GATEWAY_PORT="${GATEWAY_PORT:-18789}"
CONTROLLER_PORT="${CONTROLLER_PORT:-8080}"
if [[ -f "$CONTROLLER_ENV" ]]; then
    _gw_port=$(grep -E "^GATEWAY_PORT=" "$CONTROLLER_ENV" 2>/dev/null | cut -d= -f2 || true)
    _ctrl_port=$(grep -E "^CONTROLLER_PORT=" "$CONTROLLER_ENV" 2>/dev/null | cut -d= -f2 || true)
    [[ -n "$_gw_port" ]] && GATEWAY_PORT="$_gw_port"
    [[ -n "$_ctrl_port" ]] && CONTROLLER_PORT="$_ctrl_port"
fi
export GATEWAY_PORT CONTROLLER_PORT

# Source Firecracker helpers if in firecracker mode
if [[ "$SANDBOX_MODE" == "firecracker" ]]; then
    if [[ -f "${SCRIPT_DIR}/sandbox/firecracker/vm.sh" ]]; then
        source "${SCRIPT_DIR}/sandbox/firecracker/vm.sh"
    else
        echo "Error: sandbox/firecracker/vm.sh not found" >&2
        echo "Run ./sandbox/firecracker/setup.sh first." >&2
        exit 1
    fi
fi

# --- Docker-mode helpers (non-firecracker) ---

# Check if a port is in use by another clawfactory instance
port_in_use() {
    local port="$1"
    docker ps --filter "name=clawfactory-" --format "{{.Ports}}" 2>/dev/null | grep -q ":${port}->"
}

# Find next available port starting from base
find_available_port() {
    local base="$1"
    local port="$base"
    while port_in_use "$port"; do
        port=$((port + 1))
    done
    echo "$port"
}

# Ensure ports are configured and available
ensure_ports() {
    local env_file="${SCRIPT_DIR}/secrets/${INSTANCE_NAME}/controller.env"

    # Check if ports are already set in env file
    local has_gw_port=$(grep -q "^GATEWAY_PORT=" "$env_file" 2>/dev/null && echo "yes" || echo "no")
    local has_ctrl_port=$(grep -q "^CONTROLLER_PORT=" "$env_file" 2>/dev/null && echo "yes" || echo "no")

    # If both ports are set, check if they're available
    if [[ "$has_gw_port" == "yes" && "$has_ctrl_port" == "yes" ]]; then
        if port_in_use "$GATEWAY_PORT" || port_in_use "$CONTROLLER_PORT"; then
            echo "Warning: Configured ports ($CONTROLLER_PORT/$GATEWAY_PORT) are in use by another instance"
            echo "  Stop the other instance or update ports in: $env_file"
            return 1
        fi
        return 0
    fi

    # Auto-assign ports if not set
    local new_gw_port=$(find_available_port 18789)
    local new_ctrl_port=$(find_available_port 8080)

    # If defaults are available, use them
    if [[ "$new_gw_port" == "18789" && "$new_ctrl_port" == "8080" ]]; then
        GATEWAY_PORT=18789
        CONTROLLER_PORT=8080
        export GATEWAY_PORT CONTROLLER_PORT
        return 0
    fi

    # Need to assign new ports - add to env file
    echo "Assigning ports for [${INSTANCE_NAME}]: Controller=${new_ctrl_port}, Gateway=${new_gw_port}"

    if [[ "$has_gw_port" == "no" ]]; then
        echo "" >> "$env_file"
        echo "# Auto-assigned ports for multi-instance support" >> "$env_file"
        echo "GATEWAY_PORT=${new_gw_port}" >> "$env_file"
    fi
    if [[ "$has_ctrl_port" == "no" ]]; then
        echo "CONTROLLER_PORT=${new_ctrl_port}" >> "$env_file"
    fi

    GATEWAY_PORT="$new_gw_port"
    CONTROLLER_PORT="$new_ctrl_port"
    export GATEWAY_PORT CONTROLLER_PORT
}

# --- Helper to print access URLs ---
print_urls() {
    if [[ -n "$GATEWAY_TOKEN" ]]; then
        echo "  Gateway:    http://localhost:${GATEWAY_PORT}/?token=${GATEWAY_TOKEN}"
        echo "  Controller: http://localhost:${CONTROLLER_PORT}/controller?token=${CONTROLLER_TOKEN}"
    else
        echo "  Gateway:    http://localhost:${GATEWAY_PORT}"
        echo "  Controller: http://localhost:${CONTROLLER_PORT}/controller"
    fi
}

# ============================================================
# Command dispatch
# ============================================================
case "${1:-help}" in
    start)
        if [[ "$SANDBOX_MODE" == "firecracker" ]]; then
            fc_ensure_lima
            fc_setup_network
            fc_start_vm
            fc_sync
            fc_build
            fc_services start
            fc_forward_ports
            echo ""
            echo "ClawFactory [${INSTANCE_NAME}] started (Firecracker VM)"
            print_urls
        else
            ensure_ports || exit 1
            ${COMPOSE_CMD} up -d
            echo "ClawFactory [${INSTANCE_NAME}] started"
            print_urls
        fi
        ;;
    stop)
        if [[ "$SANDBOX_MODE" == "firecracker" ]]; then
            fc_services stop
            fc_stop_vm
            echo "ClawFactory [${INSTANCE_NAME}] stopped (Firecracker VM)"
        else
            ${COMPOSE_CMD} down
            echo "ClawFactory [${INSTANCE_NAME}] stopped"
        fi
        ;;
    restart)
        if [[ "$SANDBOX_MODE" == "firecracker" ]]; then
            fc_services restart
            echo "ClawFactory [${INSTANCE_NAME}] restarted (Firecracker VM)"
        else
            ${COMPOSE_CMD} up -d --force-recreate
            echo "ClawFactory [${INSTANCE_NAME}] restarted"
        fi
        ;;
    rebuild)
        if [[ "$SANDBOX_MODE" == "firecracker" ]]; then
            echo "Rebuilding ClawFactory [${INSTANCE_NAME}] in Firecracker VM..."
            fc_sync
            fc_build
            fc_services restart
            echo "ClawFactory [${INSTANCE_NAME}] rebuilt and restarted"
            print_urls
        else
            echo "Rebuilding ClawFactory [${INSTANCE_NAME}]..."
            ${COMPOSE_CMD} build --no-cache
            ${COMPOSE_CMD} up -d --force-recreate
            echo "ClawFactory [${INSTANCE_NAME}] rebuilt and restarted"
            print_urls
        fi
        ;;
    status)
        if [[ "$SANDBOX_MODE" == "firecracker" ]]; then
            fc_status
        else
            docker ps -a --filter "name=clawfactory-" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
        fi
        ;;
    logs)
        service="${2:-gateway}"
        if [[ "$SANDBOX_MODE" == "firecracker" ]]; then
            case "$service" in
                gateway)    fc_exec "journalctl -u openclaw-gateway -f --no-pager" ;;
                controller) fc_exec "journalctl -u clawfactory-controller -f --no-pager" ;;
                proxy)      fc_exec "journalctl -u nginx -f --no-pager" ;;
                docker)     fc_exec "journalctl -u docker -f --no-pager" ;;
                *)          fc_exec "journalctl -u $service -f --no-pager" ;;
            esac
        else
            docker logs -f "${CONTAINER_PREFIX}-${service}"
        fi
        ;;
    shell)
        if [[ "$SANDBOX_MODE" == "firecracker" ]]; then
            fc_ssh
        else
            container="${2:-gateway}"
            docker exec -it "${CONTAINER_PREFIX}-${container}" /bin/bash
        fi
        ;;
    controller)
        echo "Controller UI for [${INSTANCE_NAME}]:"
        if [[ -n "$CONTROLLER_TOKEN" ]]; then
            echo "http://localhost:${CONTROLLER_PORT}/controller?token=${CONTROLLER_TOKEN}"
        else
            echo "http://localhost:${CONTROLLER_PORT}/controller"
        fi
        ;;
    audit)
        curl -s "http://localhost:${CONTROLLER_PORT}/controller/audit" | jq '.entries[-10:]'
        ;;
    info)
        echo "Instance: ${INSTANCE_NAME}"
        echo "Sandbox:  ${SANDBOX_MODE}"
        if [[ "$SANDBOX_MODE" == "firecracker" ]]; then
            echo "Mode:     Firecracker microVM"
        else
            echo "Mode:     Docker Compose"
            echo "Containers: ${CONTAINER_PREFIX}-{gateway,controller,proxy}"
        fi
        echo "Ports: Gateway=${GATEWAY_PORT}, Controller=${CONTROLLER_PORT}"
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
            echo "Remote updated"
        else
            if [[ -n "$GITHUB_OWNER" ]]; then
                expected_remote="https://github.com/${GITHUB_OWNER}/${INSTANCE_NAME}-bot.git"
                echo "Expected remote: $expected_remote"
                current_clean=$(echo "$current_remote" | sed 's|https://[^@]*@|https://|')
                if [[ "$current_clean" != "$expected_remote" && "$current_clean" != "${expected_remote%.git}" ]]; then
                    echo ""
                    echo "Remote mismatch! Run './clawfactory.sh -i ${INSTANCE_NAME} remote fix' to correct"
                else
                    echo "Remote is correct"
                fi
            fi
        fi
        ;;
    list)
        echo "Configured instances:"
        echo "  Sandbox mode: ${SANDBOX_MODE}"
        echo ""
        if [[ -d "${SCRIPT_DIR}/secrets" ]]; then
            for d in "${SCRIPT_DIR}"/secrets/*/; do
                if [[ -d "$d" ]] && [[ -f "${d}controller.env" ]]; then
                    name=$(basename "$d")
                    gw_port=$(grep -E "^GATEWAY_PORT=" "${d}controller.env" 2>/dev/null | cut -d= -f2 || true)
                    ctrl_port=$(grep -E "^CONTROLLER_PORT=" "${d}controller.env" 2>/dev/null | cut -d= -f2 || true)
                    gw_port="${gw_port:-18789}"
                    ctrl_port="${ctrl_port:-8080}"
                    echo "  ${name}: localhost:${ctrl_port} (gateway: ${gw_port})"
                fi
            done
        else
            echo "  (none - run install.sh first)"
        fi
        echo ""
        if [[ "$SANDBOX_MODE" == "firecracker" ]]; then
            echo "Firecracker VM status:"
            fc_status 2>/dev/null || echo "  Not running"
        else
            echo "Running containers:"
            docker ps --filter "name=clawfactory-" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null || echo "  (none)"
        fi
        ;;
    firecracker)
        subcmd="${2:-help}"
        case "$subcmd" in
            setup)
                bash "${SCRIPT_DIR}/sandbox/firecracker/setup.sh" setup
                ;;
            ssh)
                if [[ "$SANDBOX_MODE" != "firecracker" ]]; then
                    echo "Error: SANDBOX_MODE is '${SANDBOX_MODE}', not 'firecracker'" >&2
                    exit 1
                fi
                fc_ensure_lima
                fc_ssh
                ;;
            teardown)
                bash "${SCRIPT_DIR}/sandbox/firecracker/setup.sh" teardown
                ;;
            status)
                if [[ "$SANDBOX_MODE" != "firecracker" ]]; then
                    echo "Error: SANDBOX_MODE is '${SANDBOX_MODE}', not 'firecracker'" >&2
                    exit 1
                fi
                fc_status
                ;;
            *)
                echo "Firecracker sandbox commands:"
                echo ""
                echo "  ./clawfactory.sh firecracker setup     Provision Lima + Firecracker"
                echo "  ./clawfactory.sh firecracker ssh        SSH into Firecracker VM"
                echo "  ./clawfactory.sh firecracker status     Show VM + service status"
                echo "  ./clawfactory.sh firecracker teardown   Remove Lima VM and all data"
                ;;
        esac
        ;;
    *)
        echo "ClawFactory - Agent Runtime"
        echo ""
        echo "Usage: ./clawfactory.sh [-i <instance>] <command>"
        echo ""
        echo "Options:"
        echo "  -i, --instance <name>   Specify instance (required)"
        echo ""
        echo "Commands:"
        echo "  start           Start services"
        echo "  stop            Stop all services"
        echo "  restart         Restart services"
        echo "  rebuild         Rebuild and restart"
        echo "  status          Show service status"
        echo "  logs [service]  Follow logs (gateway/proxy/controller)"
        echo "  shell [service] Open shell (Firecracker: VM shell)"
        echo "  controller      Show controller URL"
        echo "  audit           Show recent audit log"
        echo "  info            Show instance info and tokens"
        echo "  remote [fix]    Show/fix git remote URL"
        echo "  list            List all instances"
        echo "  firecracker     Firecracker sandbox management"
        echo ""
        echo "Sandbox mode: ${SANDBOX_MODE}"
        echo ""
        echo "Examples:"
        echo "  ./clawfactory.sh -i sandy start"
        echo "  ./clawfactory.sh -i sandy logs gateway"
        echo "  ./clawfactory.sh -i sandy stop"
        echo "  ./clawfactory.sh firecracker ssh"
        echo "  ./clawfactory.sh list"
        ;;
esac
