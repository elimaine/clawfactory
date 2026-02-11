# Plan Mode — Agent Skill

## Overview

Plan mode is a structured workflow where the agent explores the codebase and designs an implementation approach **before writing any code**. The user must approve the plan before implementation begins.

## When It Triggers

The agent enters plan mode proactively (via `EnterPlanMode`) for non-trivial tasks:

- New feature implementation
- Multi-file changes
- Architectural decisions with multiple valid approaches
- Unclear requirements that need investigation first
- Refactors that affect existing behavior

It is **not** used for trivial tasks (typo fixes, single-line changes, pure research).

## Workflow

```
User Request
    │
    ▼
┌─────────────────────┐
│  EnterPlanMode       │  Agent requests to enter plan mode.
│  (requires consent)  │  User must approve the transition.
└─────────┬───────────┘
          ▼
┌─────────────────────┐
│  Research Phase      │  Agent uses read-only tools:
│                      │  - Glob (find files)
│                      │  - Grep (search contents)
│                      │  - Read (read files)
│                      │  - WebFetch / WebSearch
│                      │  - Task (explore subagents)
│                      │
│  NO editing/writing  │  Edit, Write, NotebookEdit are
│  allowed here.       │  disabled during plan mode.
└─────────┬───────────┘
          ▼
┌─────────────────────┐
│  Clarify (optional)  │  If ambiguous, agent uses
│  AskUserQuestion     │  AskUserQuestion to resolve
│                      │  unknowns before finalizing.
└─────────┬───────────┘
          ▼
┌─────────────────────┐
│  Write Plan          │  Agent writes the plan to a
│  (to plan file)      │  designated file with:
│                      │  - Files to modify
│                      │  - Step-by-step changes
│                      │  - Trade-offs considered
│                      │  - Risks or open questions
└─────────┬───────────┘
          ▼
┌─────────────────────┐
│  ExitPlanMode        │  Agent signals plan is ready.
│                      │  User reviews and either:
│                      │  - Approves → agent implements
│                      │  - Rejects / requests changes
└─────────┬───────────┘
          ▼
┌─────────────────────┐
│  Implementation      │  Agent executes the approved
│                      │  plan using all tools (Edit,
│                      │  Write, Bash, etc.)
└─────────────────────┘
```

## Tool Access by Phase

| Phase          | Glob | Grep | Read | Web | Edit | Write | Bash |
|----------------|------|------|------|-----|------|-------|------|
| Plan Mode      | ✅   | ✅   | ✅   | ✅  | ❌   | ❌    | ✅   |
| Implementation | ✅   | ✅   | ✅   | ✅  | ✅   | ✅    | ✅   |

## Key Properties

- **User consent required** — the agent cannot enter plan mode without the user approving the transition.
- **No code changes during planning** — the agent can only read and research, not modify files. This prevents wasted work on an unapproved approach.
- **Plan is written to a file** — the user reviews the actual plan file contents, not a chat summary.
- **Approval gates implementation** — `ExitPlanMode` is the handoff. Nothing gets built until the user says go.
- **Clarification happens inside plan mode** — if the agent needs input (e.g., "JWT or OAuth?"), it asks via `AskUserQuestion` before finalizing the plan, not after.

## Example Flow

```
User: "Add rate limiting to the API"

Agent: [enters plan mode]
Agent: [reads main.py, checks existing middleware, searches for rate limit patterns]
Agent: [asks: "Redis-based or in-memory? Per-user or per-IP?"]
User:  "In-memory, per-user"
Agent: [writes plan: modify middleware.py, add token bucket class, wire into FastAPI]
Agent: [exits plan mode]
User:  [approves]
Agent: [implements the approved plan]
```
