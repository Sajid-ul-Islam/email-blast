# Email Blast — Multi-Agent Coordination

**Version:** 1.0  
**Status:** Active  
**Last updated:** 2026-09-29

---

## 1. Purpose

This document describes how multiple Hermes agent profiles (or humans) collaborate
on the email-blast project without stepping on each other. It is written for the
agents that may be dispatched to work on this repo, and for any human reviewer who
wants to understand the division of responsibility.

---

## 2. Roles and responsibilities

| Role | Profile | Responsibility |
|------|---------|----------------|
| Owner | Human (deenb) | Owns the Gmail account, the app password, the recipient lists, and the production data. Approves credential changes and campaign launches. |
| Implementor | `email-camp` (default) | Builds and ships features, fixes bugs, runs tests, writes docs. This profile is the primary agent on the task. |
| Reviewer | any reviewer profile assigned via `kanban_request_review` | Reviews changes before they land. Checks correctness, security (no leaked creds), and that acceptance criteria are met. |
| Scheduler operator | human or automated | Manages the cronjob/scheduler lifecycle: registering campaign configs, watching runner logs, restarting the scheduler process when it dies. |

---

## 3. Handoff protocol

When an agent finishes a piece of work on email-blast, it leaves a handoff that
contains, at minimum:

1. **What changed** — files modified/created, with a one-line summary of each.
2. **What was verified** — which acceptance criteria were exercised, and how
   (test client, real send, dry-run, etc.).
3. **What is not yet done** — any known gaps, TODOs, or follow-up work.
4. **Anything a reviewer must check** — security-sensitive changes, credential
   handling, new env vars, new file paths.

The handoff is written into the task's `kanban_complete` summary/metadata or into
a `kanban_comment` on the active task. No handoff is considered complete without a
verification note.

---

## 4. Work partitioning

Large features are decomposed before execution. A feature is split when:

- It touches more than one of the subsystem boundaries below, **or**
- It would require more than one focused session to ship and verify.

Subsystem boundaries (a change should usually live in one of these, not straddle):

| Subsystem | Root file(s) | What lives here |
|-----------|--------------|----------------|
| Web UI | `app.py`, `templates/`, `static/style.css` | Routes, templates, CSS, client-side JS, upload/preview/send flow |
| Sending engine | `sender.py` | SMTP logic, `RateLimiter`, `SendResult`, multipart/alternative, retry/backoff, daily cap |
| CLI | `send_emails.py`, `run_sender.sh` | Headless send, dry-run, credential guard |
| Scheduling | `scheduler.py` (future), campaign configs | Cronjob runner, config registry, state persistence |
| Config & secrets | `.env`, `.env.example` | Gmail user + app password (never committed) |
| Data | `uploads/`, `sent_log.csv`, `uploads/jobs/*.json` | Recipient lists, content files, job snapshots, send log |

When a task touches two subsystems, the agent owns the integration point but should
not silently drift implementation details from one subsystem into another. If a
decision must be made at the boundary (e.g. a new env var, a new file layout), the
agent documents the decision in the handoff and, if it is reusable, in `architecture.md`.

---

## 5. Communication

- **Inside one agent run:** the agent reasons internally; it does not narrate every
  step to the user unless the user asked for a status update.
- **Cross-agent:** if a dispatched worker needs something from another profile (e.g.
  the user's Gmail App Password, a decision on cadence), it does not guess. It
  blocks with `kanban_block(reason=...)` and states exactly what is missing.
- **User steering:** if the user sends a mid-turn message (the `[OUT-OF-BAND USER
  MESSAGE]` marker), it overrides whatever the agent was doing. The agent adjusts
  course and does not continue the interrupted work unless the message explicitly
  says to resume.

---

## 6. Security handoff rule

Any change that touches credentials, secrets, or the credential-loading path must
be flagged in the handoff with a `security` note. Examples:

- A new env var is read at runtime → list it in the handoff and add it to
  `.env.example`.
- A file path that holds secrets is added → confirm it is git-ignored.
- A log or JSON snapshot could accidentally capture a secret → confirm it does not.

Reviewers treat a missing security note on a credential-related change as a reason
to request changes.

---

## 7. Review expectations

- A reviewer does not need to re-derive the whole diff. The handoff summary is
  enough to know what to look at.
- If the reviewer finds a real defect, they use `kanban_request_changes` with a
  concrete, actionable reason. "Needs review" without specifics is not actionable.
- If the reviewer approves, they use `kanban_complete` on the review task.

---

## 8. State the agent must preserve across runs

Because email-blast is a small repo with a live `.env`, agents should assume:

- `.env` may or may not contain a real password. The placeholder guard is the
  source of truth for "can we send right now?".
- `uploads/` may contain prior uploads; do not delete them during a build or test
  unless the task explicitly says to clean up.
- `sent_log.csv` is append-only. Do not truncate it as part of a code change.

---

*End of agents.md.*
