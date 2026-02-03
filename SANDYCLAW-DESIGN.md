⸻

Design Document

Local-First Agent System with Discord DM Control, GitHub Auto-Promote, Offline Mode, Dual Isolation, Cloudflare Zero Trust Ingress, AI Gateway Egress, and Host Kill Switch

⸻

1. Purpose & Philosophy

This system is a local-first autonomous agent runtime that:
	•	Runs primarily on a local VM using Docker
	•	Uses Discord DMs as the human interaction surface
	•	Uses GitHub PRs as the authoritative promotion mechanism when online
	•	Continues to function fully offline
	•	Enforces hard separation between proposal and authority
	•	Can be hard-stopped at the host level via a kill switch
	•	Integrates with Cloudflare Zero Trust for inbound access and Cloudflare AI Gateway for outbound LLM traffic in online mode

Core principle

Discord is UI, GitHub / Controller are authority.
The bot may propose, but can never silently promote or persist changes.

⸻

2. High-Level Architecture

Components
	1.	Gateway (container, unprivileged)
	•	Main agent runtime
	•	Connects to Discord
	•	Reads active configuration (brain)
	•	Delegates risky actions to runner
	•	Cannot modify active config
	•	Cannot promote or deploy
	2.	Runner (container, quarantined)
	•	Executes tools (shell, git, file edits)
	•	Writes only to proposal workspace
	•	No Docker socket
	•	No privileged capabilities
	•	No access to active config
	3.	Controller (authority, narrow privilege)
	•	Receives GitHub webhooks
	•	Performs promotions
	•	Restarts gateway
	•	Hosts human-only approval UI (offline)
	•	Exposed only via Cloudflare Zero Trust and/or Tailscale
	•	Never controlled directly by the agent
	4.	cloudflared (online mode only)
	•	Cloudflare Tunnel
	•	Outbound-only ingress for controller UI + webhook
	5.	Optional DB
	•	Audit logs (can be JSONL on host for v1)

⸻

3. Repository Model (GitOps)

Repositories
	•	runtime repo
	•	Gateway, runner, controller code
	•	Rarely changes
	•	Deployed via Docker
	•	brain repo
	•	Prompts (SOUL.md)
	•	Policies
	•	Tool definitions
	•	Agent configuration
	•	Frequently changed

Local Git Layout (on VM)

/srv/agent/
  brain/
    brain.git        # bare repo (canonical local truth)
    brain_ro/        # checked-out active SHA (READ ONLY)
    brain_work/      # writable working tree (proposals)

Rules
	•	brain_ro is the only source used by the gateway
	•	brain_work is the only place the bot may write
	•	Promotion = moving a SHA from brain_work → brain_ro

⸻

4. Authority & Promotion Model

Online Mode (Preferred)

Authority = GitHub merge
	1.	Bot edits brain_work
	2.	Bot creates branch + commit
	3.	Bot opens PR on GitHub (via GitHub App)
	4.	Human merges PR to main
	5.	GitHub webhook → controller
	6.	Controller verifies:
	•	webhook signature
	•	repo + branch
	•	merge event
	•	merge actor is allowed
	7.	Controller:
	•	fetches main into brain.git
	•	updates brain_ro to merged SHA
	•	restarts gateway
	8.	Gateway announces success via Discord DM

The bot never calls “promote” online.

⸻

Offline Mode

Authority = human approval via controller UI
	1.	Bot commits change locally
	2.	Bot DMs diff + SHA
	3.	Human opens controller UI (via Tailscale)
	4.	UI shows diff and provides Promote button (or one-time code)
	5.	Controller promotes SHA and restarts gateway

Discord is transport only. Authority is outside Discord.

⸻

5. Discord Control Plane

Platform
	•	Discord

Rules
	•	DMs only
	•	Hard allowlist by Discord User ID
	•	Bot ignores all other users

Allowed Commands (examples)
	•	status
	•	show diff
	•	open pr
	•	restart
	•	kill
	•	help

Forbidden Actions (without authority)
	•	Promote
	•	Deploy
	•	Rotate secrets
	•	Re-enable network

Identity Enforcement
	•	Every command checks author_user_id ∈ allowed_user_ids
	•	Commands are logged with user ID + timestamp

⸻

6. Isolation Model

