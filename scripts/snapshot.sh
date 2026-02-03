#!/usr/bin/env bash
#
# ClawFactory Encrypted Snapshot System
#
# Creates encrypted snapshots of bot state using age encryption.
# Snapshots include memory, embeddings, config, and credentials.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log() { echo -e "${GREEN}[snapshot]${NC} $1"; }
warn() { echo -e "${YELLOW}[snapshot]${NC} $1"; }
error() { echo -e "${RED}[snapshot]${NC} $1" >&2; }

# Load instance config
INSTANCE_NAME="${INSTANCE_NAME:-}"
if [[ -z "$INSTANCE_NAME" && -f "${ROOT_DIR}/.clawfactory.conf" ]]; then
    source "${ROOT_DIR}/.clawfactory.conf"
fi
INSTANCE_NAME="${INSTANCE_NAME:-default}"

# Paths
BOT_REPOS_DIR="${ROOT_DIR}/bot_repos"
STATE_DIR="${BOT_REPOS_DIR}/${INSTANCE_NAME}/state"
SECRETS_DIR="${ROOT_DIR}/secrets/${INSTANCE_NAME}"
SNAPSHOTS_DIR="${ROOT_DIR}/snapshots/${INSTANCE_NAME}"
AGE_KEY="${SECRETS_DIR}/snapshot.key"
AGE_PUB="${SECRETS_DIR}/snapshot.pub"

# Snapshot retention (keep last N snapshots)
KEEP_SNAPSHOTS="${KEEP_SNAPSHOTS:-10}"

# ============================================================
# Key Management
# ============================================================

check_age() {
    if ! command -v age &>/dev/null; then
        error "age is not installed. Install with: brew install age"
        exit 1
    fi
}

generate_keys() {
    check_age

    if [[ -f "$AGE_KEY" ]]; then
        warn "Keys already exist at $AGE_KEY"
        echo "To regenerate, delete existing keys first."
        return 0
    fi

    mkdir -p "$SECRETS_DIR"

    log "Generating age keypair..."
    age-keygen -o "$AGE_KEY" 2>"$AGE_PUB"
    chmod 600 "$AGE_KEY"
    chmod 644 "$AGE_PUB"

    log "Keys generated:"
    echo "  Private key: $AGE_KEY (keep this safe!)"
    echo "  Public key:  $AGE_PUB"
    echo ""
    cat "$AGE_PUB"
}

# ============================================================
# Snapshot Creation
# ============================================================

create_snapshot() {
    check_age

    if [[ ! -f "$AGE_KEY" ]]; then
        error "No encryption key found. Run: $0 keygen"
        exit 1
    fi

    if [[ ! -d "$STATE_DIR" ]]; then
        error "State directory not found: $STATE_DIR"
        exit 1
    fi

    # Get public key from private key file
    local pubkey
    pubkey=$(grep "public key:" "$AGE_KEY" | sed 's/.*: //')

    mkdir -p "$SNAPSHOTS_DIR"

    local timestamp
    timestamp=$(date -u +"%Y-%m-%dT%H-%M-%SZ")
    local snapshot_name="snapshot-${timestamp}.tar.age"
    local snapshot_path="${SNAPSHOTS_DIR}/${snapshot_name}"
    local temp_tar
    temp_tar=$(mktemp)

    log "Creating snapshot for instance: $INSTANCE_NAME"
    log "Timestamp: $timestamp"

    # Create tarball of state
    # Include:
    # - memory/ (embeddings database)
    # - workspace/memory/ (memory markdown files)
    # - openclaw.json (config)
    # - devices/ (paired devices)
    # - credentials/ (allowlists)
    # - identity/ (device keys)

    log "Packaging state..."
    tar -C "$STATE_DIR" -cf "$temp_tar" \
        --exclude='*.tmp*' \
        --exclude='agents/*/sessions/*.jsonl' \
        --exclude='installed' \
        --exclude='installed/*' \
        . 2>/dev/null || true

    local tar_size
    tar_size=$(du -h "$temp_tar" | cut -f1)
    log "Unencrypted size: $tar_size"

    # Encrypt with age
    log "Encrypting..."
    age -r "$pubkey" -o "$snapshot_path" "$temp_tar"
    rm -f "$temp_tar"

    local enc_size
    enc_size=$(du -h "$snapshot_path" | cut -f1)
    log "Encrypted size: $enc_size"

    # Update latest symlink
    ln -sf "$snapshot_name" "${SNAPSHOTS_DIR}/latest.tar.age"

    log "Snapshot created: $snapshot_path"

    # Prune old snapshots
    prune_snapshots

    echo "$snapshot_path"
}

