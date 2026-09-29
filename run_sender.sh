#!/usr/bin/env bash
# run_sender.sh — Load .env and run the email sender.
#
# Usage:
#   ./run_sender.sh              # send all emails
#   ./run_sender.sh --dry-run   # preview only
#
# The script expects .env in the same directory with:
#   GMAIL_USER, GMAIL_APP_PASSWORD, EMAIL_LIST

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -f .env ]; then
  echo "[ERROR] .env not found in $SCRIPT_DIR"
  echo "Copy .env.example to .env and fill in your credentials."
  exit 1
fi

# Guard: refuse to run if the password is still the placeholder
if grep -q 'PASTE_YOUR_16_CHAR_APP_PASSWORD_HERE\|<YOUR_16_CHAR_APP_PASSWORD>' .env 2>/dev/null; then
  echo "[ERROR] .env still contains the password placeholder."
  echo "Open .env and paste your 16-character Gmail App Password next to GMAIL_APP_PASSWORD."
  exit 1
fi

python3 send_emails.py "$@"
