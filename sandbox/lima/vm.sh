#!/usr/bin/env bash
#
# sandbox/lima/vm.sh
#
# Runtime helpers for the Lima sandbox.
# Sourced by clawfactory.sh when SANDBOX_MODE=lima.
#
# Services run directly in the Lima VM as systemd units.
# No Firecracker, no TAP networking — fast VZ networking.
#
# Provides: lima_ensure, lima_sync, lima_build, lima_services,
#           lima_openclaw, lima_status, lima_shell

# --- Configuration ---
LIMA_VM_NAME="clawfactory"
LIMA_SRV="/srv/clawfactory"

# --- Internal helpers ---
_lima_exec() {
    limactl shell "$LIMA_VM_NAME" -- "$@"
}

_lima_root() {
    limactl shell "$LIMA_VM_NAME" -- sudo bash -c "$1"
}

# SSH config for host-to-VM rsync (not running rsync inside the VM)
LIMA_SSH_CONFIG="${HOME}/.lima/${LIMA_VM_NAME}/ssh.config"
LIMA_SSH_HOST="lima-${LIMA_VM_NAME}"

# ============================================================
# lima_ensure — Start Lima VM if not running
# ============================================================
lima_ensure() {
    if ! command -v limactl >/dev/null 2>&1; then
        echo "Error: limactl not found. Run: ./sandbox/lima/setup.sh" >&2
        return 1
    fi

    if ! limactl list -q 2>/dev/null | grep -q "^${LIMA_VM_NAME}$"; then
        echo "Error: Lima VM '${LIMA_VM_NAME}' not found. Run: ./sandbox/lima/setup.sh" >&2
        return 1
    fi

    local status
    status=$(limactl list --json 2>/dev/null | python3 -c "
import json,sys
for vm in json.loads(sys.stdin.read().rstrip().replace('}\n{', '},{')):
    if vm.get('name')=='$LIMA_VM_NAME': print(vm.get('status',''))
" 2>/dev/null || echo "unknown")

    if [ "$status" = "Running" ]; then
        return 0
    fi

    echo "Starting Lima VM '${LIMA_VM_NAME}'..."
    limactl start "$LIMA_VM_NAME"
    echo "Lima VM started"
}

# ============================================================
# lima_sync — Rsync ClawFactory files into Lima VM
# ============================================================
lima_sync() {
    local instance="${INSTANCE_NAME:-default}"

    # Pull snapshots from VM before syncing (so we don't lose any)
    _lima_snapshot_pull "$instance"

    echo "Syncing files to Lima VM (instance: ${instance})..."

    local cf_root="${SCRIPT_DIR}"
    local staging="/tmp/cf-sync"
    local rsh="ssh -F ${LIMA_SSH_CONFIG}"

    _lima_exec mkdir -p "${staging}/controller" "${staging}/proxy"

    # Sync controller (host → VM over SSH)
    rsync -a --delete --exclude '__pycache__' \
        -e "$rsh" \
        "${cf_root}/controller/" "${LIMA_SSH_HOST}:${staging}/controller/"

    # Sync proxy config
    rsync -a --delete \
        -e "$rsh" \
        "${cf_root}/proxy/" "${LIMA_SSH_HOST}:${staging}/proxy/"

    # Sync bot_repos for this instance
    if [[ -d "${cf_root}/bot_repos/${instance}" ]]; then
        _lima_exec mkdir -p "${staging}/bot_repos/${instance}/approved"

        rsync -a --delete \
            --exclude '.git' \
            --exclude 'node_modules' \
            --exclude '.DS_Store' \
            -e "$rsh" \
            "${cf_root}/bot_repos/${instance}/approved/" \
            "${LIMA_SSH_HOST}:${staging}/bot_repos/${instance}/approved/"

        # Sync state directory (OpenClaw config, credentials, identity)
        # Uses --update so VM-side changes aren't overwritten by stale host files
        if [[ -d "${cf_root}/bot_repos/${instance}/state" ]]; then
            _lima_exec mkdir -p "${staging}/bot_repos/${instance}/state"
            rsync -a --update \
                --exclude '.DS_Store' \
                --exclude '.obsidian' \
                --exclude 'installed' \
                --exclude 'sandboxes' \
                --exclude 'subagents' \
                --exclude 'workspace' \
                --exclude 'media' \
                --exclude 'update-check.json' \
                -e "$rsh" \
                "${cf_root}/bot_repos/${instance}/state/" \
                "${LIMA_SSH_HOST}:${staging}/bot_repos/${instance}/state/"
        fi
    fi

    # Sync secrets for this instance (restricted permissions)
    if [[ -d "${cf_root}/secrets/${instance}" ]]; then
        _lima_exec mkdir -p "${staging}/secrets/${instance}"
        rsync -a \
            -e "$rsh" \
            "${cf_root}/secrets/${instance}/" \
            "${LIMA_SSH_HOST}:${staging}/secrets/${instance}/"
    fi

    # Deploy from staging to /srv/clawfactory (runs inside VM)
    _lima_root "
        rsync -a --delete \
            --exclude 'node_modules' \
            --exclude '.pnpm-lock-hash' \
            --exclude 'snapshots' \
            ${staging}/ ${LIMA_SRV}/

        # Ensure directory structure
        mkdir -p ${LIMA_SRV}/bot_repos/${instance}/state \
                 ${LIMA_SRV}/audit \
                 ${LIMA_SRV}/snapshots/${instance}

        # Lock down secrets — only root can read controller env
        chmod 700 ${LIMA_SRV}/secrets/${instance}/ 2>/dev/null || true
        chmod 600 ${LIMA_SRV}/secrets/${instance}/*.env 2>/dev/null || true
    "

    # Keep staging for build step

    # Migrate Docker-era paths in state config
    _lima_fix_docker_paths "$instance"

    echo "Files synced"
}

# ============================================================
# _lima_fix_docker_paths — Fix paths left over from Docker mode
# ============================================================
_lima_fix_docker_paths() {
    local instance="$1"
    local state_dir="${LIMA_SRV}/bot_repos/${instance}/state"

    _lima_root "
        changed=false

        # Fix all JSON files that may contain Docker-era paths:
        #   openclaw.json, agents/*/sessions/sessions.json, etc.
        for f in \$(find ${state_dir} -name '*.json' ! -name '*.bak*' 2>/dev/null); do
            if grep -q '/home/node' \"\$f\" 2>/dev/null; then
                sed -i 's|/home/node/.openclaw/workspace|${state_dir}/workspace|g' \"\$f\"
                sed -i 's|/home/node/.openclaw|${state_dir}|g' \"\$f\"
                sed -i 's|/home/node|/home/openclaw-${instance}|g' \"\$f\"
                changed=true
            fi
            if grep -q 'host.docker.internal' \"\$f\" 2>/dev/null; then
                sed -i 's|host.docker.internal|127.0.0.1|g' \"\$f\"
                changed=true
            fi
        done

        if [ \"\$changed\" = true ]; then
            mkdir -p ${state_dir}/workspace
            chown -R openclaw-${instance}:openclaw-${instance} ${state_dir}/workspace 2>/dev/null || true
            echo '[sync] Migrated Docker-era paths in state files'
        fi
    "
}

# ============================================================
# lima_build — Install deps and build OpenClaw inside Lima VM
# ============================================================
lima_build() {
    local instance="${INSTANCE_NAME:-default}"
    local staging="/tmp/cf-sync"
    local src_dir="${staging}/bot_repos/${instance}/approved"
    echo "Building (instance: ${instance})..."

    # --- Node.js dependencies ---
    local needs_install="yes"
    local vm_hash
    vm_hash=$(_lima_root "cat ${LIMA_SRV}/bot_repos/${instance}/approved/.pnpm-lock-hash 2>/dev/null" 2>/dev/null || true)
    if [[ -n "$vm_hash" ]]; then
        local src_hash
        src_hash=$(_lima_exec bash -c "md5sum ${src_dir}/pnpm-lock.yaml 2>/dev/null | cut -d' ' -f1" 2>/dev/null || true)
        if [[ "$vm_hash" == "$src_hash" ]]; then
            needs_install="no"
        fi
    fi

    if _lima_exec test -f "${src_dir}/package.json" 2>/dev/null; then
        if [[ "$needs_install" == "yes" ]]; then
            echo "[build] Installing Node.js dependencies..."
            _lima_exec bash -c "
                cd ${src_dir}
                pnpm install --reporter=silent --frozen-lockfile 2>/dev/null || \
                    pnpm install --reporter=silent 2>/dev/null || \
                    pnpm install
            "
            # Copy node_modules to the service directory
            _lima_root "
                rsync -a --delete \
                    ${src_dir}/node_modules/ \
                    ${LIMA_SRV}/bot_repos/${instance}/approved/node_modules/
            "
            # Cache lockfile hash
            _lima_exec bash -c "md5sum ${src_dir}/pnpm-lock.yaml 2>/dev/null | cut -d' ' -f1" | \
                _lima_root "cat > ${LIMA_SRV}/bot_repos/${instance}/approved/.pnpm-lock-hash"
            echo "[build] Dependencies installed"
        else
            echo "[build] Dependencies up to date (skipped)"
        fi

        echo "[build] Building OpenClaw..."
        _lima_root "
            cd ${LIMA_SRV}/bot_repos/${instance}/approved
            pnpm build 2>/dev/null || npm run build
        "
        echo "[build] OpenClaw built"

        echo "[build] Building Control UI..."
        _lima_root "
            cd ${LIMA_SRV}/bot_repos/${instance}/approved
            pnpm ui:build 2>/dev/null || npm run ui:build 2>/dev/null || echo '[build] No ui:build script found (skipped)'
        "
        echo "[build] Control UI built"
    else
        echo "[build] No package.json found, skipping Node.js build"
    fi

    # --- Python dependencies ---
    _lima_root "
        if [ -f ${LIMA_SRV}/controller/requirements.txt ]; then
            echo '[build] Installing Python dependencies...'
            pip3 install -q --break-system-packages -r ${LIMA_SRV}/controller/requirements.txt 2>/dev/null
            echo '[build] Python dependencies installed'
        fi
    "

    # Clean up staging
    _lima_exec rm -rf "$staging"

    echo "Build complete"
}

# ============================================================
# lima_services — Start/stop/restart systemd services
# ============================================================
lima_services() {
    local action="${1:-start}"
    local instance="${INSTANCE_NAME:-default}"

    # Determine per-instance gateway port
    local gw_port="${GATEWAY_PORT:-18789}"
    local svc_user="openclaw-${instance}"

    # Create per-instance user (if not exists) with docker group access
    _lima_root "
        id ${svc_user} >/dev/null 2>&1 || useradd -r -m -s /bin/bash ${svc_user}
        usermod -aG docker ${svc_user} 2>/dev/null || true

        # Set up isolated directory ownership
        mkdir -p ${LIMA_SRV}/bot_repos/${instance}/state
        mkdir -p ${LIMA_SRV}/bot_repos/${instance}/approved
        chown -R ${svc_user}:${svc_user} ${LIMA_SRV}/bot_repos/${instance}/
        chmod 700 ${LIMA_SRV}/bot_repos/${instance}/

        # Secrets: split permissions so gateway can't read admin token
        if [ -d ${LIMA_SRV}/secrets/${instance} ]; then
            chown root:${svc_user} ${LIMA_SRV}/secrets/${instance}/
            chmod 750 ${LIMA_SRV}/secrets/${instance}/

            # controller.env: root-only (admin token, GitHub tokens)
            if [ -f ${LIMA_SRV}/secrets/${instance}/controller.env ]; then
                chown root:root ${LIMA_SRV}/secrets/${instance}/controller.env
                chmod 600 ${LIMA_SRV}/secrets/${instance}/controller.env
            fi

            # gateway.env: readable by gateway user (API keys, internal token)
            if [ -f ${LIMA_SRV}/secrets/${instance}/gateway.env ]; then
                chown root:${svc_user} ${LIMA_SRV}/secrets/${instance}/gateway.env
                chmod 640 ${LIMA_SRV}/secrets/${instance}/gateway.env
            fi

            # snapshot key: readable by gateway user (for age encryption)
            if [ -f ${LIMA_SRV}/secrets/${instance}/snapshot.key ]; then
                chown root:${svc_user} ${LIMA_SRV}/secrets/${instance}/snapshot.key
                chmod 640 ${LIMA_SRV}/secrets/${instance}/snapshot.key
            fi

            # Generate GATEWAY_INTERNAL_TOKEN if missing
            if ! grep -q 'GATEWAY_INTERNAL_TOKEN' ${LIMA_SRV}/secrets/${instance}/gateway.env 2>/dev/null; then
                itok=\$(head -c 32 /dev/urandom | xxd -p)
                echo \"GATEWAY_INTERNAL_TOKEN=\${itok}\" >> ${LIMA_SRV}/secrets/${instance}/gateway.env
                echo '[services] Generated GATEWAY_INTERNAL_TOKEN'
            fi
        fi

        # Snapshots owned by instance user
        mkdir -p ${LIMA_SRV}/snapshots/${instance}
        chown -R ${svc_user}:${svc_user} ${LIMA_SRV}/snapshots/${instance}/

        # Shared audit log — append-only for gateway users
        touch ${LIMA_SRV}/audit/audit.jsonl
        chmod 662 ${LIMA_SRV}/audit/audit.jsonl
    "

    # Create instance-specific systemd overrides
    _lima_root "
        # Gateway override — unique port, user, isolated state
        mkdir -p /etc/systemd/system/openclaw-gateway@${instance}.service.d
        cat > /etc/systemd/system/openclaw-gateway@${instance}.service.d/override.conf <<EOF
[Service]
User=${svc_user}
Group=${svc_user}
Environment=INSTANCE_NAME=${instance}
Environment=OPENCLAW_GATEWAY_MODE=local
Environment=OPENCLAW_GATEWAY_AUTH=none
Environment=OPENCLAW_STATE_DIR=${LIMA_SRV}/bot_repos/${instance}/state
Environment=HOME=/home/${svc_user}
ExecStart=
ExecStart=/usr/bin/node dist/index.js gateway --port ${gw_port} --bind lan
EOF

        # Gateway override — load gateway secrets + LLM proxy env vars
        cat >> /etc/systemd/system/openclaw-gateway@${instance}.service.d/override.conf <<EOF
EnvironmentFile=${LIMA_SRV}/secrets/${instance}/gateway.env
Environment=ANTHROPIC_BASE_URL=http://127.0.0.1:9090/anthropic
Environment=OPENAI_BASE_URL=http://127.0.0.1:9090/openai
Environment=GEMINI_API_BASE=http://127.0.0.1:9090/gemini
EOF

        # Controller override
        mkdir -p /etc/systemd/system/clawfactory-controller.service.d
        cat > /etc/systemd/system/clawfactory-controller.service.d/override.conf <<EOF
[Service]
EnvironmentFile=${LIMA_SRV}/secrets/${instance}/controller.env
EnvironmentFile=${LIMA_SRV}/secrets/${instance}/gateway.env
Environment=APPROVED_DIR=${LIMA_SRV}/bot_repos/${instance}/approved
Environment=OPENCLAW_HOME=${LIMA_SRV}/bot_repos/${instance}/state
Environment=AUDIT_LOG=${LIMA_SRV}/audit/audit.jsonl
Environment=INSTANCE_NAME=${instance}
Environment=GATEWAY_CONTAINER=local
Environment=SNAPSHOTS_DIR=${LIMA_SRV}/snapshots/${instance}
Environment=AGE_KEY=${LIMA_SRV}/secrets/${instance}/snapshot.key
Environment=TRAFFIC_LOG=${LIMA_SRV}/audit/traffic.jsonl
Environment=SCRUB_RULES_PATH=${LIMA_SRV}/audit/scrub_rules.json
Environment=CAPTURE_STATE_FILE=${LIMA_SRV}/audit/capture_enabled
Environment=NGINX_LOG=/var/log/nginx/access.json
ExecStart=
ExecStart=/usr/bin/python3 -m uvicorn main:app --host 0.0.0.0 --port ${CONTROLLER_PORT:-8080}
EOF

        # LLM Proxy override
        mkdir -p /etc/systemd/system/clawfactory-llm-proxy.service.d
        cat > /etc/systemd/system/clawfactory-llm-proxy.service.d/override.conf <<EOF
[Service]
Environment=TRAFFIC_LOG=${LIMA_SRV}/audit/traffic.jsonl
Environment=SCRUB_RULES_PATH=${LIMA_SRV}/audit/scrub_rules.json
Environment=CAPTURE_STATE_FILE=${LIMA_SRV}/audit/capture_enabled
EOF

        systemctl daemon-reload
    "

    case "$action" in
        start)
            echo "Starting ClawFactory services..."
            _lima_root "systemctl start openclaw-gateway@${instance} clawfactory-llm-proxy clawfactory-controller nginx docker"
            echo "Services started"
            ;;
        stop)
            echo "Stopping ClawFactory services..."
            _lima_root "systemctl stop openclaw-gateway@${instance} clawfactory-llm-proxy clawfactory-controller nginx" 2>/dev/null || true
            echo "Services stopped"
            ;;
        restart)
            echo "Restarting ClawFactory services..."
            _lima_root "systemctl restart openclaw-gateway@${instance} clawfactory-llm-proxy clawfactory-controller nginx"
            echo "Services restarted"
            ;;
        status)
            _lima_root "systemctl status --no-pager openclaw-gateway@${instance} clawfactory-llm-proxy clawfactory-controller nginx docker" 2>/dev/null || true
            ;;
        *)
            echo "Usage: lima_services {start|stop|restart|status}" >&2
            return 1
            ;;
    esac
}

