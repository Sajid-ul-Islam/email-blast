"""app.py -- Email Blast Flask web application."""
from __future__ import annotations

import hmac
import json
import logging
import os
import threading
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
from sender import (
    parse_recipients, parse_content_upload,
    send_campaign,
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024

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


# ---------------------------------------------------------------------------
# Session auth -- enabled when APP_PASSWORD is set (see .env.example)
# ---------------------------------------------------------------------------

# Endpoints called via fetch() from the UI; they get a JSON 401 instead of a
# login redirect so the client can surface a clean error.
_JSON_ENDPOINT_PREFIXES = ("/job", "/send", "/content")


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

    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user_ok = hmac.compare_digest(username.encode("utf-8"), APP_USERNAME.encode("utf-8"))
        pass_ok = hmac.compare_digest(password.encode("utf-8"), APP_PASSWORD.encode("utf-8"))
        if user_ok and pass_ok:
            session.clear()
            session["authed"] = True
            session.permanent = True
            dest = request.args.get("next") or url_for("index")
            # Only allow relative destinations (no open redirects)
            if not dest.startswith("/") or dest.startswith("//"):
                dest = url_for("index")
            return redirect(dest)
        error = "Invalid username or password."
        log.warning("Failed login attempt (username=%r)", username)

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
        filepath = UPLOADS_DIR / file_id
        if filepath.exists() and filepath.is_file():
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

    safe_name = f"{uuid.uuid4().hex[:8]}_{Path(file.filename).name}"
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
    return send_from_directory(UPLOADS_DIR, filename, as_attachment=True)


@app.route("/preview/<file_id>", methods=["GET"])
def preview_full(file_id):
    filepath = UPLOADS_DIR / file_id
    if not filepath.exists() or not filepath.is_file():
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

    content_id = f"{uuid.uuid4().hex[:8]}_{Path(file.filename).name}"
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

    list_path = UPLOADS_DIR / file_id
    if not list_path.exists():
        return jsonify({"ok": False, "error": "Recipient list file not found"}), 404

    try:
        recipients = parse_recipients(list_path)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Error parsing recipients: {e}"}), 400

    if not recipients:
        return jsonify({"ok": False, "error": "No valid recipients found in file"}), 400

    html_body_template = None
    if content_file_id:
        c_path = CONTENT_DIR / content_file_id
        if c_path.exists() and c_path.suffix.lower() == ".html":
            parsed = parse_content_upload(c_path)
            html_body_template = parsed.get("html_body")

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
    with open(job_file, "w", encoding="utf-8") as f:
        json.dump(initial_job_data, f, indent=2)

    cancel_event = threading.Event()
    with _cancel_events_lock:
        _cancel_events[job_id] = cancel_event

    def run_worker():
        gmail_user = os.getenv("GMAIL_USER", "").strip()
        gmail_pass = os.getenv("GMAIL_APP_PASSWORD", "").strip()
        from_name = os.getenv("FROM_NAME", FROM_NAME_DEFAULT)
        unsub_url = os.getenv("UNSUBSCRIBE_URL", UNSUBSCRIBE_URL_DEFAULT)

        initial_job_data["status"] = "running"
        with open(job_file, "w", encoding="utf-8") as f:
            json.dump(initial_job_data, f, indent=2)

        try:
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
            )

            sent = sum(1 for r in results if r.status == "sent")
            failed = sum(1 for r in results if r.status == "failed")
            skipped = sum(1 for r in results if r.status == "skipped")

            initial_job_data["sent"] = sent
            initial_job_data["failed"] = failed
            initial_job_data["skipped"] = skipped
            initial_job_data["results"] = [r.to_dict() for r in results]
            initial_job_data["status"] = "cancelled" if cancel_event.is_set() else "done"
            initial_job_data["finished_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        except Exception as e:
            log.exception("Campaign execution error")
            initial_job_data["status"] = "error"
            initial_job_data["error"] = str(e)
            initial_job_data["finished_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        finally:
            with open(job_file, "w", encoding="utf-8") as f:
                json.dump(initial_job_data, f, indent=2)
            with _cancel_events_lock:
                _cancel_events.pop(job_id, None)

    thread = threading.Thread(target=run_worker, daemon=True)
    thread.start()

    return jsonify({"ok": True, "job_id": job_id})


@app.route("/job/<job_id>", methods=["GET"])
def view_job(job_id):
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
    job_file = JOBS_DIR / f"{job_id}.json"
    if not job_file.exists():
        return jsonify({"ok": False, "error": "Job not found"}), 404
    with open(job_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["ok"] = True
    return jsonify(data)


@app.route("/job/<job_id>/cancel", methods=["POST"])
def cancel_job(job_id):
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
    app.run(host="0.0.0.0", port=5000, debug=debug)