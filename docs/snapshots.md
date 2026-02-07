# Snapshots & State

## Encrypted Snapshots

Think of snapshots as cryogenic backups for your agent. They capture everything that makes the bot *itself* — the stuff that isn't tracked in git but would be devastating to lose:

- Vector embeddings database (`memory/main.sqlite`)
- Runtime configuration (`openclaw.json`)
- Paired devices and credentials
- Device identity keys

**Not captured** (rebuilt automatically from git on startup):
- `installed/` — npm packages declared in `{instance}_save/package.json`

### Usage

```bash
# Generate an encryption keypair (once per agent)
./clawfactory.sh snapshot keygen

# Freeze current state into an encrypted archive
./clawfactory.sh snapshot create

# Browse available snapshots
./clawfactory.sh snapshot list

# Restore from a snapshot (shut down the agent first!)
./clawfactory.sh stop
./clawfactory.sh snapshot restore latest
./clawfactory.sh start
```

The agent can also trigger its own snapshots through the Controller API:
```bash
curl -X POST http://localhost:8080/snapshot
```

## Memory Systems

Your agent builds up memory over time — daily journals, long-term recall, and vector embeddings for semantic search. All of it lives in `state/` and gets swept into encrypted snapshots.

| Type | Location | Backup Method |
|------|----------|---------------|
| Daily journals | `state/workspace/memory/YYYY-MM-DD.md` | Encrypted snapshots |
| Long-term memory | `state/workspace/MEMORY.md` | Encrypted snapshots |
| Vector embeddings | `state/memory/main.sqlite` | Encrypted snapshots |

Memory stays in `state/` rather than git for good reasons:
- The vector database needs real filesystem access for indexing
- Constant memory updates would flood git history with noise
- Encrypted snapshots keep the agent's thoughts private

## Bot Save State

Agents can declare persistent state they want to carry across restarts. The `{instance}_save/` directory is their designated cargo hold:

```
workspace/{instance}_save/
├── package.json        # npm dependencies (auto-installed on startup)
├── config.json         # Agent-specific configuration
└── tools/              # Scripts the agent has written for itself
```

Changes to save state go through the standard promotion pipeline — the agent can't just mutate its own config unchecked:

1. Agent edits files in `{instance}_save/`
2. Agent commits and pushes a branch
3. Agent opens a PR
4. Human reviews and merges
5. Gateway restarts and picks up the new state
