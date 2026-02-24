# ClawFactory
<img width="853" height="497" alt="Screenshot 2026-02-24 at 2 15 17 PM" src="https://github.com/user-attachments/assets/9cea71cf-061d-4353-8d9c-77fc80ac6353" />

> **Status**: Likely unstable for new installs. I haven't tested the newer openclaw commandline install flow, new users will likely need to either ssh into the lima to complete install or use the lima cli. This was built on latest osx on m4 apple silicon. I also recommend you use tails and forward ports to your tailnet so you can access the gateway and controller (and killswitch!) from your phone anywhere.
>
> Is this a vibecoded mess? You got me there. Is it still helpful? Absolutely

## NO FLUFF "What this is actually good for"
- Installing openclaw for local use in a lima VM on mac. I have included solutions to sync to and from the VM to make managing your agent less of a hassle in a VM (which is a very good idea for the security layer!).
- The added controller can be used as a non code way of managing your openclaw gateway. Main features of the controller is the restart gateway button (important since openclaw made it so by default gateway cant restart itself anymore, a good idea), as well as memory snapshots (probably my favorite feature). Some features may not be currently working (like mitm web traffic capture), or are working poorly like the snapshot file editor and temporal integration. As far as I know there aren't any super difficult blockers to fixing those.. I just ran out of claude credits and haven't came around to fixing those. If you need them fixing it and setting it up for your use cases probably wont be difficult (plz open PR!). The controller is also a great scaffolding for whatever you want to add to your own openclaw. 
- Multiple running openclaw instances / VMs has not been tested a while, probably some wires crossed somewhere.. no promises.
- Security still has a lot of tradeoffs.. I would not use this in production yet.
- Syncing, controller config chages, and snapshoting have the worst kinks worked out but can still be fussy for edge cases. Keep an eye out for ballooning snapshot sizes and add appropriate filetype ignores to snapshot creation.

OK heres the rest of the docs, hopefully not out of date..

## Vms all the way down.

This project has 3 modes: 

- nested virtualization (recommended, osx apple silicon implemented)
- single layer virtualization, openclaw gateway sandboxed
- and no virtualization, with agents sandboxed through openclaw

Pulls from latest version of openclaw but you can also swap in your workspace.

Your agents talk through channels (Discord, Telegram, Slack, and more via OpenClaw extensions), but every meaningful change flows through the controller. Soft lock on bot promotion, need to be taught to push version proposals. No silent mutations should last long term. Humans hold the merge to long term state.

## Launch Sequence

```bash
git clone https://github.com/elimaine/clawfactory
cd clawfactory
./install.sh              # Interactive setup — provisions your first agent
./clawfactory.sh start    # Bring systems online
./clawfactory.sh info     # Display access credentials
```

Once online:
- **Gateway UI**: http://localhost:18789?token=YOUR_GATEWAY_TOKEN
- **Mission Control**: http://localhost:8080/controller?token=YOUR_CONTROLLER_TOKEN

The installer generates all auth tokens automatically. Run `./clawfactory.sh info` to reveal them.

## Requirements

- API keys for your chosen AI providers — see the [API Key Guide](docs/API-KEYS.md)

## How It Works

Three subsystems behind a reverse proxy, each with a distinct role:

| Subsystem | Function |
|-----------|----------|
| **Proxy** (nginx) | Front door — routes traffic on :18789 and :8080 |
| **Gateway** (OpenClaw) | The agent brain — channels, LLM calls, tool execution, memory |
| **Controller** (FastAPI) | Mission control — webhooks, promotion authority, snapshots, device pairing |

The bot proposes changes via git branches. Humans approve via PR merge or the Controller UI. The gateway cannot promote itself — ever.

## Containment Protocols

These invariants hold at all times, no exceptions:

1. Bots propose via git branches — they cannot merge to main
2. No self-promotion — requires human merge or explicit UI action
3. Sandbox boundaries are enforced (Sysbox on Linux, Lima VM on macOS)
4. Chat messages carry zero authority
5. The kill switch always wins
6. All sensitive state is encrypted at rest (age encryption)
7. Private keys never touch GitHub
8. Tool execution is isolated when sandbox mode is active

## Emergency Shutdown

If an agent goes sideways, pull the plug instantly:

```bash
./killswitch.sh lock      # Everything stops. Now.
./killswitch.sh restore   # Bring systems back after review
```

## Field Manual

- [Setup Guide](docs/setup.md) — Installation, secrets, remote access
- [Architecture](docs/architecture.md) — System diagrams, data flow, promotion pipeline
- [Commands](docs/commands.md) — Full CLI reference
- [Sandbox](docs/sandbox.md) — Containment modes (none, Sysbox, Lima VM)
- [Snapshots](docs/snapshots.md) — Encrypted backups, memory systems, bot state
- [Controller API](docs/controller-api.md) — Endpoints and the config editor
- [API Keys](docs/API-KEYS.md) — Provider key setup

## License

MIT