# ============================================================
# lima_openclaw — Run OpenClaw CLI inside Lima VM (interactive)
# ============================================================
lima_openclaw() {
    local instance="${INSTANCE_NAME:-default}"
    local svc_user="openclaw-${instance}"
    _lima_root "
        sudo -u ${svc_user} \
            env HOME=/home/${svc_user} \
            OPENCLAW_STATE_DIR=${LIMA_SRV}/bot_repos/${instance}/state \
            \$(cat ${LIMA_SRV}/secrets/${instance}/gateway.env 2>/dev/null | grep -v '^#' | xargs) \
            bash -c 'cd ${LIMA_SRV}/bot_repos/${instance}/approved && node openclaw.mjs $*'
    "
}

# ============================================================
# lima_shell — Interactive shell inside Lima VM
# ============================================================
lima_shell() {
    limactl shell "$LIMA_VM_NAME"
}

# ============================================================
# lima_status — Check VM and service status
# ============================================================
lima_status() {
    echo "=== Lima Sandbox Status ==="
    echo ""

    # Lima VM status
    echo "Lima VM:"
    if limactl list -q 2>/dev/null | grep -q "^${LIMA_VM_NAME}$"; then
        local status
        status=$(limactl list --json 2>/dev/null | python3 -c "
import json,sys
for vm in json.loads(sys.stdin.read().rstrip().replace('}\n{', '},{')):
    if vm.get('name')=='$LIMA_VM_NAME': print(vm.get('status',''))
" 2>/dev/null || echo "unknown")
        echo "  ${LIMA_VM_NAME}: ${status}"
    else
        echo "  Not provisioned (run ./sandbox/lima/setup.sh)"
        return 1
    fi

    # Service status
    echo ""
    echo "Services:"
    for svc in openclaw-gateway@"${INSTANCE_NAME:-default}" clawfactory-llm-proxy clawfactory-controller nginx docker; do
        local svc_status
        svc_status=$(_lima_root "systemctl is-active $svc" 2>/dev/null || echo "unknown")
        printf "  %-40s %s\n" "$svc" "$svc_status"
    done

    # Ports (Lima VZ auto-forwards)
    echo ""
    echo "Access:"
    echo "  Gateway:    http://localhost:${GATEWAY_PORT:-18789}"
    echo "  Controller: http://localhost:${CONTROLLER_PORT:-8080}"
}

# ============================================================
# _lima_snapshot_pull — Sync snapshots from VM back to host
# ============================================================
_lima_snapshot_pull() {
    local instance="${1:-${INSTANCE_NAME:-default}}"
    local host_dir="${SCRIPT_DIR}/snapshots/${instance}"
    local rsh="ssh -F ${LIMA_SSH_CONFIG}"

    # Only pull if snapshots exist in the VM
    if ! _lima_exec test -d "${LIMA_SRV}/snapshots/${instance}" 2>/dev/null; then
        return 0
    fi

    mkdir -p "$host_dir"
    rsync -a \
        --exclude 'latest.tar.age' \
        -e "$rsh" \
        "${LIMA_SSH_HOST}:${LIMA_SRV}/snapshots/${instance}/" \
        "${host_dir}/"

    local count
    count=$(ls "$host_dir"/*.tar.age 2>/dev/null | wc -l | tr -d ' ')
    if [[ "$count" -gt 0 ]]; then
        echo "[snapshots] Pulled ${count} snapshot(s) to host"
    fi
}

# ============================================================
# lima_stop — Stop services (doesn't stop VM)
# ============================================================
lima_stop() {
    local instance="${INSTANCE_NAME:-default}"
    _lima_snapshot_pull "$instance"
    echo "Stopping ClawFactory services..."
    _lima_root "
        systemctl stop openclaw-gateway@${instance} 2>/dev/null || true
        systemctl stop clawfactory-llm-proxy 2>/dev/null || true
        systemctl stop clawfactory-controller 2>/dev/null || true
        systemctl stop nginx 2>/dev/null || true
    "
    echo "Services stopped"
}
