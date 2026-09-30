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
import hashlib
import hmac
import math
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
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from html.parser import HTMLParser
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
    """Return 'Display Name <email>' or just 'email'.

    Uses formataddr so non-ASCII display names (Bangla, accents, emoji) are
    RFC 2047-encoded into ASCII-safe words instead of producing a malformed
    From header that spam filters penalise.
    """
    display = name.strip()
    if display:
        return formataddr((display, email.strip()))
    return email.strip()


def _message_id_domain(from_addr: str) -> str:
    """Domain part of the envelope address, for make_msgid()."""
    addr = parseaddr(from_addr)[1]
    if "@" in addr:
        return addr.rsplit("@", 1)[-1] or "localhost"
    return "localhost"


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
# Recipient parsing & cleaning (CSV & Excel)
# ---------------------------------------------------------------------------

def _normalized_header(s: str) -> str:
    """Lowercase and strip separators so 'E-mail_Address' == 'emailaddress'."""
    return re.sub(r"[\s_\-]+", "", s.strip().lower())


_RAW_EMAIL_HEADER_NAMES = {
    "email", "mail", "emailaddress", "emailid", "mailid",
    "recipientemail", "useremail", "customeremail", "clientemail",
    "electronicmail", "contactemail", "primaryemail", "toemail", "to"
}
_EMAIL_HEADER_NAMES = {_normalized_header(x) for x in _RAW_EMAIL_HEADER_NAMES}

_OTHER_PERSON_MARKERS = (
    "referrer", "referral", "referer", "backup", "alternate", "altmail",
    "manager", "emergency", "parent", "guardian", "replyto", "bounce",
    "ccmail", "ccemail", "cc", "bcc"
)

_RAW_NAME_HEADER_NAMES = {
    "name", "fullname", "full_name", "customer_name", "client_name",
    "recipient_name", "first_name", "firstname", "display_name", "user_name",
    "username", "client", "customer", "recipient", "contact"
}
_NAME_HEADER_NAMES = {_normalized_header(x) for x in _RAW_NAME_HEADER_NAMES}


def clean_and_validate_email(val: Any) -> str | None:
    """Clean noise, surrounding punctuation, and validate email syntax.
    
    Returns clean email string, or None if invalid/noise.
    """
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None

    # Strip zero-width spaces, BOM, and non-breaking spaces
    s = re.sub(r"[\u200b\u200c\u200d\ufeff\u00a0]", "", s).strip()

    # Handle multiple emails separated by , or ; if any
    if ("," in s or ";" in s) and not (s.startswith("<") and s.endswith(">")):
        if s.count("@") > 1:
            for sub in re.split(r"[,;]\s*", s):
                clean_sub = clean_and_validate_email(sub)
                if clean_sub:
                    return clean_sub

    # Strip mailto:
    if s.lower().startswith("mailto:"):
        s = s[7:].strip()

    # Strip surrounding quotes or angle brackets
    s = s.strip("'\"`")
    if s.startswith("<") and s.endswith(">"):
        s = s[1:-1].strip()

    # If format is "Name <user@domain.com>", extract user@domain.com
    if "<" in s and ">" in s:
        match = re.search(r"<([^>]+)>", s)
        if match:
            s = match.group(1).strip()

    # Strip trailing punctuation (dots, commas, semicolons, colons)
    s = s.rstrip(".,;:")

    if not s or "@" not in s:
        return None

    parts = s.split("@")
    if len(parts) != 2:
        return None

    local, domain = parts[0].strip(), parts[1].strip()
    if not local or not domain:
        return None

    # Domain checks: must contain at least one dot, no consecutive dots
    if "." not in domain or ".." in domain or domain.startswith(".") or domain.endswith("."):
        return None

    tld = domain.rsplit(".", 1)[-1]
    if len(tld) < 2 or not tld.isalpha():
        return None

    EMAIL_RE = re.compile(
        r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)+$"
    )
    if not EMAIL_RE.match(f"{local}@{domain}"):
        return None

    # Drop header words or common dummy noise values
    if local.lower() in {"email", "mail", "none", "null", "undefined", "test", "n/a", "no-email", "username"}:
        return None

    return f"{local.lower()}@{domain.lower()}"


def _looks_like_email(candidate: str) -> bool:
    return clean_and_validate_email(candidate) is not None


