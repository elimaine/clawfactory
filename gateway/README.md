# Gateway Sandbox Layer

This directory contains the Docker configuration for enabling OpenClaw sandbox support via Sysbox.

## Overview

When sandbox mode is enabled, the gateway container runs with Docker-in-Docker (DinD) capabilities provided by Sysbox. This allows OpenClaw to create isolated containers for tool execution.

## Files

- `Dockerfile` - Extends the base OpenClaw image with Docker daemon
- `sandbox-entrypoint.sh` - Entrypoint script that starts dockerd and then OpenClaw

## How It Works

1. The base gateway image is built from `bot_repos/{instance}/code/Dockerfile`
2. This sandbox layer adds Docker to that image
3. Sysbox runtime provides secure nested container isolation
4. The entrypoint starts dockerd before running the OpenClaw gateway

## Requirements

- Sysbox installed on the host: https://github.com/nestybox/sysbox
- Verify: `docker info | grep -i sysbox`

## Enable/Disable

```bash
./clawfactory.sh sandbox enable   # Enable sandbox mode
./clawfactory.sh sandbox disable  # Disable sandbox mode
./clawfactory.sh rebuild          # Rebuild after changing
```

## Security

Sysbox provides:
- User namespace isolation (root in container = unprivileged on host)
- No privileged mode required
- Complete isolation between inner and outer Docker
- Secure execution of untrusted code in nested containers
