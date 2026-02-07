#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IPTABLES_BACKUP="${SCRIPT_DIR}/.iptables.backup"

# Load sandbox mode
SANDBOX_MODE="none"
if [[ -f "${SCRIPT_DIR}/.clawfactory.conf" ]]; then
    source "${SCRIPT_DIR}/.clawfactory.conf"
fi
# Backward compat
if [[ -z "${SANDBOX_MODE:-}" ]]; then
    if [[ "${SANDBOX_ENABLED:-false}" == "true" ]]; then
        SANDBOX_MODE="sysbox"
    else
        SANDBOX_MODE="none"
    fi
fi

case "${1:-}" in
    lock)
        echo "KILL SWITCH ACTIVATED"
        echo ""

        if [[ "$SANDBOX_MODE" == "lima" ]]; then
            # Lima mode: stop the VM entirely
            if command -v limactl >/dev/null 2>&1; then
                echo "Stopping Lima VM (timeout 30s)..."
                limactl stop clawfactory &>/dev/null &
                _lima_pid=$!
                _waited=0
                while kill -0 "$_lima_pid" 2>/dev/null && [[ $_waited -lt 30 ]]; do
                    sleep 1
                    ((_waited++))
                done
                if kill -0 "$_lima_pid" 2>/dev/null; then
                    echo "Graceful stop timed out, force stopping..."
                    kill "$_lima_pid" 2>/dev/null || true
                    limactl stop --force clawfactory 2>/dev/null || true
                fi
            fi
        fi

        # Stop Docker stack (regardless of mode — catch any running containers)
        echo "Stopping containers..."
        cd "${SCRIPT_DIR}"
        docker compose down --timeout 5 2>/dev/null || true

        # Save current iptables (macOS doesn't use iptables)
        if command -v iptables >/dev/null 2>&1; then
            echo "Saving firewall rules..."
            iptables-save > "${IPTABLES_BACKUP}" 2>/dev/null || true

            echo "Applying restrictive firewall..."
            iptables -F
            iptables -X
            iptables -P INPUT DROP
            iptables -P FORWARD DROP
            iptables -P OUTPUT DROP
            iptables -A INPUT -i lo -j ACCEPT
            iptables -A OUTPUT -o lo -j ACCEPT
            iptables -A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
            iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
        else
            echo "Note: iptables not available (macOS?). Network not locked."
        fi

        echo ""
        echo "System locked. All services stopped."
        if [[ "$SANDBOX_MODE" == "lima" ]]; then
            echo "  Lima VM stopped."
        fi
        echo "  Run './killswitch.sh restore' to restore."
        ;;

    restore)
        echo "Restoring system..."

        # Restore iptables
        if [[ -f "${IPTABLES_BACKUP}" ]] && command -v iptables >/dev/null 2>&1; then
            echo "Restoring firewall rules..."
            iptables-restore < "${IPTABLES_BACKUP}"
            rm -f "${IPTABLES_BACKUP}"
        fi

        if [[ "$SANDBOX_MODE" == "lima" ]]; then
            echo "Lima mode: use './clawfactory.sh -i <instance> start' to restart."
        else
            # Restart Docker stack
            echo "Starting containers..."
            cd "${SCRIPT_DIR}"
            docker compose up -d
        fi

        echo ""
        echo "System restored."
        ;;

    *)
        echo "Usage: ./killswitch.sh [lock|restore]"
        echo ""
        echo "  lock    - Stop everything, lock down network"
        echo "  restore - Restore normal operation"
        echo ""
        echo "  Current sandbox mode: ${SANDBOX_MODE}"
        ;;
esac
