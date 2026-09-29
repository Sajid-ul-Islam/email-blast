#!/usr/bin/env python3
"""Test send: HTML email to 3 test addresses."""
import os, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from app import parse_content_upload
from sender import load_dotenv_once, parse_recipients, send_campaign, validate_credentials
from dotenv import load_dotenv

load_dotenv(dotenv_path=HERE / ".env")
load_dotenv_once(HERE / ".env")

GMAIL_USER = os.getenv("GMAIL_USER", "").strip()
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").strip()

missing = validate_credentials(GMAIL_USER, GMAIL_APP_PASSWORD)
if missing:
    print("[ERROR] Missing credentials:")
    for m in missing:
        print(f"  - {m}")
    sys.exit(1)

# Parse HTML content file
content = parse_content_upload(HERE / "test_html_content.html")
subject = content["subject"]
html_body = content["html_body"]
plain_body = content["body"]

print(f"Subject: {subject}")
print(f"HTML body length: {len(html_body)} chars")
print(f"Plain body length: {len(plain_body)} chars")

# Parse recipients
recipients = parse_recipients(HERE / "test_recipients.csv")
print(f"\nRecipients: {len(recipients)}")
for r in recipients:
    print(f"  - {r['email']} ({r['name']!r})")

print("\n=== SENDING HTML EMAILS ===")
log_path = HERE / "test_sent_log.csv"
results = send_campaign(
    GMAIL_USER,
    GMAIL_APP_PASSWORD,
    recipients,
    subject,
    plain_body,
    html_body_template=html_body,
    log_path=log_path,
    pace_mode="fixed",
    pace_seconds=3.0,  # very short for test
    daily_cap=100,
)

print(f"\n=== RESULTS ===")
for r in results:
    print(f"  {r.email:<35} {r.status:<10} {r.error[:60] if r.error else ''}")

success = sum(1 for r in results if r.status == "sent")
failed = sum(1 for r in results if r.status == "failed")
skipped = sum(1 for r in results if r.status == "skipped")
print(f"\nDone. Sent: {success}, Failed: {failed}, Skipped: {skipped}, Total: {len(recipients)}")
print(f"Log: {log_path}")