# ============================================================
# Snapshot Listing
# ============================================================

list_snapshots() {
    if [[ ! -d "$SNAPSHOTS_DIR" ]]; then
        echo "No snapshots found for instance: $INSTANCE_NAME"
        return 0
    fi

    echo "Snapshots for instance: $INSTANCE_NAME"
    echo "Directory: $SNAPSHOTS_DIR"
    echo ""

    local count=0
    for snapshot in "$SNAPSHOTS_DIR"/snapshot-*.tar.age; do
        if [[ -f "$snapshot" ]]; then
            local name
            name=$(basename "$snapshot")
            local size
            size=$(du -h "$snapshot" | cut -f1)
            local timestamp
            timestamp=$(echo "$name" | sed 's/snapshot-\(.*\)\.tar\.age/\1/')

            # Check if this is latest
            local latest=""
            if [[ -L "${SNAPSHOTS_DIR}/latest.tar.age" ]]; then
                local latest_target
                latest_target=$(readlink "${SNAPSHOTS_DIR}/latest.tar.age")
                if [[ "$latest_target" == "$name" ]]; then
                    latest=" (latest)"
                fi
            fi

            echo "  $name  ${size}${latest}"
            ((count++))
        fi
    done

    if [[ $count -eq 0 ]]; then
        echo "  (no snapshots)"
    else
        echo ""
        echo "Total: $count snapshots"
    fi
}

# ============================================================
# Snapshot Restoration
# ============================================================

restore_snapshot() {
    local snapshot_file="$1"

    check_age

    if [[ ! -f "$AGE_KEY" ]]; then
        error "No decryption key found at $AGE_KEY"
        exit 1
    fi

    # Resolve snapshot path
    if [[ "$snapshot_file" == "latest" ]]; then
        snapshot_file="${SNAPSHOTS_DIR}/latest.tar.age"
    elif [[ ! -f "$snapshot_file" ]]; then
        # Try as filename in snapshots dir
        if [[ -f "${SNAPSHOTS_DIR}/${snapshot_file}" ]]; then
            snapshot_file="${SNAPSHOTS_DIR}/${snapshot_file}"
        else
            error "Snapshot not found: $snapshot_file"
            exit 1
        fi
    fi

    if [[ ! -f "$snapshot_file" ]]; then
        error "Snapshot not found: $snapshot_file"
        exit 1
    fi

    log "Restoring from: $snapshot_file"

    # Stop gateway first
    warn "This will overwrite current state. Gateway should be stopped."
    echo -n "Continue? [y/N] "
    read -r confirm
    if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
        echo "Aborted."
        exit 0
    fi

    # Create backup of current state
    local backup_dir="${STATE_DIR}.backup-$(date +%s)"
    if [[ -d "$STATE_DIR" ]]; then
        log "Backing up current state to: $backup_dir"
        mv "$STATE_DIR" "$backup_dir"
    fi

    mkdir -p "$STATE_DIR"

    # Decrypt and extract
    log "Decrypting and extracting..."
    age -d -i "$AGE_KEY" "$snapshot_file" | tar -C "$STATE_DIR" -xf -

    log "Restore complete!"
    echo ""
    echo "Next steps:"
    echo "  1. Review restored state in: $STATE_DIR"
    echo "  2. Restart gateway: ./clawfactory.sh restart"
    echo ""
    echo "Previous state backed up to: $backup_dir"
}

