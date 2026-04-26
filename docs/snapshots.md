# Snapshots

Snapshots are the recovery system for bot state. They are age-encrypted tar archives stored under:

```text
snapshots/<instance>/
```

The private key lives at:

```text
secrets/<instance>/snapshot.key
```

Keep that key safe. Without it, snapshots cannot be decrypted.

## What Is Included

Controller-created snapshots package `OPENCLAW_HOME`, which is normally:

- Docker mode: `/srv/bot/state`
- Lima mode: `/srv/clawfactory/bot_repos/<instance>/state`

That state includes runtime config, memory indexes, paired devices, credentials, agent state, and workspace state managed by OpenClaw.

## What Is Excluded

The controller excludes generated or bulky data such as:

- temp files;
- session JSONL logs;
- `installed`;
- nested `.git` directories;
- `node_modules`;
- Python virtualenvs;
- `__pycache__`;
- `subagents`;
- `media`;
- SQLite WAL files and selected auth/cache logs.

The intent is to preserve recoverable state, not dependency caches.

## Create And List

```bash
./clawfactory.sh -i <instance> snapshot create
./clawfactory.sh -i <instance> snapshot create before-upgrade
./clawfactory.sh -i <instance> snapshot list
```

Controller filenames use this shape:

```text
snapshot--YYYY-MM-DDTHH-MM-SSZ.tar.age
before-upgrade--YYYY-MM-DDTHH-MM-SSZ.tar.age
```

The controller also updates `latest.tar.age` as a symlink to the newest created snapshot.

## Restore

```bash
./clawfactory.sh -i <instance> snapshot restore latest
./clawfactory.sh -i <instance> snapshot restore <filename>
```

Restore behavior:

- stops the gateway;
- moves current state aside to a timestamped backup directory;
- decrypts and extracts the snapshot;
- migrates Docker-era paths when restoring into Lima;
- fixes ownership in Lima mode;
- restarts the gateway.

If extraction fails, the controller attempts to move the backup state back.

## Rename And Delete

```bash
./clawfactory.sh -i <instance> snapshot rename <filename> <new-name>
./clawfactory.sh -i <instance> snapshot delete <filename>
./clawfactory.sh -i <instance> snapshot delete all
```

Snapshot labels are sanitized to letters, numbers, hyphens, and underscores. Deleting `latest` directly is blocked; delete the actual target file instead.

## Browse And Edit

The controller API can open a snapshot into a temporary workspace:

- open snapshot;
- list files;
- read, write, upload, delete, rename, duplicate files;
- download files or directories;
- save the modified workspace as a new encrypted snapshot.

Temporary workspaces live under `/tmp/cf-snapshot-edit-<uuid>` and are cleaned up after one hour or on controller shutdown.

## Lima Sync

Lima mode treats the VM as the live runtime and the host as the durable operator copy. The launcher pulls VM snapshots to the host before sync and stop operations:

```bash
./clawfactory.sh -i <instance> snapshot pull
./clawfactory.sh -i <instance> snapshot autopull enable
```

Host auto-pull uses a LaunchAgent every five minutes.

## Legacy Script

`scripts/snapshot.sh` is an older host-side snapshot helper. The supported operational path is the controller-backed `./clawfactory.sh snapshot ...` commands.
