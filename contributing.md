# Email Blast — Contribution Guide

**Version:** 1.0  
**Status:** Active  
**Last updated:** 2026-09-29

---

## 1. How to contribute

Email Blast is a small, focused repo. Contributions are welcome in the usual form:
a clear problem or feature description, a branch or patch, and evidence that it
works (tests, dry-runs, real sends where appropriate).

Because the project ships a credential-holding `.env`, contributors must never
commit real secrets. The rules in `rules.md` apply to everyone, including
contributors.

---

## 2. Repo layout

```
email-blast/
├── app.py                  # Flask web app (UI + send orchestration)
├── sender.py               # Sending engine (SMTP, pacing, retry, logging)
├── send_emails.py          # CLI entry point (thin wrapper over sender.py)
├── run_sender.sh           # Shell wrapper with credential guard
├── scheduler.py            # (future) headless campaign scheduler
├── campaign_configs/       # (future) per-campaign JSON configs
├── templates/
│   ├── index.html          # Upload + content + send page
│   ├── preview.html        # Full recipient list preview
│   ├── send_job.html       # Live campaign progress page
│   └── history.html        # Past campaign list
├── static/
│   └── style.css           # Stylesheet
├── uploads/                # Uploaded recipient CSVs + content files (git-ignored)
│   └── jobs/              # Per-campaign job-status JSONs (git-ignored)
├── sample_emails.csv       # Downloadable recipient-list template
├── sent_log.csv            # Append-only per-recipient send log (git-ignored)
├── .env                    # Runtime secrets (git-ignored)
├── .env.example            # Template for .env — safe to commit
├── .gitignore              # Excludes .env, uploads/, logs, pycache
├── prd.md                  # Product requirements
├── agents.md               # Multi-agent coordination
├── design.md               # UI/UX and component design
├── architecture.md         # System architecture
├── rules.md                # Operational and security rules
└── this file               # Contribution guide
```

---

## 3. Development setup

### 3.1 Prerequisites

- Python 3.11+ (the project targets 3.11).
- Standard library modules only for the core engine (`smtplib`, `email`, `csv`,
  `json`, `pathlib`, `threading`, `uuid`, `os`, `re`, `html.parser`).
- Flask for the web app.
- `python-dotenv` for loading `.env` (optional but recommended).

### 3.2 First-time setup

```bash
cd H:\Repo\email-blast
cp .env.example .env
# Edit .env: set GMAIL_USER and GMAIL_APP_PASSWORD (16-char Gmail App Password)
# Leave GMAIL_APP_PASSWORD as the placeholder until you are ready to send.
```

### 3.3 Running the web app

```bash
python app.py
# Opens http://127.0.0.1:5000
```

### 3.4 Running a dry-run (no sends)

```bash
python send_emails.py --dry-run
# Validates credentials presence + list parsing, sends nothing.
```

### 3.5 Running a real send from the CLI

```bash
python send_emails.py
# Uses .env + emails.csv by default.
# Use --list, --subject, --body, --html, --pace-mode, --daily-cap, --dry-run.
```

---

## 4. Testing approach

Because the project sends real emails, the testing strategy is layered:

### 4.1 Unit-style checks (no SMTP)

- Parse recipients from a known CSV and assert the count, dedup, and merge-field
  readiness.
- Parse content from `.txt`, `.json`, `.html` files and assert the subject, body,
  and html_body come out as expected.
- Exercise `RateLimiter` in isolation: daily cap, pacing modes, backoff, retry
  accounting, `estimate_duration_seconds`.

### 4.2 Integration checks (Flask test client, no SMTP)

- Upload a CSV through the test client, assert the preview response shows the
  right counts and that the file landed on disk.
- Upload a content file, assert the `/content` JSON returns the right subject,
  body, and html_body.
- Attempt a send with empty subject/body and assert a 400.
- Attempt a send with a missing list file_id and assert a 404.
- Attempt a send with a placeholder password and assert the send is rejected
  (the credential guard fires before any SMTP connection).

### 4.3 Real-send checks (only with a real `.env`)

- A small list (2-3 addresses you control) with a clear subject line, sent with
  `--dry-run` false, to confirm the full path from upload → send → `sent_log.csv`
    → job JSON → progress page.
- Always use your own addresses for this. Never send test emails to real people
  as part of a test.

---

## 5. Coding conventions

- **No duplicated send logic.** The web app and the CLI must both call into
  `sender.py`. If you find send logic in `app.py` that is not in `sender.py`,
  move it.
- **Validate at the boundary.** Reject missing/placeholder credentials, empty
  subject, empty body, and missing list files in the interface layer, before the
  engine runs.
- **One email at a time.** The campaign loop must not pipeline SMTP calls.
- **Audit by default.** Any new send path must write to `sent_log.csv` and, for
  campaigns, produce a job-status JSON.
- **No secrets in logs or JSON.** `sent_log.csv` contains email, status, error —
  never the password. Job JSONs contain previews of the body but never the
  password.
- **Git-ignore new secret-holding paths.** If you add a new file or directory that
  holds secrets or uploads, add it to `.gitignore` before you commit.

---

## 6. Documentation updates

The docs are part of the product. If a change affects any of these, update the
matching doc:

| Change | Update |
|--------|--------|
| A new feature or requirement | `prd.md` (goals, user stories, functional requirements, acceptance criteria) |
| A new subsystem or file | `architecture.md` (component diagram, layer breakdown, data flow) |
| A new UI element or flow | `design.md` (user journeys, component inventory, visual tokens) |
| A new operational or security rule | `rules.md` (operational rules, security rules, email rules) |
| A new agent handoff pattern | `agents.md` (roles, handoff protocol, security handoff rule) |
| A new contributor path or setup step | this file |

Keep docs in sync with code. A feature that ships without a doc update is
incomplete.

---

## 7. Pull request / handoff checklist

Before you hand off a change, confirm:

- [ ] The change does what the PRD/user story says it should do.
- [ ] The change is verified (test client, dry-run, or real send as appropriate).
- [ ] No new secret is committed or logged.
- [ ] New env vars are documented in `.env.example` and in the handoff.
- [ ] New git-ignored paths are in `.gitignore`.
- [ ] The matching doc file is updated if the change affects behavior, UI, arch,
      rules, or setup.
- [ ] The handoff notes what was verified and what is not yet done.

---

## 8. Filing issues

If you find a bug or want a feature, describe it in concrete terms:

- **What you did** — the exact steps.
- **What you expected** — the expected behavior.
- **What happened** — the actual behavior, including any error text.
- **Environment** — OS, Python version, whether `.env` had a real password or a
  placeholder, and whether it was a web or CLI path.

Concrete reports are much easier to act on than "it doesn't work".

---

*End of contributing.md.*
