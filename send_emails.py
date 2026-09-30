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
    build_unsubscribe_url,
    count_today_sent,
    load_dotenv_once,
    load_suppressed_emails,
    parse_recipients,
    send_campaign,
    unknown_merge_fields,
    validate_credentials,
)

load_dotenv_once(HERE / ".env")

GMAIL_USER = os.getenv("GMAIL_USER", "").strip()
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").strip()
EMAIL_LIST = os.getenv("EMAIL_LIST", "emails.csv").strip()
SUBJECT = os.getenv("SUBJECT", "Hello from DEEN").strip()
BODY = os.getenv("BODY", "Hello,\n\nThis is a test email from the email-blast sender.\n\nThanks,\nThe Team").strip()
THROTTLE_SECONDS = float(os.getenv("THROTTLE_SECONDS", "0").strip() or "0")
UNSUBSCRIBE_URL = os.getenv("UNSUBSCRIBE_URL", "").strip()
UNSUBSCRIBE_SECRET = os.getenv("UNSUBSCRIBE_SECRET", "").strip()
SUPPRESSION_PATH = Path(os.getenv("SUPPRESSION_PATH", str(HERE / "suppression.txt")))


def main() -> None:
    parser = argparse.ArgumentParser(description="Send emails via Gmail SMTP")
    parser.add_argument("--dry-run", action="store_true", help="Print intent only, send nothing")
    parser.add_argument("--list", default=None, help="Override EMAIL_LIST path")
    parser.add_argument(
        "--max-per-day", type=int, default=None,
        help="Safety stop before the Gmail/Workspace daily cap "
             "(default: MAX_PER_DAY env or 1950; Workspace standard = 2000/day)",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Skip recipients already recorded as sent today in sent_log.csv",
    )
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

    # Honor opt-outs before anything else.
    suppressed = load_suppressed_emails(SUPPRESSION_PATH)
    if suppressed:
        before = len(recipients)
        recipients = [r for r in recipients if r["email"].strip().lower() not in suppressed]
        print(f"Suppression list  : {SUPPRESSION_PATH}  ({len(suppressed)} entries, "
              f"{before - len(recipients)} recipient(s) skipped)")

    # Daily-cap safety: max_per_day (default 1950, under Workspace's 2000/day)
    # is enforced against sent_log.csv, so it survives restarts. The engine
    # stops cleanly at the limit; --resume skips today's already-sent rows so
    # the remainder can be sent the next run without duplicates.
    max_per_day = args.max_per_day
    if max_per_day is None:
        max_per_day = int(os.getenv("MAX_PER_DAY", "1950").strip() or "1950")
    if max_per_day < 0:
        print(f"[ERROR] --max-per-day must be >= 0 (got {max_per_day})")
        sys.exit(1)

    # Pre-flight merge-field check (R-C2): fail before sending rather than
    # delivering a literal {{field}} to every recipient.
    columns = set(recipients[0].get("extra", {}).keys()) if recipients else set()
    unknown = unknown_merge_fields([SUBJECT, BODY], columns)
    if unknown:
        print("[ERROR] Unknown merge field(s) in SUBJECT/BODY:")
        for f in unknown:
            print(f"  - {{{{{f}}}}}")
        print(f"Available CSV columns: {', '.join(sorted(columns)) or 'none'}")
        sys.exit(1)

    print(f"Sender            : {GMAIL_USER}")
    print(f"SMTP              : {SMTP_HOST}:{SMTP_PORT} (STARTTLS)")
    print(f"Email list        : {list_path}  ({len(recipients)} addresses)")
    print(f"Subject           : {SUBJECT}")
    print(f"Throttle          : {THROTTLE_SECONDS}s between sends")
    print(f"Dry run           : {args.dry_run}")
    print("-" * 60)

    if args.dry_run:
        for i, r in enumerate(recipients, 1):
            unsub = ""
            if UNSUBSCRIBE_URL:
                unsub = build_unsubscribe_url(UNSUBSCRIBE_URL, r, UNSUBSCRIBE_SECRET)
            print(f"[{i:>4}] would send to {r['email']}  (name={r['name']!r}"
                  f"{'  unsub=' + unsub if unsub else ''})")
        print("-" * 60)
        print(f"Total: {len(recipients)} emails (NOT sent — dry run)")
        print(f"Daily cap         : {max_per_day} (today's sent so far: "
              f"{count_today_sent(HERE / 'sent_log.csv')})")
        return

    log_path = HERE / "sent_log.csv"
    sent_today = count_today_sent(log_path)
    if args.resume:
        before = len(recipients)
        already = sent_today
        recipients = recipients[already:] if already < before else []
        print(f"Resume            : skipping first {min(already, before)} "
              f"recipient(s) already sent today ({before - len(recipients)} skipped)")

    print(f"Daily cap         : {max_per_day} (today's sent so far: {sent_today})")
    if sent_today >= max_per_day:
        print("[STOP] Daily safety cap already reached today. "
              "Run again tomorrow (use --resume to skip today's sent rows).")
        return
    if sent_today + len(recipients) > max_per_day:
        room = max_per_day - sent_today
        print(f"[TRIM] List trimmed to today's remaining cap: "
              f"{len(recipients)} -> {room} (rest stays for the next run with --resume)")
        recipients = recipients[:room]

    results = send_campaign(
        GMAIL_USER,
        GMAIL_APP_PASSWORD,
        recipients,
        SUBJECT,
        BODY,
        log_path=log_path,
        throttle_seconds=THROTTLE_SECONDS,
        daily_cap=max_per_day,
        unsubscribe_url=UNSUBSCRIBE_URL,
        unsubscribe_secret=UNSUBSCRIBE_SECRET,
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
                print(f"  - {r.email}  ({r.error})")


if __name__ == "__main__":
    main()
