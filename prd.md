# Email Blast — Product Requirements Document

**Version:** 1.0  
**Status:** Active  
**Last updated:** 2026-09-29  
**Owner:** email-camp (Hermes agent profile)  
**Project root:** `H:\Repo\email-blast\`

---

## 1. Problem statement

The user needs to send email campaigns from their own Gmail account
(`owner@example.com`) to arbitrary recipient lists, with a
recoverable, auditable, rate-limited pipeline — both ad-hoc (upload → preview →
send) and on a recurring schedule.

Constraints:
- Sender identity is a single personal Gmail, not a dedicated marketing platform.
- Sending must stay within Gmail's practical SMTP limits and avoid spam
  classification.
- Credentials must never be committed to the repo (Git-ignored `.env`).
- Recipient lists come from CSV uploads; content can be typed, pasted, or
  uploaded as `.txt` / `.json` / `.html`.
- The user wants both a web UI for one-off sends and a headless scheduler for
  recurring campaigns.

---

## 2. Goals

### 2.1 Primary goals

1. **Send email campaigns from Gmail SMTP** using a Gmail App Password (not the
   account password).
2. **Upload a CSV recipient list**, preview it, download it back, and send to it.
3. **Author email content** as plain text or HTML, with merge fields
   (`{{name}}`, `{{city}}`, etc.) resolved per recipient.
4. **Rate-limit sends** to stay under daily caps and avoid spam — with a default
   auto-pacing scheme and a manual override.
5. **Schedule recurring campaigns** so the system can run headless on a cadence
   without a human clicking "Send".
6. **Audit everything** — a per-campaign log, a `sent_log.csv`, and a job-status
   JSON snapshot readable by the progress page.

### 2.2 Out of scope (for now)

- Multi-account sending / rotating sender identities.
- Unsubscribe link generation or list-unsubscribe header management.
- Bounce handling / list hygiene beyond what the user provides.
- Drag-and-drop HTML editor (HTML is pasted or uploaded as a file).
- Provider switching (AgentMail, SendGrid, etc.) — Gmail SMTP is the only target.

---

## 3. User stories

| # | As a… | I want to… | So that… |
|---|-------|------------|----------|
| US1 | Sender | upload a CSV of emails and see a preview of how many valid addresses were found | I don't blast to a broken list |
| US2 | Sender | download a sample CSV to understand the expected format | I can prepare my list correctly |
| US3 | Sender | type or paste a subject + plain-text body with merge fields | I can personalize each email |
| US4 | Sender | upload an HTML file (or paste HTML) and send as multipart/alternative | recipients whose clients prefer HTML get a styled version |
| US5 | Sender | see a live progress page while a campaign sends | I know it's working and can stop if something looks wrong |
| US6 | Sender | choose Auto pacing or set my own seconds-per-email / per-hour / daily cap | I control how aggressive the send is |
| US7 | Sender | schedule a campaign to run automatically every N minutes/hours/days | I don't have to remember to click Send |
| US8 | Sender | see a history of past campaigns and their outcome | I can audit what was sent and when |
| US9 | Ops | rotate the Gmail App Password in `.env` without touching code | credentials stay fresh and secret |
| US10 | Ops | run a dry-run to validate the list + content without sending | I catch mistakes before they leave the machine |

---

## 4. Functional requirements

### 4.1 Recipient list

- FR1.1 — Accept a CSV upload with at least one column containing email addresses
  (any cell containing `@` is treated as an email).
- FR1.2 — Accept an optional `name` column; use it for `{{name}}` merge.
- FR1.3 — Accept any extra named columns; expose them as `{{column_name}}` merge
  fields.
- FR1.4 — Deduplicate by lowercased email.
- FR1.5 — Surface row count, valid email count, and duplicate count in the preview.
- FR1.6 — Provide a downloadable sample CSV (`/sample.csv`) showing the recommended
  format.
- FR1.7 — Allow downloading the uploaded file back (`/uploads/<filename>`).

### 4.2 Content

- FR2.1 — Subject text input (required for send).
- FR2.2 — Plain-text body textarea with `{{name}}` / `{{col}}` merge fields.
- FR2.3 — HTML body textarea; when non-empty the email is sent as
  `multipart/alternative` (plain + HTML parts).
- FR2.4 — Upload `.txt` content file: line 1 = subject, rest = plain body.
- FR2.5 — Upload `.json` content file: `{"subject": "...", "body": "...", "html": "..."}`.
- FR2.6 — Upload `.html` content file: `<title>` → subject, inner HTML → `html_body`,
  auto-generated plain-text fallback in `body`.
- FR2.7 — Content uploads fill the form fields but remain editable before send.
- FR2.8 — Body-mode toggle: Plain / HTML — decides which body the sender uses.

### 4.3 Sending

- FR3.1 — Send from `GMAIL_USER` (Gmail address) using `GMAIL_APP_PASSWORD` over
  `smtp.gmail.com:587` with STARTTLS.
- FR3.2 — One email at a time; the next email is only attempted after the previous
  one's result is known.
- FR3.3 — Three pacing modes:
  - **Auto** — pace derived from list size (45s for ≤50, 60s for 51-200,
    90s for 201-500, 120s for 500+), with a daylight boost that shortens waits
    during business hours in the recipient's local timezone.
  - **Fixed** — a user-specified `pace_seconds` between every email.
  - **Per hour** — a user-specified `emails_per_hour` cap.
- FR3.4 — Daily cap (default 100) enforced against `sent_log.csv`; a campaign whose
  full send would exceed the cap is rejected up-front with a clear message.
- FR3.5 — Adaptive backoff: on transient SMTP errors (421, timeout, connection
  reset) the pace is temporarily doubled up to 8x.
- FR3.6 — Retry: transient failures are retried up to 3 times with backoff at the
  end of the campaign.
- FR3.7 — Login failure is detected before any email is sent and reported immediately.
- FR3.8 — Merge fields are resolved per recipient with empty-string fallback for
  missing columns.
- FR3.9 — A send runs in a background thread so the browser is not blocked; a
  job ID is returned and the progress page polls the job-status JSON.

### 4.4 Scheduling (cronjob)

- FR4.1 — A campaign config file (JSON) specifies: which CSV, which content
  (inline subject/body/html or a content-file ID), pacing mode, daily cap, and
  cadence (interval in minutes, or cron expression, or "run once").
- FR4.2 — A scheduler process reads registered configs and launches campaigns when
  their next_run is due.
- FR4.3 — After a campaign finishes, its `next_run` is advanced by the cadence.
- FR4.4 — If a campaign is still running when its next slot arrives, the next slot
  is skipped (no concurrent sends of the same campaign).
- FR4.5 — Scheduler state (last run, next run, last error) is persisted per config
  so a restart resumes correctly.
- FR4.6 — Failed sends are logged with the campaign config name for audit.

### 4.5 Observability

- FR5.1 — Per-recipient log row in `sent_log.csv`: timestamp, email, status, error.
- FR5.2 — Per-campaign job-status JSON under `uploads/jobs/<job_id>.json` with
  subject, body preview, HTML preview, body mode, pacing, counts, and per-email
  results.
- FR5.3 — Web progress page (`/job/<job_id>`) that polls and renders the snapshot.
- FR5.4 — Campaign history page listing past jobs with status and counts.
- FR5.5 — CLI dry-run (`send_emails.py --dry-run`) that validates credentials
  presence and list parsing without sending.

---

## 5. Non-functional requirements

### 5.1 Security

- NF1.1 — `.env` (containing `GMAIL_APP_PASSWORD`) is git-ignored and never
  committed.
- NF1.2 — `.env.example` exists with placeholder values as the setup template.
- NF1.3 — No password appears in logs, job JSONs, or the UI.
- NF1.4 — Uploaded files are stored under `uploads/` which is git-ignored.

### 5.2 Reliability

- NF2.1 — The sender never pipelines emails; it confirms each result before the
  next send.
- NF2.2 — Transient errors are retried, not treated as permanent failures.
- NF2.3 — The daily cap is enforced from `sent_log.csv` so restarts respect prior
  sends.

### 5.3 Performance

- NF3.1 — The web UI responds immediately when "Send" is clicked; the campaign
  runs in a thread and the browser polls.
- NF3.2 — Job-status writes are batched (not per-email) to limit disk churn on
  large campaigns.

### 5.4 Usability

- NF4.1 — The sample CSV is one click away from the upload page.
- NF4.2 — The send button is disabled until subject and body are both non-empty.
- NF4.3 — The progress page shows a human-readable pace description and estimated
  finish time.

---

## 6. Acceptance criteria

| ID | Given | When | Then |
|----|-------|------|------|
| AC1 | a valid CSV with 3 emails is uploaded | the preview shows 3 valid emails and 0 duplicates | the user can proceed to content |
| AC2 | a `.html` file is uploaded as content | the subject field is filled from `<title>` and an HTML body field appears | the user can edit both before sending |
| AC3 | subject + body are filled and Send is clicked | a job is created and the browser navigates to `/job/<id>` | the progress page shows status "running" |
| AC4 | a campaign is running | each email is sent only after the previous result is known | no two emails are in flight at once |
| AC5 | the daily cap would be exceeded | the send request is rejected with a clear error | no email is sent |
| AC6 | a transient SMTP error occurs | the pace doubles and the email is retried | the campaign continues instead of failing permanently |
| AC7 | a scheduled campaign's next_run is due | the scheduler launches it | the campaign appears in the history with its config name |
| AC8 | `.env` still contains the password placeholder | any send attempt (CLI or web) is blocked with an error | no email is sent and no crash occurs |
| AC9 | the user opens `/sample.csv` | a CSV with header + 3 sample rows is downloaded | the user can use it as a template |
| AC10 | a campaign finishes | the job-status JSON and `sent_log.csv` both reflect the outcome | the progress page shows "done" with final counts |

---

## 7. Open questions

- Q1 — Should the scheduler support multiple concurrent campaigns, or one at a time
  globally? (Current design: per-config locking, so different campaigns can run in
  parallel if the user wants; same campaign is serialized.)
- Q2 — Should sent emails be deduplicated across campaigns (global dedupe), or only
  within a single campaign? (Current design: within-campaign dedup only; cross-campaign
  dedup is future work.)

---

*End of PRD.*
