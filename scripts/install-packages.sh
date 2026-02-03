#!/usr/bin/env bash
#
# Install bot packages from declarative package.json
#
# Called on container startup. Only runs npm install if package.json changed.
#
set -euo pipefail

INSTANCE_NAME="${INSTANCE_NAME:-default}"
WORKSPACE_DIR="${WORKSPACE_DIR:-/workspace/approved/workspace}"
STATE_DIR="${STATE_DIR:-/home/node/.openclaw}"
SAVE_DIR="${WORKSPACE_DIR}/${INSTANCE_NAME}_save"
INSTALL_DIR="${STATE_DIR}/installed"
HASH_FILE="${INSTALL_DIR}/.package-hash"

log() { echo "[packages] $1"; }

# Check if save directory exists
if [[ ! -d "$SAVE_DIR" ]]; then
    log "No save directory found at $SAVE_DIR - skipping package install"
    exit 0
fi

# Check if package.json exists
PACKAGE_JSON="${SAVE_DIR}/package.json"
if [[ ! -f "$PACKAGE_JSON" ]]; then
    log "No package.json found - skipping package install"
    exit 0
fi

# Compute current hash
CURRENT_HASH=$(sha256sum "$PACKAGE_JSON" | cut -d' ' -f1)

# Check last installed hash
LAST_HASH=""
if [[ -f "$HASH_FILE" ]]; then
    LAST_HASH=$(cat "$HASH_FILE")
fi

# Compare
if [[ "$CURRENT_HASH" == "$LAST_HASH" ]]; then
    log "package.json unchanged - skipping install"
    exit 0
fi

log "package.json changed - installing packages..."

# Create install directory
mkdir -p "$INSTALL_DIR"

# Copy package.json to install dir and run npm install there
cp "$PACKAGE_JSON" "$INSTALL_DIR/package.json"

# Also copy package-lock.json if it exists
if [[ -f "${SAVE_DIR}/package-lock.json" ]]; then
    cp "${SAVE_DIR}/package-lock.json" "$INSTALL_DIR/package-lock.json"
fi

# Install packages
cd "$INSTALL_DIR"
npm install --production 2>&1 | while read -r line; do
    echo "[npm] $line"
done

# Save hash
echo "$CURRENT_HASH" > "$HASH_FILE"

log "Package installation complete"