def _find_email(row: dict[str, str]) -> str | None:
    """Pick the recipient's email address from a row dict (backwards compatibility)."""
    for key, value in row.items():
        if _normalized_header(str(key)) in _EMAIL_HEADER_NAMES:
            candidate = clean_and_validate_email(value)
            if candidate:
                return candidate
    for key, value in row.items():
        norm = _normalized_header(str(key))
        if any(marker in norm for marker in _OTHER_PERSON_MARKERS):
            continue
        candidate = clean_and_validate_email(value)
        if candidate:
            return candidate
    return None


def _read_csv_raw_rows(filepath: Path) -> list[list[str]]:
    """Read CSV or text file, handling different encodings, delimiters, and comments."""
    content = ""
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin1"):
        try:
            with filepath.open(newline="", encoding=enc) as f:
                content = f.read()
            break
        except (UnicodeDecodeError, LookupError):
            continue

    if not content:
        return []

    lines = [
        line for line in content.splitlines()
        if line.strip() and not line.strip().startswith(("#", "//"))
    ]
    if not lines:
        return []

    sample = "\n".join(lines[:20])
    try:
        sniffer = csv.Sniffer()
        dialect = sniffer.sniff(sample, delimiters=",;\t|")
    except Exception:
        dialect = csv.excel

    reader = csv.reader(lines, dialect=dialect)
    raw_rows: list[list[str]] = []
    for row in reader:
        r = list(row)
        while r and (r[-1] is None or str(r[-1]).strip() == ""):
            r.pop()
        if r and any(str(c).strip() for c in r if c is not None):
            raw_rows.append(r)
    return raw_rows


def _read_excel_raw_rows(filepath: Path) -> list[list[Any]]:
    """Read Excel workbook (.xlsx, .xls, .xlsm, etc.), picking the first non-empty sheet."""
    ext = filepath.suffix.lower()
    raw_rows: list[list[Any]] = []

    if ext in {".xlsx", ".xlsm", ".xltx", ".xltm"}:
        try:
            import openpyxl
            wb = openpyxl.load_workbook(filepath, read_only=True, data_only=True)
            for sheetname in wb.sheetnames:
                sheet = wb[sheetname]
                for row in sheet.iter_rows(values_only=True):
                    r = list(row)
                    while r and (r[-1] is None or str(r[-1]).strip() == ""):
                        r.pop()
                    if r and any(c is not None and str(c).strip() != "" for c in r):
                        raw_rows.append(r)
                if raw_rows:
                    break
            wb.close()
            return raw_rows
        except Exception:
            pass

    if ext == ".xls" or not raw_rows:
        try:
            import xlrd
            book = xlrd.open_workbook(filepath)
            for sheet_idx in range(book.nsheets):
                sheet = book.sheet_by_index(sheet_idx)
                for r_idx in range(sheet.nrows):
                    r = sheet.row_values(r_idx)
                    while r and (r[-1] is None or str(r[-1]).strip() == ""):
                        r.pop()
                    if r and any(c is not None and str(c).strip() != "" for c in r):
                        raw_rows.append(r)
                if raw_rows:
                    break
            return raw_rows
        except Exception:
            pass

    # Generic fallback using pandas if available
    try:
        import pandas as pd
        df = pd.read_excel(filepath, header=None)
        for _, row in df.iterrows():
            r = [None if pd.isna(c) else c for c in row]
            while r and (r[-1] is None or str(r[-1]).strip() == ""):
                r.pop()
            if r and any(c is not None and str(c).strip() != "" for c in r):
                raw_rows.append(r)
    except Exception:
        pass

    return raw_rows


def _read_file_raw_rows(filepath: Path) -> list[list[Any]]:
    """Dispatch file reading by extension."""
    ext = filepath.suffix.lower()
    if ext in {".xlsx", ".xls", ".xlsm", ".xltx", ".xltm"}:
        return _read_excel_raw_rows(filepath)
    return _read_csv_raw_rows(filepath)


def _read_csv_rows(filepath: Path) -> list[dict[str, str]]:
    """Backwards-compatible CSV reader returning rows as dicts."""
    raw_rows = _read_csv_raw_rows(filepath)
    if not raw_rows:
        return []
    # If first row has email, treat as no header
    if any(_looks_like_email(c) for c in raw_rows[0]):
        headers = [f"col_{i}" for i in range(len(raw_rows[0]))]
        data = raw_rows
    else:
        headers = [str(c).strip() for c in raw_rows[0]]
        data = raw_rows[1:]
    out = []
    for r in data:
        row_dict = {}
        for i, h in enumerate(headers):
            row_dict[h] = str(r[i]).strip() if i < len(r) and r[i] is not None else ""
        out.append(row_dict)
    return out


