# Native Architecture Refactor

## Overview

Simplify ClawFactory by running OpenClaw natively on the host instead of containerized, leveraging OpenClaw's built-in Docker sandboxing for tool execution.

## Current Architecture (containerized)

```
Host
└── Docker
    ├── Gateway container (OpenClaw + Sysbox/DinD for nested sandboxing)
    ├── Controller container (credentials, promotions, webhooks)
    └── cloudflared (optional)
```

**Problems:**
- Sysbox doesn't work reliably on all platforms
- Docker-in-Docker complexity
- Multiple layers of containerization

## Proposed Architecture (hybrid)

```
Host OS
├── openclaw (native, user: openclaw)
│   ├── Discord bot
│   ├── LLM client
│   ├── Docker sandbox for tool execution (uses host Docker)
│   └── Reads: brain_ro, Writes: brain_work
│
├── controller (container OR native, user: controller)
│   ├── Holds: GITHUB_TOKEN, webhook secret
│   ├── Webhook endpoint
│   ├── Approval UI
│   ├── Snapshot create/restore
│   └── Can: update brain_ro, restart openclaw
│
└── killswitch.sh (root)
```

## User/Permission Model

```bash
# Users
openclaw   - runs gateway, member of 'docker' group
controller - runs controller, owns promotion credentials

# Directory structure
/srv/agent/
├── brain_ro/           # owner: controller:clawfactory, mode: 755
├── brain_work/         # owner: openclaw:clawfactory, mode: 755
├── state/              # owner: openclaw:clawfactory, mode: 2775 (for snapshots)
├── secrets/
│   ├── controller/     # owner: controller, mode: 700
│   │   └── controller.env  (GITHUB_TOKEN, webhook secret)
│   └── gateway/        # owner: openclaw, mode: 700
│       └── gateway.env (DISCORD_TOKEN, LLM keys)
└── audit/              # append-only logs
```

## What Changes

### Remove
- `gateway/Dockerfile` - no longer needed
- `gateway/sandbox-entrypoint.sh` - no longer needed
- Sysbox requirement
- Docker-in-Docker complexity
- `docker-compose.sandbox.yml`

### Keep
- `controller/main.py` - runs as systemd service or minimal container
- `killswitch.sh` - host-level safety
- Git promotion flow (brain_work → brain_ro)
- Webhook/approval logic
- Snapshot functionality

### Add
- `openclaw.service` - systemd unit for openclaw
- `controller.service` - systemd unit for controller (if not containerized)
- User setup script (create users, groups, permissions)

## Controller Options

### Option A: Keep controller containerized (recommended)
```yaml
services:
  controller:
    image: clawfactory-controller
    volumes:
      - /srv/agent/state:/srv/state:rw
      - /srv/agent/brain_ro:/srv/brain_ro:rw
      - /srv/agent/brain_work:/srv/brain_work:ro
      - /srv/agent/secrets/controller:/srv/secrets:ro
    ports:
      - "127.0.0.1:8080:8080"
```
- Easiest secrets isolation
- Snapshots work via volume mounts
- Can restart openclaw via Docker socket or systemd dbus

### Option B: Native controller with separate user
```bash
# /etc/sudoers.d/controller
controller ALL=(openclaw) NOPASSWD: /usr/bin/tar, /usr/local/bin/age
controller ALL=(root) NOPASSWD: /bin/systemctl restart openclaw
```
- More Unix-native
- Requires careful permission setup

### Option C: Shared group for state access
```bash
groupadd clawfactory
usermod -aG clawfactory openclaw
usermod -aG clawfactory controller
chown openclaw:clawfactory /srv/agent/state
chmod 2775 /srv/agent/state
```

## Systemd Units

### openclaw.service
```ini
[Unit]
Description=OpenClaw Gateway
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
User=openclaw
Group=openclaw
EnvironmentFile=/srv/agent/secrets/gateway/gateway.env
WorkingDirectory=/srv/agent
ExecStart=/usr/local/bin/openclaw gateway --port 18789
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### controller.service (if native)
```ini
[Unit]
Description=ClawFactory Controller
After=network.target

[Service]
Type=simple
User=controller
Group=controller
EnvironmentFile=/srv/agent/secrets/controller/controller.env
WorkingDirectory=/srv/agent
ExecStart=/usr/bin/python3 /opt/clawfactory/controller/main.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## Migration Steps

1. [ ] Install OpenClaw natively on host
2. [ ] Create users: `openclaw`, `controller`
3. [ ] Set up directory structure with permissions
4. [ ] Move secrets to appropriate locations
5. [ ] Create systemd units
6. [ ] Update controller to restart openclaw via systemctl instead of docker
7. [ ] Test snapshot create/restore with new permissions
8. [ ] Update install.sh for native setup option
9. [ ] Update killswitch.sh for native services
10. [ ] Remove Sysbox/DinD related code

## Security Considerations

- openclaw has Docker access (docker group) - can spawn any container
- Critical credentials (GITHUB_TOKEN) stay with controller user
- Bot cannot self-promote (controller owns brain_ro)
- Kill switch still works at host level
- Audit logging unchanged

## Open Questions

- [ ] How does openclaw's native Docker sandbox configure containers? (network, mounts, caps)
- [ ] Does controller need Docker socket access for anything other than gateway restart?
- [ ] Should we support both containerized and native installs?
