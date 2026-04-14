# ClawFactory
<img width="853" height="497" alt="Screenshot 2026-02-24 at 2 15 17 PM" src="https://github.com/user-attachments/assets/9cea71cf-061d-4353-8d9c-77fc80ac6353" />

> **Status**: 26-4-14 openclaw has made a number of releases around their install flow. The provided speed install may not be sufficient. I suspect install will need to be completed manually with the newer openclaw commandline flow using './clawfactoy -i *instanceName* openclaw'. I recommend claude or opencode to help you get setup if you hit a snag.

## Why use Clawfactory? Harness of Harnesses.

Openclaw is an incredible full stack agent platform and orchestration tool. However, using it and setting it up safetly has some real (and recurring) pain points this software seeks to make easy. 
- OC (Openclaw) Updates often, and often breaks things. Clawfactoy makes them easy with a single button or command.
- OC local security boundary is tricky. Maximize usefulness while minimize harm. Clawfactoy VM in VM allows OC control over itself, while protecting host machine and still allowing for more strict subagent VMs.
- OC is a mercurial software, sometimes a single poor prompt or experiment can wreck your setup. Clawfactory has two ways of keeping your setups solid. Instances and snapshots. Have an agent running just the way you want? Copy it to a new instance to keep it seperate from your experiments. Snapshots can be for more temporary insurance, allowing you to snapshot agent state both using the agent, and also before risky updates so you can roll back.
- The added controller can be used as a non code way of managing your openclaw gateway. Main features of the controller is the restart gateway button (important since openclaw made it so by default gateway cant restart itself anymore, a good idea), as well as memory snapshots (probably my favorite feature). Some features may not be currently working (like mitm web traffic capture), or are working poorly like the snapshot file editor and temporal integration. As far as I know there aren't any super difficult blockers to fixing those.. I just ran out of claude credits and haven't came around to fixing those. If you need them fixing it and setting it up for your use cases probably wont be difficult (plz open PR!). The controller is also a great scaffolding for whatever you want to add to your own openclaw. 

## Vms all the way down.

This project has 3 modes: 

- nested virtualization (recommended, osx apple silicon implemented)
- single layer virtualization, openclaw gateway sandboxed
- and no virtualization, with agents sandboxed through openclaw

Pulls from latest version of openclaw but you can also swap in your workspace.

## Assembly Sequence

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

## Emergency Shutdown

If an agent goes sideways, pull the plug instantly:

Either use the GUI and hit the big red killswitch button or use the CLI:

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
