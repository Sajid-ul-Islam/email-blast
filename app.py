"""app.py -- Email Blast Flask web application."""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import threading
import time as _time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Load .env BEFORE importing sender (which reads SMTP_HOST/SMTP_PORT at
# import time) and before the os.getenv() defaults below.
load_dotenv(Path(__file__).resolve().parent / ".env")

from flask import (
    Flask, abort, flash, jsonify, redirect,
    render_template, request, send_from_directory, session, url_for
)
from werkzeug.utils import secure_filename
from sender import (
    parse_recipients, parse_content_upload,
    send_campaign, unknown_merge_fields,
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024

# Bind to loopback by default: with no APP_PASSWORD every route is open, and a
# 0.0.0.0 bind would let anyone on the network send mail through this account.
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "5000"))

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
FROM_NAME_DEFAULT = os.getenv("FROM_NAME", "")
UNSUBSCRIBE_URL_DEFAULT = os.getenv("UNSUBSCRIBE_URL", "")
WEBHOOK_URL_DEFAULT = os.getenv("WEBHOOK_URL", "")
DAILY_CAP_DEFAULT = int(os.getenv("DAILY_CAP", "100"))
APP_USERNAME = os.getenv("APP_USERNAME", "admin")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
AUTH_ENABLED = bool(APP_PASSWORD)
_secret = os.getenv("SECRET_KEY", "").strip()
app.secret_key = _secret if _secret else os.urandom(32)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)

BASE_DIR = Path(__file__).resolve().parent
UPLOADS_DIR = BASE_DIR / "uploads"
CONTENT_DIR = UPLOADS_DIR / "content"
JOBS_DIR = UPLOADS_DIR / "jobs"
ATTACH_DIR = UPLOADS_DIR / "attachments"
SAMPLE_PATH = BASE_DIR / "sample_emails.csv"

for _d in (UPLOADS_DIR, CONTENT_DIR, JOBS_DIR, ATTACH_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# Shared send log: enforces DAILY_CAP across web jobs AND the CLI, and the
# count survives restarts (rules.md R-O6). Recipient PII stays local;
# .gitignore already covers sent_log.csv.
SENT_LOG_PATH = Path(os.getenv("SENT_LOG_PATH", str(BASE_DIR / "sent_log.csv")))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

ALLOWED_LIST_EXTS = {".csv", ".txt"}
ALLOWED_CONTENT_EXTS = {".txt", ".json", ".html"}
ALLOWED_ATTACH_EXTS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".zip", ".txt", ".csv"
}

_cancel_events: dict[str, threading.Event] = {}
_cancel_events_lock = threading.Lock()

_JOB_ID_RE = re.compile(r"^[0-9a-f]{12}$")


def safe_child(base: Path, name: str) -> Path | None:
    """Resolve name under base; return it only if it is a file still under base.

    Blocks path traversal such as file_id=../../.env.
    """
    p = (base / name).resolve()
    return p if p.is_file() and p.is_relative_to(base.resolve()) else None


