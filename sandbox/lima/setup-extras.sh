#!/usr/bin/env bash
# Auto-managed by `clawfactory.sh setup-extras approve`.
#
# Approved entries are appended below the "=== Approved entries ===" marker;
# manual edits ABOVE the marker are preserved across approvals.
#
# Run modes:
#   bash setup-extras.sh              # default: install if missing (idempotent)
#   bash setup-extras.sh install      # same as default
#   bash setup-extras.sh --upgrade    # re-run apt-get install --only-upgrade for managed packages

set -uo pipefail

UPGRADE_MODE="${1:-install}"

# ensure_apt <package> [<verify-binary>]
# Idempotent apt install. Skips if verify-binary already on PATH.
ensure_apt() {
    local pkg="$1" verify="${2:-$1}"
    if [[ "$UPGRADE_MODE" == "--upgrade" ]]; then
        sudo DEBIAN_FRONTEND=noninteractive apt-get install --only-upgrade -y -qq "$pkg" || true
        return
    fi
    if command -v "$verify" >/dev/null 2>&1; then
        return 0
    fi
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$pkg"
}

# ensure_apt_repo <name> <key-url> <repo-line>
# Idempotent third-party apt repo registration.
ensure_apt_repo() {
    local name="$1" key_url="$2" repo_line="$3"
    if [[ -f "/etc/apt/keyrings/${name}.gpg" ]]; then
        return 0
    fi
    sudo install -m 0755 -d /etc/apt/keyrings
    curl -fsSL "$key_url" | sudo gpg --dearmor -o "/etc/apt/keyrings/${name}.gpg"
    echo "$repo_line" | sudo tee "/etc/apt/sources.list.d/${name}.list" >/dev/null
    sudo apt-get update -qq
}

# ensure_shell <verify-cmd> <installer-cmd...>
# Run installer-cmd unless verify-cmd already succeeds.
ensure_shell() {
    local verify="$1"; shift
    if eval "$verify" >/dev/null 2>&1; then
        return 0
    fi
    "$@"
}

# === Approved entries (managed by clawfactory.sh setup-extras) ===
# Do not edit below this line manually; use `clawfactory.sh -i <inst> setup-extras approve`.

# extras: e_2026-04-26T02-50-31_200e (Install xtradeb/apps Chromium on Ubuntu noble arm64 for OpenClaw browser control; includes PPA key/source setup because Google Chrome Linux arm64 is unavailable and Ubuntu's chromium-browser is a snap wrapper.)
ensure_shell chromium\ --version bash -c $'set -euo pipefail\ninstall -d -m 0755 /etc/apt/keyrings\nrm -f /etc/apt/keyrings/xtradeb-apps.gpg\ncurl -fsSL \'https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x5301FA4FD93244FBC6F6149982BB6851C64F6880\' | gpg --dearmor --batch --yes -o /etc/apt/keyrings/xtradeb-apps.gpg\nchmod 0644 /etc/apt/keyrings/xtradeb-apps.gpg\nprintf \'%s\\n\' \'deb [arch=arm64 signed-by=/etc/apt/keyrings/xtradeb-apps.gpg] https://ppa.launchpadcontent.net/xtradeb/apps/ubuntu noble main\' > /etc/apt/sources.list.d/xtradeb-apps.list\napt-get update\nDEBIAN_FRONTEND=noninteractive apt-get install -y chromium\nchromium --version'
