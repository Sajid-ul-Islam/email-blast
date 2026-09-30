"""
sender.py -- shared email-sending engine for email-blast.

What's new vs. the original:
  - Configurable SMTP (SMTP_HOST / SMTP_PORT / SMTP_USE_TLS env vars).
    Works with Gmail, cPanel, Zoho, SendGrid SMTP, AWS SES, etc.
  - FROM_NAME env var / from_name parameter for a display name in the
    From header, e.g. "Deen Commerce <hello@deencommerce.com>".
  - Unsubscribe support: List-Unsubscribe header + footer appended to
    both the plain-text and HTML bodies (unsubscribe_url parameter).
  - Attachment support: pass a list[Path] to send_campaign().
  - cancel_event (threading.Event) for real mid-campaign cancellation.
  - categorize_smtp_error() for human-readable error grouping in the UI.
  - Structured SendResult.to_dict() includes error_category / error_hint.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import mimetypes
import os
import re
import smtplib
import socket
import time
from email import encoders
from email.message import EmailMessage
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

# ---------------------------------------------------------------------------
# SMTP / identity config -- read from .env at runtime
# ---------------------------------------------------------------------------
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() not in ("false", "0", "no")
FROM_NAME = os.getenv("FROM_NAME", "")

# Default shared send log. The web app passes its own (same file by default),
# so the daily cap holds across the CLI, web jobs, and restarts (rules.md R-O6).
_default_log = Path(__file__).resolve().parent / "sent_log.csv"
SENT_LOG_PATH = Path(os.getenv("SENT_LOG_PATH", str(_default_log)))


def load_dotenv_once(path: Path | None = None) -> None:
    """Load .env once. Safe to call multiple times."""
    try:
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=path)
    except Exception:
        pass


def _get_env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def build_from_address(email: str, name: str = "") -> str:
    """Return 'Display Name <email>' or just 'email'."""
    display = name.strip()
    if display:
        return f"{display} <{email}>"
    return email


def validate_credentials(gmail_user: str, gmail_password: str) -> list[str]:
    """Return a list of missing credential descriptions (empty list = OK)."""
    missing: list[str] = []
    if not gmail_user:
        missing.append("GMAIL_USER / sender email")
    pw = gmail_password.replace(" ", "").replace("*", "").replace("-", "")
    if not pw or len(pw) < 6:
        missing.append("GMAIL_APP_PASSWORD / mailbox password")
    return missing


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------

def parse_recipients(filepath: Path) -> list[dict[str, Any]]:
    """
    Read a CSV and return one dict per unique email.
    Each dict: { "email": str, "name": str, "extra": {col: val, ...} }
    """
    if not filepath.exists():
        return []
    rows = _read_csv_rows(filepath)
    if not rows:
        return []

    columns = list(rows[0].keys()) if rows else []
    name_keys = {
        k for k in columns
        if k and k.strip().lower() in {"name", "fullname", "full_name", "customer_name"}
    }
    # The chosen email column is exposed as the top-level "email" field;
    # keep it out of `extra` so it does not double as a merge column.
    email_keys = {
        k for k in columns
        if k and _normalized_header(str(k)) in _EMAIL_HEADER_NAMES
    }

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        email = _find_email(row)
        if not email:
            continue
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)
        name = ""
        for k in name_keys:
            name = str(row.get(k, "")).strip()
            if name:
                break
        extra = {
            k: v for k, v in row.items()
            if k and k not in name_keys and k not in email_keys
        }
        out.append({"email": email, "name": name, "extra": extra})
    return out


def parse_content_upload(filepath: Path | str) -> dict[str, Any]:
    """Parse a .txt, .json, or .html content file into subject, plain body, and html body."""
    path = Path(filepath)
    ext = path.suffix.lower()
    text = path.read_text(encoding="utf-8", errors="replace")

    if ext == ".html":
        title_match = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
        subject = title_match.group(1).strip() if title_match else path.stem
        # Generate readable plain text version
        body = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
        body = re.sub(r"<script[^>]*>.*?</script>", "", body, flags=re.DOTALL | re.IGNORECASE)
        body = re.sub(r"<[^>]+>", " ", body)
        body = "\n".join(line.strip() for line in body.splitlines() if line.strip())
        return {
            "subject": subject,
            "body": body,
            "html_body": text,
            "source": path.name,
        }
    elif ext == ".json":
        import json
        data = json.loads(text)
        return {
            "subject": data.get("subject", path.stem),
            "body": data.get("body", ""),
            "html_body": data.get("html_body"),
            "source": path.name,
        }
    else:  # default or .txt
        lines = text.splitlines()
        subject = lines[0].strip() if lines else path.stem
        body = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""
        return {
            "subject": subject,
            "body": body,
            "html_body": None,
            "source": path.name,
        }


def _read_csv_rows(filepath: Path) -> list[dict[str, str]]:
    with filepath.open(newline="", encoding="utf-8", errors="replace") as f:
        sample = f.read(8192)
        f.seek(0)
        try:
            sniffer = csv.Sniffer()
            # Restricted delimiters: on small samples the sniffer can otherwise
            # guess odd separators (colons, spaces) and shred the rows.
            dialect = sniffer.sniff(sample, delimiters=",;\t")
        except Exception:
            dialect = csv.excel

        # Header detection: csv.Sniffer.has_header() is unreliable on small
        # recipient lists ("email,name" vs "a@x.com,A" both score 1.0), which
        # used to drop real column names and replace them with col_0, col_1...
        # The type-based check is decisive for our use case: a header row's
        # cells are column NAMES, so no cell in it can look like an address.
        first_line = sample.splitlines()[0] if sample else ""
        try:
            header_cells = next(csv.reader([first_line], dialect=dialect))
            has_header = not any(_looks_like_email(str(c)) for c in header_cells)
        except (StopIteration, csv.Error):
            has_header = True

        reader = csv.DictReader(f, dialect=dialect)
        if not has_header:
            first = next(reader, None)            if first is not None:
                synthetic = [f"col_{i}" for i in range(len(first))]
                f.seek(0)
                reader = csv.DictReader(f, fieldnames=synthetic)
        return list(reader)


def _normalized_header(s: str) -> str:
    """Lowercase and strip separators so 'E-mail_Address' == 'emailaddress'."""
    return re.sub(r"[\s_\-]+", "", s.strip().lower())


# Headers that explicitly name the recipient's own email address.
_EMAIL_HEADER_NAMES = {"email", "mail", "emailaddress"}

# Column names that hold SOMEONE ELSE'S address (referrer, backup, ...) --
# never auto-selected, because sending there would mail the wrong person.
_OTHER_PERSON_MARKERS = (
    "referrer", "referral", "referer", "backup", "alternate", "altmail",
    "manager", "emergency", "parent", "guardian", "replyto", "bounce",
    "ccmail", "ccemail",
)


def _find_email(row: dict[str, str]) -> str | None:
    """Pick the recipient's email address from a CSV row.

    1. Prefer a column explicitly named for the recipient's address
       (email, e-mail, mail, email_address...). This stops a
       referrer_email column earlier in the row from winning.
    2. Fall back to the first email-looking value in a column that is
       not flagged as someone else's address (referrer_email,
       backup_email, reply_to...). A generic address-ish column such as
       contact_email still qualifies; if ONLY other-person columns
       hold addresses, return None rather than guess.
    """
    for key, value in row.items():
        if _normalized_header(str(key)) in _EMAIL_HEADER_NAMES:
            candidate = str(value).strip()
            if _looks_like_email(candidate):
                return candidate
    for key, value in row.items():
        norm = _normalized_header(str(key))
        if any(marker in norm for marker in _OTHER_PERSON_MARKERS):
            continue
        candidate = str(value).strip()
        if _looks_like_email(candidate):
            return candidate
    return None


def _looks_like_email(candidate: str) -> bool:
    c = candidate.strip().lower()
    if not c or "@" not in c:
        return False
    local, _, domain = c.partition("@")
    return bool(local and domain and "." in domain)


# ---------------------------------------------------------------------------
# Personalisation
# ---------------------------------------------------------------------------

# Merge field syntax: {{field}}, {{ field }}, case-insensitive.
_MERGE_FIELD_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


def _recipient_field_values(recipient: dict[str, Any], extra_vars: dict | None = None) -> dict[str, str]:
    """Lowercased field name -> resolved value for one recipient."""
    values = {
        _normalized_header(str(k)): str(v)
        for k, v in (recipient.get("extra") or {}).items()
    }
    if extra_vars:
        values.update({_normalized_header(str(k)): str(v) for k, v in extra_vars.items()})
    # email/name are authoritative even if a CSV column collides.
    values["email"] = recipient.get("email", "")
    values["name"] = recipient.get("name", "")
    return values


def personalize(
    body_template: str,
    recipient: dict[str, Any],
    extra_vars: dict | None = None,
) -> str:
    """
    Replace {{field}} merge fields with recipient data (rules.md R-C2).

    Supports: {{email}}, {{name}}, any CSV column key, and extra_vars.
    Lookup is case-insensitive ({{Name}} matches a 'name' column). A field
    with no value for this recipient resolves to an empty string -- a
    literal {{field}} is never sent.
    """
    if not body_template:
        return body_template
    values = _recipient_field_values(recipient, extra_vars)
    return _MERGE_FIELD_RE.sub(lambda m: values.get(_normalized_header(m.group(1)), ""), body_template)


def unknown_merge_fields(templates: list[str], known_fields: set[str]) -> list[str]:
    """Return merge fields referenced in templates but missing from known_fields.

    'email' and 'name' are always known (they resolve to '' when the CSV has
    no such column). Use this before sending so a typo like {{city}} against
    a 'town' column is caught instead of going to every recipient as ''.
    """
    known = {"email", "name"} | {_normalized_header(f) for f in known_fields}
    unknown: list[str] = []
    for template in templates:
        if not template:
            continue
        for m in _MERGE_FIELD_RE.finditer(template):
            field = _normalized_header(m.group(1))
            if field and field not in known and field not in unknown:
                unknown.append(field)
    return unknown


# ---------------------------------------------------------------------------
# Unsubscribe tokens + suppression list
# ---------------------------------------------------------------------------

# Tokens are HMAC(email, secret) bound to an expiry, so an unsubscribe link
# cannot be forged for an address the sender chose not to link.
UNSUBSCRIBE_TOKEN_TTL_SECONDS = 2 * 365 * 24 * 3600


def make_unsubscribe_token(email: str, secret: str) -> str:
    """Return a URL-safe signed token authorizing opt-out for `email`."""
    exp = int(time.time()) + UNSUBSCRIBE_TOKEN_TTL_SECONDS
    msg = f"{email.strip().lower()}:{exp}"
    sig = hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode(f"{exp}:{sig}".encode("utf-8")).decode("ascii").rstrip("=")


def verify_unsubscribe_token(email: str, token: str, secret: str) -> bool:
    """True when `token` is a valid, unexpired signature for `email`."""
    if not email or not token or not secret:
        return False
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("ascii")
        exp_str, sig = raw.split(":", 1)
        exp = int(exp_str)
    except Exception:
        return False
    if exp < time.time():
        return False
    msg = f"{email.strip().lower()}:{exp}"
    expected = hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(sig, expected)


def build_unsubscribe_url(template: str, recipient: dict[str, Any], secret: str = "") -> str:
    """Resolve {{email}} (URL-encoded) and {{token}} (signed, if secret given)."""
    email = recipient.get("email", "")
    values = {
        "email": quote(email, safe=""),  # addresses with + or spaces stay valid in URLs
        "token": make_unsubscribe_token(email, secret) if secret else "",
    }
    return _MERGE_FIELD_RE.sub(lambda m: values.get(_normalized_header(m.group(1)), ""), template)


def load_suppressed_emails(path: Path | None) -> set[str]:
    """Lowercased emails from the suppression file (one per line)."""
    if not path or not path.exists():
        return set()
    suppressed: set[str] = set()
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            entry = line.strip().lower()
            if entry and not entry.startswith("#") and _looks_like_email(entry):
                suppressed.add(entry)
    except OSError:
        pass
    return suppressed


def append_to_suppression(path: Path | None, email: str) -> None:
    """Record an opt-out. File is append-only, addresses stored lowercased."""
    if not path:
        return
    entry = email.strip().lower()
    if not entry:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(entry + "\n")


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

# Replies seen in the wild (RFC 5321 + common extensions). Restricting code
# extraction to this set stops numbers inside message text (queue IDs,
# timestamps, '5000 recipients') from being read as SMTP status codes.
_VALID_SMTP_CODES = {
    "211", "214", "220", "221", "235", "250", "251", "252",
    "334", "354",
    "421", "431", "450", "451", "452", "454", "455", "458", "459", "465",
    "500", "501", "502", "503", "504", "521", "523", "530", "534", "535",
    "538", "550", "551", "552", "553", "554", "555",
}
# The lookbehinds reject number-like tokens around a code:
#   'f450x'  (word char)   '2,450' (digit grouping)   '12:450:00' (odd clock)
#   '<450.99@host>' is rejected on the right by (?!\.\d).
# Real replies look like '450 4.2.0 ...' or "(450, 'reason')" and still match.
_SMTP_CODE_RE = re.compile(r"(?<![\d,.:\w])([245]\d{2})\b(?!\.\d)")


def _smtp_status_code(error: str) -> str:
    """Extract the first plausible 3-digit SMTP status code from an error."""
    for m in _SMTP_CODE_RE.finditer(error):
        if m.group(1) in _VALID_SMTP_CODES:
            return m.group(1)
    return ""


def categorize_smtp_error(error: str) -> dict[str, str]:
    """Return {code, category, hint} for a human-readable error display."""
    if not error:
        return {"code": "", "category": "unknown", "hint": ""}
    e = error.lower()
    code = _smtp_status_code(error)

    if code.startswith("5") and code not in ("500", "501", "502", "503", "504", "505"):
        # Permanent 5xx: refine by the specific code before phrases.
        if code == "550" or any(x in e for x in ("does not exist", "no such user", "user unknown", "invalid recipient", "recipient address rejected")):
            return {"code": code, "category": "invalid_address", "hint": "Email address does not exist."}
        if code == "552" or any(x in e for x in ("message too large", "exceeded size", "too big")):
            return {"code": code, "category": "message_too_large", "hint": "Message or attachment is too large."}
        if code == "553":
            return {"code": code, "category": "policy_rejection", "hint": "Rejected by recipient mail policy."}
        if code in ("530", "534", "535") or any(x in e for x in (
                "authentication required", "authenticationfailed", "auth failed",
                "invalid credentials", "username and password", "app password",
                "badcredentials", "please log in via your web browser")):
            return {"code": code, "category": "auth_error", "hint": "Authentication failed. Check SMTP credentials."}
        if any(x in e for x in ("spam", "blocked", "blacklist", "dnsbl", "junkmail")):
            return {"code": code, "category": "spam_blocked", "hint": "Message blocked as spam."}
        if code == "554" and "try again" in e:
            return {"code": code, "category": "transient", "hint": "Temporary failure — retried automatically."}
        return {"code": code, "category": "policy_rejection", "hint": "Permanently rejected by the mail server."}

    if code.startswith("4") or any(x in e for x in (
            "try again later", "rate limit", "greylist", "server busy",
            "queue full", "temporary failure", "resources temporarily unavailable")):
        return {"code": code, "category": "transient", "hint": "Temporary failure — retried automatically."}

    if code.startswith("2"):
        # A 2xx string alone is not an error description; classify by text.
        pass

    if any(x in e for x in ("spam", "blocked", "blacklist", "dnsbl")):
        return {"code": code, "category": "spam_blocked", "hint": "Message blocked as spam."}
    if any(x in e for x in (
            "authentication required", "authenticationfailed", "auth failed",
            "invalid credentials", "username and password", "app password",
            "badcredentials")):
        return {"code": code, "category": "auth_error", "hint": "Authentication failed. Check SMTP credentials."}
    if any(x in e for x in ("connection", "timeout", "timed out", "network", "errno", "reset by peer", "disconnected")):
        return {"code": code, "category": "connection_error", "hint": "Connection to SMTP server failed."}
    return {"code": code, "category": "other", "hint": error[:120]}


def is_transient_error(error: str) -> bool:
    """True for errors worth retrying: any 4xx code, or a drop/timeout phrase.

    Codes are extracted with _smtp_status_code (validated, boundary-anchored)
    instead of substring matching, so '450' inside a queue ID or a timestamp
    no longer reads as an SMTP status.
    """
    if not error:
        return False
    code = _smtp_status_code(error)
    if code.startswith("4"):
        return True
    e = error.lower()
    phrases = [
        "try again", "too many connections", "rate limit",
        "timeout", "timed out", "connection reset", "connection refused",
        "temporary failure", "resources temporarily unavailable",
        "greylist", "server busy", "queue full",
        # Server may close the session between sends; treat as transient so
        # the recipient is retried after a fresh connection.
        "server disconnected", "connection unexpectedly closed",
        "bad handshake", "broken pipe", "not connected",
        "reset by peer", "errno",
    ]
    return any(p in e for p in phrases)


_TRANSIENT_SMTP_CODE_RE = re.compile(r"^4\d\d")


def _is_disconnect_error(exc: BaseException) -> bool:
    """True when the exception means the SMTP session is no longer usable."""
    if isinstance(exc, (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError)):
        return True
    if isinstance(exc, (OSError, socket.timeout, TimeoutError, EOFError)):
        # smtplib wraps many drops in plain OSError / socket.timeout.
        return True
    if isinstance(exc, smtplib.SMTPResponseException):
        # A 4xx session-level reply usually means the session is unusable.
        return _TRANSIENT_SMTP_CODE_RE.match(str(exc.smtp_code)) is not None
    return False


# ---------------------------------------------------------------------------
# Log helpers
# ---------------------------------------------------------------------------

def count_today_sent(log_path: Path | None) -> int:
    """Count rows with status=sent logged today (UTC) in a CSV log."""
    if not log_path:
        log_path = SENT_LOG_PATH
    if not log_path or not log_path.exists():
        return 0
    today = datetime.now(timezone.utc).date().isoformat()
    count = 0
    try:
        with log_path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("status") == "sent" and row.get("timestamp", "").startswith(today):
                    count += 1
    except Exception:
        pass
    return count


def _append_log(log_path: Path, result: "SendResult") -> None:
    write_header = not log_path.exists() or log_path.stat().st_size == 0
    with log_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["timestamp", "email", "status", "error", "error_category"],
        )
        if write_header:
            writer.writeheader()
        d = result.to_dict()
        writer.writerow({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "email": d["email"],
            "status": d["status"],
            "error": d["error"],
            "error_category": d.get("error_category", ""),
        })


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class RateLimiter:
    """Pacing + daily cap for a send campaign."""

    def __init__(
        self,
        daily_cap: int = 100,
        pace_mode: str = "auto",
        pace_seconds: float | None = None,
        emails_per_hour: float | None = None,
        sent_today: int = 0,
    ):
        self.daily_cap = int(daily_cap) if daily_cap else 100
        self.pace_mode = pace_mode or "auto"
        self.pace_seconds = float(pace_seconds) if pace_seconds else None
        self.emails_per_hour = float(emails_per_hour) if emails_per_hour else None
        self._list_size = 100
        self._auto_pace_seconds = 60.0
        self._backoff_multiplier = 1.0
        self._transient_failures = 0
        self.sent_today = int(sent_today)
        self.last_send_time = 0.0
        self._compute_auto_pace()

    def set_list_size(self, n: int) -> None:
        self._list_size = n
        if self.pace_mode == "auto":
            self._compute_auto_pace()

    def _compute_auto_pace(self) -> None:
        n = self._list_size
        if n <= 50:
            self._auto_pace_seconds = 45.0
        elif n <= 200:
            self._auto_pace_seconds = 60.0
        elif n <= 500:
            self._auto_pace_seconds = 90.0
        else:
            self._auto_pace_seconds = 120.0

    @property
    def pace_description(self) -> str:
        if self.pace_mode == "fixed":
            return f"1 email every {self.pace_seconds:.0f}s"
        if self.pace_mode == "per_hour":
            return f"{self.emails_per_hour:.0f} emails/hour"
        return f"Auto: 1 email every {self._auto_pace_seconds:.0f}s (with daylight pacing)"

    def remaining_today(self) -> int:
        return max(0, self.daily_cap - self.sent_today)

    def can_send(self) -> bool:
        return self.sent_today < self.daily_cap

    def record_success(self) -> None:
        self.sent_today += 1
        self.last_send_time = time.monotonic()
        self._backoff_multiplier = 1.0
        self._transient_failures = 0

    def record_transient_failure(self) -> None:
        self._transient_failures += 1
        self._backoff_multiplier = min(8.0, float(2 ** self._transient_failures))
        self.last_send_time = time.monotonic()

    def record_permanent_failure(self) -> None:
        self.last_send_time = time.monotonic()

    @property
    def _effective_pace_seconds_value(self) -> float:
        if self.pace_mode == "fixed" and self.pace_seconds:
            return self.pace_seconds
        if self.pace_mode == "per_hour" and self.emails_per_hour and self.emails_per_hour > 0:
            return 3600.0 / self.emails_per_hour
        return self._auto_pace_seconds

    def _effective_pace_seconds(self, recipient_utc_offset_hours: float = 6.0) -> float:
        """Base pace multiplied by a smooth daylight curve."""
        base = self._effective_pace_seconds_value
        if base <= 0:
            return base
        utc_now = datetime.now(timezone.utc)
        local_now = utc_now + timedelta(hours=recipient_utc_offset_hours)
        local_hour = local_now.hour + local_now.minute / 60.0
        return base * max(0.5, min(1.5, _daylight_boost_for_hour(local_hour)))

    @property
    def effective_pace_seconds(self) -> float:
        return self._effective_pace_seconds()

    def wait_before_next(self, recipient_utc_offset_hours: float = 6.0) -> float:
        now = time.monotonic()
        elapsed = now - self.last_send_time if self.last_send_time else 0.0
        target = self._effective_pace_seconds(recipient_utc_offset_hours) * self._backoff_multiplier
        return max(0.0, target - elapsed)

    def estimate_duration_seconds(self, total: int) -> float:
        return max(0.0, total * self._effective_pace_seconds_value) if total > 0 else 0.0

    def estimate_finish_iso(self, total: int) -> str:
        secs = self.estimate_duration_seconds(total)
        return (datetime.now(timezone.utc) + timedelta(seconds=secs)).isoformat()


def _daylight_boost_for_hour(local_hour: float) -> float:
    if 10 <= local_hour <= 16:
        return 1.0
    if 6 <= local_hour < 10:
        return 1.1 - 0.1 * ((local_hour - 6) / 4.0)
    if 16 < local_hour <= 21:
        return 1.0 + 0.2 * ((local_hour - 16) / 5.0)
    return 1.5


# ---------------------------------------------------------------------------
# SendResult
# ---------------------------------------------------------------------------

class SendResult:
    """Outcome for one recipient."""

    def __init__(self, email: str, status: str, error: str = ""):
        self.email = email
        self.status = status  # "sent" | "failed" | "skipped"
        self.error = error

    def to_dict(self) -> dict[str, str]:
        err_info = categorize_smtp_error(self.error)
        return {
            "email": self.email,
            "status": self.status,
            "error": self.error,
            "error_category": err_info["category"],
            "error_hint": err_info["hint"],
            "error_code": err_info["code"],
        }


# ---------------------------------------------------------------------------
# Message builder (handles plain / HTML / attachments)
# ---------------------------------------------------------------------------

def _build_message(
    from_addr: str,
    to: str,
    subject: str,
    plain_body: str,
    html_body: str = "",
    attachments: list[Path] | None = None,
    unsubscribe_url: str = "",
) -> Any:
    """
    Build an email message object.

    MIME structure:
      No attachments:  EmailMessage (plain or multipart/alternative)
      With attachments: MIMEMultipart/mixed
                          multipart/alternative (plain + html)
                          attachment(s)

    Raises ValueError when an attachment cannot be read -- the send must not
    silently go out without a file the operator explicitly attached.
    """
    plain = plain_body
    html = html_body

    # Unsubscribe footer
    if unsubscribe_url:
        plain = plain.rstrip() + f"\n\n---\nTo unsubscribe: {unsubscribe_url}"
        if html:
            unsub_html = (
                '<hr style="margin:32px 0;border:none;border-top:1px solid #e2e5ea">'
                '<p style="font-size:12px;color:#6b7280;text-align:center">'
                f'<a href="{unsubscribe_url}" style="color:#6b7280">Unsubscribe</a></p>'
            )
            html = re.sub(r"</body\s*>", unsub_html + "</body>", html, flags=re.IGNORECASE) \
                if re.search(r"</body\s*>", html, re.IGNORECASE) \
                else html + unsub_html

    lu_header = f"<{unsubscribe_url}>" if unsubscribe_url else ""

    if attachments:
        msg = MIMEMultipart("mixed")
        msg["From"] = from_addr
        msg["To"] = to
        msg["Subject"] = subject
        if lu_header:
            msg["List-Unsubscribe"] = lu_header
            msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(plain, "plain", "utf-8"))
        if html:
            alt.attach(MIMEText(html, "html", "utf-8"))
        msg.attach(alt)

        for filepath in attachments:
            p = Path(filepath)
            mime_type, _ = mimetypes.guess_type(str(p))
            maintype, subtype = (mime_type or "application/octet-stream").split("/", 1)
            with p.open("rb") as f:
                part = MIMEBase(maintype, subtype)
                part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", "attachment", filename=p.name)
                msg.attach(part)
    else:
        msg = EmailMessage()
        msg["From"] = from_addr
        msg["To"] = to
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=False, usegmt=True)
        msg["Message-ID"] = make_msgid(domain=(from_addr.split("@")[-1].rstrip(">") or "localhost"))
        if lu_header:
            msg["List-Unsubscribe"] = lu_header
            msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        msg.set_content(plain, subtype="plain", charset="utf-8")
        if html:
            msg.add_alternative(html, subtype="html", charset="utf-8")

    return msg


# ---------------------------------------------------------------------------
# Core send function
# ---------------------------------------------------------------------------

def send_campaign(
    gmail_user: str,
    gmail_password: str,
    recipients: list[dict[str, Any]],
    subject: str,
    body_template: str,
    html_body_template: str = "",
    log_path: Path | None = None,
    throttle_seconds: float = 0.0,
    on_result: callable | None = None,
    daily_cap: int = 100,
    pace_mode: str = "auto",
    pace_seconds: float | None = None,
    emails_per_hour: float | None = None,
    cancel_event=None,
    from_name: str = "",
    unsubscribe_url: str = "",
    unsubscribe_secret: str = "",
    attachments: list[Path] | None = None,
) -> list[SendResult]:
    """
    Send one email per recipient with full rate limiting and cancellation.

    Opens a fresh SMTP connection per send and reconnects transparently, so a
    long-paced campaign survives the server dropping the idle session.

    Parameters
    ----------
    from_name       : Display name for the From header.
    unsubscribe_url : Appended as footer + List-Unsubscribe header.
                      Supports {{email}} (URL-encoded automatically) and
                      {{token}} (signed, requires unsubscribe_secret).
    unsubscribe_secret : HMAC secret for {{token}} in unsubscribe links.
    attachments     : Files to attach to every email. A missing/unreadable
                      attachment fails the send loudly instead of sending
                      without it.
    cancel_event    : threading.Event -- stops cleanly when set. Waits between
                      sends wake up immediately on cancel.
    """
    missing = validate_credentials(gmail_user, gmail_password)
    if missing:
        err = "Missing credentials: " + ", ".join(missing)
        results = [SendResult(r["email"], "failed", err) for r in recipients]
        if on_result:
            for r in results:
                on_result(r)
        return results

    # Legacy throttle_seconds upgrades auto -> fixed
    if throttle_seconds > 0 and pace_mode == "auto":
        pace_mode = "fixed"
        pace_seconds = throttle_seconds

    from_addr = build_from_address(gmail_user, from_name or FROM_NAME)

    limiter = RateLimiter(
        daily_cap=daily_cap,
        pace_mode=pace_mode,
        pace_seconds=pace_seconds,
        emails_per_hour=emails_per_hour,
        sent_today=count_today_sent(log_path),
    )
    limiter.set_list_size(len(recipients))

    # One connection per send: idle SMTP sessions are routinely dropped after a
    # few minutes, which would otherwise fail every remaining recipient.
    def _connect() -> smtplib.SMTP:
        s = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
        try:
            s.ehlo()
            if SMTP_USE_TLS:
                s.starttls()
                s.ehlo()
            s.login(gmail_user, gmail_password)
            return s
        except BaseException:
            try:
                s.quit()
            except Exception:
                pass
            raise

    def _close(s: smtplib.SMTP | None) -> None:
        if s is not None:
            try:
                s.quit()
            except Exception:
                pass

    def _send_once(
        recipient: dict[str, Any],
        unsub: str,
        smtp: smtplib.SMTP | None,
    ) -> tuple[SendResult, smtplib.SMTP | None]:
        """One send attempt on the given connection (new connection if None).

        Returns (result, smtp). On a dead session, returns a sentinel result
        and None so the caller reconnects and retries the same recipient.
        """
        plain = personalize(body_template, recipient)
        html = personalize(html_body_template, recipient) if html_body_template else ""
        raw_from = from_addr.split("<")[-1].rstrip(">") if "<" in from_addr else from_addr

        # Build the message outside the try: an unreadable attachment must fail
        # the send loudly, not silently go out without the file.
        try:
            msg = _build_message(
                from_addr=from_addr,
                to=recipient["email"],
                subject=subject,
                plain_body=plain,
                html_body=html,
                attachments=attachments,
                unsubscribe_url=unsub,
            )
        except Exception as exc:
            return SendResult(recipient["email"], "failed", f"Could not build message: {exc}"), smtp

        try:
            if smtp is None:
                smtp = _connect()
            smtp.sendmail(raw_from, [recipient["email"]], msg.as_string())
            return SendResult(recipient["email"], "sent", ""), smtp
        except Exception as exc:
            if _is_disconnect_error(exc):
                _close(smtp)
                # Sentinel: caller reconnects and retries this same recipient.
                return SendResult(recipient["email"], "failed", "__DISCONNECTED__"), None
            return SendResult(recipient["email"], "failed", str(exc)), smtp

    def _send_recipient(
        recipient: dict[str, Any],
        unsub: str,
        smtp: smtplib.SMTP | None,
    ) -> tuple[SendResult, smtplib.SMTP | None]:
        """Send with up to 2 reconnect attempts on dropped sessions."""
        result, smtp = _send_once(recipient, unsub, smtp)
        if result.error == "__DISCONNECTED__":
            for _ in range(2):
                result, smtp = _send_once(recipient, unsub, None)
                if result.error != "__DISCONNECTED__":
                    break
            if result.error == "__DISCONNECTED__":
                result = SendResult(
                    recipient["email"], "failed",
                    f"SMTP connection to {SMTP_HOST}:{SMTP_PORT} dropped repeatedly; giving up.",
                )
        return result, smtp

    def _cancelable_wait(seconds: float) -> None:
        """Sleep for `seconds`, returning early when cancel_event is set."""
        if seconds <= 0:
            return
        if cancel_event is not None:
            if cancel_event.wait(seconds):
                return
        else:
            time.sleep(seconds)

    def _record(result: SendResult) -> None:
        """Emit to on_result and append to the shared CSV log exactly once."""
        if on_result:
            on_result(result)
        if log_path is not None:
            _append_log(log_path, result)

    results: list[SendResult] = []
    smtp: smtplib.SMTP | None = None
    retry_queue: list[dict[str, Any]] = []
    connection_error = None

    try:
        for i, recipient in enumerate(recipients, 1):
            if cancel_event is not None and cancel_event.is_set():
                result = SendResult(recipient["email"], "skipped", "Job cancelled by user.")
                results.append(result)
                _record(result)
                continue

            if not limiter.can_send():
                err = f"Daily send cap ({limiter.daily_cap}) reached after {limiter.sent_today} emails."
                result = SendResult(recipient["email"], "skipped", err)
                results.append(result)
                _record(result)
                continue

            wait = limiter.wait_before_next()
            _cancelable_wait(wait)
            if cancel_event is not None and cancel_event.is_set():
                result = SendResult(recipient["email"], "skipped", "Job cancelled by user.")
                results.append(result)
                _record(result)
                continue

            # Personalise unsubscribe URL per recipient: {{email}} is
            # URL-encoded and {{token}} carries a signed opt-out (when a
            # secret is configured).
            if unsubscribe_url:
                unsub = build_unsubscribe_url(unsubscribe_url, recipient, unsubscribe_secret)
            else:
                unsub = ""

            try:
                result, smtp = _send_recipient(recipient, unsub, smtp)
            except Exception as exc:
                # Reconnect+retries exhausted (e.g. auth revoked mid-run):
                # stop instead of failing the rest one by one.
                connection_error = exc
                break

            if result.status == "sent":
                limiter.record_success()
            elif result.error == "__DISCONNECTED__":
                limiter.record_transient_failure()
                retry_queue.append({"recipient": recipient, "unsub": unsub})
            elif is_transient_error(result.error):
                limiter.record_transient_failure()
                retry_queue.append({"recipient": recipient, "unsub": unsub})
            else:
                limiter.record_permanent_failure()

            results.append(result)
            _record(result)

        # Retry transient failures (up to 3 attempts each)
        if connection_error is None:
            for entry in retry_queue:
                if cancel_event is not None and cancel_event.is_set():
                    break
                recipient = entry["recipient"]
                unsub = entry.get("unsub", "")
                if not limiter.can_send():
                    result = SendResult(recipient["email"], "skipped",
                                        "Daily cap reached before retry.")
                    results.append(result)
                    _record(result)
                    continue
                for attempt in range(1, 4):
                    wait = limiter.wait_before_next()
                    _cancelable_wait(wait)
                    if cancel_event is not None and cancel_event.is_set():
                        break
                    try:
                        result, smtp = _send_recipient(recipient, unsub, smtp)
                    except Exception as exc:
                        connection_error = exc
                        break
                    if result.status == "sent":
                        # Retry note kept out of `error` so a successful send is
                        # never categorised as an error (categorize_smtp_error).
                        result.note = f"sent on retry attempt {attempt}"
                        limiter.record_success()
                        results.append(result)
                        _record(result)
                        break
                    elif result.error == "__DISCONNECTED__":
                        limiter.record_transient_failure()
                        continue
                    elif is_transient_error(result.error):
                        limiter.record_transient_failure()
                        _cancelable_wait(
                            limiter.effective_pace_seconds * limiter._backoff_multiplier
                        )
                    else:
                        limiter.record_permanent_failure()
                        results.append(result)
                        _record(result)
                        break
                else:
                    result = SendResult(recipient["email"], "failed", "Max retries (3) exceeded")
                    results.append(result)
                    _record(result)
    finally:
        _close(smtp)

    if connection_error is not None:
        # Mark the not-yet-attempted remainder as skipped, without recording
        # duplicate failures for recipients that already have an outcome.
        attempted = {r.email for r in results}
        for recipient in recipients:
            if recipient["email"] in attempted:
                continue
            result = SendResult(
                recipient["email"], "skipped",
                f"SMTP connection failed ({SMTP_HOST}:{SMTP_PORT}): {connection_error}",
            )
            results.append(result)
            _record(result)

    return results
