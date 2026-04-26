# Commands

All commands are run from the repository root.

```bash
./clawfactory.sh [-i <instance>] <command>
```

The instance comes from `-i`, `--instance`, or `.clawfactory.conf`.

## Lifecycle

```bash
./clawfactory.sh -i <instance> start
./clawfactory.sh -i <instance> stop
./clawfactory.sh -i <instance> restart
./clawfactory.sh -i <instance> rebuild
./clawfactory.sh -i <instance> status
```

In Lima mode, these commands manage the VM-side systemd services. In Docker mode, they manage the compose stack.

`rebuild` in Lima mode syncs, builds, optionally restores a snapshot, then restarts services. `rebuild` in Docker mode rebuilds compose images with `--no-cache`.

## Logs And Shells

```bash
./clawfactory.sh -i <instance> logs gateway
./clawfactory.sh -i <instance> logs controller
./clawfactory.sh -i <instance> logs proxy
./clawfactory.sh -i <instance> logs temporal
./clawfactory.sh -i <instance> logs worker
./clawfactory.sh -i <instance> shell
```

Docker mode follows `docker logs` for `clawfactory-<instance>-<service>`. Lima mode follows `journalctl` for systemd units.

## Controller And Audit

```bash
./clawfactory.sh -i <instance> controller
./clawfactory.sh -i <instance> audit
./clawfactory.sh -i <instance> info
./clawfactory.sh bots
```

`controller` prints the local controller URL, including the token when known. `info` prints mode, ports, and saved tokens. `bots` lists local instance folders, whether secrets and snapshots exist, and running service status.

## Snapshots

```bash
./clawfactory.sh -i <instance> snapshot list
./clawfactory.sh -i <instance> snapshot create [name]
./clawfactory.sh -i <instance> snapshot rename <filename> <new-name>
./clawfactory.sh -i <instance> snapshot delete <filename>
./clawfactory.sh -i <instance> snapshot delete all
./clawfactory.sh -i <instance> snapshot restore [filename|latest]
```

Lima-only snapshot sync helpers:

```bash
./clawfactory.sh -i <instance> snapshot pull
./clawfactory.sh -i <instance> snapshot autopull enable
./clawfactory.sh -i <instance> snapshot autopull disable
./clawfactory.sh -i <instance> snapshot autopull status
```

Snapshot actions call controller endpoints. The older `scripts/snapshot.sh` is a host-side helper and does not cover every controller snapshot feature.

## OpenClaw CLI

```bash
./clawfactory.sh -i <instance> openclaw <args>
./clawfactory.sh -i <instance> openclaw onboard
```

Docker mode runs inside the gateway container. Lima mode runs as the per-instance service user in the VM with the instance state directory and gateway env loaded.

## Updates

```bash
./clawfactory.sh -i <instance> update
./clawfactory.sh -i <instance> update --merge
```

Default update resets the bot source to `upstream/main`, then restores selected local files. `--merge` uses a merge flow for instances that carry source patches.

## Instance Management

```bash
./clawfactory.sh init
./clawfactory.sh -i <instance> delete
```

`init` can create a fresh OpenClaw instance or clone an existing local instance. `delete` removes the instance's local code, secrets, and snapshots, and removes the VM-side data in Lima mode.

## Lima-Specific Commands

```bash
./clawfactory.sh lima setup
./clawfactory.sh lima shell
./clawfactory.sh lima status
./clawfactory.sh lima teardown
./clawfactory.sh -i <instance> sync
./clawfactory.sh -i <instance> sync watch
./clawfactory.sh -i <instance> config
./clawfactory.sh -i <instance> config --jq '<jq-filter>'
./clawfactory.sh -i <instance> code pull
./clawfactory.sh -i <instance> tunnels status
./clawfactory.sh -i <instance> mount list
```

`sync` pushes controller/proxy/code/secrets into the VM and restarts controller plus gateway. `sync watch` requires `fswatch`.

`config` edits the live VM `openclaw.json`, validates JSON, pushes it back, and preserves ownership.

## Emergency Stop

```bash
./killswitch.sh lock
./killswitch.sh restore
```

In Lima mode, `lock` stops the Lima VM. In Docker mode, `restore` brings compose services back up. The controller also has a `/killswitch` endpoint that snapshots, signals the host-side Lima watcher, and stops the gateway.
