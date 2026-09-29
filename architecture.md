# Email Blast — Architecture Document

**Version:** 1.0  
**Status:** Active  
**Last updated:** 2026-09-29

---

## 1. System overview

Email Blast is a small Python system for sending email campaigns from a single
Gmail account. It has two entry points:

- **Web UI** — a Flask app (`app.py`) that serves upload, preview, content editing,
  send, progress, and history pages.
- **CLI** — `send_emails.py` (via `run_sender.sh`) for headless sends and dry-runs.

Both entry points call into the same sending engine (`sender.py`), so behavior is
consistent across the web and the command line.

A scheduler (future, `scheduler.py`) runs campaigns headless on a cadence.

---

## 2. Component diagram (textual)

```
                    ┌──────────────────────────────────────────┐
                    │              Human operator               │
                    └─────────────┬────────────────────────────┘
                                  │
                    ┌─────────────▼────────────────────────────┐
                    │              Flask web app                │
                    │           (app.py :5000)                  │
                    │  routes: /, /upload, /preview,           │
                    │          /content, /send, /job/<id>,      │
                    │          /history, /sample.csv,           │
                    │          /uploads/<path>                  │
                    └──────┬──────────────┬────────────────────┘
                           │              │
              ┌────────────▼──┐   ┌──────▼───────────────────────┐
              │ uploads/       │   │ uploads/content/             │
              │  - <list.csv>  │   │  - <content>.txt/.json/.html │
              │  - <content>…  │   └──────────────────────────────┘
              │  - jobs/       │
              │    - <job>.json│
              └────────────────┘
                           │
                    ┌──────▼───────────────────────┐
                    │        sender.py             │
                    │  - parse_recipients()        │
                    │  - parse_content_upload()    │
                    │  - personalize()            │
                    │  - send_campaign()          │
                    │  - RateLimiter              │
                    │  - SendResult               │
                    │  - _send_one()              │
                    │  - _append_log()            │
                    │  - snapshot_job()           │
                    └──────┬──────────────────────┘
                           │
                    ┌──────▼───────────────────────┐
                    │      smtp.gmail.com:587     │
                    │      (STARTTLS)             │
                    └──────────────────────────────┘

                           ▲
                    ┌──────┴───────────────────────┐
                    │   sent_log.csv (append-only) │
                    └──────────────────────────────┘

                    ┌──────────────────────────────┐
                    │  Scheduler (future)          │
                    │  - reads campaign configs    │
                    │  - launches send_campaign    │
                    │  - persists next_run state   │
                    └──────────────────────────────┘
```

---

## 3. Layer breakdown

### 3.1 Interface layer (Flask)

`app.py` owns all HTTP concerns: routing, file upload handling, form parsing,
template rendering, and the background-thread launch for sends. It has two jobs:

1. **Validate at the boundary.** Missing file_id, empty subject, empty body,
   placeholder password — each is rejected here with a 400/403-style JSON error
   before any sending logic runs.
2. **Translate to/from the engine.** It calls `sender.parse_recipients`,
   `sender.send_campaign`, and writes job-status JSONs that the progress page
   reads.

`app.py` deliberately does not own SMTP logic. If sender.py is missing, it has an
inline fallback with the same behavior, but the primary path is the engine.

### 3.2 Engine layer (sender.py)

`sender.py` owns all sending concerns:

- **Parsing** — `parse_recipients` (CSV → list of dicts), `parse_content_upload`
  (`.txt`/`.json`/`.html` → subject/body/html_body), `personalize` (merge fields).
- **Sending** — `send_campaign` (the main loop), `_send_one` (one SMTP call),
  `_append_log` (append to `sent_log.csv`), `snapshot_job` (write a JSON snapshot).
- **Rate limiting** — `RateLimiter` (daily cap, pacing modes, backoff, retry).
- **Modeling** — `SendResult` (a typed result with `to_dict()` for JSON/UI use).

### 3.3 Storage layer

Two stores:

- **`uploads/`** — recipient CSVs and content files. Ephemeral from the app's
  perspective; the user manages them.
- **`sent_log.csv`** — append-only per-recipient log. The daily-cap checkpoint
  and the post-mortem audit trail both come from here.
- **`uploads/jobs/<job_id>.json`** — per-campaign snapshot for the progress page.
  Overwritten as the campaign progresses.

### 3.4 External dependency

- **Gmail SMTP** — `smtp.gmail.com:587`, STARTTLS, App Password auth. The only
  external service. If it is unreachable, the sender retries transiently and
  reports failures; it does not fall back to another provider.

---

## 4. Data flow

### 4.1 One-off send (web)

