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
# Priority: secrets/tokens.env overrides > instance env files
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
# Fallback: read tokens directly from instance env files
if [[ -z "$CONTROLLER_TOKEN" ]]; then
    CONTROLLER_TOKEN=$(grep -E "^CONTROLLER_API_TOKEN=" "${SCRIPT_DIR}/secrets/${INSTANCE_NAME}/controller.env" 2>/dev/null | cut -d= -f2- || true)
fi
if [[ -z "$GATEWAY_TOKEN" ]]; then
    GATEWAY_TOKEN=$(grep -E "^OPENCLAW_GATEWAY_TOKEN=" "${SCRIPT_DIR}/secrets/${INSTANCE_NAME}/gateway.env" 2>/dev/null | cut -d= -f2- || true)
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

# --- GitHub PAT for bot repo remotes ---
GITHUB_PAT_FILE="${SCRIPT_DIR}/secrets/github.pat"

# Ensure bot repo remote has credentials embedded
ensure_bot_remote() {
    local instance="${1:-$INSTANCE_NAME}"
    local repo_dir="${SCRIPT_DIR}/bot_repos/${instance}/approved"

    [[ -d "$repo_dir/.git" ]] || return 0

    local current_remote
    current_remote=$(git -C "$repo_dir" remote get-url origin 2>/dev/null || true)
    [[ -n "$current_remote" ]] || return 0

    # Already has credentials embedded
    if [[ "$current_remote" == *"x-access-token:"* ]]; then
        # Check if the PAT file exists and differs from what's in the URL
        if [[ -f "$GITHUB_PAT_FILE" ]]; then
            local stored_pat
            stored_pat=$(cat "$GITHUB_PAT_FILE")
            local url_pat
            url_pat=$(echo "$current_remote" | sed -n 's|.*x-access-token:\([^@]*\)@.*|\1|p')
            if [[ "$url_pat" != "$stored_pat" ]]; then
                # Update with newer PAT
                local clean_url
                clean_url=$(echo "$current_remote" | sed 's|https://[^@]*@|https://|')
                local new_url="https://x-access-token:${stored_pat}@${clean_url#https://}"
                git -C "$repo_dir" remote set-url origin "$new_url"
                echo "Updated credentials for ${instance} bot repo"
            fi
        fi
        return 0
    fi

    # No credentials — inject PAT if available
    if [[ ! -f "$GITHUB_PAT_FILE" ]]; then
        echo "Warning: ${instance} bot repo has no credentials on remote URL"
        echo "  Create secrets/github.pat with a PAT that has 'repo' + 'workflow' scopes"
        echo "  echo 'ghp_your_token_here' > ${GITHUB_PAT_FILE} && chmod 600 ${GITHUB_PAT_FILE}"
        return 0
    fi

    local pat
    pat=$(cat "$GITHUB_PAT_FILE")
    local new_url="https://x-access-token:${pat}@${current_remote#https://}"
    git -C "$repo_dir" remote set-url origin "$new_url"
    echo "Added credentials to ${instance} bot repo remote"
}

# Source Lima helpers if in lima mode
if [[ "$SANDBOX_MODE" == "lima" ]]; then
    if [[ -f "${SCRIPT_DIR}/sandbox/lima/vm.sh" ]]; then
        source "${SCRIPT_DIR}/sandbox/lima/vm.sh"
    else
        echo "Error: sandbox/lima/vm.sh not found" >&2
        echo "Run ./sandbox/lima/setup.sh first." >&2
        exit 1
    fi
fi

