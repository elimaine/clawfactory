#!/usr/bin/env bash
#
# sandbox/firecracker/setup.sh
#
# One-time provisioning: Lima VM + Firecracker + rootfs for ClawFactory.
#
# Stack: macOS -> Lima (VZ, nested virt) -> KVM -> Firecracker microVM
#   Inside the microVM:
#     - nginx (proxy)              systemd service
#     - node (OpenClaw gateway)    systemd service
#     - python (controller)        systemd service
#     - dockerd (tool sandbox)     systemd service
#
# Usage:
#   ./sandbox/firecracker/setup.sh          # Full setup
#   ./sandbox/firecracker/setup.sh teardown # Remove everything
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CF_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# --- Configuration ---
LIMA_VM_NAME="clawfactory-fc"
LIMA_DISK="100GiB"
FC_VERSION="1.10.1"
FC_TAP_DEV="tap0"
FC_GUEST_IP="172.16.0.2"
FC_HOST_IP="172.16.0.1"
FC_GUEST_MAC="AA:FC:00:00:00:01"
FC_DIR="/opt/firecracker"

# Password + sizing file (generated during setup)
FC_SECRETS_DIR="${CF_ROOT}/secrets"
FC_PASSWORD_FILE="${FC_SECRETS_DIR}/firecracker.password"
FC_SIZING_FILE="${FC_SECRETS_DIR}/firecracker.sizing"

# --- Helpers ---
info()  { printf "\033[1;34m[INFO]\033[0m  %s\n" "$*"; }
ok()    { printf "\033[1;32m[OK]\033[0m    %s\n" "$*"; }
warn()  { printf "\033[1;33m[WARN]\033[0m  %s\n" "$*"; }
err()   { printf "\033[1;31m[ERROR]\033[0m %s\n" "$*" >&2; }
die()   { err "$@"; exit 1; }

lima_exec() {
    limactl shell "$LIMA_VM_NAME" -- "$@"
}

lima_root() {
    limactl shell "$LIMA_VM_NAME" -- sudo bash -c "$1"
}

# ============================================================
# Generate VM password and compute resource sizing
# ============================================================
generate_password() {
    mkdir -p "$FC_SECRETS_DIR"
    chmod 700 "$FC_SECRETS_DIR"

    if [[ -f "$FC_PASSWORD_FILE" ]]; then
        ROOT_PASSWORD=$(cat "$FC_PASSWORD_FILE")
        ok "Using existing VM password from secrets/firecracker.password"
    else
        ROOT_PASSWORD=$(openssl rand -base64 24)
        echo "$ROOT_PASSWORD" > "$FC_PASSWORD_FILE"
        chmod 600 "$FC_PASSWORD_FILE"
        ok "Generated VM password -> secrets/firecracker.password"
    fi
}

compute_sizing() {
    echo ""
    echo "=== Resource Sizing ==="
    echo ""
    echo "How many OpenClaw instances do you plan to run concurrently?"
    echo "Each instance needs ~2 GiB RAM (gateway + controller)."
    echo "Base VM overhead (kernel, nginx, Docker): ~2 GiB."
    echo ""
    read -p "Number of concurrent instances [1]: " instance_count
    instance_count="${instance_count:-1}"

    # Validate
    if ! [[ "$instance_count" =~ ^[0-9]+$ ]] || [[ "$instance_count" -lt 1 ]]; then
        warn "Invalid input, defaulting to 1"
        instance_count=1
    fi
    if [[ "$instance_count" -gt 8 ]]; then
        warn "Capping at 8 instances (16 GiB + 2 GiB base = 18 GiB)"
        instance_count=8
    fi

    # Compute sizes
    # Firecracker VM: 2 GiB base + 2 GiB per instance
    FC_MEM_MIB=$(( (2 + instance_count * 2) * 1024 ))
    # Lima VM: Firecracker + 1 GiB overhead for Lima OS
    local lima_mem_gib=$(( 2 + instance_count * 2 + 1 ))
    LIMA_MEMORY="${lima_mem_gib}GiB"
    # CPUs: 2 base + 1 per instance, capped at host-1
    local host_cpus
    host_cpus=$(sysctl -n hw.ncpu 2>/dev/null || echo 8)
    local max_cpus=$(( host_cpus - 1 ))
    [[ "$max_cpus" -lt 2 ]] && max_cpus=2
    LIMA_CPUS=$(( 2 + instance_count ))
    [[ "$LIMA_CPUS" -gt "$max_cpus" ]] && LIMA_CPUS="$max_cpus"
    FC_VCPUS="$LIMA_CPUS"
    # Rootfs: 4 GiB base + 2 GiB per instance (for node_modules, Docker images)
    ROOTFS_SIZE_MB=$(( (4 + instance_count * 2) * 1024 ))

    # Save sizing so vm.sh can read it
    cat > "$FC_SIZING_FILE" <<EOF
# Firecracker VM sizing (auto-generated)
FC_INSTANCE_COUNT=${instance_count}
FC_MEM_MIB=${FC_MEM_MIB}
FC_VCPUS=${FC_VCPUS}
LIMA_CPUS=${LIMA_CPUS}
LIMA_MEMORY=${LIMA_MEMORY}
ROOTFS_SIZE_MB=${ROOTFS_SIZE_MB}
EOF
    chmod 600 "$FC_SIZING_FILE"

    echo ""
    info "Sizing for ${instance_count} instance(s):"
    info "  Firecracker: ${FC_VCPUS} vCPUs, $(( FC_MEM_MIB / 1024 )) GiB RAM"
    info "  Lima VM:     ${LIMA_CPUS} CPUs, ${LIMA_MEMORY} RAM"
    info "  Rootfs:      $(( ROOTFS_SIZE_MB / 1024 )) GiB disk"
    echo ""
}

