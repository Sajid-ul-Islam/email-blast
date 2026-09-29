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

import csv
import mimetypes
import os
import re
import smtplib
import time
from email import encoders
from email.message import EmailMessage
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# SMTP / identity config -- read from .env at runtime
# ---------------------------------------------------------------------------
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() not in ("false", "0", "no")
FROM_NAME = os.getenv("FROM_NAME", "")


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
        extra = {k: v for k, v in row.items() if k and k.strip().lower() not in name_keys}
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
            dialect = sniffer.sniff(sample)
            has_header = sniffer.has_header(sample)
        except Exception:
            dialect = csv.excel
            has_header = True

        reader = csv.DictReader(f, dialect=dialect)
        if not has_header:
            first = next(reader, None)
            if first is not None:
                synthetic = [f"col_{i}" for i in range(len(first))]
                f.seek(0)
                reader = csv.DictReader(f, fieldnames=synthetic)
        return list(reader)


def _find_email(row: dict[str, str]) -> str | None:
    for value in row.values():
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

def personalize(
    body_template: str,
    recipient: dict[str, Any],
    extra_vars: dict | None = None,
) -> str:
    """
    Replace {{field}} merge fields with recipient data.
    Supports: {{name}}, {{email}}, any CSV column key, and extra_vars.
    """
    text = body_template
    text = text.replace("{{email}}", recipient.get("email", ""))
    name = recipient.get("name", "")
    text = text.replace("{{name}}", name)
    for key, value in recipient.get("extra", {}).items():
        text = text.replace("{{" + key + "}}", str(value))
    if extra_vars:
        for key, value in extra_vars.items():
            text = text.replace("{{" + key + "}}", str(value))
    return text


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

def categorize_smtp_error(error: str) -> dict[str, str]:
    """Return {code, category, hint} for a human-readable error display."""
    if not error:
        return {"code": "", "category": "unknown", "hint": ""}
    e = error.lower()
    m = re.search(r"\b([245]\d\d)\b", error)
    code = m.group(1) if m else ""

    if code == "550" or any(x in e for x in ("does not exist", "no such user", "user unknown", "invalid recipient")):
        return {"code": code, "category": "invalid_address", "hint": "Email address does not exist."}
    if code == "552" or any(x in e for x in ("message too large", "exceeded size", "too big")):
        return {"code": code, "category": "message_too_large", "hint": "Message or attachment is too large."}
    if code == "553" or "not allowed" in e:
        return {"code": code, "category": "policy_rejection", "hint": "Rejected by recipient mail policy."}
    if code in ("421", "450", "451") or any(x in e for x in ("try again later", "rate limit", "greylisted", "too many")):
        return {"code": code, "category": "transient", "hint": "Temporary failure — retried automatically."}
    if any(x in e for x in ("spam", "blocked", "blacklist", "dnsbl")):
        return {"code": code, "category": "spam_blocked", "hint": "Message blocked as spam."}
    if any(x in e for x in ("auth", "login", "credential", "535", "534")):
        return {"code": code, "category": "auth_error", "hint": "Authentication failed. Check SMTP credentials."}
    if any(x in e for x in ("connection", "timeout", "network", "errno", "reset by peer")):
        return {"code": code, "category": "connection_error", "hint": "Connection to SMTP server failed."}
    return {"code": code, "category": "other", "hint": error[:120]}


def is_transient_error(error: str) -> bool:
    if not error:
        return False
    e = error.lower()
    markers = [
        "try again", "too many connections", "rate limit", "421", "450", "451",
        "timeout", "connection reset", "temporary", "greylisted", "server busy",
        "queue full", "timed out", "errno 110", "errno 104",
    ]
    return any(m in e for m in markers)


# ---------------------------------------------------------------------------
# Log helpers
# ---------------------------------------------------------------------------

