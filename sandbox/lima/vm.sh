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
# Provides: lima_ensure, lima_sync, lima_sync_watch, lima_build,
#           lima_services, lima_tunnels, lima_openclaw, lima_status, lima_shell,
#           lima_mounts

# --- Configuration ---
LIMA_VM_NAME="clawfactory"
LIMA_SRV="/srv/clawfactory"

# --- Internal helpers ---
_lima_exec() {
    limactl shell --workdir /tmp "$LIMA_VM_NAME" -- "$@"
}

_lima_root() {
    limactl shell --workdir /tmp "$LIMA_VM_NAME" -- sudo bash -c "$1"
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

    # Pull code changes from VM (e.g. upstream merges) before pushing
    _lima_code_pull "$instance"

    echo "Syncing files to Lima VM (instance: ${instance})..."

    local cf_root="${SCRIPT_DIR}"
    local staging="/tmp/cf-sync"
    local rsh="ssh -F ${LIMA_SSH_CONFIG}"

    _lima_exec mkdir -p "${staging}/controller" "${staging}/proxy"

    # Sync controller (host → VM over SSH)
    rsync -av --delete --exclude '__pycache__' \
        -e "$rsh" \
        "${cf_root}/controller/" "${LIMA_SSH_HOST}:${staging}/controller/"

    # Sync proxy config
    rsync -av --delete \
        -e "$rsh" \
        "${cf_root}/proxy/" "${LIMA_SSH_HOST}:${staging}/proxy/"

    # Sync bot_repos for this instance
    if [[ -d "${cf_root}/bot_repos/${instance}" ]]; then
        _lima_exec mkdir -p "${staging}/bot_repos/${instance}/code"

        rsync -av --delete \
            --exclude 'node_modules' \
            --exclude '.DS_Store' \
            --exclude 'workspace' \
            -e "$rsh" \
            "${cf_root}/bot_repos/${instance}/code/" \
            "${LIMA_SSH_HOST}:${staging}/bot_repos/${instance}/code/"

        # State directory is NOT synced from host.
        # Snapshots are the source of truth for bot state.
        # State is restored from snapshots via _lima_restore_snapshot.
    fi

    # Push workspace edits from host to VM (so local edits reach the gateway)
    if [[ -d "${cf_root}/bot_repos/${instance}/state/workspace" ]]; then
        rsync -a \
            --rsync-path="sudo rsync" \
            --exclude '.git' \
            -e "$rsh" \
            "${cf_root}/bot_repos/${instance}/state/workspace/" \
            "${LIMA_SSH_HOST}:${LIMA_SRV}/bot_repos/${instance}/state/workspace/"
    fi

    # Sync secrets for this instance (restricted permissions)
    if [[ -d "${cf_root}/secrets/${instance}" ]]; then
        _lima_exec mkdir -p "${staging}/secrets/${instance}"
        rsync -av \
            -e "$rsh" \
            "${cf_root}/secrets/${instance}/" \
            "${LIMA_SSH_HOST}:${staging}/secrets/${instance}/"
    fi

    # Deploy from staging to /srv/clawfactory (runs inside VM)
    # Excludes protect VM-only dirs from --delete:
    #   state/     — bot runtime state (source of truth: snapshots)
    #   dist/      — build output (rebuilt by lima_build)
    #   snapshots/ — encrypted snapshots
    #   audit/     — traffic logs
    #   mitm-ca/   — mitmproxy CA certs
    _lima_root "
        rsync -av --delete \
            --exclude 'node_modules' \
            --exclude '.pnpm-lock-hash' \
            --exclude 'snapshots' \
            --exclude 'bot_repos/*/state' \
            --exclude 'bot_repos/*/code/dist' \
            --exclude 'audit' \
            --exclude 'mitm-ca' \
            ${staging}/ ${LIMA_SRV}/

        # Ensure directory structure
        mkdir -p ${LIMA_SRV}/bot_repos/${instance}/state \
                 ${LIMA_SRV}/audit \
                 ${LIMA_SRV}/snapshots/${instance}

        # Lock down secrets — only root can read controller env
        chmod 700 ${LIMA_SRV}/secrets/${instance}/ 2>/dev/null || true
        chmod 600 ${LIMA_SRV}/secrets/${instance}/*.env 2>/dev/null || true

        # Fix ownership so gateway user can access deployed files
        svc_user=openclaw-${instance}
        if id \${svc_user} >/dev/null 2>&1; then
            chown -R \${svc_user}:\${svc_user} ${LIMA_SRV}/bot_repos/${instance}/ 2>/dev/null || true
        fi
    "

    # Keep staging for build step

    # Migrate Docker-era paths in state config
    _lima_fix_docker_paths "$instance"

    echo "Files synced"
}

# ============================================================
# lima_sync_watch — Watch for file changes and auto-sync
# ============================================================
lima_sync_watch() {
    local instance="${INSTANCE_NAME:-default}"

    if ! command -v fswatch &>/dev/null; then
        echo "Error: fswatch not found. Install: brew install fswatch" >&2
        return 1
    fi

    local watch_dirs=("${SCRIPT_DIR}/controller" "${SCRIPT_DIR}/proxy")
    if [[ -d "${SCRIPT_DIR}/bot_repos/${instance}/code" ]]; then
        watch_dirs+=("${SCRIPT_DIR}/bot_repos/${instance}/code")
    fi

    echo "[sync] Watching for changes (instance: ${instance})..."
    echo "[sync] Dirs: ${watch_dirs[*]}"
    echo "[sync] Press Ctrl+C to stop"

    fswatch --recursive --latency 2 \
        --exclude '__pycache__' --exclude '.DS_Store' --exclude 'node_modules' \
        "${watch_dirs[@]}" | while read -r changed; do
        # Drain remaining buffered lines to avoid redundant syncs
        local files=("$(basename "$changed")")
        local security_flag=false
        _lima_sync_watch_check_security "$changed" && security_flag=true
        while read -r -t 0.1 extra; do
            files+=("$(basename "$extra")")
            _lima_sync_watch_check_security "$extra" && security_flag=true
        done

        echo ""
        echo "[sync] $(date +%H:%M:%S) ${#files[@]} file(s) changed: ${files[*]}"
        if [[ "$security_flag" == true ]]; then
            echo "[sync] *** SECURITY-SENSITIVE file changed — review before deploy ***"
        fi
        lima_sync
        _lima_root "systemctl restart clawfactory-controller openclaw-gateway@${instance}"
        echo "[sync] Controller + gateway restarted"
    done
}

# Check if a path is security-sensitive (secrets, env, keys, auth code)
_lima_sync_watch_check_security() {
    local path="$1"
    case "$path" in
        */secrets/*|*.env|*.key|*.pem|*.age|*token*|*auth*|*credential*|*scrub*)
            return 0 ;;
        *)
            return 1 ;;
    esac
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
    local src_dir="${staging}/bot_repos/${instance}/code"
    echo "Building (instance: ${instance})..."

    # --- Node.js dependencies ---
    local needs_install="yes"
    local vm_hash
    vm_hash=$(_lima_root "cat ${LIMA_SRV}/bot_repos/${instance}/code/.pnpm-lock-hash 2>/dev/null" 2>/dev/null || true)
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
                    ${LIMA_SRV}/bot_repos/${instance}/code/node_modules/
            "
            # Cache lockfile hash
            _lima_exec bash -c "md5sum ${src_dir}/pnpm-lock.yaml 2>/dev/null | cut -d' ' -f1" | \
                _lima_root "cat > ${LIMA_SRV}/bot_repos/${instance}/code/.pnpm-lock-hash"
            echo "[build] Dependencies installed"
        else
            echo "[build] Dependencies up to date (skipped)"
        fi

        echo "[build] Building OpenClaw..."
        _lima_root "
            cd ${LIMA_SRV}/bot_repos/${instance}/code
            pnpm build 2>/dev/null || npm run build
        "
        echo "[build] OpenClaw built"

        echo "[build] Building Control UI..."
        _lima_root "
            cd ${LIMA_SRV}/bot_repos/${instance}/code
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

    # Fix ownership so the gateway user can access built files and state
    local svc_user="openclaw-${instance}"
    _lima_root "
        chown -R ${svc_user}:${svc_user} ${LIMA_SRV}/bot_repos/${instance}/
        chmod 700 ${LIMA_SRV}/bot_repos/${instance}/
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
        mkdir -p ${LIMA_SRV}/bot_repos/${instance}/code
        chown -R ${svc_user}:${svc_user} ${LIMA_SRV}/bot_repos/${instance}/
        chmod 700 ${LIMA_SRV}/bot_repos/${instance}/

        # Lock down default state dir in user home
        mkdir -p /home/${svc_user}/.openclaw
        chown ${svc_user}:${svc_user} /home/${svc_user}/.openclaw
        chmod 700 /home/${svc_user}/.openclaw

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

            # Generate AGENT_API_TOKEN if missing (scoped token for sandboxed agent)
            if ! grep -q 'AGENT_API_TOKEN' ${LIMA_SRV}/secrets/${instance}/gateway.env 2>/dev/null; then
                atok=\$(head -c 32 /dev/urandom | xxd -p)
                echo \"AGENT_API_TOKEN=\${atok}\" >> ${LIMA_SRV}/secrets/${instance}/gateway.env
                echo '[services] Generated AGENT_API_TOKEN'
            fi
        fi

        # Snapshots owned by instance user
        mkdir -p ${LIMA_SRV}/snapshots/${instance}
        chown -R ${svc_user}:${svc_user} ${LIMA_SRV}/snapshots/${instance}/

        # Shared audit log — append-only for gateway users
        touch ${LIMA_SRV}/audit/audit.jsonl
        chmod 662 ${LIMA_SRV}/audit/audit.jsonl

        # MITM CA directory — root only
        mkdir -p ${LIMA_SRV}/mitm-ca
        chmod 700 ${LIMA_SRV}/mitm-ca

        # Encrypted traffic log — root only
        touch ${LIMA_SRV}/audit/traffic.enc.jsonl
        chown root:root ${LIMA_SRV}/audit/traffic.enc.jsonl
        chmod 600 ${LIMA_SRV}/audit/traffic.enc.jsonl

        # Install MITM CA into system trust store (if it exists)
        if [ -f ${LIMA_SRV}/mitm-ca/mitmproxy-ca-cert.pem ]; then
            cp ${LIMA_SRV}/mitm-ca/mitmproxy-ca-cert.pem /usr/local/share/ca-certificates/mitmproxy-ca.crt
            update-ca-certificates >/dev/null 2>&1 || true
        fi
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
Environment=OPENCLAW_STATE_DIR=${LIMA_SRV}/bot_repos/${instance}/state
Environment=HOME=/home/${svc_user}
ExecStart=
ExecStart=/usr/bin/node dist/index.js gateway --port ${gw_port} --bind lan
EOF

        # Gateway override — load gateway secrets
        cat >> /etc/systemd/system/openclaw-gateway@${instance}.service.d/override.conf <<EOF
EnvironmentFile=${LIMA_SRV}/secrets/${instance}/gateway.env
EOF
        # NOTE: LLM proxy intercept (ANTHROPIC_BASE_URL etc.) removed — it added
        # latency and only covered 3 providers. The proxy still runs on :9090 and
        # can be re-enabled per-provider via openclaw.json baseUrl overrides.

        # Controller override
        mkdir -p /etc/systemd/system/clawfactory-controller.service.d
        cat > /etc/systemd/system/clawfactory-controller.service.d/override.conf <<EOF
[Service]
EnvironmentFile=${LIMA_SRV}/secrets/${instance}/controller.env
EnvironmentFile=${LIMA_SRV}/secrets/${instance}/gateway.env
Environment=CODE_DIR=${LIMA_SRV}/bot_repos/${instance}/code
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
Environment=ENCRYPTED_TRAFFIC_LOG=${LIMA_SRV}/audit/traffic.enc.jsonl
Environment=FERNET_KEY_FILE=${LIMA_SRV}/audit/traffic.fernet.key
Environment=FERNET_KEY_AGE=${LIMA_SRV}/audit/traffic.fernet.key.age
Environment=MITM_CA_DIR=${LIMA_SRV}/mitm-ca
ExecStart=
ExecStart=/usr/bin/python3 -m uvicorn main:app --host 0.0.0.0 --port ${CONTROLLER_PORT:-8080}
EOF

        # MITM proxy override
        mkdir -p /etc/systemd/system/clawfactory-mitm.service.d
        cat > /etc/systemd/system/clawfactory-mitm.service.d/override.conf <<EOF
[Service]
Environment=TRAFFIC_LOG=${LIMA_SRV}/audit/traffic.enc.jsonl
Environment=FERNET_KEY_FILE=${LIMA_SRV}/audit/traffic.fernet.key
Environment=FERNET_KEY_AGE=${LIMA_SRV}/audit/traffic.fernet.key.age
Environment=AGE_KEY=${LIMA_SRV}/secrets/${instance}/snapshot.key
Environment=CAPTURE_STATE_FILE=${LIMA_SRV}/audit/capture_enabled
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

    # Clean up stale MITM iptables rules if capture is not enabled
    _lima_root "
        capture_on=false
        if [ -f ${LIMA_SRV}/audit/capture_enabled ] && [ \"\$(cat ${LIMA_SRV}/audit/capture_enabled)\" = '1' ]; then
            capture_on=true
        fi
        if [ \"\$capture_on\" = false ]; then
            iptables -t nat -D OUTPUT -m owner --uid-owner ${svc_user} -p tcp --dport 443 -j REDIRECT --to-port 8888 2>/dev/null || true
            iptables -t nat -D OUTPUT -m owner --uid-owner ${svc_user} -p tcp --dport 80 -j REDIRECT --to-port 8888 2>/dev/null || true
            systemctl stop clawfactory-mitm 2>/dev/null || true
        fi
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
# lima_tunnels — SSH port forwarding (localhost + Tailscale)
# ============================================================
lima_tunnels() {
    local action="${1:-start}"
    local instance="${INSTANCE_NAME:-default}"
    local gw_port="${GATEWAY_PORT:-18789}"
    local ctrl_port="${CONTROLLER_PORT:-8080}"
    local proxy_port=9090
    local pidfile="/tmp/clawfactory-tunnels-${instance}.pid"

    case "$action" in
        start)
            # Kill stale tunnels
            lima_tunnels stop 2>/dev/null

            # Detect Tailscale IP (macOS app binary, bare CLI errors)
            local ts_ip=""
            ts_ip=$(/Applications/Tailscale.app/Contents/MacOS/Tailscale ip --4 2>/dev/null || true)

            # Build forward args — always localhost (use array for safe expansion)
            local fwd=()
            fwd+=(-L "127.0.0.1:${gw_port}:127.0.0.1:${gw_port}")
            fwd+=(-L "127.0.0.1:${ctrl_port}:127.0.0.1:${ctrl_port}")
            fwd+=(-L "127.0.0.1:${proxy_port}:127.0.0.1:${proxy_port}")

            # Add Tailscale binds if available
            if [[ -n "$ts_ip" ]]; then
                fwd+=(-L "${ts_ip}:${gw_port}:127.0.0.1:${gw_port}")
                fwd+=(-L "${ts_ip}:${ctrl_port}:127.0.0.1:${ctrl_port}")
                fwd+=(-L "${ts_ip}:${proxy_port}:127.0.0.1:${proxy_port}")
            fi

            # Launch background SSH tunnel
            ssh -F "${LIMA_SSH_CONFIG}" "${fwd[@]}" -N -f "${LIMA_SSH_HOST}"
            # Find the SSH PID we just spawned
            pgrep -nf "ssh.*-N.*${LIMA_SSH_HOST}" > "$pidfile" 2>/dev/null || true

            echo "  Tunnels: localhost (${gw_port}, ${ctrl_port}, ${proxy_port})"
            [[ -n "$ts_ip" ]] && echo "  Tailnet: ${ts_ip} (${gw_port}, ${ctrl_port}, ${proxy_port})"

            # Set up Tailscale HTTPS serve (auto-certs via MagicDNS)
            local ts_bin="/Applications/Tailscale.app/Contents/MacOS/Tailscale"
            if [[ -x "$ts_bin" ]]; then
                # Reset only our paths (not all serves)
                "$ts_bin" serve off --https=443 --set-path / 2>/dev/null || true
                "$ts_bin" serve off --https=8443 --set-path / 2>/dev/null || true

                local serve_err
                serve_err=$("$ts_bin" serve --bg --https=443 --set-path / "http://127.0.0.1:${gw_port}" 2>&1)
                if [[ $? -ne 0 ]]; then
                    echo "  Tailscale HTTPS (gateway): FAILED — $serve_err"
                    echo "  Hint: ensure MagicDNS + HTTPS are enabled in Tailscale admin"
                else
                    serve_err=$("$ts_bin" serve --bg --https=8443 --set-path / "http://127.0.0.1:${ctrl_port}" 2>&1)
                    if [[ $? -ne 0 ]]; then
                        echo "  Tailscale HTTPS (controller): FAILED — $serve_err"
                    fi

                    local ts_hostname
                    ts_hostname=$("$ts_bin" status --self --json 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))" 2>/dev/null || true)
                    if [[ -n "$ts_hostname" ]]; then
                        echo "  HTTPS:   https://${ts_hostname}/ (gateway)"
                        echo "  HTTPS:   https://${ts_hostname}:8443/ (controller)"
                    fi
                fi
            else
                echo "  Tailscale: not found (install Tailscale.app for HTTPS)"
            fi
            ;;
        stop)
            if [[ -f "$pidfile" ]]; then
                local pid
                pid=$(cat "$pidfile")
                kill "$pid" 2>/dev/null || true
                rm -f "$pidfile"
            fi
            # Also kill any orphaned tunnel processes for this VM
            pkill -f "ssh.*-N.*${LIMA_SSH_HOST}" 2>/dev/null || true
            rm -f "${HOME}/.lima/${LIMA_VM_NAME}/ssh.sock"
            # Tear down Tailscale serve (our paths only)
            local ts_bin="/Applications/Tailscale.app/Contents/MacOS/Tailscale"
            if [[ -x "$ts_bin" ]]; then
                "$ts_bin" serve off --https=443 --set-path / 2>/dev/null || true
                "$ts_bin" serve off --https=8443 --set-path / 2>/dev/null || true
            fi
            ;;
        status)
            if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
                echo "Tunnels: running (PID $(cat "$pidfile"))"
            else
                echo "Tunnels: not running"
            fi
            local ts_bin="/Applications/Tailscale.app/Contents/MacOS/Tailscale"
            if [[ -x "$ts_bin" ]]; then
                "$ts_bin" serve status 2>/dev/null || true
            fi
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
            bash -c 'cd ${LIMA_SRV}/bot_repos/${instance}/code && node openclaw.mjs $*'
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

    # Prune old auto-snapshots in VM before pulling
    _lima_prune_snapshots "$instance"

    mkdir -p "$host_dir"
    rsync -a \
        --exclude 'latest.tar.age' \
        --delete \
        -e "$rsh" \
        "${LIMA_SSH_HOST}:${LIMA_SRV}/snapshots/${instance}/" \
        "${host_dir}/"

    local count
    count=$(ls "$host_dir"/*.tar.age 2>/dev/null | wc -l | tr -d ' ')
    if [[ "$count" -gt 0 ]]; then
        echo "[snapshots] Pulled ${count} snapshot(s) to host"
    fi

    # Also pull state alongside snapshots
    _lima_state_pull "$instance"
}

# ============================================================
# _lima_state_pull — Sync bot state from VM back to host
# ============================================================
_lima_state_pull() {
    local instance="${1:-${INSTANCE_NAME:-default}}"
    local host_dir="${SCRIPT_DIR}/bot_repos/${instance}/state"
    local rsh="ssh -F ${LIMA_SSH_CONFIG}"

    if ! _lima_exec test -d "${LIMA_SRV}/bot_repos/${instance}/state" 2>/dev/null; then
        return 0
    fi

    mkdir -p "$host_dir"
    rsync -a \
        --rsync-path="sudo rsync" \
        --exclude 'installed' \
        --exclude 'installed/*' \
        --exclude 'subagents' \
        --exclude 'media' \
        --exclude '*.tmp*' \
        -e "$rsh" \
        "${LIMA_SSH_HOST}:${LIMA_SRV}/bot_repos/${instance}/state/" \
        "${host_dir}/"

    echo "[state] Pulled state to host"
}

# ============================================================
# _lima_code_pull — Sync code from VM back to host
# ============================================================
_lima_code_pull() {
    local instance="${1:-${INSTANCE_NAME:-default}}"
    local host_dir="${SCRIPT_DIR}/bot_repos/${instance}/code"
    local rsh="ssh -F ${LIMA_SSH_CONFIG}"

    if ! _lima_exec test -d "${LIMA_SRV}/bot_repos/${instance}/code" 2>/dev/null; then
        return 0
    fi

    mkdir -p "$host_dir"
    rsync -a \
        --rsync-path="sudo rsync" \
        --exclude 'node_modules' \
        --exclude 'dist' \
        --exclude '.pnpm-lock-hash' \
        --exclude 'workspace' \
        -e "$rsh" \
        "${LIMA_SSH_HOST}:${LIMA_SRV}/bot_repos/${instance}/code/" \
        "${host_dir}/"

    echo "[code] Pulled code to host"
}

# ============================================================
# Snapshot state management — snapshots are source of truth
# ============================================================

# Check if latest snapshot exists in VM
_lima_has_snapshot() {
    local instance="${1:-${INSTANCE_NAME:-default}}"
    _lima_exec test -e "${LIMA_SRV}/snapshots/${instance}/latest.tar.age" 2>/dev/null &&
    _lima_exec test -f "${LIMA_SRV}/secrets/${instance}/snapshot.key" 2>/dev/null
}

# Check if state dir has meaningful content
_lima_has_state() {
    local instance="${1:-${INSTANCE_NAME:-default}}"
    _lima_exec test -f "${LIMA_SRV}/bot_repos/${instance}/state/openclaw.json" 2>/dev/null
}

# Get latest snapshot filename
_lima_latest_snapshot_name() {
    local instance="${1:-${INSTANCE_NAME:-default}}"
    _lima_root "readlink -f ${LIMA_SRV}/snapshots/${instance}/latest.tar.age 2>/dev/null | xargs basename 2>/dev/null" 2>/dev/null
}

# Take a backup snapshot of current state (echoes backup filename)
_lima_backup_state() {
    local instance="${1:-${INSTANCE_NAME:-default}}"
    local snapshot_dir="${LIMA_SRV}/snapshots/${instance}"
    local state_dir="${LIMA_SRV}/bot_repos/${instance}/state"
    local key_file="${LIMA_SRV}/secrets/${instance}/snapshot.key"

    _lima_root "
        backup_name=\"pre-start--\$(date -u +%Y-%m-%dT%H-%M-%SZ).tar.age\"
        pubkey=\$(age-keygen -y ${key_file} 2>/dev/null)
        if [ -z \"\$pubkey\" ]; then
            echo 'ERROR' >&2
            exit 1
        fi

        mkdir -p ${snapshot_dir}
        cd ${state_dir}
        tar --exclude='*.tmp*' \
            --exclude='agents/*/sessions/*.jsonl' \
            --exclude='agents/*/sessions/*.jsonl.deleted.*' \
            --exclude='installed' \
            --exclude='installed/*' \
            --exclude='workspace/*/.git' \
            --exclude='*/venv' \
            --exclude='*/node_modules' \
            --exclude='*/__pycache__' \
            --exclude='*/.venv' \
            --exclude='subagents' \
            --exclude='media' \
            -cf - . | age -r \"\$pubkey\" -o \"${snapshot_dir}/\$backup_name\"

        echo \"\$backup_name\"
    " 2>/dev/null
}

