# Harden OpenClaw Update Process

## Problem

`clawfactory.sh update` uses `git merge` which accumulates stale local files
that upstream deleted/moved. These cause runtime `ReferenceError`s when they
reference symbols upstream refactored away. The build doesn't fail because TS
errors are suppressed, so broken code deploys silently.

## Fix: Reset-and-overlay instead of merge

The local fork only carries `workspace/`, `agents/`, `config/`, and `SOUL.md`.
No source patches. Replace merge with:

```bash
prev_head=$(git rev-parse HEAD)
git fetch upstream
git reset --hard upstream/main
git checkout "$prev_head" -- workspace/ agents/ config/ SOUL.md 2>/dev/null || true
git add -A && git commit --no-verify -m "Update to upstream $(git rev-parse --short upstream/main)"
```

Safe — `node_modules/`, `dist/`, `npx` cache (`~/.npm/_npx/`), and
`state/extensions/` are all outside the tracked tree.

Add `--merge` flag to fall back to old behavior if someone adds local source patches.

## Fix: Build gate with rollback

`lima_build` should propagate `pnpm build` exit code. On failure, reset to
previous commit and rebuild:

```bash
if ! lima_build; then
    echo "Build failed — rolling back"
    git -C "$repo" reset --hard "$prev_head"
    lima_build
    lima_services restart
    exit 1
fi
```

## Scope

- `clawfactory.sh` `update)` case (~line 520): replace merge logic, add rollback
- `sandbox/lima/vm.sh` `lima_build()`: propagate build exit code
