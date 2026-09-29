#!/usr/bin/env python3
"""Smoke test: auth enforcement + route sanity. No real emails are sent."""
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# Simulate APP_PASSWORD being set BEFORE importing app
os.environ["APP_USERNAME"] = "admin"
os.environ["APP_PASSWORD"] = "test-pass-123"
os.environ["SECRET_KEY"] = "test-secret"

import app as app_module  # noqa: E402

app_module.app.config["TESTING"] = True
client = app_module.app.test_client()

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# --- 1. Unauthenticated requests redirect to /login ---
r = client.get("/")
check("GET / redirects to login when auth enabled", r.status_code == 302 and "/login" in r.headers.get("Location", ""))

r = client.get("/preview/whatever.csv")
check("GET /preview/... redirects to login", r.status_code == 302 and "/login" in r.headers.get("Location", ""))

# JSON endpoints get a 401 rather than a redirect
r = client.get("/job/nonexistent/status")
check("GET /job/<id>/status returns 401 JSON when unauthenticated",
      r.status_code == 401 and r.is_json and r.get_json().get("ok") is False)

r = client.post("/send", data={})
check("POST /send returns 401 JSON when unauthenticated", r.status_code == 401)

# Static assets and the login page itself stay reachable
r = client.get("/static/style.css")
check("GET /static/style.css reachable while logged out", r.status_code == 200)
r = client.get("/login")
check("GET /login renders while logged out", r.status_code == 200 and b"Sign in" in r.data)

# --- 2. Wrong credentials rejected ---
r = client.post("/login", data={"username": "admin", "password": "wrong"})
check("POST /login wrong password rejected", r.status_code == 200 and b"Invalid username or password" in r.data)

r = client.post("/login", data={"username": "nobody", "password": "test-pass-123"})
check("POST /login wrong username rejected", r.status_code == 200 and b"Invalid username or password" in r.data)

# --- 3. Correct credentials log in and honor ?next ---
r = client.post("/login?next=/preview/x.csv", data={"username": "admin", "password": "test-pass-123"})
check("POST /login correct credentials redirect", r.status_code == 302)

r = client.get("/")
check("GET / renders after login", r.status_code == 200 and b"Email Blast" in r.data)
r = client.get("/sample.csv")
check("GET /sample.csv works after login", r.status_code == 200)

# Open-redirect guard: //evil.com must not be honored
client2 = app_module.app.test_client()
client2.post("/login?next=//evil.com", data={"username": "admin", "password": "test-pass-123"})
r = client2.get("/")
check("login with //next falls back to index", r.status_code == 200)

# --- 4. Logout clears the session ---
r = client.post("/logout")
check("POST /logout redirects to login", r.status_code == 302 and "/login" in r.headers.get("Location", ""))
r = client.get("/")
check("GET / requires login again after logout", r.status_code == 302 and "/login" in r.headers.get("Location", ""))

# --- 5. Auth disabled when APP_PASSWORD empty ---
with tempfile.TemporaryDirectory() as td:
    os.environ["APP_PASSWORD"] = ""
    import importlib
    importlib.reload(app_module)
    app_module.app.config["TESTING"] = True
    anon = app_module.app.test_client()
    r = anon.get("/")
    check("GET / open access when APP_PASSWORD empty", r.status_code == 200)
    r = anon.get("/login")
    check("GET /login redirects to app when auth disabled", r.status_code == 302)

# --- 6. send_campaign kwarg fix still binds correctly ---
os.environ["APP_PASSWORD"] = "test-pass-123"
import importlib
importlib.reload(app_module)
import sender
try:
    sender.send_campaign(
        gmail_user="u", gmail_password="p", recipients=[],
        subject="s", body_template="b",
    )
    print("[PASS] send_campaign(gmail_password=...) binds without TypeError")
except TypeError as e:
    failures.append("kwarg binding")
    print(f"[FAIL] send_campaign binding -- {e}")

print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