def process_table_records(raw_rows: list[list[Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Process 2D rows into recipients and comprehensive parse statistics.
    
    Dynamically detects the email column and name column, extracts extra
    merge fields, deduplicates valid emails, and drops errors/noise.
    """
    clean_rows: list[list[Any]] = []
    for row in raw_rows:
        if not row:
            continue
        if any(c is not None and str(c).strip() != "" for c in row):
            clean_rows.append(row)

    if not clean_rows:
        return [], {
            "total_rows": 0,
            "valid_count": 0,
            "duplicate_count": 0,
            "invalid_count": 0,
            "detected_column": None,
        }

    # Determine header row (if any)
    header_idx: int | None = None
    for idx, row in enumerate(clean_rows[:5]):
        if any(clean_and_validate_email(c) for c in row):
            if idx == 0:
                header_idx = None
            break
        row_norm = [_normalized_header(str(c)) for c in row if c is not None]
        if any(h in _EMAIL_HEADER_NAMES or "email" in h or "mail" in h or h in _NAME_HEADER_NAMES for h in row_norm):
            header_idx = idx
            break

    if header_idx is not None:
        raw_headers = clean_rows[header_idx]
        data_rows = clean_rows[header_idx + 1:]
    else:
        max_cols = max(len(r) for r in clean_rows)
        raw_headers = [f"Column_{i+1}" for i in range(max_cols)]
        data_rows = clean_rows

    headers = [
        str(c).strip() if c is not None and str(c).strip() != "" else f"Column_{i+1}"
        for i, c in enumerate(raw_headers)
    ]
    num_cols = len(headers)

    if not data_rows:
        return [], {
            "total_rows": 0,
            "valid_count": 0,
            "duplicate_count": 0,
            "invalid_count": 0,
            "detected_column": None,
        }

    # Dynamic email column detection
    best_col_idx: int | None = None
    best_score = -999999.0

    for col_idx in range(num_cols):
        col_name = headers[col_idx]
        norm = _normalized_header(col_name)

        if norm in _EMAIL_HEADER_NAMES or norm == "email":
            header_score = 90.0
        elif any(marker in norm for marker in _OTHER_PERSON_MARKERS):
            header_score = -50.0
        elif "email" in norm or norm.endswith("mail"):
            header_score = 45.0
        else:
            header_score = 0.0

        valid_emails = 0
        non_empty = 0
        for r in data_rows:
            val = r[col_idx] if col_idx < len(r) else None
            if val is not None and str(val).strip() != "":
                non_empty += 1
                if clean_and_validate_email(val):
                    valid_emails += 1

        if valid_emails == 0 or non_empty == 0:
            score = -100.0
        else:
            ratio = valid_emails / non_empty
            score = header_score + (ratio * 100.0) + min(valid_emails, 50.0)

        if score > best_score and valid_emails > 0:
            best_score = score
            best_col_idx = col_idx

    if best_col_idx is None:
        return [], {
            "total_rows": len(data_rows),
            "valid_count": 0,
            "duplicate_count": 0,
            "invalid_count": len(data_rows),
            "detected_column": None,
        }

    detected_col_name = headers[best_col_idx]

    # Name column detection
    name_col_idx: int | None = None
    for col_idx in range(num_cols):
        if col_idx == best_col_idx:
            continue
        norm = _normalized_header(headers[col_idx])
        if norm in _NAME_HEADER_NAMES or "fullname" in norm or "customername" in norm:
            name_col_idx = col_idx
            break

    # Extract recipients, drop noise/invalid/duplicates
    recipients: list[dict[str, Any]] = []
    seen: set[str] = set()
    duplicate_count = 0
    invalid_count = 0

    for r in data_rows:
        val = r[best_col_idx] if best_col_idx < len(r) else None
        cleaned_email = clean_and_validate_email(val)
        if not cleaned_email:
            invalid_count += 1
            continue

        dedup_key = cleaned_email.lower()
        if dedup_key in seen:
            duplicate_count += 1
            continue
        seen.add(dedup_key)

        # Name extraction
        name = ""
        if name_col_idx is not None and name_col_idx < len(r):
            nval = r[name_col_idx]
            if nval is not None:
                name = str(nval).strip()

        # If name is empty, check if email cell had Display Name <email>
        if not name and val is not None and "<" in str(val) and ">" in str(val):
            m = re.match(r"^([^<]+)<", str(val).strip())
            if m:
                name = m.group(1).strip().strip("'\"`")

        # Extra columns for merge fields
        extra: dict[str, str] = {}
        for c_idx in range(len(headers)):
            if c_idx == best_col_idx or c_idx == name_col_idx:
                continue
            col_key = headers[c_idx]
            cval = r[c_idx] if c_idx < len(r) else None
            if cval is None:
                str_val = ""
            elif isinstance(cval, float):
                if math.isnan(cval):
                    str_val = ""
                elif cval.is_integer():
                    str_val = str(int(cval))
                else:
                    str_val = str(cval)
            else:
                str_val = str(cval).strip()
            extra[col_key] = str_val

        recipients.append({"email": cleaned_email, "name": name, "extra": extra})

    stats = {
        "total_rows": len(data_rows),
        "valid_count": len(recipients),
        "duplicate_count": duplicate_count,
        "invalid_count": invalid_count,
        "detected_column": detected_col_name,
    }
    return recipients, stats


def parse_recipients_with_stats(filepath: Path | str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Parse recipients from CSV or Excel file and return (recipients, stats)."""
    p = Path(filepath)
    if not p.exists():
        return [], {
            "total_rows": 0,
            "valid_count": 0,
            "duplicate_count": 0,
            "invalid_count": 0,
            "detected_column": None,
        }
    raw_rows = _read_file_raw_rows(p)
    return process_table_records(raw_rows)


def parse_recipients(filepath: Path | str) -> list[dict[str, Any]]:
    """Read a CSV or Excel file and return one dict per unique email.
    
    Each dict: { "email": str, "name": str, "extra": {col: val, ...} }
    """
    recipients, _ = parse_recipients_with_stats(filepath)
    return recipients


class _HTMLToTextParser(HTMLParser):
    """Convert HTML to readable plain text.

    - Entities are decoded (convert_charrefs=True): &amp; -> &
    - <script>/<style> content is dropped entirely
    - Block tags produce line breaks; <br> produces a line break
    - <a href> keeps the URL in parentheses after the link text
    - <img alt> renders as [image: alt]
    """

    _BLOCK_TAGS = {
        "p", "div", "section", "article", "header", "footer", "main", "aside",
        "table", "tr", "ul", "ol", "li", "blockquote", "pre", "form", "fieldset",
        "h1", "h2", "h3", "h4", "h5", "h6", "hr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0        # inside <script>/<style>
        self._href = ""             # pending <a href>
        self._anchor_text: list[str] = []
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "head", "title"):
            self._skip_depth += 1
            return
        if tag == "a":
            self._href = (dict(attrs).get("href") or "").strip()
            self._anchor_text = []
        elif tag == "br":
            self.parts.append("\n")
        elif tag == "img":
            alt = (dict(attrs).get("alt") or "").strip()
            if alt:
                self.parts.append(f"[image: {alt}]")
        elif tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "head", "title"):
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "a":
            text = "".join(self._anchor_text).strip()
            if text:
                self.parts.append(text)
            if self._href and text and self._href != "#" and text != self._href:
                self.parts.append(f" ({self._href})")
            self._href = ""
            self._anchor_text = []
        elif tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._href:
            self._anchor_text.append(data)
        else:
            self.parts.append(data)

    def get_text(self) -> str:
        text = "".join(self.parts)
        lines = [re.sub(r"[ \t]+", " ", line.strip()) for line in text.splitlines()]
        return "\n".join(line for line in lines if line)


def html_to_text(html_text: str) -> str:
    """Convert an HTML email body to readable plain text.

    Proper entity decoding and block handling via html.parser -- replaces the
    old regex tag-strip that left &amp; behind and ragged whitespace.
    """
    parser = _HTMLToTextParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:
        # Malformed HTML: fall back to a minimal tag strip rather than failing
        # the whole upload.
        fallback = re.sub(r"<[^>]+>", " ", html_text)
        return "\n".join(line.strip() for line in fallback.splitlines() if line.strip())
    text = parser.get_text()
    # Collapse runs of 3+ blank lines that nested blocks can produce.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_content_upload(filepath: Path | str) -> dict[str, Any]:
    """Parse a .txt, .json, or .html content file into subject, plain body, and html body."""
    path = Path(filepath)
    ext = path.suffix.lower()
    text = path.read_text(encoding="utf-8", errors="replace")

    if ext == ".html":
        title_match = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
        subject = title_match.group(1).strip() if title_match else path.stem
        body = html_to_text(text)
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
    """Return a signed opt-out token for `email`.

    Format: 8 hex digits of expiry + 32 hex digits of HMAC-SHA256 — pure
    lowercase hex by design. Base64 tokens can contain '-'/'_', which forces
    mail libraries to RFC 2047-encode the whole List-Unsubscribe header;
    recipients' clients would then see an undecodable blob. Hex keeps the
    header raw ASCII on the wire.
    """
    exp = int(time.time()) + UNSUBSCRIBE_TOKEN_TTL_SECONDS
    msg = f"{email.strip().lower()}:{exp}"
    sig = hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    return f"{exp:08x}{sig}"


def verify_unsubscribe_token(email: str, token: str, secret: str) -> bool:
    """True when `token` is a valid, unexpired signature for `email`."""
    if not email or not token or not secret or len(token) != 40:
        return False
    try:
        exp = int(token[:8], 16)
        sig = token[8:]
    except ValueError:
        return False
    if exp < time.time():
        return False
    msg = f"{email.strip().lower()}:{exp}"
    expected = hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(sig, expected)


def build_unsubscribe_url(template: str, recipient: dict[str, Any], secret: str = "") -> str:
    """Resolve {{email}} (URL-encoded) and {{token}} (signed, if secret given).

    Also provides {{email_hex}} (hex of the address, no %/@/+ characters).
    Use {{email_hex}} in List-Unsubscribe header URLs: a '%' anywhere in an
    unstructured header makes email libraries RFC 2047-encode the entire
    value, which mail clients cannot decode -- breaking one-click unsub.
    """
    email = recipient.get("email", "")
    values = {
        "email": quote(email, safe=""),  # addresses with + or spaces stay valid in URLs
        "emailhex": email.encode("utf-8").hex(),
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
    list_unsubscribe_url: str = "",
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

    # List-Unsubscribe header: prefer the RFC 8058 one-click POST endpoint;
    # fall back to the human GET link. Footer always uses the GET link.
    header_url = list_unsubscribe_url or unsubscribe_url
    lu_header = f"<{header_url}>" if header_url else ""

    if attachments:
        msg = MIMEMultipart("mixed")
        msg["From"] = from_addr
        msg["To"] = to
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=False, usegmt=True)
        msg["Message-ID"] = make_msgid(domain=_message_id_domain(from_addr))
        if lu_header:
            msg["List-Unsubscribe"] = lu_header
            if list_unsubscribe_url:
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
            if list_unsubscribe_url:
                msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        msg.set_content(plain, subtype="plain", charset="utf-8")
        if html:
            msg.add_alternative(html, subtype="html", charset="utf-8")
        # Unsubscribe URLs exceed the default 78-char fold limit; folding an
        # unstructured header makes the email library RFC 2047-encode the
        # whole value, which mail clients cannot decode. SMTP allows 998.
        msg.policy = msg.policy.clone(max_line_length=998)

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
    list_unsubscribe_url: str = "",
    attachments: list[Path] | None = None,
) -> list[SendResult]:
    """
    Send one email per recipient with full rate limiting and cancellation.

    Opens a fresh SMTP connection per send and reconnects transparently, so a
    long-paced campaign survives the server dropping the idle session.

    Parameters
    ----------
    from_name       : Display name for the From header.
    unsubscribe_url : Human GET link in the footer. Supports {{email}}
                      (URL-encoded) and {{token}} (signed).
    unsubscribe_secret : HMAC secret for {{token}} in unsubscribe links.
    list_unsubscribe_url : Template for the List-Unsubscribe header (the
                      RFC 8058 one-click POST endpoint). Supports {{email}}
                      and {{token}}. When set, List-Unsubscribe-Post is added.
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
        is_ssl = (SMTP_PORT == 465) or os.getenv("SMTP_USE_SSL", "").strip().lower() in ("true", "1", "yes")
        if is_ssl:
            s = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30)
        else:
            s = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
            s.ehlo()
            if SMTP_USE_TLS:
                s.starttls()
                s.ehlo()
        try:
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
        list_unsub: str = "",
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
                list_unsubscribe_url=list_unsub,
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
        list_unsub: str = "",
    ) -> tuple[SendResult, smtplib.SMTP | None]:
        """Send with up to 2 reconnect attempts on dropped sessions."""
        result, smtp = _send_once(recipient, unsub, smtp, list_unsub)
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

            # Personalise unsubscribe links per recipient: the footer link is
            # human-facing; the header points at the signed one-click POST URL.
            if unsubscribe_url:
                unsub = build_unsubscribe_url(unsubscribe_url, recipient, unsubscribe_secret)
            else:
                unsub = ""
            list_unsub = ""
            if list_unsubscribe_url:
                list_unsub = build_unsubscribe_url(list_unsubscribe_url, recipient, unsubscribe_secret)

            try:
                result, smtp = _send_recipient(recipient, unsub, smtp, list_unsub)
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
                        result, smtp = _send_recipient(recipient, unsub, smtp, list_unsub)
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
