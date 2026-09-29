# Email Blast — Rules

**Version:** 1.0  
**Status:** Active  
**Last updated:** 2026-09-29

These are the operational, security, and sending rules for the email-blast project.
They are not suggestions — they are the constraints the system and its operators
must respect. If a feature or an operator action would break one of these rules,
the feature does not ship until the rule is satisfied or formally amended here.

---

## 1. Operational rules

### 1.1 Credentials

- **R-O1.** The Gmail App Password (`GMAIL_APP_PASSWORD`) lives only in the local,
  git-ignored `.env`. It is never committed, never pasted into the UI, never logged,
  and never embedded in code.
- **R-O2.** `.env.example` is the public template. It may contain the Gmail address
  but must never contain a real password — only a placeholder.
- **R-O3.** If `.env` contains the placeholder (or is missing a required variable),
  the system must refuse to send and must say so clearly. It must not proceed and
  fail mid-send.
- **R-O4.** Rotating the App Password is the operator's job. The system does not
  generate, store, or rotate credentials on its own.

### 1.2 Sending

- **R-O5.** One email is in flight at a time. A new send is not initiated until the
  previous send's result is known.
- **R-O6.** The daily send cap is enforced from `sent_log.csv`, not from in-memory
  state, so a restart does not lose the count.
- **R-O7.** Transient failures are retried; permanent failures are not. The system
  must distinguish the two and act accordingly.
- **R-O8.** The system must not send to a recipient it has already sent to in the
  same campaign. Within-campaign deduplication is mandatory.
- **R-O9.** A dry-run must validate everything except the actual SMTP sends. It is
  the pre-flight check.

### 1.3 Scheduling

- **R-O10.** A scheduled campaign must not run concurrently with itself. If a campaign
  is still running when its next slot arrives, the next slot is skipped.
- **R-O11.** The scheduler must persist `next_run` state so a restart resumes on the
  correct cadence.
- **R-O12.** A scheduled campaign must log which config it came from, so a failure can
  be traced back to the campaign definition.

### 1.4 Data and storage

- **R-O13.** `uploads/`, `uploads/jobs/`, and `sent_log.csv` are git-ignored. They are
  runtime artifacts, not source.
- **R-O14.** The send log is append-only. Code changes must not truncate or rewrite it.
- **R-O15.** The job-status JSON is a snapshot, not a log. It may be overwritten as the
  campaign progresses; it is not the durable record — `sent_log.csv` is.

### 1.5 UI and usability

- **R-O16.** The send button must be disabled until the subject and at least one body
  (plain or HTML) are non-empty.
- **R-O17.** Uploading a content file must fill the fields but must not lock them. The
  operator must be able to edit after loading.
- **R-O18.** The progress page must be refresh-safe. Reloading must replay the current
  snapshot, not lose state.

---

## 2. Security rules

### 2.1 Secrets

- **R-S1.** No secret value may appear in any log file, job JSON, template, or response
  body. This includes the Gmail App Password, and any future API keys or tokens.
- **R-S2.** Any new environment variable that holds a secret must be documented in
  `.env.example` with a placeholder and must be git-ignored.
- **R-S3.** Any new file or directory that holds secrets or uploads must be added to
  `.gitignore` before it is committed.
- **R-S4.** If a secret is ever committed to the repo, the corrective action is:
  rotate the credential immediately, scrub the history, and make the repo private if
  it is public. Rotation is the real fix; history scrubbing is secondary.

### 2.2 Scope

- **R-S5.** The system sends only to the recipients in the uploaded CSV. It does not
  discover, import, or append recipients from any other source.
- **R-S6.** The system does not forward, relay, or chain sends through any third party
  except the configured Gmail SMTP endpoint.
- **R-S7.** No recipient email may be sent to any external service for tracking,
  validation, or enrichment unless that service is explicitly configured and the
  operator has opted in.

---

## 3. Email content rules

### 3.1 Authenticity and formatting

- **R-C1.** Emails with an HTML body are sent as `multipart/alternative` with both a
  plain-text part and an HTML part. HTML-only sends are not allowed.
- **R-C2.** Merge fields (`{{name}}`, `{{city}}`, etc.) are resolved per recipient. If a
  field is missing for a recipient, it is replaced with an empty string, not left as
  `{{name}}`.
- **R-C3.** The subject line is plain text. HTML markup in the subject is not sent.

### 3.2 Sending behavior

- **R-C4.** The system sends exactly the message it constructed for each recipient. It
  does not append hidden text, tracking pixels, or extra headers beyond what the
  engine explicitly sets.
- **R-C5.** The from address is the configured Gmail user. The operator is responsible
  for the content and legality of what is sent.
- **R-C6.** The system does not suppress, modify, or censor the operator's content. It
  sends what it is given, within the formatting rules above.

---

## 4. Rule amendment

To change a rule:

1. Edit this file with the new text and a note about why.
2. If the rule change affects behavior, update `prd.md`, `architecture.md`, or
   `design.md` as needed.
3. Flag the change in the next handoff so reviewers check the rule, not just the code.

Rules are amended by the same process as code — they are not changed silently.

---

*End of rules.md.*