# ============================================================
# Phase 1: Install Lima on macOS
# ============================================================
install_lima() {
    info "Phase 1: Installing Lima..."

    if ! command -v brew >/dev/null 2>&1; then
        die "Homebrew is required. Install from https://brew.sh"
    fi

    if command -v limactl >/dev/null 2>&1; then
        ok "Lima already installed: $(limactl --version)"
    else
        brew install lima
        ok "Lima installed: $(limactl --version)"
    fi

    # Ensure sshpass is available (needed for Firecracker SSH)
    if ! command -v sshpass >/dev/null 2>&1; then
        info "Installing sshpass..."
        brew install hudochenkov/sshpass/sshpass 2>/dev/null || \
            brew install esolitos/ipa/sshpass 2>/dev/null || \
            warn "Could not install sshpass via brew. Install manually."
    fi
}

# ============================================================
# Phase 2: Create Lima VM with nested virtualization
# ============================================================
create_lima_vm() {
    info "Phase 2: Creating Lima VM..."

    if limactl list -q 2>/dev/null | grep -q "^${LIMA_VM_NAME}$"; then
        local status
        status=$(limactl list --json 2>/dev/null | python3 -c "
import json,sys
for vm in json.loads(sys.stdin.read().rstrip().replace('}\n{', '},{')):
    if vm.get('name')=='$LIMA_VM_NAME': print(vm.get('status',''))
" 2>/dev/null || echo "unknown")
        if [ "$status" = "Running" ]; then
            ok "Lima VM '${LIMA_VM_NAME}' already running"
            return 0
        fi
        info "Starting existing Lima VM..."
        limactl start "$LIMA_VM_NAME"
        ok "Lima VM started"
        return 0
    fi

    info "Creating Lima VM '${LIMA_VM_NAME}' (VZ + nested virt)..."
    info "  CPUs: ${LIMA_CPUS}, Memory: ${LIMA_MEMORY}, Disk: ${LIMA_DISK}"

    local lima_yaml
    lima_yaml=$(mktemp /tmp/lima-cf-XXXXXX.yaml)
    cat > "$lima_yaml" <<YAML
vmType: vz
nestedVirtualization: true
rosetta:
  enabled: false
cpus: ${LIMA_CPUS}
memory: "${LIMA_MEMORY}"
disk: "${LIMA_DISK}"
images:
  - location: "https://cloud-images.ubuntu.com/releases/24.04/release/ubuntu-24.04-server-cloudimg-arm64.img"
    arch: "aarch64"
mounts: []
YAML

    limactl start --name="$LIMA_VM_NAME" "$lima_yaml"
    rm -f "$lima_yaml"

    # Verify KVM
    if lima_exec ls -la /dev/kvm >/dev/null 2>&1; then
        ok "Lima VM created with KVM support"
    else
        die "Lima VM created but /dev/kvm not available. Nested virtualization may not be supported."
    fi
}

# ============================================================
# Phase 3: Install Firecracker inside Lima VM
# ============================================================
install_firecracker() {
    info "Phase 3: Installing Firecracker v${FC_VERSION}..."

    if lima_exec firecracker --version >/dev/null 2>&1; then
        ok "Firecracker already installed inside Lima"
        return 0
    fi

    local arch
    arch=$(lima_exec uname -m)

    lima_exec bash -c "
        set -e
        curl -fsSL 'https://github.com/firecracker-microvm/firecracker/releases/download/v${FC_VERSION}/firecracker-v${FC_VERSION}-${arch}.tgz' -o /tmp/fc.tgz
        tar -xzf /tmp/fc.tgz -C /tmp
        sudo mv /tmp/release-v${FC_VERSION}-${arch}/firecracker-v${FC_VERSION}-${arch} /usr/local/bin/firecracker
        sudo mv /tmp/release-v${FC_VERSION}-${arch}/jailer-v${FC_VERSION}-${arch} /usr/local/bin/jailer
        sudo chmod +x /usr/local/bin/firecracker /usr/local/bin/jailer
        rm -rf /tmp/fc.tgz /tmp/release-v${FC_VERSION}-${arch}
    "

    # Install socat for port forwarding
    lima_root "apt-get update -qq && apt-get install -y -qq socat sshpass >/dev/null 2>&1"

    ok "Firecracker $(lima_exec firecracker --version 2>&1 | head -1) installed"
}

# ============================================================
# Phase 4: Prepare kernel
# ============================================================
prepare_kernel() {
    info "Phase 4: Preparing kernel..."
    lima_root "mkdir -p ${FC_DIR} && chmod 777 ${FC_DIR}"

    if lima_exec test -f "${FC_DIR}/vmlinux" 2>/dev/null; then
        ok "Kernel already prepared"
        return 0
    fi

    # Use Lima host kernel — has all netfilter/cgroup modules Docker needs
    lima_root "
        cp /boot/vmlinuz-\$(uname -r) /tmp/vmlinuz.gz
        gunzip -f /tmp/vmlinuz.gz
        cp /tmp/vmlinuz ${FC_DIR}/vmlinux
        chmod 644 ${FC_DIR}/vmlinux
    "
    ok "Kernel extracted: $(lima_exec uname -r)"
}

# ============================================================
# Phase 5: Build rootfs with ClawFactory dependencies
# ============================================================
build_rootfs() {
    if lima_exec test -f "${FC_DIR}/rootfs.ext4" 2>/dev/null; then
        ok "Rootfs already exists"
        return 0
    fi

    info "Phase 5: Building ${ROOTFS_SIZE_MB}MB rootfs (this takes several minutes)..."

    # Install debootstrap if needed
    lima_root "apt-get update -qq && apt-get install -y -qq debootstrap >/dev/null 2>&1"

    lima_root "
        set -e
        ROOTFS=${FC_DIR}/rootfs.ext4
        MNT=/mnt/fc-rootfs

        # Create ext4 image
        dd if=/dev/zero of=\$ROOTFS bs=1M count=${ROOTFS_SIZE_MB} status=none
        mkfs.ext4 -q \$ROOTFS

        # Mount and bootstrap
        mkdir -p \$MNT
        mount \$ROOTFS \$MNT
        debootstrap --arch=arm64 noble \$MNT http://ports.ubuntu.com/ubuntu-ports/

        # Configure apt sources (Ubuntu 24.04 noble)
        cat > \$MNT/etc/apt/sources.list <<APT
deb http://ports.ubuntu.com/ubuntu-ports noble main restricted universe
deb http://ports.ubuntu.com/ubuntu-ports noble-updates main restricted universe
APT
        echo 'nameserver 8.8.8.8' > \$MNT/etc/resolv.conf

        # Bind mounts for chroot
        mount --bind /proc \$MNT/proc
        mount --bind /sys \$MNT/sys
        mount --bind /dev \$MNT/dev

        chroot \$MNT bash -c '
            set -e
            export DEBIAN_FRONTEND=noninteractive

            apt-get update -qq

            # ---- Base system ----
            apt-get install -y -qq \
                ca-certificates curl gnupg lsb-release \
                iproute2 iptables systemd systemd-sysv dbus kmod udev \
                openssh-server haveged \
                git rsync >/dev/null 2>&1

            # ---- age (for snapshots) ----
            apt-get install -y -qq age 2>/dev/null || {
                # age may not be in repo, install from GitHub
                ARCH=\$(dpkg --print-architecture)
                curl -fsSL \"https://dl.filippo.io/age/latest?for=linux/\${ARCH}\" -o /tmp/age.tar.gz
                tar -xzf /tmp/age.tar.gz -C /usr/local/bin/ --strip-components=1
                rm -f /tmp/age.tar.gz
            }

            # ---- Node.js 22 via NodeSource ----
            curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
            apt-get install -y -qq nodejs >/dev/null 2>&1

            # ---- pnpm + bun ----
            npm install -g pnpm 2>/dev/null
            curl -fsSL https://bun.sh/install | bash 2>/dev/null || true
            # Move bun to system path if installed to root home
            if [ -f /root/.bun/bin/bun ]; then
                cp /root/.bun/bin/bun /usr/local/bin/bun
                chmod +x /usr/local/bin/bun
            fi

            # ---- Python 3.12 + pip ----
            apt-get install -y -qq python3 python3-pip python3-venv >/dev/null 2>&1

            # ---- Controller pip deps ----
            pip3 install --break-system-packages \
                fastapi uvicorn python-multipart httpx \
                PyGithub pyyaml docker python-jose 2>/dev/null

            # ---- Nginx ----
            apt-get install -y -qq nginx >/dev/null 2>&1

            # ---- Docker CE (for OpenClaw tool sandbox) ----
            install -m 0755 -d /etc/apt/keyrings
            curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
            chmod a+r /etc/apt/keyrings/docker.gpg
            echo \"deb [arch=arm64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu noble stable\" \
                > /etc/apt/sources.list.d/docker.list
            apt-get update -qq
            apt-get install -y -qq docker-ce docker-ce-cli containerd.io >/dev/null 2>&1

            # ---- Enable services ----
            systemctl enable docker haveged systemd-networkd systemd-resolved ssh
            systemctl enable nginx

            # Disable slow boot services
            systemctl disable systemd-networkd-wait-online.service 2>/dev/null || true

            # iptables legacy mode
            update-alternatives --set iptables /usr/sbin/iptables-legacy 2>/dev/null || true
            update-alternatives --set ip6tables /usr/sbin/ip6tables-legacy 2>/dev/null || true

            # Pre-generate SSH host keys
            ssh-keygen -A

            # Allow root login via SSH
            echo \"PermitRootLogin yes\" >> /etc/ssh/sshd_config

            # Clean apt cache
            apt-get clean
            rm -rf /var/lib/apt/lists/*
        '

        umount \$MNT/proc \$MNT/sys \$MNT/dev

        # ---- System configuration ----
        echo 'clawfactory-vm' > \$MNT/etc/hostname
        chroot \$MNT bash -c 'echo root:$(cat "$FC_PASSWORD_FILE") | chpasswd'

        # ---- Static networking ----
        cat > \$MNT/etc/systemd/network/20-eth0.network <<NET
[Match]
Name=eth0

[Network]
Address=${FC_GUEST_IP}/24
Gateway=${FC_HOST_IP}
DNS=8.8.8.8
NET

        # fstab
        echo '/dev/vda / ext4 defaults 0 1' > \$MNT/etc/fstab

        # ---- Copy host kernel modules ----
        cp -a /lib/modules/\$(uname -r) \$MNT/lib/modules/

        # ---- Create ClawFactory directory structure ----
        mkdir -p \$MNT/srv/clawfactory/{controller,proxy,bot_repos,secrets,audit,snapshots}

        # ---- Systemd unit: OpenClaw Gateway ----
        cat > \$MNT/etc/systemd/system/openclaw-gateway.service <<'SVC'
[Unit]
Description=OpenClaw Gateway
After=network.target docker.service
Wants=docker.service

[Service]
Type=simple
WorkingDirectory=/srv/clawfactory/bot_repos/%i/approved
EnvironmentFile=/srv/clawfactory/secrets/%i/gateway.env
ExecStart=/usr/bin/node dist/index.js gateway --port 18789 --bind lan
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
SVC

        # ---- Systemd unit: ClawFactory Controller ----
        cat > \$MNT/etc/systemd/system/clawfactory-controller.service <<'SVC'
[Unit]
Description=ClawFactory Controller
After=network.target

[Service]
Type=simple
WorkingDirectory=/srv/clawfactory/controller
EnvironmentFile=/srv/clawfactory/secrets/%i/controller.env
Environment=APPROVED_DIR=/srv/clawfactory/bot_repos/%i/approved
Environment=OPENCLAW_HOME=/srv/clawfactory/bot_repos/%i/state
Environment=AUDIT_LOG=/srv/clawfactory/audit/audit.jsonl
Environment=SNAPSHOTS_DIR=/srv/clawfactory/snapshots/%i
Environment=AGE_KEY=/srv/clawfactory/secrets/%i/snapshot.key
ExecStart=/usr/bin/python3 -m uvicorn main:app --host 0.0.0.0 --port 8081
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
SVC

        # ---- Systemd unit: ClawFactory Proxy (nginx) ----
        # nginx is already installed as a system service; we just
        # need to drop our config into place at runtime (fc_sync handles this)

        # ---- Default nginx config for ClawFactory ----
        cat > \$MNT/etc/nginx/sites-available/clawfactory <<'NGINX'
server {
    listen 80;
    server_name _;

    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
    proxy_connect_timeout 60s;

    location / {
        proxy_pass http://127.0.0.1:18789;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection \"upgrade\";
        proxy_buffering off;
        proxy_cache off;
    }

    location /health {
        proxy_pass http://127.0.0.1:18789/health;
    }
}

server {
    listen 8080;
    server_name _;

    location / {
        proxy_pass http://127.0.0.1:8081;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    location /health {
        proxy_pass http://127.0.0.1:8081/health;
    }
}
NGINX

        # Enable ClawFactory site, disable default
        rm -f \$MNT/etc/nginx/sites-enabled/default
        ln -sf /etc/nginx/sites-available/clawfactory \$MNT/etc/nginx/sites-enabled/clawfactory

        umount \$MNT
    "
    ok "Rootfs built with Node.js, Python, nginx, Docker, and ClawFactory services"
}

# ============================================================
# Teardown
# ============================================================
teardown() {
    info "Tearing down Firecracker sandbox..."

    if limactl list -q 2>/dev/null | grep -q "^${LIMA_VM_NAME}$"; then
        # Kill socat port forwarders
        lima_root "pkill -f 'socat.*172\.16\.0\.2' 2>/dev/null || true" 2>/dev/null || true

        # Stop Firecracker VM
        lima_root "
            if [ -f ${FC_DIR}/fc.pid ]; then
                kill \$(cat ${FC_DIR}/fc.pid) 2>/dev/null || true
                rm -f ${FC_DIR}/fc.pid /tmp/firecracker.socket
            fi
            # Kill any orphaned firecracker processes
            pkill -f firecracker 2>/dev/null || true
            # Clean up TAP device
            ip link del ${FC_TAP_DEV} 2>/dev/null || true
            # Clean up NAT rules
            iptables -t nat -F 2>/dev/null || true
            iptables -F FORWARD 2>/dev/null || true
        " 2>/dev/null || true

        # Stop and delete the Lima VM
        limactl stop "$LIMA_VM_NAME" 2>/dev/null || true
        limactl delete "$LIMA_VM_NAME" 2>/dev/null || true
        ok "Lima VM '${LIMA_VM_NAME}' deleted"
    else
        ok "Lima VM not found"
    fi

    # Clean up generated secrets
    if [[ -f "$FC_PASSWORD_FILE" ]]; then
        rm -f "$FC_PASSWORD_FILE"
        ok "Removed secrets/firecracker.password"
    fi
    if [[ -f "$FC_SIZING_FILE" ]]; then
        rm -f "$FC_SIZING_FILE"
        ok "Removed secrets/firecracker.sizing"
    fi

    ok "Firecracker sandbox fully cleaned up"
}

# ============================================================
# Main
# ============================================================
main() {
    echo ""
    echo "=========================================="
    echo " ClawFactory Firecracker Sandbox Setup"
    echo "=========================================="
    echo ""

    if [[ "$(uname)" != "Darwin" ]]; then
        die "This setup script is for macOS only (Lima + VZ framework)"
    fi

    generate_password
    compute_sizing

    install_lima
    create_lima_vm
    install_firecracker
    prepare_kernel
    build_rootfs

    echo ""
    ok "=== Firecracker sandbox provisioned ==="
    echo ""
    echo "  Lima VM:     ${LIMA_VM_NAME} (${LIMA_CPUS} CPUs, ${LIMA_MEMORY})"
    echo "  Firecracker: v${FC_VERSION} (${FC_VCPUS} vCPUs, $(( FC_MEM_MIB / 1024 )) GiB)"
    echo "  Rootfs:      $(( ROOTFS_SIZE_MB / 1024 )) GiB ext4"
    echo "  Guest IP:    ${FC_GUEST_IP}"
    echo "  VM password: secrets/firecracker.password"
    echo ""
    echo "  Start with:  ./clawfactory.sh -i <instance> start"
    echo "  SSH into VM: ./clawfactory.sh firecracker ssh"
    echo ""
}

case "${1:-setup}" in
    setup)    main ;;
    teardown) teardown ;;
    *)
        echo "Usage: $0 [setup|teardown]"
        echo ""
        echo "  setup     Provision Lima VM + Firecracker rootfs (default)"
        echo "  teardown  Remove everything"
        exit 1
        ;;
esac
