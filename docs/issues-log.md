# Issues Log

This log was created during the documentation rewrite on 2026-04-25. It records issues found while reading the codebase, not a complete audit.

## Open

1. `install.sh` can overwrite the current launcher with stale code.
   - Evidence: `create_helper()` embeds a simplified `clawfactory.sh` that lacks current Lima, snapshot, update, init, delete, mount, tunnels, and sync commands.
   - Impact: running the installer after these features were added can regress the local launcher.
   - Suggested fix: stop generating `clawfactory.sh` from an embedded heredoc, or make the heredoc source the tracked launcher/template.

2. Docker controller image is missing required Python dependencies.
   - Evidence: `controller/traffic_log.py` imports `cryptography.fernet`, but `controller/requirements.txt` does not include `cryptography`.
   - Impact: the Docker controller can fail at import time if `cryptography` is not installed transitively.
   - Suggested fix: add `cryptography` explicitly. Add `mitmproxy` only if Docker mode should support MITM capture.

3. API tests reference routes that do not exist.
   - Evidence: `tests/test_controller_api.py` calls `/branches` and `/local-changes`; `controller/main.py` has no matching route decorators.
   - Impact: API tests fail against the current controller.
   - Suggested fix: either implement the routes or remove/update the tests.

4. GitHub PR promotion is prompted by installer but not implemented in the controller.
   - Evidence: installer configures webhook settings and mentions PR workflow; `controller/main.py` has no `/webhook/github` route or branch promotion endpoints.
   - Impact: operators can configure a feature that cannot receive webhooks.
   - Suggested fix: remove installer prompts until implemented, or implement and document the full flow.

5. `/internal/snapshot` endpoints are unauthenticated despite comments implying local-only use.
   - Evidence: `internal_snapshot_create()` and `internal_snapshot_list()` do not call `check_internal_auth()`.
   - Impact: if the controller is exposed, unauthenticated callers can create and list snapshots.
   - Suggested fix: require `GATEWAY_INTERNAL_TOKEN` or `CONTROLLER_API_TOKEN`; optionally enforce loopback or private network checks.

6. Docker `/gateway/rebuild` likely uses the wrong working directory.
   - Evidence: in Docker mode it runs compose from `CODE_DIR.parent.parent.parent`; with `CODE_DIR=/srv/bot/code`, that resolves to `/`.
   - Impact: controller-triggered gateway rebuild may fail in Docker mode.
   - Suggested fix: pass the ClawFactory root path into the controller or mount the compose root intentionally.

7. `/audit` and `/status` are unauthenticated even when controller auth is configured.
   - Evidence: `get_audit()` and `status()` do not call `check_auth()`.
   - Impact: exposed controllers can leak audit events and instance status without a token.
   - Suggested fix: keep `/health` open but require controller auth for `/audit` and optionally `/status`.

8. Docker Compose has no Temporal service, but the launcher always prints a Temporal URL.
   - Evidence: `docker-compose.yml` defines no Temporal service; `print_urls()` always prints `http://localhost:8082`.
   - Impact: Docker-mode operators see a dead URL.
   - Suggested fix: print Temporal only in Lima mode or add a Docker Temporal service.

9. LLM traffic logging behavior differs sharply by mode.
   - Evidence: Docker mode rewrites Anthropic, OpenAI, and Gemini base URLs to `llm-proxy`; Lima mode explicitly removed those rewrites in `sandbox/lima/vm.sh`.
   - Impact: an operator may assume all provider calls are logged when only some Docker providers are, and Lima providers are not routed by default.
   - Suggested fix: make capture mode explicit in the UI/docs and expose a per-provider config helper.

10. Environment examples are stale.
   - Evidence: `secrets/gateway.env.example` mentions Gemini embeddings and `OLLAMA_HOST`; current installer writes memory search into `openclaw.json` and writes `OLLAMA_BASE_URL`.
   - Impact: manual setup can produce configs that differ from installer output.
   - Suggested fix: update examples after settling token names and current provider support.

11. Host-side snapshot script is legacy.
    - Evidence: `scripts/snapshot.sh` uses older filename assumptions and does not cover controller browse/edit/save, named snapshot management, Lima sync, or current prune rules.
    - Impact: users can pick a weaker path when the controller snapshot flow is the supported path.
    - Suggested fix: mark it legacy or refactor it to call the controller API.

12. `install.sh` requires Docker before the user can choose Lima.
    - Evidence: `preflight()` requires `docker` and `docker info` before `configure_sandbox()`.
    - Impact: a macOS Lima-only install can be blocked by Docker Desktop not running.
    - Suggested fix: move Docker runtime checks after sandbox selection or make Lima setup independent.

13. Repository-local config files are tracked despite being generated/local.
    - Evidence: `.env` and `.clawfactory.conf` are tracked, while `.gitignore` now ignores `*.env`.
    - Impact: local defaults can leak into other clones and confuse instance selection.
    - Suggested fix: keep examples tracked and untrack generated local config.

14. Snapshot decrypt/extract uses shell pipelines.
    - Evidence: `open_snapshot_workspace()` and `restore_snapshot()` call `subprocess.run(..., shell=True)` around `age | tar`.
    - Impact: current snapshot names are mostly constrained by creation/rename paths, but shell pipelines increase hardening burden.
    - Suggested fix: use `subprocess.Popen` pipelines with argument arrays.

15. Sysbox gateway entrypoint assumes `sudo` is available.
    - Evidence: `gateway/sandbox-entrypoint.sh` starts `sudo dockerd`; `gateway/Dockerfile` does not install `sudo`.
    - Impact: Sysbox mode can fail if the base OpenClaw image does not include sudo.
    - Suggested fix: install sudo in the wrapper image or start dockerd as root before dropping privileges.

16. ~~Proposal overlay hooks exist without a tracked CLI implementation.~~ **RESOLVED 2026-04-25.** The `proposals` subcommand now exists in `clawfactory.sh` (dispatcher + `lima_proposals` helpers in `vm.sh`); the three `/agent/system/*` endpoints are wired to write `state/proposals.json` and the host-side `approve` flow promotes entries to `secrets/<inst>/*.env` or `sandbox/lima/setup-extras.sh`. Smoke-tested end to end on sandy.

17. ~~Agent system endpoints take immediate effect before approval.~~ **RESOLVED 2026-04-25 — intended by design.** The two-layer model (immediate VM mutation + accumulating host-approval TODO) is the explicit goal: agents must be usable remotely without host-side coordination per install. Approval gates only host-artifact persistence, not in-VM execution. Tradeoff accepted by operator (Eli) during planning.

18. CLI tests are not hermetic and assert older launcher behavior.
    - Evidence: `tests/test_clawfactory_sh.sh` expects unknown commands to show help before instance validation, expects `list` output to include `instance` or `container`, and hard-codes a `testbot` instance for `info` and `controller`.
    - Impact: the suite fails on a real checkout with only the `sandy` instance and current validation behavior.
    - Suggested fix: create a temporary fixture instance for tests or rewrite assertions around current `bots/list` output and validation rules.

## Documentation Actions Taken

- Replaced implementation docs with code-grounded docs.
- Removed stale TODO docs from `docs/` that described old plans rather than shipped behavior.
- Added this issues log as the place for implementation gaps discovered during docs work.