Gateway Container
	•	Root filesystem: read-only
	•	Mounts:
	•	/workspace/brain_ro:ro
	•	/tmp (tmpfs)
	•	No write access to:
	•	brain_ro
	•	secrets
	•	host

Runner Container
	•	Mounts:
	•	/workspace/brain_work:rw
	•	/tmp (tmpfs)
	•	Cannot access:
	•	brain_ro
	•	Docker socket
	•	Host network
	•	Used for:
	•	git
	•	file edits
	•	shell tools

Controller
	•	Minimal authority:
	•	update brain_ro
	•	restart gateway
	•	No general shell exposed to bot
	•	UI + webhook endpoints only

⸻

7. Secrets & Install Flow

Secrets Manifest

A single file secrets.yml (chmod 600):

mode: online   # online | offline

discord:
  bot_token: ""
  allowed_user_ids:
    - "123456789012345678"

github:
  app_id: ""
  installation_id: ""
  private_key_pem: |
    -----BEGIN RSA PRIVATE KEY-----
    ...
    -----END RSA PRIVATE KEY-----
  webhook_secret: ""

cloudflare:
  account_id: ""
  ai_gateway_id: ""
  ai_gateway_token: ""

tailscale:
  enabled: true

Install Script Responsibilities
	•	Read secrets.yml
	•	Prompt for missing required values
	•	Skip online-only secrets if mode: offline
	•	Write secrets to Docker secrets (/run/secrets)
	•	Initialize local git repos
	•	Render configs
	•	Start Docker Compose
	•	Smoke-test Discord login

⸻

8. Cloudflare Integration (Online Mode)

Inbound: Zero Trust + Tunnel
	•	Cloudflare Tunnel (cloudflared)
	•	Publishes:
	•	controller-ui.example.com
	•	controller-webhook.example.com
	•	Protected by:
	•	Cloudflare Access (IdP login for UI)
	•	Service token for GitHub webhook
	•	No inbound ports opened on VM

Outbound: AI Gateway
	•	All LLM calls go through Cloudflare AI Gateway
	•	OpenAI-compatible API
	•	Headers:
	•	Authorization: Bearer <provider_key>
	•	cf-aig-authorization: Bearer <cf_token>
	•	Base URL:

https://gateway.ai.cloudflare.com/v1/{account_id}/{gateway_id}/openai



Offline Behavior
	•	Cloudflare services disabled
	•	LLM routed to local provider or disabled

⸻

9. Kill Switch (Host-Level)

Goals
	•	Immediate containment
	•	Zero reliance on containers
	•	No expansion of access during incident

Lock (killswitch lock)
	1.	Stop Docker stack
	2.	Save iptables rules
	3.	Apply firewall:
	•	DROP all inbound/outbound
	•	Allow:
	•	loopback
	•	optional SSH
	•	optional existing tailscale0
	4.	Record state

Restore (killswitch restore)
	1.	Optionally prompt for new secrets (optional)
	2.	Restore iptables
	3.	Restart Docker stack

Rules
	•	Kill switch must not enable Tailscale
	•	Kill switch always overrides bot

⸻

10. Audit & Observability

Minimum logs (JSONL):
	•	Discord commands
	•	PR creation
	•	Promotions (SHA, actor, time)
	•	Restarts
	•	Kill switch events

⸻

11. Non-Negotiable Safety Invariants
	1.	Active brain is immutable at runtime
	2.	Bot cannot promote itself
	3.	Runner cannot escalate to host
	4.	Discord is not an authority signal
	5.	Kill switch always wins
	6.	Offline mode remains functional

⸻

12. Implementation Checklist (for another LLM)
	•	Docker Compose with gateway / runner / controller / cloudflared
	•	Discord DM adapter with user ID allowlist
	•	GitHub App PR + webhook flow
	•	Local GitOps brain promotion logic
	•	Controller UI (offline approval)
	•	Kill switch scripts (lock / restore)
	•	Cloudflare Tunnel config
	•	AI Gateway client config
	•	Install script with prompting

⸻

13. Intentional Open Knobs
	•	Whether offline promotion requires DM code or UI button
	•	Whether SSH allowed during kill switch
	•	Whether local DB used or file logs
	•	Which local LLM provider to use offline

⸻

Final Note

This system is intentionally boring at the authority layer and flexible at the agent layer.
All power is explicit, reviewable, and revocable.

If the bot ever surprises you, it’s a bug.