# Prune auto-snapshots older than 24 hours (runs in VM)
_lima_prune_snapshots() {
    local instance="${1:-${INSTANCE_NAME:-default}}"
    local snapshot_dir="${LIMA_SRV}/snapshots/${instance}"

    _lima_root "
        cutoff=\$(date -u -d '24 hours ago' +%Y-%m-%dT%H-%M-%SZ 2>/dev/null || date -u -v-24H +%Y-%m-%dT%H-%M-%SZ)
        latest_target=\$(readlink -f ${snapshot_dir}/latest.tar.age 2>/dev/null | xargs basename 2>/dev/null)
        pruned=0

        for f in ${snapshot_dir}/snapshot--*.tar.age ${snapshot_dir}/pre-start--*.tar.age; do
            [ -f \"\$f\" ] || continue
            fname=\$(basename \"\$f\")
            [ \"\$fname\" = \"\$latest_target\" ] && continue

            # Extract timestamp from name--YYYY-MM-DDTHH-MM-SSZ.tar.age
            ts=\$(echo \"\$fname\" | sed 's/.*--\\(.*\\)\\.tar\\.age/\\1/')
            [ \"\$ts\" \\< \"\$cutoff\" ] && rm -f \"\$f\" && pruned=\$((pruned + 1))
        done

        [ \$pruned -gt 0 ] && echo \"[snapshots] Pruned \$pruned old snapshot(s)\"
    " 2>/dev/null
}

# Restore state from latest snapshot
_lima_restore_snapshot() {
    local instance="${1:-${INSTANCE_NAME:-default}}"
    local snapshot_dir="${LIMA_SRV}/snapshots/${instance}"
    local state_dir="${LIMA_SRV}/bot_repos/${instance}/state"
    local key_file="${LIMA_SRV}/secrets/${instance}/snapshot.key"

    _lima_root "
        snapshot=\$(readlink -f ${snapshot_dir}/latest.tar.age 2>/dev/null)
        if [ ! -f \"\$snapshot\" ]; then
            echo '[snapshot] Could not resolve latest snapshot' >&2
            exit 1
        fi

        mkdir -p ${state_dir}
        if age -d -i ${key_file} \"\$snapshot\" | tar -C ${state_dir} -xf -; then
            echo '[snapshot] State restored'
        else
            echo '[snapshot] Restore failed' >&2
            exit 1
        fi
    "
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

# ============================================================
# lima_snapshot_autopull — Set up periodic snapshot sync to host
# ============================================================
_SNAPSHOT_AUTOPULL_LABEL="com.clawfactory.snapshot-sync"

lima_snapshot_autopull() {
    local action="${1:-status}"
    local instance="${INSTANCE_NAME:-default}"
    local plist_path="${HOME}/Library/LaunchAgents/${_SNAPSHOT_AUTOPULL_LABEL}.plist"

    case "$action" in
        enable)
            mkdir -p "${HOME}/Library/LaunchAgents"
            cat > "$plist_path" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${_SNAPSHOT_AUTOPULL_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${SCRIPT_DIR}/clawfactory.sh</string>
        <string>-i</string>
        <string>${instance}</string>
        <string>snapshot</string>
        <string>pull</string>
    </array>
    <key>StartInterval</key>
    <integer>300</integer>
    <key>StandardOutPath</key>
    <string>/tmp/clawfactory-snapshot-sync.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/clawfactory-snapshot-sync.log</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
PLIST
            launchctl load "$plist_path" 2>/dev/null || true
            echo "[snapshots] Auto-pull enabled (every 5 minutes)"
            ;;
        disable)
            launchctl unload "$plist_path" 2>/dev/null || true
            rm -f "$plist_path"
            echo "[snapshots] Auto-pull disabled"
            ;;
        status)
            if launchctl list "$_SNAPSHOT_AUTOPULL_LABEL" &>/dev/null; then
                echo "[snapshots] Auto-pull: enabled (every 5 minutes)"
            else
                echo "[snapshots] Auto-pull: disabled"
                echo "  Enable: ./clawfactory.sh snapshot autopull enable"
            fi
            ;;
    esac
}

# ============================================================
# lima_mounts — Manage host directory mounts in the Lima VM
# ============================================================

_lima_mount_restart() {
    local yq_expr="$1"
    local instance="${INSTANCE_NAME:-default}"

    echo "Stopping tunnels..."
    lima_tunnels stop 2>/dev/null || true

    echo "Stopping services..."
    _lima_root "
        systemctl stop openclaw-gateway@${instance} 2>/dev/null || true
        systemctl stop clawfactory-llm-proxy 2>/dev/null || true
        systemctl stop clawfactory-controller 2>/dev/null || true
        systemctl stop nginx 2>/dev/null || true
    "

    echo "Stopping VM..."
    limactl stop "$LIMA_VM_NAME"

    echo "Applying mount change..."
    limactl edit "$LIMA_VM_NAME" --set "$yq_expr"

    echo "Starting VM..."
    limactl start "$LIMA_VM_NAME" 2>/dev/null || true

    echo "Syncing files..."
    lima_sync
    lima_build

    echo "Starting services..."
    lima_services start
    lima_tunnels start 2>/dev/null || true
}

_lima_mount_fix_perms() {
    local vm_path="$1"
    local instance="${INSTANCE_NAME:-default}"
    local svc_user="openclaw-${instance}"

    _lima_root "
        command -v setfacl >/dev/null 2>&1 || apt-get install -y acl >/dev/null 2>&1
        setfacl -R -m u:${svc_user}:rwX '${vm_path}' 2>/dev/null || true
        setfacl -R -d -m u:${svc_user}:rwX '${vm_path}' 2>/dev/null || true
    "
}

lima_mounts() {
    local action="${1:-list}"
    local lima_yaml="${HOME}/.lima/${LIMA_VM_NAME}/lima.yaml"

    case "$action" in
        list)
            if [[ ! -f "$lima_yaml" ]]; then
                echo "No Lima config found at ${lima_yaml}" >&2
                return 1
            fi

            local output
            output=$(python3 -c "
import yaml, sys
with open('${lima_yaml}') as f:
    cfg = yaml.safe_load(f)
mounts = cfg.get('mounts') or []
if not mounts:
    print('No mounts configured')
    sys.exit(0)
for m in mounts:
    loc = m.get('location', '?')
    mp = m.get('mountPoint', '?')
    wr = 'rw' if m.get('writable', False) else 'ro'
    print(f'  {loc} -> {mp} ({wr})')
" 2>&1)
            echo "$output"
            ;;

        add)
            shift
            local host_path=""
            local vm_name=""

            # Parse args
            while [[ $# -gt 0 ]]; do
                case "$1" in
                    --name)
                        vm_name="$2"
                        shift 2
                        ;;
                    *)
                        if [[ -z "$host_path" ]]; then
                            host_path="$1"
                        fi
                        shift
                        ;;
                esac
            done

            if [[ -z "$host_path" ]]; then
                echo "Usage: ./clawfactory.sh mount add <host-path> [--name <vm-name>]" >&2
                return 1
            fi

            # Resolve to absolute path
            host_path="$(cd "$host_path" 2>/dev/null && pwd || echo "$host_path")"

            if [[ ! -e "$host_path" ]]; then
                echo "Error: Path does not exist: ${host_path}" >&2
                return 1
            fi

            # Compute VM mount point
            local basename
            basename="$(basename "$host_path")"
            local mount_point="/mnt/${vm_name:-$basename}"

            # Check for duplicates
            if [[ -f "$lima_yaml" ]]; then
                local dup
                dup=$(python3 -c "
import yaml, sys
with open('${lima_yaml}') as f:
    cfg = yaml.safe_load(f)
for m in (cfg.get('mounts') or []):
    if m.get('location') == '${host_path}' or m.get('mountPoint') == '${mount_point}':
        print('dup')
        break
" 2>/dev/null)
                if [[ "$dup" == "dup" ]]; then
                    echo "Error: Mount already exists for ${host_path} or ${mount_point}" >&2
                    return 1
                fi
            fi

            echo "This will mount:"
            echo "  ${host_path} -> ${mount_point} (writable)"
            echo ""
            echo "The VM must restart. Continue? [y/N]"
            read -r confirm
            if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
                echo "Cancelled"
                return 0
            fi

            local yq_expr=".mounts += [{\"location\": \"${host_path}\", \"mountPoint\": \"${mount_point}\", \"writable\": true}]"
            _lima_mount_restart "$yq_expr"

            echo "Fixing permissions for ${mount_point}..."
            _lima_mount_fix_perms "$mount_point"

            echo "Mounted: ${host_path} -> ${mount_point}"
            ;;

        remove)
            shift
            local target="${1:-}"

            if [[ -z "$target" ]]; then
                echo "Usage: ./clawfactory.sh mount remove <host-path-or-vm-path>" >&2
                return 1
            fi

            if [[ ! -f "$lima_yaml" ]]; then
                echo "No Lima config found" >&2
                return 1
            fi

            # Build new mounts list excluding the target
            local new_mounts
            new_mounts=$(python3 -c "
import yaml, json, sys
with open('${lima_yaml}') as f:
    cfg = yaml.safe_load(f)
mounts = cfg.get('mounts') or []
target = '${target}'
filtered = [m for m in mounts if m.get('location') != target and m.get('mountPoint') != target]
if len(filtered) == len(mounts):
    print('NOT_FOUND')
    sys.exit(0)
# Output as JSON for yq
print(json.dumps(filtered))
" 2>&1)

            if [[ "$new_mounts" == "NOT_FOUND" ]]; then
                echo "Error: No mount found matching '${target}'" >&2
                return 1
            fi

            echo "This will remove the mount for '${target}'."
            echo "The VM must restart. Continue? [y/N]"
            read -r confirm
            if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
                echo "Cancelled"
                return 0
            fi

            local yq_expr=".mounts = ${new_mounts}"
            _lima_mount_restart "$yq_expr"

            echo "Mount removed: ${target}"
            ;;

        *)
            echo "Usage: ./clawfactory.sh mount <list|add|remove>"
            echo ""
            echo "  list                              Show current mounts"
            echo "  add <host-path> [--name <name>]   Mount host directory into VM"
            echo "  remove <host-path-or-vm-path>     Remove a mount"
            ;;
    esac
}
