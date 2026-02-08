# Sandbox Modes

Every agent needs a containment field. ClawFactory offers three isolation tiers depending on your platform and threat model. The installer auto-detects your system and presents the right options.

| Mode | Platform | Isolation Level | Setup |
|------|----------|-----------------|-------|
| `none` | Any | Container-level only | Default — no extra dependencies |
| `sysbox` | Linux | Docker-in-Docker | Install [Sysbox](https://github.com/nestybox/sysbox) |
| `lima` | macOS (Apple Silicon) | Full virtual machine | `./sandbox/lima/setup.sh` |

## None (Default)

The lightweight option. Services run as standard Docker containers and tools execute directly on the gateway. Works well when you're relying on OpenClaw's own built-in sandbox modes for tool isolation.

## Sysbox (Linux)

Sysbox gives you secure Docker-in-Docker — the gateway container can spawn fully isolated tool containers without exposing the host Docker socket. Clean separation between the agent runtime and whatever tools it decides to run.

```bash
# Install Sysbox
wget https://downloads.nestybox.com/sysbox/releases/v0.6.4/sysbox-ce_0.6.4-0.linux_amd64.deb
sudo dpkg -i sysbox-ce_0.6.4-0.linux_amd64.deb

# Verify it's registered
docker info | grep -i sysbox

# Activate in ClawFactory
./clawfactory.sh sandbox enable
```

## Lima VM (macOS)

The heavy-duty option. The entire agent stack runs as systemd services inside a Lima virtual machine using Apple's VZ framework, which delivers near-native networking speeds. Docker only exists inside the VM for OpenClaw's tool sandbox — your host stays clean.

```
┌─────────────────────────────────────────────────────────────────┐
│  macOS Host                                                     │
│                                                                 │
│  clawfactory.sh ── rsync over SSH ──┐                           │
│                                     ▼                           │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │  Lima VM (VZ framework, Ubuntu 24.04)                     │  │
│  │                                                           │  │
│  │  systemd services:                                        │  │
│  │  ┌─────────┐   ┌──────────────┐   ┌──────────────────┐   │  │
│  │  │  nginx  │──►│   Gateway    │──►│    LLM Proxy     │   │  │
│  │  │  :80    │   │  (OpenClaw)  │   │  :9090           │   │  │
│  │  │  :8081  │─┐ │  :18789      │   │                  │   │  │
│  │  └─────────┘ │ └──────────────┘   │ Anthropic ──► api.anthropic.com
│  │              │                    │ OpenAI   ──► api.openai.com
│  │              │ ┌──────────────┐   │ Gemini   ──► googleapis.com
│  │              └►│  Controller  │   └──────────────────┘   │  │
│  │                │  (FastAPI)   │         │                │  │
│  │                │  :8080       │         ▼                │  │
│  │                └──────────────┘   /srv/clawfactory/      │  │
│  │                                  audit/traffic.jsonl     │  │
│  │                ┌──────────────┐                          │  │
│  │                │  MITM Proxy │  (opt-in, off by default) │  │
│  │                │  mitmproxy  │  iptables REDIRECT        │  │
│  │                │  :8888      │──► audit/traffic.enc.jsonl │  │
│  │                └──────────────┘  (Fernet encrypted)      │  │
│  │  dockerd (tool sandbox only)                             │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                 │
│  VZ auto-forwards ports to macOS localhost                      │
│  [Kill Switch] ── stops Lima VM entirely                        │
└─────────────────────────────────────────────────────────────────┘
```

### File Sync

The host filesystem is **not mounted** into the VM (`mounts: []`). On every `start` or `rebuild`, `clawfactory.sh` syncs files from the host to the VM using rsync over the Lima SSH tunnel:

```
Host                              Lima VM
controller/  ──rsync──►  /tmp/cf-sync/controller/
proxy/       ──rsync──►  /tmp/cf-sync/proxy/         ──sudo rsync──►  /srv/clawfactory/
bot_repos/   ──rsync──►  /tmp/cf-sync/bot_repos/
secrets/     ──rsync──►  /tmp/cf-sync/secrets/
```

State directories use `--update` to preserve VM-side changes. Node modules are excluded from sync and installed inside the VM during the build step.

### Provisioning

One command to build the whole environment. Takes a few minutes the first time — it's downloading an Ubuntu image, booting a VM, and installing the full stack.

```bash
./sandbox/lima/setup.sh            # Build the VM from scratch
./sandbox/lima/setup.sh teardown   # Nuke everything and start fresh
```

### Resource Scaling

During setup, you choose how many agents you want to run concurrently. The VM scales its resources to match:

| Agents | VM RAM | vCPUs |
|--------|--------|-------|
| 1      | 4 GiB  | 3     |
| 2      | 6 GiB  | 4     |
| 3      | 8 GiB  | 5     |

### Multi-Agent Isolation

When you're running a fleet, each agent gets hard boundaries inside the VM:

- Dedicated system user (`openclaw-{instance}`) — agents can't see each other
- Locked-down directories (`chmod 700`) — no cross-instance snooping
- Secrets readable only by root and the owning agent (750/640 permissions)
- Unique gateway port per instance via systemd overrides
- Docker group access for tool sandboxing
- Separate snapshot storage per agent

### Commands

```bash
./clawfactory.sh lima setup        # Provision the Lima VM
./clawfactory.sh lima shell        # Drop into the VM shell
./clawfactory.sh lima status       # VM + service health check
./clawfactory.sh lima teardown     # Tear down the VM entirely
```