# ============================================================
# Snapshot Pruning
# ============================================================

prune_snapshots() {
    if [[ ! -d "$SNAPSHOTS_DIR" ]]; then
        return 0
    fi

    local snapshots=()
    while IFS= read -r -d '' file; do
        snapshots+=("$file")
    done < <(find "$SNAPSHOTS_DIR" -name "snapshot-*.tar.age" -type f -print0 | sort -z -r)

    local count=${#snapshots[@]}
    if [[ $count -le $KEEP_SNAPSHOTS ]]; then
        return 0
    fi

    log "Pruning old snapshots (keeping $KEEP_SNAPSHOTS)..."
    local to_delete=$((count - KEEP_SNAPSHOTS))

    for ((i = KEEP_SNAPSHOTS; i < count; i++)); do
        local file="${snapshots[$i]}"
        log "  Removing: $(basename "$file")"
        rm -f "$file"
    done
}

# ============================================================
# Main
# ============================================================

usage() {
    cat <<EOF
ClawFactory Encrypted Snapshot System

Usage: $0 [-i <instance>] <command>

Commands:
  create       Create encrypted snapshot of current state
  list         List available snapshots
  restore <f>  Restore from snapshot (filename or 'latest')
  keygen       Generate encryption keys

Options:
  -i, --instance <name>   Specify instance (default: from .clawfactory.conf)

Environment:
  KEEP_SNAPSHOTS    Number of snapshots to retain (default: 10)

Examples:
  $0 keygen                     # Generate encryption keys
  $0 create                     # Create snapshot
  $0 list                       # List snapshots
  $0 restore latest             # Restore latest snapshot
  $0 restore snapshot-2026-02-03T12-00-00Z.tar.age

Snapshots include:
  - Embeddings database (memory/main.sqlite)
  - Configuration (openclaw.json)
  - Paired devices and credentials
  - Device identity keys

Snapshots EXCLUDE (rebuilt from git):
  - installed/ (npm packages - rebuilt from {instance}_save/package.json)

EOF
}

# Parse instance flag
while [[ $# -gt 0 ]]; do
    case "$1" in
        -i|--instance)
            INSTANCE_NAME="$2"
            shift 2
            # Re-set paths with new instance
            STATE_DIR="${BOT_REPOS_DIR}/${INSTANCE_NAME}/state"
            SECRETS_DIR="${ROOT_DIR}/secrets/${INSTANCE_NAME}"
            SNAPSHOTS_DIR="${ROOT_DIR}/snapshots/${INSTANCE_NAME}"
            AGE_KEY="${SECRETS_DIR}/snapshot.key"
            AGE_PUB="${SECRETS_DIR}/snapshot.pub"
            ;;
        -i=*|--instance=*)
            INSTANCE_NAME="${1#*=}"
            shift
            STATE_DIR="${BOT_REPOS_DIR}/${INSTANCE_NAME}/state"
            SECRETS_DIR="${ROOT_DIR}/secrets/${INSTANCE_NAME}"
            SNAPSHOTS_DIR="${ROOT_DIR}/snapshots/${INSTANCE_NAME}"
            AGE_KEY="${SECRETS_DIR}/snapshot.key"
            AGE_PUB="${SECRETS_DIR}/snapshot.pub"
            ;;
        *)
            break
            ;;
    esac
done

case "${1:-help}" in
    create)
        create_snapshot
        ;;
    list)
        list_snapshots
        ;;
    restore)
        if [[ -z "${2:-}" ]]; then
            error "Usage: $0 restore <snapshot-file|latest>"
            exit 1
        fi
        restore_snapshot "$2"
        ;;
    keygen)
        generate_keys
        ;;
    *)
        usage
        ;;
esac