```
POST /upload  →  save CSV to uploads/<name>  →  parse + count  →  render preview
POST /content →  save content to uploads/content/  →  parse  →  return JSON
POST /send    →  read form + list file
                →  validate subject/body/non-empty
                →  read GMAIL_USER + GMAIL_APP_PASSWORD from env
                →  launch background thread
                →  thread: parse list → send_campaign → write job JSON
                →  return {job_id}
GET  /job/<id> →  read uploads/jobs/<id>.json  →  render progress
```

### 4.2 Send campaign internals

```
send_campaign:
  validate credentials  →  fail fast if missing
  upgrade legacy throttle_seconds to fixed mode
  build RateLimiter(daily_cap, pace_mode, pace_seconds, emails_per_hour)
  connect SMTP (ehlo/starttls/ehlo/login)
  for each recipient:
    can_send()?  →  no → mark skipped, continue
    personalize body  (+ html_body if HTML mode)
    wait_before_next()  (paces + daylight boost + backoff)
    _send_one()  →  success → log sent; failure → log failed + retry queue
  retry transient failures up to 3x with backoff
  disconnect SMTP
  write final job snapshot
```

---

## 5. Key abstractions

### 5.1 `SendResult`

A lightweight value object:

```
SendResult(email, status, error, elapsed_s, retried)
  status in {"sent", "failed", "skipped", "pending"}
  to_dict() → JSON-serializable dict
```

### 5.2 `RateLimiter`

Stateful per-campaign object:

- `daily_cap` — max sends today; checked against `sent_log.csv` at start.
- `pace_mode` — `auto` | `fixed` | `per_hour`.
- `pace_seconds` / `emails_per_hour` — mode-specific parameters.
- `sent_today`, `last_send_time`, `consecutive_failures`, `throttled_until`
- `set_list_size(n)` — lets auto mode compute pace from list size.
- `can_send()` — daily-cap check.
- `wait_before_next()` — sleeps for the chosen pace, including daylight boost and
  backoff.
- `record_sent()` / `record_failed()` — update counters and backoff state.
- `estimate_duration_seconds(total)` / `estimate_finish_iso(total)` — UI-facing
  estimates.

### 5.3 Job-status JSON

A single JSON file per campaign, readable by the progress page and refresh-safe:

```
{
  "job_id", "list_file", "subject",
  "body_preview", "html_body_preview", "is_html", "body_mode",
  "pace_mode", "pace_seconds", "emails_per_hour", "daily_cap",
  "pace_description", "estimated_duration_s", "estimated_finish_iso",
  "status", "total", "sent", "failed", "skipped", "pending",
  "started_at", "finished_at",
  "results": [ ... ]  // most recent N
}
```

---

## 6. Error handling strategy

| Error class | Detection point | Action |
|-------------|-----------------|--------|
| Missing/placeholder password | `validate_credentials` (before any SMTP) | fail all recipients with one message; no connection |
| SMTP login failure | `_send_one` on first send | fail all; no retry (auth problem, not transient) |
| Transient SMTP (421, timeout, connection drop) | `_send_one` | mark failed, enqueue retry, backoff pace |
| Permanent SMTP (5xx auth/data errors) | `_send_one` | mark failed, no retry |
| Empty subject/body after content load | `/send` before launch | 400 JSON error, no thread launched |
| Missing list file | `/send` before launch | 404 JSON error |
| Daily cap exceeded | `RateLimiter.can_send()` | mark skipped, continue loop (so the UI shows the real total) |
| Recipient parse error (bad CSV) | `parse_recipients` | skip the bad row, continue; log is not yet written per-row for parse failures (future) |

---

## 7. Concurrency model

- The web app is single-threaded Flask in dev; the send runs in a daemon thread so
  the request returns immediately.
- The job-status JSON is the coordination point between the thread and the pollers.
- The scheduler (future) must not launch the same campaign config concurrently. Per-config
  locking (a `running` flag in the config state) is the intended mechanism.
- `sent_log.csv` is append-only and written once per recipient. In the current version
  there is no concurrent-writer contention because one send runs at a time per process.

---

## 8. Extension points

- **New content format** — add a branch in `parse_content_upload`.
- **New pacing mode** — add a branch in `RateLimiter._base_pace_seconds` and
  `RateLimiter.wait_before_next`.
- **New recipient source** — add a parser and call it from the send path; the rest
  of the campaign loop is format-agnostic (it consumes a list of dicts).
- **New provider** — replace `_send_one`'s SMTP block with a provider adapter.
  `send_campaign`'s loop, pacing, retry, and logging are provider-agnostic.

---

## 9. Non-goals (documented)

- No shared state across processes for the daily cap (the log is the only
  cross-restart checkpoint; in-memory counters reset on restart).
- No delivery tracking (opens, clicks) — the system sends; it does not track
  downstream engagement.
- No list segmentation UI — the CSV is the segment.
- No multi-account rotation — one Gmail account per install.

---

*End of architecture.md.*