_job_write_lock = threading.Lock()


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    """Write JSON via temp file + os.replace so pollers never read a torn file."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.replace(tmp, path)
    except OSError:
        # Windows: a concurrent reader can hold the destination open briefly.
        _time.sleep(0.05)
        os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Session auth -- enabled when APP_PASSWORD is set (see .env.example)
# ---------------------------------------------------------------------------

# Endpoints called via fetch() from the UI; they get a JSON 401 instead of a
# login redirect so the client can surface a clean error.
_JSON_ENDPOINT_PREFIXES = ("/job", "/send", "/content")

# ---------------------------------------------------------------------------
# Login rate limiting -- slows down password guessing. In-process only,
# which is fine for the single-process Flask deployment this app targets.
# Tunable via .env: LOGIN_MAX_ATTEMPTS, LOGIN_WINDOW_SECONDS, LOGIN_LOCKOUT_SECONDS
# ---------------------------------------------------------------------------
LOGIN_MAX_ATTEMPTS = int(os.getenv("LOGIN_MAX_ATTEMPTS", "5"))
LOGIN_WINDOW_SECONDS = float(os.getenv("LOGIN_WINDOW_SECONDS", "300"))
LOGIN_LOCKOUT_SECONDS = float(os.getenv("LOGIN_LOCKOUT_SECONDS", "900"))
_login_attempts: dict[str, list[float]] = {}
_login_locks: dict[str, float] = {}
_login_limit_lock = threading.Lock()


def _client_ip() -> str:
    """Best-effort client IP for rate limiting (single deployment, no proxy chain)."""
    return request.remote_addr or "unknown"


def _prune_login_state(now: float) -> None:
    """Drop expired entries so the dicts cannot grow unbounded."""
    expired_ips = [
        ip for ip, stamps in _login_attempts.items()
        if not stamps or now - stamps[-1] > max(LOGIN_WINDOW_SECONDS, LOGIN_LOCKOUT_SECONDS)
    ]
    for ip in expired_ips:
        _login_attempts.pop(ip, None)
    for ip in [ip for ip, until in _login_locks.items() if until <= now]:
        _login_locks.pop(ip, None)


def _login_is_locked(ip: str) -> float:
    """Return remaining lockout seconds for this IP (0 = not locked)."""
    now = _time.monotonic()
    with _login_limit_lock:
        _prune_login_state(now)
        until = _login_locks.get(ip, 0.0)
        return max(0.0, until - now)


def _login_record_failure(ip: str) -> float:
    """Record a failed attempt; return remaining lockout seconds (0 = not locked)."""
    now = _time.monotonic()
    with _login_limit_lock:
        stamps = [t for t in _login_attempts.get(ip, []) if now - t < LOGIN_WINDOW_SECONDS]
        stamps.append(now)
        _login_attempts[ip] = stamps
        if len(stamps) >= LOGIN_MAX_ATTEMPTS:
            _login_locks[ip] = now + LOGIN_LOCKOUT_SECONDS
            _login_attempts.pop(ip, None)
            return LOGIN_LOCKOUT_SECONDS
        return 0.0


def _login_reset(ip: str) -> None:
    with _login_limit_lock:
        _login_attempts.pop(ip, None)
        _login_locks.pop(ip, None)


def _logged_in() -> bool:
    return session.get("authed") is True


@app.before_request
def require_login():
    if not AUTH_ENABLED or _logged_in():
        return None
    if request.endpoint in ("login", "static"):
        return None
    if request.path.startswith(_JSON_ENDPOINT_PREFIXES):
        return jsonify({"ok": False, "error": "Not authenticated"}), 401
    return redirect(url_for("login", next=request.full_path.rstrip("?")))


@app.route("/login", methods=["GET", "POST"])
def login():
    if not AUTH_ENABLED:
        return redirect(url_for("index"))
    if request.method == "GET" and _logged_in():
        return redirect(url_for("index"))

    ip = _client_ip()

    error = None
    if request.method == "POST":
        remaining = _login_is_locked(ip)
        if remaining > 0:
            minutes = int(remaining // 60) + 1
            log.warning("Rate-limited login attempt from %s", ip)
            error = f"Too many failed attempts. Try again in ~{minutes} minute(s)."
            return render_template("login.html", error=error, username=APP_USERNAME), 429

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user_ok = hmac.compare_digest(username.encode("utf-8"), APP_USERNAME.encode("utf-8"))
        pass_ok = hmac.compare_digest(password.encode("utf-8"), APP_PASSWORD.encode("utf-8"))
        if user_ok and pass_ok:
            _login_reset(ip)
            session.clear()
            session["authed"] = True
            session.permanent = True
            dest = request.args.get("next") or url_for("index")
            # Only allow relative destinations (no open redirects)
            if not dest.startswith("/") or dest.startswith("//"):
                dest = url_for("index")
            return redirect(dest)
        _login_record_failure(ip)
        error = "Invalid username or password."
        log.warning("Failed login attempt (username=%r) from %s", username, ip)

    return render_template("login.html", error=error, username=APP_USERNAME)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


def get_all_uploaded_files() -> list[dict[str, Any]]:
    files = []
    for f in UPLOADS_DIR.glob("*"):
        if f.is_file() and f.suffix.lower() in ALLOWED_LIST_EXTS:
            try:
                recipients = parse_recipients(f)
                email_count = len(recipients)
            except Exception:
                email_count = 0
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            files.append({
                "name": f.name,
                "email_count": email_count,
                "uploaded": mtime.strftime("%Y-%m-%d %H:%M UTC")
            })
    files.sort(key=lambda x: x["uploaded"], reverse=True)
    return files


@app.route("/", methods=["GET"])
def index():
    file_id = request.args.get("file_id", "").strip()
    uploaded = False
    filename = ""
    row_count = 0
    email_count = 0
    duplicates = 0
    email_list = []
    preview_limit = 20
    preview_names = False

    if file_id:
        filepath = safe_child(UPLOADS_DIR, file_id)
        if filepath is not None:
            uploaded = True
            filename = filepath.name
            try:
                recipients = parse_recipients(filepath)
                email_count = len(recipients)
                preview_names = any(bool(r.get("name")) for r in recipients)
                email_list = recipients[:preview_limit]
                # Rough row count
                with filepath.open("r", encoding="utf-8", errors="replace") as f:
                    row_count = max(0, sum(1 for line in f if line.strip()) - 1)
                duplicates = max(0, row_count - email_count)
            except Exception as e:
                log.error("Failed to parse %s: %s", filepath, e)

    return render_template(
        "index.html",
        auth_enabled=AUTH_ENABLED,
        all_files=get_all_uploaded_files(),
        uploaded=uploaded,
        filename=filename,
        file_id=file_id,
        row_count=row_count,
        email_count=email_count,
        duplicates=duplicates,
        email_list=email_list,
        preview_limit=preview_limit,
        preview_names=preview_names,
    )


@app.route("/upload", methods=["POST"])
def upload_file():
    if "file" not in request.files:
        return redirect(url_for("index"))
    file = request.files["file"]
    if not file or not file.filename:
        return redirect(url_for("index"))

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_LIST_EXTS:
        flash("Invalid file extension. Please upload a .csv file.")
        return redirect(url_for("index"))

    safe_name = f"{uuid.uuid4().hex[:8]}_{secure_filename(file.filename) or f'file{ext}'}"
    save_path = UPLOADS_DIR / safe_name
    file.save(save_path)

    return redirect(url_for("index", file_id=safe_name))


@app.route("/sample.csv", methods=["GET"])
def sample_csv():
    if SAMPLE_PATH.exists():
        return send_from_directory(BASE_DIR, "sample_emails.csv", as_attachment=True)
    content = "email,name,city\nuser1@example.com,Alice,Dhaka\nuser2@example.com,Bob,Chittagong\n"
    return (content, 200, {"Content-Type": "text/csv", "Content-Disposition": "attachment; filename=sample.csv"})


@app.route("/uploads/<path:filename>", methods=["GET"])
def download_uploaded(filename):
    # Serve only recipient-list files sitting directly in uploads/. Never serve
    # jobs/*.json (contains every recipient email + result) or content/ files.
    p = safe_child(UPLOADS_DIR, filename)
    if p is None or p.parent != UPLOADS_DIR or p.suffix.lower() not in ALLOWED_LIST_EXTS:
        abort(404)
    return send_from_directory(UPLOADS_DIR, p.name, as_attachment=True)


@app.route("/preview/<file_id>", methods=["GET"])
def preview_full(file_id):
    filepath = safe_child(UPLOADS_DIR, file_id)
    if filepath is None:
        abort(404)
    recipients = parse_recipients(filepath)
    preview_names = any(bool(r.get("name")) for r in recipients)
    return render_template(
        "preview.html",
        filename=filepath.name,
        email_count=len(recipients),
        email_list=recipients,
        preview_names=preview_names,
    )


@app.route("/content", methods=["POST"])
def upload_content():
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No file provided"}), 400
    file = request.files["file"]
    if not file or not file.filename:
        return jsonify({"ok": False, "error": "Empty filename"}), 400

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_CONTENT_EXTS:
        return jsonify({"ok": False, "error": f"Invalid format: {ext}. Allowed: .txt, .json, .html"}), 400

    content_id = f"{uuid.uuid4().hex[:8]}_{secure_filename(file.filename) or f'content{ext}'}"
    save_path = CONTENT_DIR / content_id
    file.save(save_path)

    try:
        parsed = parse_content_upload(save_path)
        return jsonify({
            "ok": True,
            "subject": parsed["subject"],
            "body": parsed["body"],
            "html_body": parsed.get("html_body"),
            "file_id": content_id,
            "source": file.filename,
        })
    except Exception as e:
        log.exception("Error parsing content file")
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/send", methods=["POST"])
def send_route():
    file_id = request.form.get("file_id", "").strip()
    subject = request.form.get("subject", "").strip()
    body = request.form.get("body", "").strip()
    content_file_id = request.form.get("content_file_id", "").strip()

    if not file_id:
        return jsonify({"ok": False, "error": "No recipient file selected"}), 400
    if not subject:
        return jsonify({"ok": False, "error": "Subject cannot be empty"}), 400
    if not body:
        return jsonify({"ok": False, "error": "Body cannot be empty"}), 400

    list_path = safe_child(UPLOADS_DIR, file_id)
    if list_path is None:
        return jsonify({"ok": False, "error": "Recipient list file not found"}), 404

    try:
        recipients = parse_recipients(list_path)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Error parsing recipients: {e}"}), 400

    if not recipients:
        return jsonify({"ok": False, "error": "No valid recipients found in file"}), 400

    html_body_template = None
    if content_file_id:
        c_path = safe_child(CONTENT_DIR, content_file_id)
        if c_path is not None and c_path.suffix.lower() == ".html":
            parsed = parse_content_upload(c_path)
            html_body_template = parsed.get("html_body")

    # Validate merge fields against the CSV columns BEFORE starting the job,
    # so a typo like {{city}} is rejected instead of going to every recipient
    # (rules.md R-C2: unresolved fields must never reach a recipient).
    columns = set(recipients[0].get("extra", {}).keys()) if recipients else set()
    unknown = unknown_merge_fields(
        [subject, body] + ([html_body_template] if html_body_template else []),
        columns,
    )
    if unknown:
        names = ", ".join("{{%s}}" % f for f in unknown[:8])
        return jsonify({
            "ok": False,
            "error": (
                f"Unknown merge field(s): {names}. "
                f"Available columns: {', '.join(sorted(columns)) or 'none'}."
            ),
        }), 422

    job_id = uuid.uuid4().hex[:12]
    job_file = JOBS_DIR / f"{job_id}.json"

    initial_job_data = {
        "job_id": job_id,
        "status": "starting",
        "subject": subject,
        "body_preview": body[:120] + ("…" if len(body) > 120 else ""),
        "list_file": list_path.name,
        "total": len(recipients),
        "sent": 0,
        "failed": 0,
        "skipped": 0,
        "results": [],
        "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "finished_at": None,
        "error": None,
    }
    _write_json_atomic(job_file, initial_job_data)

    # Live progress: on_result updates counts + results and flushes the job
    # file atomically after every send, so /job/<id>/status shows real numbers.
    job_state = initial_job_data

    def on_result(r) -> None:
        with _job_write_lock:
            job_state["results"].append(r.to_dict())
            key = {"sent": "sent", "failed": "failed", "skipped": "skipped"}.get(r.status)
            if key:
                job_state[key] += 1
            try:
                _write_json_atomic(job_file, job_state)
            except OSError as e:
                log.warning("Could not update job file %s: %s", job_file, e)

    cancel_event = threading.Event()
    with _cancel_events_lock:
        _cancel_events[job_id] = cancel_event

    def run_worker():
        gmail_user = os.getenv("GMAIL_USER", "").strip()
        gmail_pass = os.getenv("GMAIL_APP_PASSWORD", "").strip()
        from_name = os.getenv("FROM_NAME", FROM_NAME_DEFAULT)
        unsub_url = os.getenv("UNSUBSCRIBE_URL", UNSUBSCRIBE_URL_DEFAULT)

        try:
            with _job_write_lock:
                job_state["status"] = "running"
                _write_json_atomic(job_file, job_state)

            results = send_campaign(
                gmail_user=gmail_user,
                gmail_password=gmail_pass,
                recipients=recipients,
                subject=subject,
                body_template=body,
                html_body_template=html_body_template,
                from_name=from_name,
                unsubscribe_url=unsub_url,
                cancel_event=cancel_event,
                daily_cap=DAILY_CAP_DEFAULT,
                log_path=SENT_LOG_PATH,
                on_result=on_result,
            )

            # Recompute from the authoritative return value (on_result already
            # streamed the same data; this corrects any drift).
            with _job_write_lock:
                job_state["sent"] = sum(1 for r in results if r.status == "sent")
                job_state["failed"] = sum(1 for r in results if r.status == "failed")
                job_state["skipped"] = sum(1 for r in results if r.status == "skipped")
                job_state["results"] = [r.to_dict() for r in results]
                job_state["status"] = "cancelled" if cancel_event.is_set() else "done"
                job_state["finished_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        except Exception as e:
            log.exception("Campaign execution error")
            with _job_write_lock:
                job_state["status"] = "error"
                job_state["error"] = str(e)
                job_state["finished_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        finally:
            with _job_write_lock:
                try:
                    _write_json_atomic(job_file, job_state)
                except OSError as e:
                    log.error("Could not write final job file %s: %s", job_file, e)
            with _cancel_events_lock:
                _cancel_events.pop(job_id, None)

    thread = threading.Thread(target=run_worker, daemon=True)
    thread.start()

    return jsonify({"ok": True, "job_id": job_id})


@app.route("/job/<job_id>", methods=["GET"])
def view_job(job_id):
    if not _JOB_ID_RE.match(job_id):
        abort(404)
    job_file = JOBS_DIR / f"{job_id}.json"
    if not job_file.exists():
        abort(404)
    with open(job_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    return render_template(
        "send_job.html",
        job_id=data.get("job_id"),
        status=data.get("status"),
        subject=data.get("subject"),
        body_preview=data.get("body_preview"),
        list_file=data.get("list_file"),
        total=data.get("total", 0),
        sent=data.get("sent", 0),
        failed=data.get("failed", 0),
        skipped=data.get("skipped", 0),
        results=data.get("results", []),
        started_at=data.get("started_at"),
        finished_at=data.get("finished_at"),
        error=data.get("error"),
    )


@app.route("/job/<job_id>/status", methods=["GET"])
def job_status(job_id):
    if not _JOB_ID_RE.match(job_id):
        abort(404)
    job_file = JOBS_DIR / f"{job_id}.json"
    if not job_file.exists():
        return jsonify({"ok": False, "error": "Job not found"}), 404
    with open(job_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["ok"] = True
    return jsonify(data)


@app.route("/job/<job_id>/cancel", methods=["POST"])
def cancel_job(job_id):
    if not _JOB_ID_RE.match(job_id):
        abort(404)
    with _cancel_events_lock:
        ev = _cancel_events.get(job_id)
        if ev:
            ev.set()
            return jsonify({"ok": True, "status": "cancelling"})
    return jsonify({"ok": False, "error": "Job not running or already finished"}), 400


if __name__ == "__main__":
    # Debugger off by default (remote code execution risk on 0.0.0.0).
    # Opt in for local dev only via FLASK_DEBUG=true.
    debug = os.getenv("FLASK_DEBUG", "").strip().lower() in ("1", "true", "yes")
    if not AUTH_ENABLED and HOST not in ("127.0.0.1", "localhost", "::1"):
        # No password + a reachable interface = anyone on the network can send
        # mail through this account. Refuse instead of starting wide open.
        raise SystemExit(
            f"Refusing to bind to {HOST} without APP_PASSWORD set. "
            "Set APP_PASSWORD in .env, or keep HOST=127.0.0.1 for local use."
        )
    app.run(host=HOST, port=PORT, debug=debug)