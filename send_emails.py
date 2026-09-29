#!/usr/bin/env python3
"""
send_emails.py — CLI campaign sender (thin wrapper around sender.py).

Reads config from .env:
  GMAIL_USER         — full Gmail address (e.g. you@gmail.com)
  GMAIL_APP_PASSWORD — 16-char Gmail App Password (NOT your account password)
  EMAIL_LIST         — path to CSV file with one email address per line
  SUBJECT            — (optional) subject line
  BODY               — (optional) body with {{name}} / {{col}} merge fields
  THROTTLE_SECONDS   — (optional) seconds between sends (default 0)

Sends via Gmail SMTP (smtp.gmail.com:587, STARTTLS) using the shared
sender.send_campaign() so the web app and CLI behave identically.

For each recipient the script:
  - personalizes the body from the recipient dict (name + any CSV columns)
  - sends a plain-text email
  - appends a row to sent_log.csv: timestamp, email, status, error

Gmail limits: ~500 emails/day for free accounts, ~2000 for Workspace.
Throttle for large lists.

Usage:
  python send_emails.py              # send to all in EMAIL_LIST
  python send_emails.py --dry-run   # print intent only, send nothing
  python send_emails.py --list path.csv   # override EMAIL_LIST
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Make ``import sender`` find the sibling module regardless of cwd.
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from sender import (  # noqa: E402
    SMTP_HOST,
    SMTP_PORT,
    load_dotenv_once,
    parse_recipients,
    send_campaign,
    validate_credentials,
)

load_dotenv_once(HERE / ".env")

GMAIL_USER = os.getenv("GMAIL_USER", "").strip()
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").strip()
EMAIL_LIST = os.getenv("EMAIL_LIST", "emails.csv").strip()
SUBJECT = os.getenv("SUBJECT", "Hello from DEEN").strip()
BODY = os.getenv("BODY", "Hello,\n\nThis is a test email from the email-blast sender.\n\nThanks,\nThe Team").strip()
THROTTLE_SECONDS = float(os.getenv("THROTTLE_SECONDS", "0").strip() or "0")


def main() -> None:
    parser = argparse.ArgumentParser(description="Send emails via Gmail SMTP")
    parser.add_argument("--dry-run", action="store_true", help="Print intent only, send nothing")
    parser.add_argument("--list", default=None, help="Override EMAIL_LIST path")
    args = parser.parse_args()

    list_path = Path(args.list) if args.list else HERE / EMAIL_LIST

    missing = validate_credentials(GMAIL_USER, GMAIL_APP_PASSWORD)
    if missing:
        print("[ERROR] Please set the following in .env before sending:")
        for m in missing:
            print(f"  - {m}")
        print()
        print("Generate a Gmail App Password here:")
        print("  https://myaccount.google.com/apppasswords")
        sys.exit(1)

    recipients = parse_recipients(list_path)
    if not recipients:
        print(f"[ERROR] No valid email addresses found in {list_path}")
        sys.exit(1)

    print(f"Sender            : {GMAIL_USER}")
    print(f"SMTP              : {SMTP_HOST}:{SMTP_PORT} (STARTTLS)")
    print(f"Email list        : {list_path}  ({len(recipients)} addresses)")
    print(f"Subject           : {SUBJECT}")
    print(f"Throttle          : {THROTTLE_SECONDS}s between sends")
    print(f"Dry run           : {args.dry_run}")
    print("-" * 60)

    if args.dry_run:
        from sender import personalize

        for i, r in enumerate(recipients, 1):
            body = personalize(BODY, r)
            print(f"[{i:>4}] would send to {r['email']}  (name={r['name']!r})")
        print("-" * 60)
        print(f"Total: {len(recipients)} emails (NOT sent — dry run)")
        return

    log_path = HERE / "sent_log.csv"
    results = send_campaign(
        GMAIL_USER,
        GMAIL_APP_PASSWORD,
        recipients,
        SUBJECT,
        BODY,
        log_path=log_path,
        throttle_seconds=THROTTLE_SECONDS,
    )

    success = sum(1 for r in results if r.status == "sent")
    failed = sum(1 for r in results if r.status == "failed")
    print("-" * 60)
    print(f"Done.  Sent: {success}   Failed: {failed}   Total: {len(recipients)}")
    print(f"Log: {log_path}")
    if failed:
        print()
        print("Failed recipients (for retry):")
        for r in results:
            if r.status == "failed":
                print(f"  - {r['email']}  ({r.error})")


if __name__ == "__main__":
    main()
