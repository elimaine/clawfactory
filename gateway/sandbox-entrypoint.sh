#!/bin/bash
set -e

# ClawFactory Gateway Sandbox Entrypoint
# Starts Docker daemon (via Sysbox) and then runs OpenClaw gateway

DOCKER_LOG="/var/log/dockerd.log"
DOCKER_READY_TIMEOUT=30

log() {
    echo "[sandbox-entrypoint] $*"
}

# Start Docker daemon if not already running
start_docker() {
    if docker info &>/dev/null; then
        log "Docker daemon already running"
        return 0
    fi

    log "Starting Docker daemon..."

    # Start dockerd in background (Sysbox handles the isolation)
    # Use --storage-driver=vfs for maximum compatibility inside containers
    sudo dockerd \
        --storage-driver=vfs \
        --iptables=true \
        --ip-masq=true \
        > "$DOCKER_LOG" 2>&1 &

    DOCKERD_PID=$!

    # Wait for Docker to be ready
    local count=0
    while ! docker info &>/dev/null; do
        if ! kill -0 $DOCKERD_PID 2>/dev/null; then
            log "ERROR: Docker daemon failed to start"
            cat "$DOCKER_LOG" || true
            return 1
        fi

        count=$((count + 1))
        if [ $count -ge $DOCKER_READY_TIMEOUT ]; then
            log "ERROR: Docker daemon not ready after ${DOCKER_READY_TIMEOUT}s"
            cat "$DOCKER_LOG" || true
            return 1
        fi

        sleep 1
    done

    log "Docker daemon ready (PID: $DOCKERD_PID)"
}

# Build OpenClaw sandbox image if needed
ensure_sandbox_image() {
    local image="openclaw-sandbox:bookworm-slim"

    if docker image inspect "$image" &>/dev/null; then
        log "Sandbox image already exists: $image"
        return 0
    fi

    log "Building sandbox image: $image"

    # Pull and tag the base image
    docker pull debian:bookworm-slim
    docker tag debian:bookworm-slim "$image"

    log "Sandbox image ready: $image"
}

# Main startup sequence
main() {
    # Start Docker daemon (runs as root via sudo)
    if ! start_docker; then
        log "WARNING: Failed to start Docker daemon - sandboxing will be disabled"
        log "Continuing without sandbox support..."
    else
        # Ensure sandbox image exists
        ensure_sandbox_image || log "WARNING: Failed to build sandbox image"
    fi

    # Now run the standard OpenClaw startup sequence
    log "Starting OpenClaw gateway..."

    # Secure state directory permissions
    chmod 700 /home/node/.openclaw 2>/dev/null || true

    # Sync code config files to OpenClaw workspace on startup
    log "Syncing workspace from code dir..."
    cp -r /workspace/code/workspace/*.md /home/node/.openclaw/workspace/ 2>/dev/null || true
    cp -r /workspace/code/workspace/skills /home/node/.openclaw/workspace/ 2>/dev/null || true
    cp -r /workspace/code/workspace/policies.yml /home/node/.openclaw/workspace/ 2>/dev/null || true

    # Memory stays in state/ (encrypted snapshots, not git)
    mkdir -p /home/node/.openclaw/workspace/memory

    # Migrate existing memory from code dir if state is empty (one-time)
    if [ -d "/workspace/code/workspace/memory" ] && [ -z "$(ls -A /home/node/.openclaw/workspace/memory 2>/dev/null)" ]; then
        log "Migrating memory from code dir to state..."
        cp -r /workspace/code/workspace/memory/* /home/node/.openclaw/workspace/memory/ 2>/dev/null || true
    fi
    if [ -f "/workspace/code/workspace/MEMORY.md" ] && [ ! -f "/home/node/.openclaw/workspace/MEMORY.md" ]; then
        cp /workspace/code/workspace/MEMORY.md /home/node/.openclaw/workspace/MEMORY.md
    fi

    # Install packages from {instance}_save/package.json if changed
    SAVE_DIR="/workspace/code/workspace/${INSTANCE_NAME}_save"
    INSTALL_DIR="/home/node/.openclaw/installed"
    if [ -f "$SAVE_DIR/package.json" ]; then
        mkdir -p "$INSTALL_DIR"
        CURRENT_HASH=$(sha256sum "$SAVE_DIR/package.json" | cut -d' ' -f1)
        LAST_HASH=""
        if [ -f "$INSTALL_DIR/.package-hash" ]; then
            LAST_HASH=$(cat "$INSTALL_DIR/.package-hash")
        fi
        if [ "$CURRENT_HASH" != "$LAST_HASH" ]; then
            log "Installing packages from ${INSTANCE_NAME}_save/package.json..."
            cp "$SAVE_DIR/package.json" "$INSTALL_DIR/"
            [ -f "$SAVE_DIR/package-lock.json" ] && cp "$SAVE_DIR/package-lock.json" "$INSTALL_DIR/"
            cd "$INSTALL_DIR" && npm install --production
            echo "$CURRENT_HASH" > "$INSTALL_DIR/.package-hash"
            log "Packages installed"
        else
            log "Packages unchanged, skipping install"
        fi
    fi

    log "Workspace synced"

    # Run minimal setup then start gateway
    cd /app
    node dist/index.js onboard --non-interactive --accept-risk --mode local \
        --skip-daemon --skip-channels --skip-skills --skip-health --skip-ui \
        --gateway-auth token --gateway-token "$OPENCLAW_GATEWAY_TOKEN"

    exec node dist/index.js gateway --port 18789 --bind lan
}

main "$@"
