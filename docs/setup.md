# Setup

This setup doc describes the code as it exists now. It does not assume the older GitHub-promotion plan is working.

## Prerequisites

Common requirements:

- `git`
- `docker`
- `openssl`
- `jq` for several helper commands

Lima mode requirements on macOS:

- Homebrew, used by `sandbox/lima/setup.sh` to install Lima if needed
- `limactl`
- enough memory for the VM sizing selected during setup

Optional tools:

- `age` and `age-keygen` on the host for host-side snapshot scripts
- `gh` for installer-time GitHub repository setup prompts
- `fswatch` for `./clawfactory.sh sync watch`
- Tailscale.app for HTTPS `tailscale serve` shortcuts

Current caveat: `install.sh` still requires Docker to be installed and running before it asks whether you want Lima mode.

## Install A New Instance

```bash
./install.sh
```

The installer asks for:

- instance name;
- channel tokens, currently Discord, Telegram, and Slack;
- provider keys, currently Anthropic, Moonshot/Kimi, OpenAI, Gemini, Ollama, OpenRouter, Brave Search, and ElevenLabs prompts;
- vector memory provider;
- sandbox mode;
- optional GitHub settings.

The installer writes:

- `.clawfactory.conf` and `.env`;
- `secrets/tokens.env`;
- `secrets/<instance>/gateway.env`;
- `secrets/<instance>/controller.env`;
- `secrets/<instance>/snapshot.key` and `snapshot.pub` when `age-keygen` is available;
- initial OpenClaw workspace files when the bot repo does not already have them;
- `bot_repos/<instance>/state/openclaw.json` when no state config exists.

Start the instance:

```bash
./clawfactory.sh -i <instance> start
```

Open the controller URL:

```bash
./clawfactory.sh -i <instance> controller
```

## Existing Instances

List known instances:

```bash
./clawfactory.sh bots
```

Show ports, mode, and tokens for one instance:

```bash
./clawfactory.sh -i <instance> info
```

If no instance is specified, the launcher uses `INSTANCE_NAME` from `.clawfactory.conf`. Most commands reject the implicit `default` instance unless `bot_repos/default` exists.

## Lima Setup

Provision the VM:

```bash
./clawfactory.sh lima setup
```

or:

```bash
./sandbox/lima/setup.sh setup
```

The setup script creates a Lima VM named `clawfactory`, installs Node, pnpm, Python dependencies, nginx, Docker, Temporal CLI, and systemd units. It also records VM sizing in `secrets/lima.sizing`.

Start an instance in Lima mode:

```bash
./clawfactory.sh -i <instance> start
```

What happens on start:

- the VM is started if needed;
- snapshots and code are pulled from VM to host;
- controller, proxy config, secrets, and code are synced host to VM;
- OpenClaw dependencies and builds run in the VM;
- state is restored from the latest snapshot only on cold start when no state exists;
- systemd services are started;
- Tailscale HTTPS serve is configured when Tailscale.app is available.

## Docker Compose Setup

Docker mode uses `docker-compose.yml`:

```bash
SANDBOX_MODE=none ./clawfactory.sh -i <instance> start
```

The compose stack binds gateway and controller to localhost:

- gateway: `127.0.0.1:${GATEWAY_PORT:-18789}`
- controller: `127.0.0.1:${CONTROLLER_PORT:-8080}`

The gateway image is built from `bot_repos/<instance>/code`. The controller image is built from `controller/`.

Sysbox sandbox mode uses `docker-compose.sandbox.yml` as an override and builds the gateway wrapper in `gateway/`.

## Updating OpenClaw

```bash
./clawfactory.sh -i <instance> update
```

Default behavior:

- fetch `upstream/main`;
- reset the bot code checkout to upstream;
- restore selected local-only paths such as `workspace`, `agents`, `config`, and `SOUL.md`;
- rebuild and redeploy.

Use merge mode only when you intentionally carry local source patches:

```bash
./clawfactory.sh -i <instance> update --merge
```

## State Recovery

Create a named snapshot before risky work:

```bash
./clawfactory.sh -i <instance> snapshot create before-update
```

Restore:

```bash
./clawfactory.sh -i <instance> snapshot restore latest
```

In Lima mode, snapshots are pulled back to the host before sync and stop operations. Host copies are in `snapshots/<instance>`.

## Uninstall Or Delete

Delete one instance:

```bash
./clawfactory.sh -i <instance> delete
```

This removes host `bot_repos`, `secrets`, and `snapshots` for that instance. In Lima mode it also removes the VM-side instance directories and service user.

Remove the Lima VM:

```bash
./clawfactory.sh lima teardown
```
