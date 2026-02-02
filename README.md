# ClawFactory

Secure deployment system for OpenClaw AI agents with built-in protection against prompt injection attacks.

## Why ClawFactory?

After experiencing a real-world prompt injection attack that:
- Manipulated our bot's social graph
- Wiped conversation history
- Reset session state
- Destroyed all evidence

We built ClawFactory to prevent this from ever happening again.

## Features

- **Setup Wizard** - Interactive configuration with token validation
- **Config Firewall** - Risk-based blocking of dangerous config changes
- **Content Isolation** - External content is clearly marked as untrusted
- **Action Approvals** - Destructive actions require human approval
- **Audit Logger** - Immutable record of all security-relevant events
- **Snapshot Manager** - Automatic backups with one-command rollback

## Quick Start

```bash
git clone https://github.com/elimaine/clawfactory.git
cd clawfactory
./setup.sh
```

## Documentation

- [Design Document](DESIGN.md) - Architecture and security model
- [Setup Guide](docs/setup.md) - Detailed installation instructions
- [Security Rules](docs/security-rules.md) - Risk classification reference

## Status

**Work in Progress** - Core design complete, implementation starting.

## License

MIT
