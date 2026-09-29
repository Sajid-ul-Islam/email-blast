# Email Blast — Design Document

**Version:** 1.0  
**Status:** Active  
**Last updated:** 2026-09-29

---

## 1. Design principles

1. **Thin client over the web UI.** The Flask app is the human-facing surface.
   All sending logic lives in `sender.py` so the CLI and the web app share one
   engine. No send logic is duplicated.
2. **One email in flight at a time.** We never pipeline SMTP calls. The campaign
   loop confirms each result before scheduling the next. This is the core safety
   property.
3. **Fail loudly, never silently.** A missing or placeholder password, an empty
   subject, an empty body, a missing list file — each is rejected at the boundary
   with a clear message, not allowed to proceed and fail later.
4. **Audit by default.** Every send attempt is logged per-recipient (`sent_log.csv`)
   and per-campaign (job-status JSON). A human can reconstruct what happened from
   these two sources.
5. **Progressive disclosure.** The upload page asks for a list first, then content,
   then pacing. The user is not confronted with every option at once.

---

## 2. User journeys

### 2.1 One-off send (web UI)

```
Open / 
  → Upload CSV                        (step 1)
  → Preview list (count, dedup)      (step 2)
  → Write/paste subject + body       (step 3, plain)
    OR upload .txt / .json / .html   (step 3, content file)
    OR paste HTML + switch to HTML mode
  → Choose pacing (Auto / Fixed / Per hour) + daily cap
  → Click "Send now"
  → Redirect to /job/<id>
  → Poll progress page until done
  → Read final counts + per-email results
```

### 2.2 Scheduled campaign (headless)

```
Scheduler process starts
  → Reads campaign configs from config dir
  → For each config whose next_run <= now AND not already running:
      → Launch send_campaign with the config's list + content + pacing
      → On finish: write result, advance next_run by cadence
  → Sleep until next tick (or wake on a file/watch event)
```

### 2.3 Dry-run (CLI)

```
send_emails.py --dry-run
  → Validate .env has a non-placeholder password
  → Parse emails.csv
  → Report count + sample rows
  → Exit (no SMTP connection, no sends)
```

---

## 3. Interface design

### 3.1 Upload page (`/`)

- One file picker for the CSV, with drag-and-drop.
- A "Sample CSV" link so the user can download the template before uploading.
- After upload: a preview card showing filename, storage path, row count, valid
  email count, duplicate count, and a table of the first N emails.
- A "Previously uploaded" table listing prior uploads with download links.

### 3.2 Content card (appears after upload)

- Subject input.
- Plain-text body textarea (merge fields documented in placeholder).
- HTML body textarea (shown when body mode is HTML; collapsible otherwise).
- Body-mode toggle: Plain / HTML.
- Content-file upload zone accepting `.txt`, `.json`, `.html`, with drag-and-drop.
- "Apply uploaded content" button fills the fields; the user can still edit.
- Send button, disabled until subject + (body or html_body) are non-empty.
- Send hint text that updates live with recipient count and readiness.

### 3.3 Progress page (`/job/<id>`)

- Job ID, list file, subject.
- Body preview (plain, truncated) and HTML preview (truncated) when HTML mode.
- Body mode, pacing mode, pace description, daily cap, estimated duration,
  estimated finish time.
- Live counts: total, sent, failed, skipped, pending.
- Per-email result list (email, status, error if any), most recent first.
- A "Back to home" link.

### 3.4 Campaign history (`/history`)

- Table of past jobs: job ID, list file, subject, status, total, sent, failed,
  skipped, finished at, cadence.
- Each row links to its progress page for full detail.

---

## 4. Component inventory

| Component | Type | States |
|-----------|------|--------|
| File picker (CSV) | input + drop zone | empty, has-file, drag-over, removed |
| Preview card | card + table | loading, shown, empty-list-error |
| Subject input | text input | empty, filled, error-shake |
| Plain body textarea | textarea | empty, filled, error-shake |
| HTML body textarea | textarea | hidden, shown, filled |
| Body-mode toggle | segmented control | plain, html |
| Content-file picker | input + drop zone | empty, has-file, drag-over, removed |
| Apply-content button | button | disabled, enabled, clicked (fills fields) |
| Send button | button | disabled, enabled, sending, error |
| Send hint | text | idle, ready, content-loaded, error |
| Progress page | full page | starting, running, done, error |
| Result row | table row | sent (green), failed (red), skipped (amber), pending |

---

## 5. Visual design tokens

| Token | Value | Usage |
|-------|-------|-------|
| `--bg` | `#f6f7f9` | page background |
| `--card` | `#ffffff` | card background |
| `--border` | `#e2e5ea` | card border, dividers |
| `--text` | `#1a1d23` | primary text |
| `--muted` | `#6b7280` | secondary text, hints |
| `--accent` | `#2563eb` | primary action, links |
| `--accent-hover` | `#1d4ed8` | primary action hover |
| `--danger` | `#dc2626` | errors, failed status |
| `--success` | `#16a34a` | success, sent status |
| `--row-hover` | `#f8fafc` | table row hover |

Typography: system font stack, 15px base, 1.5 line-height. Cards are
max-width 880px, centered, with 40px top padding and 60px bottom.

---

## 6. Accessibility notes

- All form inputs have explicit `<label>` elements (not just placeholders).
- The file drop zones are keyboard-operable via their underlying `<input>`.
- Status colors (green/red/amber) are paired with text labels so the page is
  usable without color perception.
- The progress page is refresh-safe: reloading replays the current snapshot.

---

## 7. Design decisions and rationale

### D1 — multipart/alternative for HTML

When an HTML body is present, the email is sent as `multipart/alternative` with
both a plain-text part and an HTML part. Rationale: maximum client compatibility.
A client that cannot render HTML falls back to the plain part. We do not send
HTML-only because some corporate mailers strip HTML-only messages.

### D2 — Auto pacing with daylight boost

Auto pacing scales with list size so small lists finish in minutes and large lists
spread across the day. The daylight boost shortens waits during business hours in
the recipient's local timezone, because opens and engagement are higher then and
the sender looks less like a bot. The boost is a smooth cosine ramp, not a step
change, so it doesn't create a burst at the boundary.

### D3 — Daily cap from `sent_log.csv`

The cap is enforced against the log, not in memory. Rationale: if the process
restarts, in-memory counters are lost but the log persists. The log is the source
of truth for "how many have we sent today?".

### D4 — Background thread, pollable JSON

The send runs in a daemon thread and writes a job-status JSON that the progress
page polls. Rationale: the browser is not blocked, and the user can close and
reopen the progress page without losing state. The JSON is the single source of
truth for the job; the in-memory state is only a cache.

### D5 — Content files fill but do not lock fields

Uploading a content file fills the subject/body/html fields but leaves them
editable. Rationale: content files are a convenience, not a contract. The user
often needs to tweak one line after loading.

---

## 8. Out-of-scope design items (documented for traceability)

- No inline HTML editor. HTML is pasted into a textarea or uploaded as a file.
- No drag-and-drop reordering of recipients. The CSV order is the send order.
- No attachment support in the current version. Attachments would require
  multipart/mixed handling and are deferred.
- No unsubscribe flow. The user sends to their own list; unsubscribe handling is
  the user's responsibility until a future version adds it.

---

*End of design.md.*
