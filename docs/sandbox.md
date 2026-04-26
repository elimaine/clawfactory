# Sandboxing

ClawFactory has three sandbox modes. They are selected by `SANDBOX_MODE` in `.clawfactory.conf`.

```text
lima    Linux VM on macOS, current primary path
sysbox  Docker-in-Docker gateway wrapper through Sysbox
none    plain Docker Compose services
```

The name is historical: `SANDBOX_MODE` controls more than tool sandboxing. In Lima mode it changes the whole deployment model.

## Lima Mode

Lima mode runs services directly in a Linux VM:

- gateway runs as `openclaw-<instance>`;
- controller and helper services run as systemd services;
- Docker runs inside the VM for OpenClaw's own tool sandbox;
- nginx and Lima's VZ networking expose localhost ports to the host;
- optional Tailscale HTTPS serve maps tailnet URLs to local ports.

Start:

```bash
./clawfactory.sh -i <instance> start
```

Provision:

```bash
./clawfactory.sh lima setup
```

The host and VM are synchronized with rsync. Host code is pushed to the VM, but VM snapshots, state, and code changes are pulled back before destructive sync steps.

### Lima Security Shape

Lima mode creates a separate Unix user per instance. It sets:

- `bot_repos/<instance>` owned by that service user;
- `controller.env` root-only;
- `gateway.env`, JSON credential files, and `snapshot.key` readable by the gateway group;
- encrypted MITM traffic logs root-only;
- audit log appendable by services.

The gateway gets Docker group access in the VM so OpenClaw can run its tool sandbox.

### Lima Capture Modes

Two logging paths exist in the VM:

- `clawfactory-llm-proxy` on port `9090`, which is not automatically wired into provider base URLs in current Lima code.
- `clawfactory-mitm`, which is started by the controller capture toggle and uses iptables owner rules to redirect gateway user HTTP/HTTPS traffic to mitmproxy. Captured entries are Fernet-encrypted line by line; the Fernet key is age-encrypted with `snapshot.key`.

## Sysbox Mode

Sysbox mode uses Docker Compose plus `docker-compose.sandbox.yml`.

The override:

- builds `gateway/Dockerfile` on top of the normal OpenClaw gateway image;
- runs the gateway container with `runtime: sysbox-runc`;
- starts Docker inside the gateway container;
- persists Docker data in the `gateway-docker-data` volume.

Use this only on a host with Sysbox installed and working:

```bash
docker info | grep -i sysbox
```

The gateway wrapper tries to create the `openclaw-sandbox:bookworm-slim` image on first boot.

## None Mode

`SANDBOX_MODE=none` uses plain Docker Compose. OpenClaw and ClawFactory still run in containers, but there is no extra VM or Sysbox layer around OpenClaw tool execution.

This mode is useful for local development and simpler deployments, but it is not the strongest isolation model.

## What Sandboxing Does Not Solve

Sandboxing does not replace operator controls:

- keep controller access token-protected if exposed beyond localhost;
- do not put real secrets in bot code or workspace files;
- snapshot before risky updates;
- use the killswitch when behavior looks wrong;
- review `docs/issues-log.md` for current gaps.