# --- Docker-mode helpers (non-lima) ---

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
        ensure_bot_remote
        if [[ "$SANDBOX_MODE" == "lima" ]]; then
            lima_ensure
            lima_sync
            lima_build

            # Snapshot-based state management
            if _lima_has_snapshot; then
                snapshot_name=$(_lima_latest_snapshot_name)

                if _lima_has_state; then
                    echo ""
                    echo "  Latest snapshot: ${snapshot_name}"
                    read -p "  Restore state from snapshot? [Y/n] " answer
                    if [[ "${answer,,}" != "n" ]]; then
                        echo "  Backing up current state..."
                        backup_name=$(_lima_backup_state)
                        if [[ -n "$backup_name" && "$backup_name" != "ERROR" ]]; then
                            echo "  Current state saved → ${backup_name}"
                        fi
                        _lima_restore_snapshot
                        echo "  Restored from: ${snapshot_name}"
                        if [[ -n "${backup_name:-}" && "$backup_name" != "ERROR" ]]; then
                            echo "  Rollback: ./clawfactory.sh snapshot restore ${backup_name}"
                        fi
                    else
                        echo "  Keeping current state"
                    fi
                else
                    echo "[snapshot] Restoring state from: ${snapshot_name}"
                    _lima_restore_snapshot
                fi
                echo ""
            fi

            lima_services start
            echo ""
            echo "ClawFactory [${INSTANCE_NAME}] started (Lima VM)"
            print_urls
        else
            ensure_ports || exit 1
            ${COMPOSE_CMD} up -d
            echo "ClawFactory [${INSTANCE_NAME}] started"
            print_urls
        fi
        ;;
    stop)
        if [[ "$SANDBOX_MODE" == "lima" ]]; then
            lima_stop
            echo "ClawFactory [${INSTANCE_NAME}] stopped (Lima VM)"
        else
            ${COMPOSE_CMD} down
            echo "ClawFactory [${INSTANCE_NAME}] stopped"
        fi
        ;;
    restart)
        if [[ "$SANDBOX_MODE" == "lima" ]]; then
            lima_services restart
            echo "ClawFactory [${INSTANCE_NAME}] restarted (Lima VM)"
        else
            ${COMPOSE_CMD} up -d --force-recreate
            echo "ClawFactory [${INSTANCE_NAME}] restarted"
        fi
        ;;
    rebuild)
        ensure_bot_remote
        if [[ "$SANDBOX_MODE" == "lima" ]]; then
            echo "Rebuilding ClawFactory [${INSTANCE_NAME}] in Lima VM..."
            lima_sync
            lima_build
            lima_services restart
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
        if [[ "$SANDBOX_MODE" == "lima" ]]; then
            lima_status
        else
            docker ps -a --filter "name=clawfactory-" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
        fi
        ;;
    logs)
        service="${2:-gateway}"
        if [[ "$SANDBOX_MODE" == "lima" ]]; then
            case "$service" in
                gateway)    _lima_root "journalctl -u openclaw-gateway@${INSTANCE_NAME} -f --no-pager" ;;
                controller) _lima_root "journalctl -u clawfactory-controller -f --no-pager" ;;
                proxy)      _lima_root "journalctl -u nginx -f --no-pager" ;;
                docker)     _lima_root "journalctl -u docker -f --no-pager" ;;
                *)          _lima_root "journalctl -u $service -f --no-pager" ;;
            esac
        else
            docker logs -f "${CONTAINER_PREFIX}-${service}"
        fi
        ;;
    shell)
        if [[ "$SANDBOX_MODE" == "lima" ]]; then
            lima_shell
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
    snapshot)
        subcmd="${2:-list}"
        case "$subcmd" in
            list)
                curl -s "http://localhost:${CONTROLLER_PORT}/controller/snapshot" | \
                    jq -r '.snapshots[] | "\(.label // "snapshot")\t\(.created)\t\(.name)\t\(.size)"' | \
                    while IFS=$'\t' read -r label created name size; do
                        if [[ "$label" == "snapshot" ]]; then
                            printf "  %-20s  %s  (%s bytes)\n" "$created" "$name" "$size"
                        else
                            printf "  %-20s  %s  (%s bytes)  [%s]\n" "$label" "$created" "$size" "$name"
                        fi
                    done
                ;;
            create)
                snap_name="${3:-}"
                if [[ -n "$snap_name" ]]; then
                    curl -s -X POST "http://localhost:${CONTROLLER_PORT}/controller/snapshot" \
                        -H "Content-Type: application/json" \
                        -d "{\"name\": \"${snap_name}\"}" | jq '.'
                else
                    curl -s -X POST "http://localhost:${CONTROLLER_PORT}/controller/snapshot" \
                        -H "Content-Type: application/json" \
                        -d '{"name": ""}' | jq '.'
                fi
                # Auto-pull to host after creation
                if [[ "$SANDBOX_MODE" == "lima" ]]; then
                    _lima_snapshot_pull "$INSTANCE_NAME"
                fi
                ;;
            pull)
                if [[ "$SANDBOX_MODE" != "lima" ]]; then
                    echo "Snapshot pull is only available in Lima mode"
                    exit 1
                fi
                _lima_snapshot_pull "$INSTANCE_NAME"
                ;;
            autopull)
                if [[ "$SANDBOX_MODE" != "lima" ]]; then
                    echo "Snapshot autopull is only available in Lima mode"
                    exit 1
                fi
                lima_snapshot_autopull "${3:-status}"
                ;;
            rename)
                old_snap="${3:-}"
                new_name="${4:-}"
                if [[ -z "$old_snap" || -z "$new_name" ]]; then
                    echo "Usage: ./clawfactory.sh snapshot rename <filename> <new-name>"
                    echo ""
                    echo "  <filename>   Current snapshot filename (e.g. snapshot--2026-02-05T01-02-09Z.tar.age)"
                    echo "  <new-name>   New display name (e.g. \"before big change\")"
                    exit 1
                fi
                curl -s -X POST "http://localhost:${CONTROLLER_PORT}/controller/snapshot/rename" \
                    -H "Content-Type: application/json" \
                    -d "{\"snapshot\": \"${old_snap}\", \"new_name\": \"${new_name}\"}" | jq '.'
                ;;
            delete)
                target="${3:-}"
                if [[ -z "$target" ]]; then
                    echo "Usage: ./clawfactory.sh snapshot delete <name|all>"
                    echo ""
                    echo "  <name>  Delete a specific snapshot (e.g. snapshot--2026-02-05T01-02-09Z.tar.age)"
                    echo "  all     Delete all snapshots"
                    exit 1
                fi
                if [[ "$target" == "all" ]]; then
                    read -p "Delete ALL snapshots for [${INSTANCE_NAME}]? [y/N]: " confirm
                    [[ "$confirm" =~ ^[Yy]$ ]] || { echo "Cancelled"; exit 0; }
                fi
                curl -s -X POST "http://localhost:${CONTROLLER_PORT}/controller/snapshot/delete" \
                    -H "Content-Type: application/json" \
                    -d "{\"snapshot\": \"${target}\"}" | jq '.'
                ;;
            restore)
                target="${3:-latest}"
                if [[ "$SANDBOX_MODE" == "lima" ]]; then
                    if ! _lima_has_snapshot; then
                        echo "No snapshots found for [${INSTANCE_NAME}]"
                        exit 1
                    fi
                    echo "This will stop the gateway and restore state from: ${target}"
                    read -p "Continue? [y/N] " confirm
                    [[ "$confirm" =~ ^[Yy]$ ]] || { echo "Cancelled"; exit 0; }
                    curl -s -X POST "http://localhost:${CONTROLLER_PORT}/controller/snapshot/restore" \
                        -H "Content-Type: application/json" \
                        -d "{\"snapshot\": \"${target}\"}" | jq '.'
                else
                    curl -s -X POST "http://localhost:${CONTROLLER_PORT}/controller/snapshot/restore" \
                        -H "Content-Type: application/json" \
                        -d "{\"snapshot\": \"${target}\"}" | jq '.'
                fi
                ;;
            *)
                echo "Snapshot commands:"
                echo ""
                echo "  ./clawfactory.sh snapshot list                              List snapshots"
                echo "  ./clawfactory.sh snapshot create [name]                     Create a snapshot (auto-syncs to host)"
                echo "  ./clawfactory.sh snapshot pull                              Pull snapshots from VM to host"
                echo "  ./clawfactory.sh snapshot autopull [enable|disable|status]  Auto-pull every 5 min (launchd)"
                echo "  ./clawfactory.sh snapshot rename <filename> <name>          Rename a snapshot"
                echo "  ./clawfactory.sh snapshot delete <name>                     Delete a snapshot"
                echo "  ./clawfactory.sh snapshot delete all                        Delete all snapshots"
                echo "  ./clawfactory.sh snapshot restore [name|latest]             Restore from a snapshot"
                ;;
        esac
        ;;
    info)
        echo "Instance: ${INSTANCE_NAME}"
        echo "Sandbox:  ${SANDBOX_MODE}"
        if [[ "$SANDBOX_MODE" == "lima" ]]; then
            echo "Mode:     Lima VM"
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
    bots|list)
        echo "=== Saved Bots ==="
        echo ""
        if [[ -d "${SCRIPT_DIR}/bot_repos" ]]; then
            bot_count=0
            for d in "${SCRIPT_DIR}"/bot_repos/*/; do
                if [[ -d "$d" ]]; then
                    name=$(basename "$d")
                    ensure_bot_remote "$name" 2>/dev/null
                    has_secrets="no"
                    has_snapshots="no"
                    gw_port="-"
                    ctrl_port="-"
                    if [[ -f "${SCRIPT_DIR}/secrets/${name}/controller.env" ]]; then
                        has_secrets="yes"
                        gw_port=$(grep -E "^GATEWAY_PORT=" "${SCRIPT_DIR}/secrets/${name}/controller.env" 2>/dev/null | cut -d= -f2 || true)
                        ctrl_port=$(grep -E "^CONTROLLER_PORT=" "${SCRIPT_DIR}/secrets/${name}/controller.env" 2>/dev/null | cut -d= -f2 || true)
                        gw_port="${gw_port:-18789}"
                        ctrl_port="${ctrl_port:-8080}"
                    fi
                    if [[ -d "${SCRIPT_DIR}/snapshots/${name}" ]] && ls "${SCRIPT_DIR}/snapshots/${name}/"*.tar.age &>/dev/null; then
                        has_snapshots="yes"
                    fi
                    printf "  %-15s secrets:%-3s  snapshots:%-3s" "$name" "$has_secrets" "$has_snapshots"
                    if [[ "$has_secrets" == "yes" ]]; then
                        printf "  ports: %s/%s" "$ctrl_port" "$gw_port"
                    fi
                    echo ""
                    ((bot_count++))
                fi
            done
            if [[ $bot_count -eq 0 ]]; then
                echo "  (none - run install.sh first)"
            fi
        else
            echo "  (none - run install.sh first)"
        fi
        echo ""
        echo "Sandbox mode: ${SANDBOX_MODE}"
        echo ""
        if [[ "$SANDBOX_MODE" == "lima" ]]; then
            echo "Lima VM status:"
            lima_status 2>/dev/null || echo "  Not running"
        else
            echo "Running containers:"
            docker ps --filter "name=clawfactory-" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null || echo "  (none)"
        fi
        ;;
    update)
        repo="${SCRIPT_DIR}/bot_repos/${INSTANCE_NAME}/approved"
        if [[ ! -d "$repo/.git" ]]; then
            echo "Error: No git repo found at $repo" >&2
            exit 1
        fi

        # Verify upstream remote exists
        if ! git -C "$repo" remote get-url upstream >/dev/null 2>&1; then
            echo "Error: No 'upstream' remote configured for [${INSTANCE_NAME}]" >&2
            echo "  Add one with: git -C $repo remote add upstream https://github.com/openclaw/openclaw.git"
            exit 1
        fi

        echo "Fetching upstream for [${INSTANCE_NAME}]..."
        git -C "$repo" fetch upstream

        echo "Merging upstream/main..."
        if ! git -C "$repo" merge upstream/main --no-edit; then
            git -C "$repo" merge --abort
            echo "Merge conflict — resolve manually in: $repo" >&2
            exit 1
        fi
        echo "Merged upstream into [${INSTANCE_NAME}]"

        # Redeploy
        if [[ "$SANDBOX_MODE" == "lima" ]]; then
            echo "Redeploying to Lima VM..."
            lima_sync
            lima_build
            lima_services restart
            echo "Update complete — [${INSTANCE_NAME}] redeployed"
            print_urls
        else
            echo "Redeploying via Docker..."
            ${COMPOSE_CMD} build
            ${COMPOSE_CMD} up -d
            echo "Update complete — [${INSTANCE_NAME}] redeployed"
            print_urls
        fi
        ;;
    init)
        # Load .env for GITHUB_ORG
        if [[ -f "${SCRIPT_DIR}/.env" ]]; then
            source "${SCRIPT_DIR}/.env"
        fi
        GITHUB_OWNER="${GITHUB_ORG:-${GITHUB_USERNAME:-}}"

        echo "=== ClawFactory Bot Setup ==="
        echo ""
        echo "  1) Create a new bot"
        echo "  2) Clone an existing bot"
        echo ""
        read -p "Choice [1/2]: " init_choice

        case "${init_choice}" in
            1)
                # --- NEW BOT ---
                read -p "Instance name (lowercase, no spaces): " new_name
                new_name=$(echo "$new_name" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9-')
                if [[ -z "$new_name" ]]; then
                    echo "Error: Invalid name" >&2
                    exit 1
                fi
                if [[ -d "${SCRIPT_DIR}/bot_repos/${new_name}" ]]; then
                    echo "Error: Instance '${new_name}' already exists" >&2
                    exit 1
                fi

                echo ""
                echo "Creating instance: ${new_name}"

                # Create directories
                mkdir -p "${SCRIPT_DIR}/bot_repos/${new_name}/"{approved,state}
                mkdir -p "${SCRIPT_DIR}/secrets/${new_name}"
                mkdir -p "${SCRIPT_DIR}/snapshots/${new_name}"

                # Clone OpenClaw
                echo "Cloning OpenClaw..."
                git clone https://github.com/openclaw/openclaw.git "${SCRIPT_DIR}/bot_repos/${new_name}/approved"

                # Set origin for bot repo
                if [[ -n "$GITHUB_OWNER" ]]; then
                    git -C "${SCRIPT_DIR}/bot_repos/${new_name}/approved" remote set-url origin \
                        "https://github.com/${GITHUB_OWNER}/${new_name}-bot.git"
                    echo "Origin set to: https://github.com/${GITHUB_OWNER}/${new_name}-bot.git"
                fi

                # Add upstream remote
                git -C "${SCRIPT_DIR}/bot_repos/${new_name}/approved" remote add upstream \
                    https://github.com/openclaw/openclaw.git 2>/dev/null || true
                echo "Upstream remote added"

                # Generate secrets
                echo "Generating secrets..."
                ctrl_token=$(openssl rand -hex 32)
                gw_token=$(openssl rand -hex 32)
                internal_token=$(openssl rand -hex 32)
                cat > "${SCRIPT_DIR}/secrets/${new_name}/controller.env" <<ENVEOF
CONTROLLER_API_TOKEN=${ctrl_token}
ENVEOF
                cat > "${SCRIPT_DIR}/secrets/${new_name}/gateway.env" <<ENVEOF
OPENCLAW_GATEWAY_TOKEN=${gw_token}
GATEWAY_INTERNAL_TOKEN=${internal_token}
ENVEOF
                chmod 600 "${SCRIPT_DIR}/secrets/${new_name}/"*.env

                # Generate snapshot encryption key
                if command -v age-keygen >/dev/null 2>&1; then
                    age-keygen -o "${SCRIPT_DIR}/secrets/${new_name}/snapshot.key" 2>/dev/null
                    chmod 600 "${SCRIPT_DIR}/secrets/${new_name}/snapshot.key"
                    echo "Snapshot encryption key generated"
                else
                    echo "Warning: age-keygen not found — snapshot encryption key not generated"
                fi

                # Inject PAT if available
                ensure_bot_remote "$new_name"

                echo ""
                echo "Instance '${new_name}' created successfully!"
                echo ""

                # Deploy if sandbox is available
                read -p "Deploy now? [Y/n]: " deploy_confirm
                if [[ ! "$deploy_confirm" =~ ^[Nn]$ ]]; then
                    export INSTANCE_NAME="$new_name"
                    if [[ "$SANDBOX_MODE" == "lima" ]]; then
                        lima_ensure
                        lima_sync
                        lima_build
                        lima_services start
                        echo ""
                        echo "Running 'openclaw onboard' — follow the prompts to configure your bot..."
                        lima_openclaw onboard
                    else
                        ${COMPOSE_CMD} build
                        ${COMPOSE_CMD} up -d
                    fi
                    echo ""
                    echo "ClawFactory [${new_name}] deployed!"
                    GATEWAY_PORT="${GATEWAY_PORT:-18789}"
                    CONTROLLER_PORT="${CONTROLLER_PORT:-8080}"
                    print_urls
                fi
                ;;
            2)
                # --- CLONE EXISTING ---
                echo ""
                echo "Available bots:"
                bot_list=()
                for d in "${SCRIPT_DIR}"/bot_repos/*/; do
                    if [[ -d "$d" ]]; then
                        bot_name=$(basename "$d")
                        bot_list+=("$bot_name")
                        echo "  ${#bot_list[@]}) ${bot_name}"
                    fi
                done

                if [[ ${#bot_list[@]} -eq 0 ]]; then
                    echo "  (none — create a new bot first)"
                    exit 1
                fi

                echo ""
                read -p "Source bot number: " src_idx
                if [[ -z "$src_idx" ]] || [[ "$src_idx" -lt 1 ]] || [[ "$src_idx" -gt ${#bot_list[@]} ]]; then
                    echo "Error: Invalid selection" >&2
                    exit 1
                fi
                src_name="${bot_list[$((src_idx-1))]}"

                read -p "New instance name (lowercase, no spaces): " clone_name
                clone_name=$(echo "$clone_name" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9-')
                if [[ -z "$clone_name" ]]; then
                    echo "Error: Invalid name" >&2
                    exit 1
                fi
                if [[ -d "${SCRIPT_DIR}/bot_repos/${clone_name}" ]]; then
                    echo "Error: Instance '${clone_name}' already exists" >&2
                    exit 1
                fi

                echo ""
                echo "Cloning '${src_name}' → '${clone_name}'..."

                # Copy approved code
                mkdir -p "${SCRIPT_DIR}/bot_repos/${clone_name}"
                cp -a "${SCRIPT_DIR}/bot_repos/${src_name}/approved" "${SCRIPT_DIR}/bot_repos/${clone_name}/approved"
                mkdir -p "${SCRIPT_DIR}/bot_repos/${clone_name}/state"

                # Reset origin for new instance
                if [[ -n "$GITHUB_OWNER" ]]; then
                    git -C "${SCRIPT_DIR}/bot_repos/${clone_name}/approved" remote set-url origin \
                        "https://github.com/${GITHUB_OWNER}/${clone_name}-bot.git"
                fi

                # Generate fresh secrets
                mkdir -p "${SCRIPT_DIR}/secrets/${clone_name}"
                mkdir -p "${SCRIPT_DIR}/snapshots/${clone_name}"
                ctrl_token=$(openssl rand -hex 32)
                gw_token=$(openssl rand -hex 32)
                internal_token=$(openssl rand -hex 32)
                cat > "${SCRIPT_DIR}/secrets/${clone_name}/controller.env" <<ENVEOF
CONTROLLER_API_TOKEN=${ctrl_token}
ENVEOF
                cat > "${SCRIPT_DIR}/secrets/${clone_name}/gateway.env" <<ENVEOF
OPENCLAW_GATEWAY_TOKEN=${gw_token}
GATEWAY_INTERNAL_TOKEN=${internal_token}
ENVEOF
                chmod 600 "${SCRIPT_DIR}/secrets/${clone_name}/"*.env

                # Generate snapshot encryption key
                if command -v age-keygen >/dev/null 2>&1; then
                    age-keygen -o "${SCRIPT_DIR}/secrets/${clone_name}/snapshot.key" 2>/dev/null
                    chmod 600 "${SCRIPT_DIR}/secrets/${clone_name}/snapshot.key"
                fi

                # Auto-assign ports
                next_gw=$(find_available_port 18789)
                next_ctrl=$(find_available_port 8080)
                if [[ "$next_gw" != "18789" || "$next_ctrl" != "8080" ]]; then
                    echo "GATEWAY_PORT=${next_gw}" >> "${SCRIPT_DIR}/secrets/${clone_name}/controller.env"
                    echo "CONTROLLER_PORT=${next_ctrl}" >> "${SCRIPT_DIR}/secrets/${clone_name}/controller.env"
                    echo "Assigned ports: Controller=${next_ctrl}, Gateway=${next_gw}"
                fi

                # Inject PAT
                ensure_bot_remote "$clone_name"

                # Offer snapshot restore
                echo ""
                src_snapshots_dir="${SCRIPT_DIR}/snapshots/${src_name}"
                if [[ -d "$src_snapshots_dir" ]] && ls "${src_snapshots_dir}/"*.tar.age &>/dev/null 2>&1; then
                    echo "Snapshots available from '${src_name}':"
                    snap_list=()
                    for sf in $(ls -t "${src_snapshots_dir}/"*.tar.age 2>/dev/null); do
                        snap_file=$(basename "$sf")
                        [[ "$snap_file" == "latest.tar.age" ]] && continue
                        snap_list+=("$snap_file")
                        echo "  ${#snap_list[@]}) ${snap_file}"
                    done

                    if [[ ${#snap_list[@]} -gt 0 ]]; then
                        echo "  0) Skip — start fresh"
                        read -p "Restore from snapshot [0]: " snap_idx
                        snap_idx="${snap_idx:-0}"
                        if [[ "$snap_idx" -gt 0 ]] && [[ "$snap_idx" -le ${#snap_list[@]} ]]; then
                            chosen_snap="${snap_list[$((snap_idx-1))]}"
                            # Copy snapshot to new instance's snapshots dir
                            cp "${src_snapshots_dir}/${chosen_snap}" "${SCRIPT_DIR}/snapshots/${clone_name}/"
                            echo "Snapshot '${chosen_snap}' will be restored after deployment"
                        fi
                    fi
                fi

                echo ""
                echo "Instance '${clone_name}' created!"
                echo ""

                # Deploy
                read -p "Deploy now? [Y/n]: " deploy_confirm
                if [[ ! "$deploy_confirm" =~ ^[Nn]$ ]]; then
                    export INSTANCE_NAME="$clone_name"
                    GATEWAY_PORT="${next_gw:-18789}"
                    CONTROLLER_PORT="${next_ctrl:-8080}"
                    export GATEWAY_PORT CONTROLLER_PORT
                    if [[ "$SANDBOX_MODE" == "lima" ]]; then
                        lima_ensure
                        lima_sync
                        lima_build
                        lima_services start
                        # Restore snapshot if one was selected
                        if [[ -n "${chosen_snap:-}" ]]; then
                            echo "Restoring snapshot..."
                            sleep 2  # Wait for controller to start
                            curl -s -X POST "http://localhost:${CONTROLLER_PORT}/controller/snapshot/restore" \
                                -H "Content-Type: application/json" \
                                -d "{\"snapshot\": \"${chosen_snap}\"}" | jq '.'
                        fi
                    else
                        ${COMPOSE_CMD} build
                        ${COMPOSE_CMD} up -d
                    fi
                    echo ""
                    echo "ClawFactory [${clone_name}] deployed!"
                    print_urls
                fi
                ;;
            *)
                echo "Invalid choice"
                exit 1
                ;;
        esac
        ;;
    openclaw)
        shift
        if [[ "$SANDBOX_MODE" == "lima" ]]; then
            lima_openclaw "$@"
        else
            # Docker mode: exec into gateway container
            docker exec -it "${CONTAINER_PREFIX}-gateway" ./openclaw.mjs "$@"
        fi
        ;;
    lima)
        subcmd="${2:-help}"
        case "$subcmd" in
            setup)
                bash "${SCRIPT_DIR}/sandbox/lima/setup.sh" setup
                ;;
            shell)
                if [[ "$SANDBOX_MODE" != "lima" ]]; then
                    echo "Error: SANDBOX_MODE is '${SANDBOX_MODE}', not 'lima'" >&2
                    exit 1
                fi
                lima_shell
                ;;
            teardown)
                bash "${SCRIPT_DIR}/sandbox/lima/setup.sh" teardown
                ;;
            status)
                if [[ "$SANDBOX_MODE" != "lima" ]]; then
                    echo "Error: SANDBOX_MODE is '${SANDBOX_MODE}', not 'lima'" >&2
                    exit 1
                fi
                lima_status
                ;;
            *)
                echo "Lima sandbox commands:"
                echo ""
                echo "  ./clawfactory.sh lima setup     Provision Lima VM"
                echo "  ./clawfactory.sh lima shell     Shell into Lima VM"
                echo "  ./clawfactory.sh lima status    Show VM + service status"
                echo "  ./clawfactory.sh lima teardown  Remove Lima VM and all data"
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
        echo "  update          Pull upstream changes and redeploy"
        echo "  status          Show service status"
        echo "  logs [service]  Follow logs (gateway/proxy/controller)"
        echo "  shell [service] Open shell (Lima: VM shell)"
        echo "  controller      Show controller URL"
        echo "  audit           Show recent audit log"
        echo "  snapshot        Manage snapshots (list/create/rename/delete)"
        echo "  openclaw <args> Run OpenClaw CLI (e.g. openclaw onboard)"
        echo "  init            Interactive new bot / clone setup"
        echo "  info            Show instance info and tokens"
        echo "  remote [fix]    Show/fix git remote URL"
        echo "  bots            List all saved bots"
        echo "  lima            Lima sandbox management"
        echo ""
        echo "Sandbox mode: ${SANDBOX_MODE}"
        echo ""
        echo "Examples:"
        echo "  ./clawfactory.sh -i bot1 start"
        echo "  ./clawfactory.sh -i bot1 logs gateway"
        echo "  ./clawfactory.sh -i bot2 stop"
        echo "  ./clawfactory.sh -i bot1 openclaw onboard"
        echo "  ./clawfactory.sh lima shell"
        echo "  ./clawfactory.sh bots"
        ;;
esac
