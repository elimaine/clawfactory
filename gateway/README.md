# Gateway Sandbox Wrapper

This directory is used only for Docker Sysbox mode. It is not the normal Lima path.

`docker-compose.sandbox.yml` rebuilds the gateway image from this directory, using the normal OpenClaw gateway image as its base:

```bash
docker compose -f docker-compose.yml -f docker-compose.sandbox.yml build gateway
```

## What It Adds

`gateway/Dockerfile` installs Docker Engine inside the gateway image, adds the `node` user to the Docker group, and copies `sandbox-entrypoint.sh`.

`sandbox-entrypoint.sh` tries to:

- start `dockerd` inside the container;
- create or reuse `openclaw-sandbox:bookworm-slim`;
- sync workspace files from `/workspace/code/workspace` into `/home/node/.openclaw/workspace`;
- keep memory in state rather than code;
- install packages from `workspace/<instance>_save/package.json` when the package hash changes;
- run OpenClaw non-interactive onboarding;
- start `node dist/index.js gateway --port 18789 --bind lan`.

## Requirements

The host must have Sysbox installed and Docker must report the runtime:

```bash
docker info | grep -i sysbox
```

The compose override sets:

```yaml
runtime: sysbox-runc
```

The wrapper is intended to let OpenClaw create nested sandbox containers without privileged Docker-in-Docker.

## Caveat

The entrypoint currently starts Docker with `sudo dockerd`, but this wrapper Dockerfile does not install `sudo`. It works only if the base OpenClaw image already provides sudo. This is tracked in [../docs/issues-log.md](../docs/issues-log.md).
