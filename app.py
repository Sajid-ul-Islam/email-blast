"""app.py -- Email Blast Flask web application."""
from __future__ import annotations
import csv, io, json, logging, os, threading, urllib.request, uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from flask import (Flask, Response, abort, flash, jsonify, redirect,
    render_template, request, send_from_directory, session, url_for)
from sender import (SendResult, build_from_address, categorize_smtp_error,
    personalize, send_campaign, validate_credentials)

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
    ".pdf",".doc",".docx",".xls",".xlsx",".ppt",".pptx",
    ".jpg",".jpeg",".png",".gif",".webp",".svg",".zip",".txt",".csv"}

_cancel_events: dict[str, threading.Event] = {}
_cancel_events_lock = threading.Lock()
_scheduler_lock = threading.Lock()
_scheduled_jobs: dict[str, str] = {}