def count_today_sent(log_path: Path) -> int:
    """Count rows with status=sent logged today (UTC) in a CSV log."""
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
            try:
                with p.open("rb") as f:
                    part = MIMEBase(maintype, subtype)
                    part.set_payload(f.read())
                    encoders.encode_base64(part)
                    part.add_header("Content-Disposition", "attachment", filename=p.name)
                    msg.attach(part)
            except Exception:
                pass
    else:
        msg = EmailMessage()
        msg["From"] = from_addr
        msg["To"] = to
        msg["Subject"] = subject
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
    attachments: list[Path] | None = None,
) -> list[SendResult]:
    """
    Send one email per recipient with full rate limiting and cancellation.

    Parameters
    ----------
    from_name       : Display name for the From header.
    unsubscribe_url : Appended as footer + List-Unsubscribe header.
                      Supports {{email}} merge field.
    attachments     : Files to attach to every email.
    cancel_event    : threading.Event -- stops cleanly when set.
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
        sent_today=count_today_sent(log_path) if log_path else 0,
    )
    limiter.set_list_size(len(recipients))

    # Connect using configurable SMTP settings
    smtp = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
    try:
        smtp.ehlo()
        if SMTP_USE_TLS:
            smtp.starttls()
            smtp.ehlo()
        smtp.login(gmail_user, gmail_password)
    except Exception as exc:
        err = f"SMTP login failed ({SMTP_HOST}:{SMTP_PORT}): {exc}"
        results = [SendResult(r["email"], "failed", err) for r in recipients]
        if on_result:
            for r in results:
                on_result(r)
        return results

    results: list[SendResult] = []
    retry_queue: list[dict[str, Any]] = []

    def _send_recipient(recipient: dict, unsub: str) -> SendResult:
        plain = personalize(body_template, recipient)
        html = personalize(html_body_template, recipient) if html_body_template else ""
        msg = _build_message(
            from_addr=from_addr,
            to=recipient["email"],
            subject=subject,
            plain_body=plain,
            html_body=html,
            attachments=attachments,
            unsubscribe_url=unsub,
        )
        raw_from = from_addr.split("<")[-1].rstrip(">") if "<" in from_addr else from_addr
        try:
            smtp.sendmail(raw_from, [recipient["email"]], msg.as_string())
            return SendResult(recipient["email"], "sent", "")
        except Exception as exc:
            return SendResult(recipient["email"], "failed", str(exc))

    try:
        for i, recipient in enumerate(recipients, 1):
            if cancel_event is not None and cancel_event.is_set():
                result = SendResult(recipient["email"], "skipped", "Job cancelled by user.")
                results.append(result)
                if on_result:
                    on_result(result)
                continue

            if not limiter.can_send():
                err = f"Daily send cap ({limiter.daily_cap}) reached after {limiter.sent_today} emails."
                result = SendResult(recipient["email"], "skipped", err)
                results.append(result)
                if on_result:
                    on_result(result)
                continue

            wait = limiter.wait_before_next()
            if wait > 0:
                time.sleep(wait)

            # Personalise unsubscribe URL per recipient
            unsub = personalize(unsubscribe_url, recipient) if unsubscribe_url else ""

            result = _send_recipient(recipient, unsub)

            if result.status == "sent":
                limiter.record_success()
            elif is_transient_error(result.error):
                limiter.record_transient_failure()
                retry_queue.append({"recipient": recipient, "unsub": unsub})
            else:
                limiter.record_permanent_failure()

            results.append(result)
            if on_result:
                on_result(result)
            if log_path is not None:
                _append_log(log_path, result)

        # Retry transient failures (up to 3 attempts each)
        for entry in retry_queue:
            if cancel_event is not None and cancel_event.is_set():
                break
            recipient = entry["recipient"]
            unsub = entry.get("unsub", "")
            if not limiter.can_send():
                result = SendResult(recipient["email"], "skipped",
                                    "Daily cap reached before retry.")
                results.append(result)
                if on_result:
                    on_result(result)
                continue
            for attempt in range(1, 4):
                wait = limiter.wait_before_next()
                if wait > 0:
                    time.sleep(wait)
                result = _send_recipient(recipient, unsub)
                if result.status == "sent":
                    result.error = f"retry {attempt}"
                    limiter.record_success()
                    results.append(result)
                    if on_result:
                        on_result(result)
                    if log_path is not None:
                        _append_log(log_path, result)
                    break
                elif is_transient_error(result.error):
                    limiter.record_transient_failure()
                    time.sleep(limiter.effective_pace_seconds * limiter._backoff_multiplier)
                else:
                    limiter.record_permanent_failure()
                    results.append(result)
                    if on_result:
                        on_result(result)
                    if log_path is not None:
                        _append_log(log_path, result)
                    break
            else:
                result = SendResult(recipient["email"], "failed", "Max retries (3) exceeded")
                results.append(result)
                if on_result:
                    on_result(result)
                if log_path is not None:
                    _append_log(log_path, result)
    finally:
        try:
            smtp.quit()
        except Exception:
            pass

    return results
