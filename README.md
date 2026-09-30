# Email Blast — web upload + send

A small Flask web app to upload CSV recipient lists, compose or upload email content, and send campaigns via Gmail SMTP with rate limiting and live progress tracking.

## Features

- **CSV & Excel upload** — supports both CSV (`.csv`, `.txt`) and Excel (`.xlsx`, `.xls`) files; dynamically detects the email column regardless of column name or placement; automatically cleans noise, removes malformed emails, and deduplicates; previews up to 20 rows with summary metrics
- **Content upload** — upload `.txt`, `.json`, or `.html` files to auto-fill subject and body
- **Merge fields** — use `{{name}}`, `{{city}}`, or any column from your recipient list for personalisation
- **HTML email** — send `multipart/alternative` (plain + HTML) when an HTML body is provided
- **Live progress** — `/job/<id>` page with auto-refreshing stats, progress bar, and per-recipient result rows; polling uses the `/job/<id>/status` JSON endpoint
- **Pacing & rate limiting** — three modes: `auto` (45–120 s based on list size), `fixed`, or `per_hour`; adaptive backoff on transient errors; daily cap (default 100)
- **Real cancellation** — Cancel button sets a `threading.Event` so the background thread stops cleanly after the current email
- **CLI sender** — `send_emails.py` + `run_sender.sh` for headless operation

## File layout

```
email-blast/
├── app.py                 # Flask web app
├── sender.py              # Shared sending engine (rate limiter, SMTP, retries)
├── send_emails.py         # CLI wrapper
├── run_sender.sh          # Shell wrapper with password guard
├── sample_emails.csv      # Downloadable sample template
├── templates/
│   ├── index.html         # Upload + content + send form
│   ├── preview.html       # Full-list preview
│   └── send_job.html      # Live send-progress page
├── static/
│   └── style.css          # Page styling
├── uploads/               # Uploaded CSVs (git-ignored)
│   ├── content/           # Uploaded content files (git-ignored)
│   └── jobs/              # Per-job JSON status files (git-ignored)
├── .env                   # Gmail credentials (git-ignored, NEVER commit)
├── .env.example           # Template for .env
└── .gitignore
```

## Quick start

```bash
cd h:/Repo/email-blast
pip install flask python-dotenv
python app.py
```

Open **http://127.0.0.1:5000** in your browser.

1. **Upload** a CSV → preview the emails found
2. **Compose** subject + body (or upload a `.txt`/`.json`/`.html` content file)
3. Click **Send now →** → watch live progress on the job page

## CLI usage

```bash
./run_sender.sh            # send to all in emails.csv
./run_sender.sh --dry-run  # preview only, send nothing
python send_emails.py --list path/to/list.csv
```

## `.env` setup (do this once)

```bash
cp .env.example .env
# Edit .env and fill in your Gmail address and App Password
```

```
GMAIL_USER=you@gmail.com
GMAIL_APP_PASSWORD=<your 16-char app password>
EMAIL_LIST=emails.csv
```

`.env` is git-ignored and never leaves this folder.

## Gmail App Password

1. Go to https://myaccount.google.com/security
2. Enable 2-Step Verification
3. Search "App Passwords" → create one named "Email Blast"
4. Paste the 16-character code into `.env`

## Limits

| Account type | Daily limit |
|---|---|
| Free Gmail | ~500 emails/day |
| Google Workspace | ~2 000 emails/day |

The default daily cap is **100**. Increase it in the Send form or via `DAILY_CAP` if your account supports more.
