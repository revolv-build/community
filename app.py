"""
Community Platform
──────────────────
A multi-tenant community platform where anyone can create and manage
their own community space. Each community has its own members, posts,
and discussions — all under one roof.

Architecture:
  - Platform level: landing, auth, dashboard, create community
  - Community level: /c/<slug>/ — feed, posts, members, settings
  - Roles: platform-wide (user account) + per-community (owner/admin/member)
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.request
import urllib.error
from datetime import datetime
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import (
    Flask, render_template, request,
    redirect, url_for, session, flash, g, abort, send_from_directory
)
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from markupsafe import escape, Markup
import markdown as md_lib
import bleach
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# ── Config ────────────────────────────────────────────────────────

load_dotenv()

APP_DIR = Path(__file__).parent
DB_PATH = APP_DIR / "data" / "community.db"

UPLOAD_DIR = APP_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
ALLOWED_EXTENSIONS = {"pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "txt", "csv", "zip",
                      "png", "jpg", "jpeg", "gif", "svg", "mp4", "mov", "webm"}
MAX_UPLOAD_MB = 50

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("FLASK_ENV") == "production"
app.config["PERMANENT_SESSION_LIFETIME"] = 86400 * 7  # 7 days
app.config["WTF_CSRF_TIME_LIMIT"] = 3600  # 1 hour CSRF token validity
DEFAULT_PORT = int(os.environ.get("PORT", 5001))

# Refuse to boot with default secret key in production
if os.environ.get("FLASK_ENV") == "production" and app.secret_key == "change-me-in-production":
    raise RuntimeError("SECRET_KEY must be set in production. Generate one with: python3 -c \"import secrets; print(secrets.token_hex(32))\"")

# CSRF protection
csrf = CSRFProtect(app)

# Rate limiting
limiter = Limiter(get_remote_address, app=app, default_limits=["200 per minute"],
                  storage_uri="memory://")

# Security headers
@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.is_secure:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response

(APP_DIR / "data").mkdir(exist_ok=True)

# ── Markdown Rendering ────────────────────────────────────────────

ALLOWED_TAGS = [
    "p", "br", "strong", "em", "a", "ul", "ol", "li", "code", "pre",
    "blockquote", "h1", "h2", "h3", "h4", "img", "hr", "del", "table",
    "thead", "tbody", "tr", "th", "td",
]
ALLOWED_ATTRS = {
    "a": ["href", "title", "rel"],
    "img": ["src", "alt", "title"],
}

def render_markdown(text):
    """Convert Markdown to sanitised HTML."""
    if not text:
        return ""
    raw_html = md_lib.markdown(text, extensions=["fenced_code", "tables", "nl2br"])
    clean = bleach.clean(raw_html, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS)
    # Make links open in new tab
    clean = clean.replace("<a ", '<a target="_blank" rel="noopener" ')
    return Markup(clean)

@app.template_filter("markdown")
def markdown_filter(text):
    return render_markdown(text)

def strip_markdown(text):
    """Remove markdown syntax for plain text previews."""
    if not text:
        return ""
    t = re.sub(r'\*\*(.+?)\*\*', r'\1', text)  # **bold**
    t = re.sub(r'\*(.+?)\*', r'\1', t)          # *italic*
    t = re.sub(r'__(.+?)__', r'\1', t)
    t = re.sub(r'_(.+?)_', r'\1', t)
    t = re.sub(r'#{1,6}\s*', '', t)              # headings
    t = re.sub(r'^\s*[-*+]\s+', '', t, flags=re.MULTILINE)  # list items
    t = re.sub(r'^\s*\d+\.\s+', '', t, flags=re.MULTILINE)  # numbered lists
    t = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', t)  # [links](url)
    t = re.sub(r'`{1,3}[^`]*`{1,3}', '', t)    # code
    t = re.sub(r'^>\s*', '', t, flags=re.MULTILINE)  # blockquotes
    t = re.sub(r'---+', '', t)                   # horizontal rules
    t = re.sub(r'\n{2,}', ' ', t)                # collapse newlines
    return t.strip()

@app.template_filter("timeago")
def timeago_filter(dt_str):
    """Convert ISO datetime string to relative time like '3h ago'."""
    if not dt_str:
        return ""
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return dt_str[:10] if dt_str else ""
    now = datetime.utcnow()
    diff = now - dt
    seconds = int(diff.total_seconds())
    if seconds < 60:
        return "just now"
    elif seconds < 3600:
        m = seconds // 60
        return f"{m}m ago"
    elif seconds < 86400:
        h = seconds // 3600
        return f"{h}h ago"
    elif seconds < 604800:
        d = seconds // 86400
        return f"{d}d ago"
    elif seconds < 2592000:
        w = seconds // 604800
        return f"{w}w ago"
    else:
        return dt_str[:10]

# ── Email (Resend) ────────────────────────────────────────────────

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "Community <noreply@revolv.uk>")

def send_email(to, subject, html_body):
    """Send email via Resend API. Returns True on success."""
    if not RESEND_API_KEY:
        print(f"[EMAIL SKIPPED — no API key] To: {to}, Subject: {subject}")
        return False
    payload = json.dumps({
        "from": EMAIL_FROM,
        "to": [to],
        "subject": subject,
        "html": html_body
    }).encode()
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except urllib.error.URLError as e:
        print(f"[EMAIL ERROR] {e}")
        return False

# Token generation for password reset and email verification
from itsdangerous import URLSafeTimedSerializer
_serializer = URLSafeTimedSerializer(app.secret_key)

def generate_token(data, salt="default"):
    return _serializer.dumps(data, salt=salt)

def verify_token(token, salt="default", max_age=3600):
    try:
        return _serializer.loads(token, salt=salt, max_age=max_age)
    except Exception:
        return None

# ── Database ──────────────────────────────────────────────────────

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(str(DB_PATH))
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db:
        db.close()

def init_db():
    db = sqlite3.connect(str(DB_PATH))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL,
            email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            is_admin      INTEGER NOT NULL DEFAULT 0,
            email_verified INTEGER NOT NULL DEFAULT 0,
            bio           TEXT DEFAULT '',
            location      TEXT DEFAULT '',
            website       TEXT DEFAULT '',
            created       TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS communities (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL,
            slug          TEXT NOT NULL UNIQUE,
            description   TEXT DEFAULT '',
            owner_id      INTEGER NOT NULL,
            welcome_message TEXT DEFAULT '',
            created       TEXT NOT NULL,
            FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS memberships (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL,
            community_id  INTEGER NOT NULL,
            role          TEXT NOT NULL DEFAULT 'member',
            joined        TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (community_id) REFERENCES communities(id) ON DELETE CASCADE,
            UNIQUE(user_id, community_id)
        );

        CREATE TABLE IF NOT EXISTS posts (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            community_id  INTEGER NOT NULL,
            user_id       INTEGER NOT NULL,
            title         TEXT NOT NULL,
            body          TEXT DEFAULT '',
            category      TEXT DEFAULT '',
            is_pinned     INTEGER NOT NULL DEFAULT 0,
            created       TEXT NOT NULL,
            updated       TEXT NOT NULL,
            FOREIGN KEY (community_id) REFERENCES communities(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS comments (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id       INTEGER NOT NULL,
            user_id       INTEGER NOT NULL,
            body          TEXT NOT NULL,
            parent_id     INTEGER DEFAULT NULL,
            created       TEXT NOT NULL,
            FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (parent_id) REFERENCES comments(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS comment_votes (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            comment_id    INTEGER NOT NULL,
            user_id       INTEGER NOT NULL,
            value         INTEGER NOT NULL DEFAULT 0,
            created       TEXT NOT NULL,
            FOREIGN KEY (comment_id) REFERENCES comments(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(comment_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS comment_awards (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            comment_id    INTEGER NOT NULL,
            user_id       INTEGER NOT NULL,
            emoji         TEXT NOT NULL,
            created       TEXT NOT NULL,
            FOREIGN KEY (comment_id) REFERENCES comments(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(comment_id, user_id, emoji)
        );

        CREATE TABLE IF NOT EXISTS events (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            community_id  INTEGER NOT NULL,
            user_id       INTEGER NOT NULL,
            title         TEXT NOT NULL,
            description   TEXT DEFAULT '',
            event_date    TEXT NOT NULL,
            event_time    TEXT DEFAULT '',
            end_time      TEXT DEFAULT '',
            location      TEXT DEFAULT '',
            location_type TEXT NOT NULL DEFAULT 'in-person',
            link          TEXT DEFAULT '',
            created       TEXT NOT NULL,
            FOREIGN KEY (community_id) REFERENCES communities(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS rsvps (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id      INTEGER NOT NULL,
            user_id       INTEGER NOT NULL,
            status        TEXT NOT NULL DEFAULT 'going',
            created       TEXT NOT NULL,
            FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(event_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS resources (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            community_id  INTEGER NOT NULL,
            user_id       INTEGER NOT NULL,
            title         TEXT NOT NULL,
            description   TEXT DEFAULT '',
            resource_type TEXT NOT NULL DEFAULT 'file',
            file_path     TEXT DEFAULT '',
            file_name     TEXT DEFAULT '',
            file_size     INTEGER DEFAULT 0,
            url           TEXT DEFAULT '',
            tags          TEXT DEFAULT '',
            created       TEXT NOT NULL,
            FOREIGN KEY (community_id) REFERENCES communities(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS votes (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id       INTEGER NOT NULL,
            user_id       INTEGER NOT NULL,
            value         INTEGER NOT NULL DEFAULT 0,
            created       TEXT NOT NULL,
            FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(post_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS bookmarks (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id       INTEGER NOT NULL,
            user_id       INTEGER NOT NULL,
            created       TEXT NOT NULL,
            FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(post_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS follows (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id       INTEGER NOT NULL,
            user_id       INTEGER NOT NULL,
            created       TEXT NOT NULL,
            FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(post_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS notifications (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL,
            community_id  INTEGER NOT NULL,
            post_id       INTEGER,
            type          TEXT NOT NULL,
            message       TEXT NOT NULL,
            is_read       INTEGER NOT NULL DEFAULT 0,
            created       TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (community_id) REFERENCES communities(id) ON DELETE CASCADE,
            FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE SET NULL
        );
    """)

    # Create default platform admin if no users exist
    row = db.execute("SELECT COUNT(*) FROM users").fetchone()
    if row[0] == 0:
        db.execute(
            "INSERT INTO users (name, email, password_hash, is_admin, created) VALUES (?, ?, ?, ?, ?)",
            ("Platform Admin", "admin@example.com", generate_password_hash("changeme"), 1, datetime.utcnow().isoformat())
        )
        print("Default platform admin created — email: admin@example.com  password: changeme")

    db.commit()
    db.close()

# ── Helpers ───────────────────────────────────────────────────────

def get_user_by_id(uid):
    return get_db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()

def get_user_by_email(email):
    return get_db().execute("SELECT * FROM users WHERE email = ? COLLATE NOCASE", (email,)).fetchone()

def current_user():
    uid = session.get("user_id")
    if uid:
        return get_user_by_id(uid)
    return None

def login_user(user):
    """Set session for user with session regeneration to prevent fixation."""
    session.clear()
    session["user_id"] = user["id"]
    session["user_name"] = user["name"]
    session.permanent = True

def get_community_by_slug(slug):
    return get_db().execute("SELECT * FROM communities WHERE slug = ?", (slug,)).fetchone()

def get_membership(user_id, community_id):
    return get_db().execute(
        "SELECT * FROM memberships WHERE user_id = ? AND community_id = ?",
        (user_id, community_id)
    ).fetchone()

def get_user_communities(user_id):
    return get_db().execute("""
        SELECT c.*, m.role AS my_role
        FROM communities c JOIN memberships m ON c.id = m.community_id
        WHERE m.user_id = ?
        ORDER BY c.name
    """, (user_id,)).fetchall()

def slugify(text):
    slug = re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')
    return slug[:60]

def get_user_flair(user_id, community_id):
    """Calculate user flair based on post activity within the community."""
    db = get_db()
    # Get total posts by all users in this community
    all_users = db.execute("""
        SELECT user_id, COUNT(*) AS cnt FROM posts
        WHERE community_id = ? GROUP BY user_id ORDER BY cnt DESC
    """, (community_id,)).fetchall()
    if not all_users:
        return None
    total_users = len(all_users)
    if total_users < 2:
        return None
    # Find this user's rank
    for i, row in enumerate(all_users):
        if row["user_id"] == user_id:
            percentile = (i / total_users) * 100
            post_count = row["cnt"]
            if post_count == 0:
                return None
            if percentile <= 1:
                return "Top 1% Contributor"
            elif percentile <= 5:
                return "Top 5% Contributor"
            elif percentile <= 10:
                return "Top 10% Contributor"
            elif percentile <= 25:
                return "Top 25% Contributor"
            return None
    return None

def get_unread_notification_count(user_id, community_id):
    return get_db().execute(
        "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND community_id = ? AND is_read = 0",
        (user_id, community_id)
    ).fetchone()[0]

def avatar_html(user, size=22):
    """Generate avatar HTML — photo if available, initial letter if not."""
    if user and user.get("avatar_path") if isinstance(user, dict) else (user and user["avatar_path"] if user else None):
        path = user["avatar_path"] if not isinstance(user, dict) else user.get("avatar_path", "")
        if path:
            return Markup(f'<img src="/uploads/{path}" class="avatar-img" style="width:{size}px;height:{size}px;" />')
    name = user["name"] if user else "?"
    return Markup(f'<span class="qa-avatar" style="width:{size}px;height:{size}px;font-size:{max(10, size//2)}px;">{name[0].upper()}</span>')

@app.context_processor
def inject_globals():
    return dict(current_user=current_user(), avatar_html=avatar_html)

# ── Auth decorators ───────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated

def platform_admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = current_user()
        if not user or not user["is_admin"]:
            flash("Platform admin access required.", "error")
            return redirect("/dashboard")
        return f(*args, **kwargs)
    return decorated

def community_member_required(f):
    """Ensures user is a member of the community in the URL slug."""
    @wraps(f)
    def decorated(slug, *args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login_page"))
        community = get_community_by_slug(slug)
        if not community:
            abort(404)
        membership = get_membership(session["user_id"], community["id"])
        if not membership:
            flash("You're not a member of this community.", "error")
            return redirect("/dashboard")
        g.community = community
        g.membership = membership
        g.notif_count = get_unread_notification_count(session["user_id"], community["id"])
        return f(slug, *args, **kwargs)
    return decorated

def community_admin_required(f):
    """Ensures user is owner or admin of the community."""
    @wraps(f)
    def decorated(slug, *args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login_page"))
        community = get_community_by_slug(slug)
        if not community:
            abort(404)
        membership = get_membership(session["user_id"], community["id"])
        if not membership or membership["role"] not in ("owner", "admin"):
            flash("Admin access required.", "error")
            return redirect(f"/c/{slug}/")
        g.community = community
        g.membership = membership
        return f(slug, *args, **kwargs)
    return decorated

# ── Platform: Auth ────────────────────────────────────────────────

@app.route("/")
def landing():
    if session.get("user_id"):
        return redirect("/dashboard")
    return render_template("landing.html")

@app.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def login_page():
    if session.get("user_id"):
        return redirect("/dashboard")
    err = None
    import time as _time
    form_ts = str(int(_time.time()))
    if request.method == "POST":
        # Honeypot
        if request.form.get("website_url", ""):
            err = "Invalid email or password."
            return render_template("login.html", err=err, form_ts=form_ts)
        # Time trap
        ts = request.form.get("_ts", "0")
        try:
            if _time.time() - int(ts) < 1.5:
                err = "Invalid email or password."
                return render_template("login.html", err=err, form_ts=form_ts)
        except (ValueError, TypeError):
            pass

        user = get_user_by_email(request.form.get("email", ""))
        if user and check_password_hash(user["password_hash"], request.form.get("password", "")):
            login_user(user)
            return redirect("/dashboard")
        err = "Invalid email or password."
    return render_template("login.html", err=err, form_ts=form_ts)

@app.route("/register", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def register_page():
    if session.get("user_id"):
        return redirect("/dashboard")
    err = None
    name = ""
    email = ""
    import time as _time
    form_ts = str(int(_time.time()))
    if request.method == "POST":
        # Honeypot — bots fill this hidden field, humans don't
        if request.form.get("website_url", ""):
            err = "Registration failed."
            return render_template("register.html", err=err, name="", email="", form_ts=form_ts)

        # Time trap — reject forms submitted in under 2 seconds
        ts = request.form.get("_ts", "0")
        try:
            elapsed = _time.time() - int(ts)
            if elapsed < 2:
                err = "Please slow down and try again."
                return render_template("register.html", err=err, name="", email="", form_ts=form_ts)
        except (ValueError, TypeError):
            pass

        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        password_confirm = request.form.get("password_confirm", "")

        if not name or not email or not password:
            err = "All fields are required."
        elif len(password) < 8:
            err = "Password must be at least 8 characters."
        elif password != password_confirm:
            err = "Passwords do not match."
        elif get_user_by_email(email):
            err = "An account with that email already exists."
        else:
            db = get_db()
            db.execute(
                "INSERT INTO users (name, email, password_hash, created) VALUES (?, ?, ?, ?)",
                (name, email, generate_password_hash(password), datetime.utcnow().isoformat())
            )
            db.commit()
            user = get_user_by_email(email)
            login_user(user)
            # Send verification email
            token = generate_token(user["id"], salt="email-verify")
            verify_url = request.host_url.rstrip("/") + f"/verify-email/{token}"
            send_email(
                to=email,
                subject="Verify your email - Community",
                html_body=f"""
                <div style="font-family:sans-serif;max-width:480px;margin:0 auto;padding:32px;">
                    <h2 style="color:#333;">Welcome to Community!</h2>
                    <p style="color:#666;">Hi {name},</p>
                    <p style="color:#666;">Please verify your email address to get full access.</p>
                    <p style="margin:24px 0;">
                        <a href="{verify_url}" style="background:#000;color:#fff;padding:12px 24px;border-radius:4px;text-decoration:none;font-weight:500;">Verify Email</a>
                    </p>
                </div>
                """
            )
            flash("Welcome! Check your email to verify your account.", "success")
            return redirect("/dashboard")
    return render_template("register.html", err=err, name=name, email=email, form_ts=form_ts)

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

# ── Password Reset ───────────────────────────────────────────────

@app.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit("3 per minute", methods=["POST"])
def forgot_password():
    sent = False
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = get_user_by_email(email)
        if user:
            token = generate_token(user["id"], salt="password-reset")
            reset_url = request.host_url.rstrip("/") + f"/reset-password/{token}"
            send_email(
                to=user["email"],
                subject="Reset your password",
                html_body=f"""
                <div style="font-family:sans-serif;max-width:480px;margin:0 auto;padding:32px;">
                    <h2 style="color:#333;">Reset your password</h2>
                    <p style="color:#666;">Hi {user['name']},</p>
                    <p style="color:#666;">Click the button below to reset your password. This link expires in 1 hour.</p>
                    <p style="margin:24px 0;">
                        <a href="{reset_url}" style="background:#000;color:#fff;padding:12px 24px;border-radius:4px;text-decoration:none;font-weight:500;">Reset Password</a>
                    </p>
                    <p style="color:#999;font-size:13px;">If you didn't request this, you can safely ignore this email.</p>
                </div>
                """
            )
        # Always show success to avoid email enumeration
        sent = True
    return render_template("forgot_password.html", sent=sent)

@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    user_id = verify_token(token, salt="password-reset", max_age=3600)
    if not user_id:
        flash("This reset link has expired or is invalid.", "error")
        return redirect("/forgot-password")
    user = get_user_by_id(user_id)
    if not user:
        flash("User not found.", "error")
        return redirect("/forgot-password")
    err = None
    if request.method == "POST":
        password = request.form.get("password", "")
        password_confirm = request.form.get("password_confirm", "")
        if len(password) < 8:
            err = "Password must be at least 8 characters."
        elif password != password_confirm:
            err = "Passwords do not match."
        else:
            db = get_db()
            db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                       (generate_password_hash(password), user_id))
            db.commit()
            flash("Password updated! You can now log in.", "success")
            return redirect("/login")
    return render_template("reset_password.html", err=err, token=token)

# ── Email Verification ───────────────────────────────────────────

@app.route("/verify-email/<token>")
def verify_email(token):
    user_id = verify_token(token, salt="email-verify", max_age=86400)  # 24 hours
    if not user_id:
        flash("This verification link has expired or is invalid.", "error")
        return redirect("/login")
    db = get_db()
    db.execute("UPDATE users SET email_verified = 1 WHERE id = ?", (user_id,))
    db.commit()
    flash("Email verified! Welcome to the community.", "success")
    if session.get("user_id"):
        return redirect("/dashboard")
    return redirect("/login")

@app.route("/resend-verification", methods=["POST"])
@login_required
@limiter.limit("2 per minute")
def resend_verification():
    user = get_user_by_id(session["user_id"])
    if user and not user["email_verified"]:
        token = generate_token(user["id"], salt="email-verify")
        verify_url = request.host_url.rstrip("/") + f"/verify-email/{token}"
        send_email(
            to=user["email"],
            subject="Verify your email",
            html_body=f"""
            <div style="font-family:sans-serif;max-width:480px;margin:0 auto;padding:32px;">
                <h2 style="color:#333;">Verify your email</h2>
                <p style="color:#666;">Hi {user['name']},</p>
                <p style="color:#666;">Click the button below to verify your email address.</p>
                <p style="margin:24px 0;">
                    <a href="{verify_url}" style="background:#000;color:#fff;padding:12px 24px;border-radius:4px;text-decoration:none;font-weight:500;">Verify Email</a>
                </p>
            </div>
            """
        )
        flash("Verification email sent! Check your inbox.", "success")
    return redirect("/dashboard")

# ── Platform: Dashboard ──────────────────────────────────────────

@app.route("/dashboard")
@login_required
def dashboard():
    communities = get_user_communities(session["user_id"])
    return render_template("dashboard.html", communities=communities)

# ── Platform: Create Community ───────────────────────────────────

@app.route("/communities/new", methods=["GET", "POST"])
@login_required
def create_community():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        slug = slugify(request.form.get("slug", "") or name)
        description = request.form.get("description", "").strip()

        if not name or not slug:
            flash("Community name is required.", "error")
            return redirect("/communities/new")

        if get_community_by_slug(slug):
            flash("That URL is already taken. Try a different one.", "error")
            return redirect("/communities/new")

        db = get_db()
        now = datetime.utcnow().isoformat()
        cursor = db.execute(
            "INSERT INTO communities (name, slug, description, owner_id, created) VALUES (?, ?, ?, ?, ?)",
            (name, slug, description, session["user_id"], now)
        )
        community_id = cursor.lastrowid
        db.execute(
            "INSERT INTO memberships (user_id, community_id, role, joined) VALUES (?, ?, ?, ?)",
            (session["user_id"], community_id, "owner", now)
        )
        db.commit()
        flash(f"'{name}' is live!", "success")
        return redirect(f"/c/{slug}/")

    return render_template("create_community.html")

# ── Platform: Join Community ─────────────────────────────────────

@app.route("/join/<slug>", methods=["GET", "POST"])
@login_required
def join_community(slug):
    community = get_community_by_slug(slug)
    if not community:
        abort(404)
    existing = get_membership(session["user_id"], community["id"])
    if existing:
        return redirect(f"/c/{slug}/")
    invite_err = None
    if request.method == "POST":
        # Check invite code if community is invite-only
        if community["invite_only"] and community["invite_code"]:
            submitted_code = request.form.get("invite_code", "").strip()
            if submitted_code != community["invite_code"]:
                invite_err = "Invalid invite code."
                return render_template("join_community.html", community=community, invite_err=invite_err)

        db = get_db()
        now = datetime.utcnow().isoformat()
        db.execute(
            "INSERT INTO memberships (user_id, community_id, role, joined) VALUES (?, ?, ?, ?)",
            (session["user_id"], community["id"], "member", now)
        )
        if community["welcome_message"]:
            db.execute("""
                INSERT INTO notifications (user_id, community_id, post_id, type, message, created)
                VALUES (?, ?, NULL, ?, ?, ?)
            """, (session["user_id"], community["id"], "welcome",
                  community["welcome_message"], now))
        db.commit()
        flash(f"Welcome to {community['name']}!", "success")
        return redirect(f"/c/{slug}/")
    return render_template("join_community.html", community=community, invite_err=invite_err)

# ── Platform: Account ────────────────────────────────────────────

@app.route("/account")
@login_required
def account_page():
    user = get_user_by_id(session["user_id"])
    return render_template("account.html", user=user)

@app.route("/account/profile", methods=["POST"])
@login_required
def account_profile():
    db = get_db()
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    bio = request.form.get("bio", "").strip()
    location = request.form.get("location", "").strip()
    website = request.form.get("website", "").strip()

    existing = get_user_by_email(email)
    if existing and existing["id"] != session["user_id"]:
        flash("Email already in use.", "error")
        return redirect("/account")

    db.execute(
        "UPDATE users SET name=?, email=?, bio=?, location=?, website=? WHERE id=?",
        (name, email, bio, location, website, session["user_id"])
    )
    db.commit()
    session["user_name"] = name
    flash("Profile updated.", "success")
    return redirect("/account")

@app.route("/account/password", methods=["POST"])
@login_required
def account_password():
    db = get_db()
    user = get_user_by_id(session["user_id"])
    if not check_password_hash(user["password_hash"], request.form.get("current", "")):
        flash("Current password incorrect.", "error")
        return redirect("/account")
    new_pw = request.form.get("new", "")
    if len(new_pw) < 8:
        flash("Password must be at least 8 characters.", "error")
        return redirect("/account")
    db.execute(
        "UPDATE users SET password_hash=? WHERE id=?",
        (generate_password_hash(new_pw), session["user_id"])
    )
    db.commit()
    flash("Password updated.", "success")
    return redirect("/account")

@app.route("/account/avatar", methods=["POST"])
@login_required
def account_avatar():
    file = request.files.get("avatar")
    if not file or not file.filename:
        flash("No file selected.", "error")
        return redirect("/account")
    ext = file.filename.rsplit(".", 1)[-1].lower()
    if ext not in ("jpg", "jpeg", "png", "webp", "gif"):
        flash("Please upload a JPG, PNG, or WebP image.", "error")
        return redirect("/account")
    avatar_dir = UPLOAD_DIR / "avatars"
    avatar_dir.mkdir(exist_ok=True)
    avatar_name = f"avatar_{session['user_id']}.{ext}"
    file.save(str(avatar_dir / avatar_name))
    db = get_db()
    db.execute("UPDATE users SET avatar_path = ? WHERE id = ?",
               (f"avatars/{avatar_name}", session["user_id"]))
    db.commit()
    flash("Profile photo updated!", "success")
    return redirect("/account")

# ══════════════════════════════════════════════════════════════════
#  COMMUNITY-SCOPED ROUTES — /c/<slug>/...
# ══════════════════════════════════════════════════════════════════

# ── Community: Feed ──────────────────────────────────────────────

@app.route("/c/<slug>/")
@community_member_required
def community_feed(slug):
    db = get_db()
    community = g.community
    sort = request.args.get("sort", "new")
    category = request.args.get("category", "")
    q = request.args.get("q", "").strip()

    order = "p.is_pinned DESC, p.created DESC"
    if sort == "top":
        order = "p.is_pinned DESC, vote_score DESC, p.created DESC"
    elif sort == "discussed":
        order = "p.is_pinned DESC, comment_count DESC, p.created DESC"

    where = "p.community_id = ? AND p.is_draft = 0"
    params = [community["id"]]
    if category:
        where += " AND p.category = ?"
        params.append(category)
    if q:
        where += " AND (p.title LIKE ? OR p.body LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]

    posts = db.execute(f"""
        SELECT p.*, u.name AS author_name,
               (SELECT COUNT(*) FROM comments c WHERE c.post_id = p.id) AS comment_count,
               (SELECT COALESCE(SUM(v.value), 0) FROM votes v WHERE v.post_id = p.id) AS vote_score
        FROM posts p JOIN users u ON p.user_id = u.id
        WHERE {where}
        ORDER BY {order} LIMIT 50
    """, params).fetchall()

    # Get all categories for this community
    categories_raw = db.execute("""
        SELECT category, COUNT(*) AS cnt FROM posts
        WHERE community_id = ? AND category != ''
        GROUP BY category ORDER BY cnt DESC
    """, (community["id"],)).fetchall()
    categories = [(r["category"], r["cnt"]) for r in categories_raw]

    # Get current user's votes, bookmarks, follows
    post_ids = [p["id"] for p in posts]
    my_votes = {}
    my_bookmarks = set()
    my_follows = set()
    if post_ids:
        ph = ",".join("?" * len(post_ids))
        uid = session["user_id"]
        rows = db.execute(
            f"SELECT post_id, value FROM votes WHERE user_id = ? AND post_id IN ({ph})",
            [uid] + post_ids
        ).fetchall()
        my_votes = {r["post_id"]: r["value"] for r in rows}
        rows = db.execute(
            f"SELECT post_id FROM bookmarks WHERE user_id = ? AND post_id IN ({ph})",
            [uid] + post_ids
        ).fetchall()
        my_bookmarks = {r["post_id"] for r in rows}
        rows = db.execute(
            f"SELECT post_id FROM follows WHERE user_id = ? AND post_id IN ({ph})",
            [uid] + post_ids
        ).fetchall()
        my_follows = {r["post_id"] for r in rows}

    posts_enriched = []
    for p in posts:
        d = dict(p)
        body = d.get("body") or ""
        plain = strip_markdown(body)
        d["preview"] = plain[:200] + ("..." if len(plain) > 200 else "")
        d["my_vote"] = my_votes.get(p["id"], 0)
        d["is_bookmarked"] = p["id"] in my_bookmarks
        d["is_following"] = p["id"] in my_follows
        d["flair"] = get_user_flair(p["user_id"], community["id"])
        d["author_initial"] = p["author_name"][0].upper() if p["author_name"] else "?"
        posts_enriched.append(d)

    member_count = db.execute(
        "SELECT COUNT(*) FROM memberships WHERE community_id = ?", (community["id"],)
    ).fetchone()[0]
    notif_count = get_unread_notification_count(session["user_id"], community["id"])

    # Onboarding checklist
    user = current_user()
    onboarding = None
    has_bio = bool(user["bio"])
    has_post = db.execute("SELECT 1 FROM posts WHERE user_id = ? AND community_id = ? AND is_draft = 0 LIMIT 1",
                          (session["user_id"], community["id"])).fetchone() is not None
    has_rsvp = db.execute("""
        SELECT 1 FROM rsvps r JOIN events e ON r.event_id = e.id
        WHERE r.user_id = ? AND e.community_id = ? LIMIT 1
    """, (session["user_id"], community["id"])).fetchone() is not None
    has_comment = db.execute("""
        SELECT 1 FROM comments c JOIN posts p ON c.post_id = p.id
        WHERE c.user_id = ? AND p.community_id = ? LIMIT 1
    """, (session["user_id"], community["id"])).fetchone() is not None
    checklist = [
        {"done": has_bio, "label": "Complete your profile", "url": "/account", "icon": "👤"},
        {"done": has_post, "label": "Publish your first post", "url": f"/c/{slug}/posts/new", "icon": "✍️"},
        {"done": has_comment, "label": "Comment on a discussion", "url": f"/c/{slug}/", "icon": "💬"},
        {"done": has_rsvp, "label": "RSVP to an event", "url": f"/c/{slug}/events", "icon": "📅"},
    ]
    completed = sum(1 for c in checklist if c["done"])
    if completed < len(checklist):
        onboarding = {"checklist": checklist, "completed": completed, "total": len(checklist),
                      "percent": int(completed / len(checklist) * 100)}

    return render_template("community/feed.html", community=community,
                           posts=posts_enriched, membership=g.membership,
                           member_count=member_count, sort=sort, notif_count=notif_count,
                           categories=categories, current_category=category, search_q=q,
                           onboarding=onboarding)

# ── Community: Posts ─────────────────────────────────────────────

@app.route("/c/<slug>/posts/new", methods=["GET", "POST"])
@community_member_required
def community_new_post(slug):
    community = g.community
    db = get_db()
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        body = request.form.get("body", "").strip()
        category = request.form.get("category", "").strip()
        is_draft = 1 if request.form.get("save_draft") else 0
        if not title:
            flash("Title is required.", "error")
            return redirect(f"/c/{slug}/posts/new")
        now = datetime.utcnow().isoformat()
        cursor = db.execute(
            "INSERT INTO posts (community_id, user_id, title, body, category, is_draft, created, updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (community["id"], session["user_id"], title, body, category, is_draft, now, now)
        )
        db.commit()
        if is_draft:
            flash("Draft saved!", "success")
            return redirect(f"/c/{slug}/drafts")
        return redirect(f"/c/{slug}/posts/{cursor.lastrowid}")
    # Get existing categories for autocomplete
    cats = db.execute(
        "SELECT DISTINCT category FROM posts WHERE community_id = ? AND category != '' ORDER BY category",
        (community["id"],)
    ).fetchall()
    existing_cats = [c["category"] for c in cats]
    return render_template("community/new_post.html", community=community,
                           membership=g.membership, post=None, existing_categories=existing_cats)

@app.route("/c/<slug>/posts/<int:pid>")
@community_member_required
def community_view_post(slug, pid):
    db = get_db()
    community = g.community
    post = db.execute("""
        SELECT p.*, u.name AS author_name,
               (SELECT COALESCE(SUM(v.value), 0) FROM votes v WHERE v.post_id = p.id) AS vote_score
        FROM posts p JOIN users u ON p.user_id = u.id
        WHERE p.id = ? AND p.community_id = ?
    """, (pid, community["id"])).fetchone()
    if not post:
        flash("Post not found.", "error")
        return redirect(f"/c/{slug}/")
    my_vote = db.execute(
        "SELECT value FROM votes WHERE post_id = ? AND user_id = ?",
        (pid, session["user_id"])
    ).fetchone()
    comments_raw = db.execute("""
        SELECT c.*, u.name AS author_name,
               (SELECT COALESCE(SUM(cv.value), 0) FROM comment_votes cv WHERE cv.comment_id = c.id) AS vote_score
        FROM comments c JOIN users u ON c.user_id = u.id
        WHERE c.post_id = ? ORDER BY c.created ASC
    """, (pid,)).fetchall()

    # Build enriched comment list with votes, awards, tree structure
    comment_ids = [c["id"] for c in comments_raw]
    my_cv = {}
    awards_map = {}
    my_awards = {}
    is_admin = g.membership["role"] in ("owner", "admin")
    emoji_map = {"fire": "\U0001f525", "love": "\u2764\ufe0f", "brain": "\U0001f9e0",
                 "clap": "\U0001f44f", "star": "\u2b50", "hundred": "\U0001f4af"}
    if comment_ids:
        ph = ",".join("?" * len(comment_ids))
        uid = session["user_id"]
        for r in db.execute(f"SELECT comment_id, value FROM comment_votes WHERE user_id=? AND comment_id IN ({ph})", [uid] + comment_ids).fetchall():
            my_cv[r["comment_id"]] = r["value"]
        for r in db.execute(f"SELECT comment_id, emoji, COUNT(*) AS cnt FROM comment_awards WHERE comment_id IN ({ph}) GROUP BY comment_id, emoji", comment_ids).fetchall():
            awards_map.setdefault(r["comment_id"], []).append({"emoji": r["emoji"], "symbol": emoji_map.get(r["emoji"], r["emoji"]), "count": r["cnt"]})
        for r in db.execute(f"SELECT comment_id, emoji FROM comment_awards WHERE user_id=? AND comment_id IN ({ph})", [uid] + comment_ids).fetchall():
            my_awards.setdefault(r["comment_id"], set()).add(r["emoji"])

    comments_enriched = []
    by_id = {}
    for c in comments_raw:
        cid = c["id"]
        ca = awards_map.get(cid, [])
        ma = my_awards.get(cid, set())
        node = {
            "id": cid, "user_id": c["user_id"], "body": c["body"],
            "parent_id": c["parent_id"], "created": c["created"],
            "author_name": c["author_name"], "vote_score": c["vote_score"],
            "my_vote": my_cv.get(cid, 0),
            "can_delete": c["user_id"] == session["user_id"] or is_admin,
            "awards": [{"emoji": a["emoji"], "symbol": a["symbol"], "count": a["count"], "mine": a["emoji"] in ma} for a in ca],
            "has_awards": len(ca) > 0,
            "children": []
        }
        by_id[cid] = node
        comments_enriched.append(node)

    # Build tree
    comment_tree = []
    for node in comments_enriched:
        pid_ref = node["parent_id"]
        if pid_ref and pid_ref in by_id:
            by_id[pid_ref]["children"].append(node)
        else:
            comment_tree.append(node)

    flair = get_user_flair(post["user_id"], community["id"])
    is_bookmarked = db.execute(
        "SELECT 1 FROM bookmarks WHERE post_id = ? AND user_id = ?",
        (pid, session["user_id"])
    ).fetchone() is not None
    is_following = db.execute(
        "SELECT 1 FROM follows WHERE post_id = ? AND user_id = ?",
        (pid, session["user_id"])
    ).fetchone() is not None
    follow_count = db.execute(
        "SELECT COUNT(*) FROM follows WHERE post_id = ?", (pid,)
    ).fetchone()[0]
    return render_template("community/post.html", community=community,
                           membership=g.membership, post=post, comment_tree=comment_tree,
                           my_vote=my_vote["value"] if my_vote else 0, flair=flair,
                           is_bookmarked=is_bookmarked, is_following=is_following,
                           follow_count=follow_count)

@app.route("/c/<slug>/posts/<int:pid>/json")
@community_member_required
def community_post_json(slug, pid):
    db = get_db()
    community = g.community
    post = db.execute("""
        SELECT p.*, u.name AS author_name,
               (SELECT COALESCE(SUM(v.value), 0) FROM votes v WHERE v.post_id = p.id) AS vote_score
        FROM posts p JOIN users u ON p.user_id = u.id
        WHERE p.id = ? AND p.community_id = ?
    """, (pid, community["id"])).fetchone()
    if not post:
        return {"error": "not found"}, 404
    my_vote = db.execute(
        "SELECT value FROM votes WHERE post_id = ? AND user_id = ?",
        (pid, session["user_id"])
    ).fetchone()
    comments_raw = db.execute("""
        SELECT c.id, c.user_id, c.body, c.parent_id, c.created, u.name AS author_name,
               (SELECT COALESCE(SUM(cv.value), 0) FROM comment_votes cv WHERE cv.comment_id = c.id) AS vote_score
        FROM comments c JOIN users u ON c.user_id = u.id
        WHERE c.post_id = ? ORDER BY c.created ASC
    """, (pid,)).fetchall()

    # Get current user's comment votes
    comment_ids = [c["id"] for c in comments_raw]
    my_comment_votes = {}
    awards_map = {}
    my_awards = {}
    if comment_ids:
        ph = ",".join("?" * len(comment_ids))
        uid = session["user_id"]
        for r in db.execute(f"SELECT comment_id, value FROM comment_votes WHERE user_id=? AND comment_id IN ({ph})", [uid] + comment_ids).fetchall():
            my_comment_votes[r["comment_id"]] = r["value"]
        for r in db.execute(f"SELECT comment_id, emoji, COUNT(*) AS cnt FROM comment_awards WHERE comment_id IN ({ph}) GROUP BY comment_id, emoji", comment_ids).fetchall():
            awards_map.setdefault(r["comment_id"], []).append({"emoji": r["emoji"], "count": r["cnt"]})
        for r in db.execute(f"SELECT comment_id, emoji FROM comment_awards WHERE user_id=? AND comment_id IN ({ph})", [uid] + comment_ids).fetchall():
            my_awards.setdefault(r["comment_id"], set()).add(r["emoji"])

    flair = get_user_flair(post["user_id"], community["id"])
    is_bookmarked = db.execute(
        "SELECT 1 FROM bookmarks WHERE post_id = ? AND user_id = ?",
        (pid, session["user_id"])
    ).fetchone() is not None
    is_following = db.execute(
        "SELECT 1 FROM follows WHERE post_id = ? AND user_id = ?",
        (pid, session["user_id"])
    ).fetchone() is not None
    follow_count = db.execute(
        "SELECT COUNT(*) FROM follows WHERE post_id = ?", (pid,)
    ).fetchone()[0]
    is_admin = g.membership["role"] in ("owner", "admin")

    emoji_map = {"fire": "\U0001f525", "love": "\u2764\ufe0f", "brain": "\U0001f9e0", "clap": "\U0001f44f", "star": "\u2b50", "hundred": "\U0001f4af"}

    comments_out = []
    for c in comments_raw:
        cid = c["id"]
        ca = awards_map.get(cid, [])
        ma = my_awards.get(cid, set())
        comments_out.append({
            "id": cid, "user_id": c["user_id"], "body": c["body"],
            "body_html": str(render_markdown(c["body"])),
            "parent_id": c["parent_id"], "created": c["created"],
            "author_name": c["author_name"],
            "author_initial": c["author_name"][0].upper(),
            "vote_score": c["vote_score"],
            "my_vote": my_comment_votes.get(cid, 0),
            "can_delete": c["user_id"] == session["user_id"] or is_admin,
            "awards": [{"emoji": a["emoji"], "symbol": emoji_map.get(a["emoji"], a["emoji"]), "count": a["count"], "mine": a["emoji"] in ma} for a in ca],
            "has_awards": len(ca) > 0
        })

    return {
        "id": post["id"], "title": post["title"], "body": post["body"],
        "body_html": str(render_markdown(post["body"])),
        "is_pinned": bool(post["is_pinned"]) if "is_pinned" in post.keys() else False,
        "category": post["category"] or "", "created": post["created"],
        "updated": post["updated"], "user_id": post["user_id"],
        "author_name": post["author_name"],
        "author_initial": post["author_name"][0].upper(),
        "vote_score": post["vote_score"],
        "my_vote": my_vote["value"] if my_vote else 0,
        "flair": flair, "is_bookmarked": is_bookmarked,
        "is_following": is_following, "follow_count": follow_count,
        "is_owner": post["user_id"] == session["user_id"],
        "is_admin": is_admin,
        "current_user_id": session["user_id"],
        "current_user_initial": current_user()["name"][0].upper(),
        "award_emojis": [{"key": k, "symbol": v} for k, v in emoji_map.items()],
        "comments": comments_out
    }

@app.route("/c/<slug>/posts/<int:pid>/edit", methods=["GET", "POST"])
@community_member_required
def community_edit_post(slug, pid):
    db = get_db()
    community = g.community
    post = db.execute("SELECT * FROM posts WHERE id = ? AND community_id = ?",
                      (pid, community["id"])).fetchone()
    if not post or post["user_id"] != session["user_id"]:
        flash("Not allowed.", "error")
        return redirect(f"/c/{slug}/")
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        body = request.form.get("body", "").strip()
        if not title:
            flash("Title is required.", "error")
            return redirect(f"/c/{slug}/posts/{pid}/edit")
        db.execute(
            "UPDATE posts SET title=?, body=?, updated=? WHERE id=?",
            (title, body, datetime.utcnow().isoformat(), pid)
        )
        db.commit()
        flash("Post updated.", "success")
        return redirect(f"/c/{slug}/posts/{pid}")
    return render_template("community/new_post.html", community=community,
                           membership=g.membership, post=post)

@app.route("/c/<slug>/posts/<int:pid>/delete", methods=["POST"])
@community_member_required
def community_delete_post(slug, pid):
    db = get_db()
    community = g.community
    post = db.execute("SELECT * FROM posts WHERE id = ? AND community_id = ?",
                      (pid, community["id"])).fetchone()
    is_admin = g.membership["role"] in ("owner", "admin")
    if not post or (post["user_id"] != session["user_id"] and not is_admin):
        flash("Not allowed.", "error")
        return redirect(f"/c/{slug}/")
    db.execute("DELETE FROM posts WHERE id = ?", (pid,))
    db.commit()
    flash("Post deleted.", "success")
    return redirect(f"/c/{slug}/")

@app.route("/c/<slug>/posts/<int:pid>/pin", methods=["POST"])
@community_admin_required
def community_pin_post(slug, pid):
    db = get_db()
    community = g.community
    post = db.execute("SELECT is_pinned FROM posts WHERE id = ? AND community_id = ?",
                      (pid, community["id"])).fetchone()
    if not post:
        flash("Post not found.", "error")
        return redirect(f"/c/{slug}/")
    new_val = 0 if post["is_pinned"] else 1
    db.execute("UPDATE posts SET is_pinned = ? WHERE id = ?", (new_val, pid))
    db.commit()
    flash(f"Post {'pinned' if new_val else 'unpinned'}.", "success")
    referrer = request.referrer or f"/c/{slug}/"
    return redirect(referrer)

@app.route("/c/<slug>/posts/<int:pid>/vote", methods=["POST"])
@community_member_required
def community_vote(slug, pid):
    value = int(request.form.get("value", 0))
    if value not in (1, -1):
        return redirect(f"/c/{slug}/")
    db = get_db()
    existing = db.execute(
        "SELECT id, value FROM votes WHERE post_id = ? AND user_id = ?",
        (pid, session["user_id"])
    ).fetchone()
    now = datetime.utcnow().isoformat()
    if existing:
        if existing["value"] == value:
            # Toggle off — remove vote
            db.execute("DELETE FROM votes WHERE id = ?", (existing["id"],))
        else:
            # Switch vote direction
            db.execute("UPDATE votes SET value = ?, created = ? WHERE id = ?",
                       (value, now, existing["id"]))
    else:
        db.execute("INSERT INTO votes (post_id, user_id, value, created) VALUES (?, ?, ?, ?)",
                   (pid, session["user_id"], value, now))
    db.commit()
    # Return to wherever they came from
    referrer = request.referrer or f"/c/{slug}/"
    return redirect(referrer)

@app.route("/c/<slug>/posts/<int:pid>/bookmark", methods=["POST"])
@community_member_required
def community_bookmark(slug, pid):
    db = get_db()
    existing = db.execute(
        "SELECT id FROM bookmarks WHERE post_id = ? AND user_id = ?",
        (pid, session["user_id"])
    ).fetchone()
    if existing:
        db.execute("DELETE FROM bookmarks WHERE id = ?", (existing["id"],))
    else:
        db.execute("INSERT INTO bookmarks (post_id, user_id, created) VALUES (?, ?, ?)",
                   (pid, session["user_id"], datetime.utcnow().isoformat()))
    db.commit()
    referrer = request.referrer or f"/c/{slug}/"
    return redirect(referrer)

@app.route("/c/<slug>/posts/<int:pid>/follow", methods=["POST"])
@community_member_required
def community_follow(slug, pid):
    db = get_db()
    existing = db.execute(
        "SELECT id FROM follows WHERE post_id = ? AND user_id = ?",
        (pid, session["user_id"])
    ).fetchone()
    if existing:
        db.execute("DELETE FROM follows WHERE id = ?", (existing["id"],))
    else:
        db.execute("INSERT INTO follows (post_id, user_id, created) VALUES (?, ?, ?)",
                   (pid, session["user_id"], datetime.utcnow().isoformat()))
    db.commit()
    referrer = request.referrer or f"/c/{slug}/"
    return redirect(referrer)

@app.route("/c/<slug>/posts/<int:pid>/comment", methods=["POST"])
@community_member_required
def community_add_comment(slug, pid):
    body = request.form.get("body", "").strip()
    parent_id = request.form.get("parent_id", "")
    parent_id = int(parent_id) if parent_id else None
    if not body:
        flash("Comment cannot be empty.", "error")
        return redirect(f"/c/{slug}/posts/{pid}")
    db = get_db()
    community = g.community
    now = datetime.utcnow().isoformat()
    db.execute(
        "INSERT INTO comments (post_id, user_id, body, parent_id, created) VALUES (?, ?, ?, ?, ?)",
        (pid, session["user_id"], body, parent_id, now)
    )
    # Notify followers (except the commenter)
    post = db.execute("SELECT title, user_id FROM posts WHERE id = ?", (pid,)).fetchone()
    commenter = get_user_by_id(session["user_id"])
    followers = db.execute(
        "SELECT user_id FROM follows WHERE post_id = ? AND user_id != ?",
        (pid, session["user_id"])
    ).fetchall()
    # Also notify the post author if they're not the commenter
    notify_ids = set(f["user_id"] for f in followers)
    if post and post["user_id"] != session["user_id"]:
        notify_ids.add(post["user_id"])
    for uid in notify_ids:
        db.execute("""
            INSERT INTO notifications (user_id, community_id, post_id, type, message, created)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (uid, community["id"], pid, "comment",
              f'{commenter["name"]} commented on "{post["title"]}"', now))

    # @mentions — find @Name patterns and notify mentioned users
    mentions = re.findall(r'@(\w[\w\s]{1,30}?)(?=\s|$|[.,!?])', body)
    for mention_name in mentions:
        mentioned = db.execute(
            "SELECT u.id FROM users u JOIN memberships m ON u.id = m.user_id WHERE m.community_id = ? AND u.name LIKE ? AND u.id != ?",
            (community["id"], mention_name.strip(), session["user_id"])
        ).fetchone()
        if mentioned and mentioned["id"] not in notify_ids:
            db.execute("""
                INSERT INTO notifications (user_id, community_id, post_id, type, message, created)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (mentioned["id"], community["id"], pid, "mention",
                  f'{commenter["name"]} mentioned you in "{post["title"]}"', now))

    db.commit()
    return redirect(f"/c/{slug}/posts/{pid}")

@app.route("/c/<slug>/comments/<int:cid>/delete", methods=["POST"])
@community_member_required
def community_delete_comment(slug, cid):
    db = get_db()
    comment = db.execute("SELECT * FROM comments WHERE id = ?", (cid,)).fetchone()
    is_admin = g.membership["role"] in ("owner", "admin")
    if not comment or (comment["user_id"] != session["user_id"] and not is_admin):
        flash("Not allowed.", "error")
        return redirect(f"/c/{slug}/")
    post_id = comment["post_id"]
    db.execute("DELETE FROM comments WHERE id = ?", (cid,))
    db.commit()
    return redirect(f"/c/{slug}/posts/{post_id}")

@app.route("/c/<slug>/comments/<int:cid>/vote", methods=["POST"])
@community_member_required
def community_comment_vote(slug, cid):
    value = int(request.form.get("value", 0))
    if value not in (1, -1):
        return redirect(f"/c/{slug}/")
    db = get_db()
    existing = db.execute(
        "SELECT id, value FROM comment_votes WHERE comment_id = ? AND user_id = ?",
        (cid, session["user_id"])
    ).fetchone()
    now = datetime.utcnow().isoformat()
    if existing:
        if existing["value"] == value:
            db.execute("DELETE FROM comment_votes WHERE id = ?", (existing["id"],))
        else:
            db.execute("UPDATE comment_votes SET value=?, created=? WHERE id=?",
                       (value, now, existing["id"]))
    else:
        db.execute("INSERT INTO comment_votes (comment_id, user_id, value, created) VALUES (?,?,?,?)",
                   (cid, session["user_id"], value, now))
    db.commit()
    referrer = request.referrer or f"/c/{slug}/"
    return redirect(referrer)

AWARD_EMOJIS = {"fire": "🔥", "love": "❤️", "brain": "🧠", "clap": "👏", "star": "⭐", "hundred": "💯"}

@app.route("/c/<slug>/comments/<int:cid>/award", methods=["POST"])
@community_member_required
def community_comment_award(slug, cid):
    emoji = request.form.get("emoji", "")
    if emoji not in AWARD_EMOJIS:
        return redirect(f"/c/{slug}/")
    db = get_db()
    existing = db.execute(
        "SELECT id FROM comment_awards WHERE comment_id=? AND user_id=? AND emoji=?",
        (cid, session["user_id"], emoji)
    ).fetchone()
    if existing:
        db.execute("DELETE FROM comment_awards WHERE id=?", (existing["id"],))
    else:
        db.execute("INSERT INTO comment_awards (comment_id, user_id, emoji, created) VALUES (?,?,?,?)",
                   (cid, session["user_id"], emoji, datetime.utcnow().isoformat()))
    db.commit()
    referrer = request.referrer or f"/c/{slug}/"
    return redirect(referrer)

# ── Community: Events ────────────────────────────────────────────

EVENT_TYPES = {
    "event": "Event", "webinar": "Webinar", "workshop": "Workshop",
    "ama": "AMA", "meetup": "Meetup", "social": "Social",
}

def _generate_ics(event, community):
    """Generate an .ics calendar file for an event."""
    uid = f"event-{event['id']}@community"
    dtstart = event["event_date"].replace("-", "")
    if event["event_time"]:
        dtstart += "T" + event["event_time"].replace(":", "") + "00Z"
    dtend = dtstart
    if event["end_time"]:
        dtend = event["event_date"].replace("-", "") + "T" + event["end_time"].replace(":", "") + "00Z"
    desc = (event["description"] or "").replace("\n", "\\n")
    return f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Community//Events//EN
BEGIN:VEVENT
UID:{uid}
DTSTART:{dtstart}
DTEND:{dtend}
SUMMARY:{event['title']}
DESCRIPTION:{desc}
LOCATION:{event['location'] or ''}
URL:{event['link'] or ''}
END:VEVENT
END:VCALENDAR"""

def _google_cal_url(event):
    """Generate a Google Calendar add URL."""
    dates = event["event_date"].replace("-", "")
    if event["event_time"]:
        dates += "T" + event["event_time"].replace(":", "") + "00Z"
    end = dates
    if event["end_time"]:
        end = event["event_date"].replace("-", "") + "T" + event["end_time"].replace(":", "") + "00Z"
    from urllib.parse import quote
    desc = (event["description"] or "")[:500]
    loc = event["location"] or ""
    return f"https://calendar.google.com/calendar/render?action=TEMPLATE&text={quote(event['title'])}&dates={dates}/{end}&details={quote(desc)}&location={quote(loc)}"

def _outlook_cal_url(event):
    """Generate an Outlook web calendar URL."""
    from urllib.parse import quote
    start = event["event_date"]
    if event["event_time"]:
        start += "T" + event["event_time"] + ":00"
    end = start
    if event["end_time"]:
        end = event["event_date"] + "T" + event["end_time"] + ":00"
    desc = (event["description"] or "")[:500]
    loc = event["location"] or ""
    return f"https://outlook.office.com/calendar/0/deeplink/compose?subject={quote(event['title'])}&startdt={start}&enddt={end}&body={quote(desc)}&location={quote(loc)}"

@app.route("/c/<slug>/events")
@community_member_required
def community_events(slug):
    db = get_db()
    community = g.community
    view = request.args.get("view", "upcoming")
    now = datetime.utcnow().strftime("%Y-%m-%d")

    if view == "past":
        events = db.execute("""
            SELECT e.*, u.name AS creator_name,
                   (SELECT COUNT(*) FROM rsvps r WHERE r.event_id = e.id AND r.status = 'going' AND r.waitlist = 0) AS going_count,
                   (SELECT COUNT(*) FROM rsvps r WHERE r.event_id = e.id AND r.waitlist = 1) AS waitlist_count
            FROM events e JOIN users u ON e.user_id = u.id
            WHERE e.community_id = ? AND e.event_date < ?
            ORDER BY e.event_date DESC
        """, (community["id"], now)).fetchall()
    elif view == "calendar":
        month = request.args.get("month", datetime.utcnow().strftime("%Y-%m"))
        events = db.execute("""
            SELECT e.*, u.name AS creator_name,
                   (SELECT COUNT(*) FROM rsvps r WHERE r.event_id = e.id AND r.status = 'going' AND r.waitlist = 0) AS going_count,
                   (SELECT COUNT(*) FROM rsvps r WHERE r.event_id = e.id AND r.waitlist = 1) AS waitlist_count
            FROM events e JOIN users u ON e.user_id = u.id
            WHERE e.community_id = ? AND e.event_date LIKE ?
            ORDER BY e.event_date ASC, e.event_time ASC
        """, (community["id"], f"{month}%")).fetchall()
        return render_template("community/events_calendar.html", community=community,
                               membership=g.membership, events=events, month=month)
    else:
        events = db.execute("""
            SELECT e.*, u.name AS creator_name,
                   (SELECT COUNT(*) FROM rsvps r WHERE r.event_id = e.id AND r.status = 'going' AND r.waitlist = 0) AS going_count,
                   (SELECT COUNT(*) FROM rsvps r WHERE r.event_id = e.id AND r.waitlist = 1) AS waitlist_count
            FROM events e JOIN users u ON e.user_id = u.id
            WHERE e.community_id = ? AND e.event_date >= ?
            ORDER BY e.event_date ASC, e.event_time ASC
        """, (community["id"], now)).fetchall()

    event_ids = [e["id"] for e in events]
    my_rsvps = {}
    if event_ids:
        placeholders = ",".join("?" * len(event_ids))
        rows = db.execute(
            f"SELECT event_id, status, waitlist FROM rsvps WHERE user_id = ? AND event_id IN ({placeholders})",
            [session["user_id"]] + event_ids
        ).fetchall()
        my_rsvps = {r["event_id"]: {"status": r["status"], "waitlist": r["waitlist"]} for r in rows}

    return render_template("community/events.html", community=community,
                           membership=g.membership, events=events,
                           my_rsvps=my_rsvps, view=view, event_types=EVENT_TYPES)

@app.route("/c/<slug>/events/new", methods=["GET", "POST"])
@community_member_required
def community_new_event(slug):
    community = g.community
    db = get_db()
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        event_date = request.form.get("event_date", "").strip()
        event_time = request.form.get("event_time", "").strip()
        end_time = request.form.get("end_time", "").strip()
        location = request.form.get("location", "").strip()
        location_type = request.form.get("location_type", "in-person")
        link = request.form.get("link", "").strip()
        event_type = request.form.get("event_type", "event")
        capacity = int(request.form.get("capacity", 0) or 0)
        speakers = request.form.get("speakers", "").strip()

        if not title or not event_date:
            flash("Title and date are required.", "error")
            return redirect(f"/c/{slug}/events/new")

        now = datetime.utcnow().isoformat()
        cursor = db.execute("""
            INSERT INTO events (community_id, user_id, title, description, event_date,
                                event_time, end_time, location, location_type, link,
                                event_type, capacity, speakers, created)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (community["id"], session["user_id"], title, description, event_date,
              event_time, end_time, location, location_type, link,
              event_type, capacity, speakers, now))
        event_id = cursor.lastrowid

        # Auto-RSVP the creator
        db.execute("INSERT INTO rsvps (event_id, user_id, status, created) VALUES (?, ?, ?, ?)",
                   (event_id, session["user_id"], "going", now))

        # Auto-create discussion thread
        post_cursor = db.execute("""
            INSERT INTO posts (community_id, user_id, title, body, category, is_pinned, created, updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (community["id"], session["user_id"],
              f"Event Discussion: {title}",
              f"Discussion thread for **{title}** on {event_date}.\n\n{description[:200] if description else 'Share your thoughts, questions, or what you are looking forward to!'}",
              "Events", 0, now, now))
        post_id = post_cursor.lastrowid
        db.execute("UPDATE events SET post_id = ? WHERE id = ?", (post_id, event_id))

        db.commit()
        flash("Event created with discussion thread!", "success")
        return redirect(f"/c/{slug}/events/{event_id}")

    # Get community members for speaker selection
    members = db.execute("""
        SELECT u.id, u.name FROM memberships m JOIN users u ON m.user_id = u.id
        WHERE m.community_id = ? ORDER BY u.name
    """, (community["id"],)).fetchall()
    return render_template("community/new_event.html", community=community,
                           membership=g.membership, event=None,
                           event_types=EVENT_TYPES, members=members)

@app.route("/c/<slug>/events/<int:eid>")
@community_member_required
def community_view_event(slug, eid):
    db = get_db()
    community = g.community
    event = db.execute("""
        SELECT e.*, u.name AS creator_name
        FROM events e JOIN users u ON e.user_id = u.id
        WHERE e.id = ? AND e.community_id = ?
    """, (eid, community["id"])).fetchone()
    if not event:
        flash("Event not found.", "error")
        return redirect(f"/c/{slug}/events")

    attendees = db.execute("""
        SELECT u.id, u.name, r.status, r.waitlist, r.created
        FROM rsvps r JOIN users u ON r.user_id = u.id
        WHERE r.event_id = ? AND r.waitlist = 0
        ORDER BY r.created ASC
    """, (eid,)).fetchall()

    waitlist = db.execute("""
        SELECT u.id, u.name, r.created
        FROM rsvps r JOIN users u ON r.user_id = u.id
        WHERE r.event_id = ? AND r.waitlist = 1
        ORDER BY r.created ASC
    """, (eid,)).fetchall()

    my_rsvp = db.execute(
        "SELECT status, waitlist FROM rsvps WHERE event_id = ? AND user_id = ?",
        (eid, session["user_id"])
    ).fetchone()

    going_count = len([a for a in attendees if a["status"] == "going"])
    is_full = event["capacity"] > 0 and going_count >= event["capacity"]
    is_past = event["event_date"] < datetime.utcnow().strftime("%Y-%m-%d")

    # Speaker info
    speaker_ids = [int(s.strip()) for s in (event["speakers"] or "").split(",") if s.strip().isdigit()]
    speaker_list = []
    for sid in speaker_ids:
        sp = get_user_by_id(sid)
        if sp:
            speaker_list.append(sp)

    google_url = _google_cal_url(event)
    outlook_url = _outlook_cal_url(event)

    return render_template("community/event_detail.html", community=community,
                           membership=g.membership, event=event,
                           attendees=attendees, waitlist=waitlist, my_rsvp=my_rsvp,
                           going_count=going_count, is_full=is_full, is_past=is_past,
                           speaker_list=speaker_list, google_url=google_url,
                           outlook_url=outlook_url, event_types=EVENT_TYPES)

@app.route("/c/<slug>/events/<int:eid>/ics")
@community_member_required
def community_event_ics(slug, eid):
    db = get_db()
    community = g.community
    event = db.execute("SELECT * FROM events WHERE id = ? AND community_id = ?",
                       (eid, community["id"])).fetchone()
    if not event:
        abort(404)
    ics = _generate_ics(event, community)
    return ics, 200, {
        "Content-Type": "text/calendar; charset=utf-8",
        "Content-Disposition": f"attachment; filename={slug}-event-{eid}.ics"
    }

@app.route("/c/<slug>/events/<int:eid>/edit", methods=["GET", "POST"])
@community_member_required
def community_edit_event(slug, eid):
    db = get_db()
    community = g.community
    event = db.execute("SELECT * FROM events WHERE id = ? AND community_id = ?",
                       (eid, community["id"])).fetchone()
    is_admin = g.membership["role"] in ("owner", "admin")
    if not event or (event["user_id"] != session["user_id"] and not is_admin):
        flash("Not allowed.", "error")
        return redirect(f"/c/{slug}/events")

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        event_date = request.form.get("event_date", "").strip()
        event_time = request.form.get("event_time", "").strip()
        end_time = request.form.get("end_time", "").strip()
        location = request.form.get("location", "").strip()
        location_type = request.form.get("location_type", "in-person")
        link = request.form.get("link", "").strip()
        event_type = request.form.get("event_type", "event")
        capacity = int(request.form.get("capacity", 0) or 0)
        speakers = request.form.get("speakers", "").strip()
        recording_url = request.form.get("recording_url", "").strip()
        slides_url = request.form.get("slides_url", "").strip()
        notes = request.form.get("notes", "").strip()

        if not title or not event_date:
            flash("Title and date are required.", "error")
            return redirect(f"/c/{slug}/events/{eid}/edit")

        db.execute("""
            UPDATE events SET title=?, description=?, event_date=?, event_time=?,
                              end_time=?, location=?, location_type=?, link=?,
                              event_type=?, capacity=?, speakers=?,
                              recording_url=?, slides_url=?, notes=?
            WHERE id=?
        """, (title, description, event_date, event_time, end_time,
              location, location_type, link, event_type, capacity, speakers,
              recording_url, slides_url, notes, eid))
        db.commit()
        flash("Event updated.", "success")
        return redirect(f"/c/{slug}/events/{eid}")

    members = db.execute("""
        SELECT u.id, u.name FROM memberships m JOIN users u ON m.user_id = u.id
        WHERE m.community_id = ? ORDER BY u.name
    """, (community["id"],)).fetchall()
    return render_template("community/new_event.html", community=community,
                           membership=g.membership, event=event,
                           event_types=EVENT_TYPES, members=members)

@app.route("/c/<slug>/events/<int:eid>/delete", methods=["POST"])
@community_member_required
def community_delete_event(slug, eid):
    db = get_db()
    community = g.community
    event = db.execute("SELECT * FROM events WHERE id = ? AND community_id = ?",
                       (eid, community["id"])).fetchone()
    is_admin = g.membership["role"] in ("owner", "admin")
    if not event or (event["user_id"] != session["user_id"] and not is_admin):
        flash("Not allowed.", "error")
        return redirect(f"/c/{slug}/events")
    db.execute("DELETE FROM events WHERE id = ?", (eid,))
    db.commit()
    flash("Event deleted.", "success")
    return redirect(f"/c/{slug}/events")

@app.route("/c/<slug>/events/<int:eid>/rsvp", methods=["POST"])
@community_member_required
def community_rsvp(slug, eid):
    status = request.form.get("status", "going")
    if status not in ("going", "maybe", "not-going"):
        status = "going"
    db = get_db()
    community = g.community
    event = db.execute("SELECT * FROM events WHERE id = ? AND community_id = ?",
                       (eid, community["id"])).fetchone()
    if not event:
        return redirect(f"/c/{slug}/events")

    existing = db.execute(
        "SELECT id, waitlist FROM rsvps WHERE event_id = ? AND user_id = ?",
        (eid, session["user_id"])
    ).fetchone()
    now = datetime.utcnow().isoformat()

    if status == "not-going":
        if existing:
            was_waitlisted = existing["waitlist"]
            db.execute("DELETE FROM rsvps WHERE event_id = ? AND user_id = ?",
                       (eid, session["user_id"]))
            # Promote first waitlisted person if someone left a full event
            if not was_waitlisted and event["capacity"] > 0:
                next_up = db.execute(
                    "SELECT id, user_id FROM rsvps WHERE event_id = ? AND waitlist = 1 ORDER BY created ASC LIMIT 1",
                    (eid,)
                ).fetchone()
                if next_up:
                    db.execute("UPDATE rsvps SET waitlist = 0 WHERE id = ?", (next_up["id"],))
                    db.execute("""
                        INSERT INTO notifications (user_id, community_id, post_id, type, message, created)
                        VALUES (?, ?, NULL, ?, ?, ?)
                    """, (next_up["user_id"], community["id"], "event",
                          f'A spot opened up! You\'re now confirmed for "{event["title"]}"', now))
    elif existing:
        db.execute("UPDATE rsvps SET status = ?, waitlist = 0 WHERE event_id = ? AND user_id = ?",
                   (status, eid, session["user_id"]))
    else:
        # Check capacity
        on_waitlist = 0
        if status == "going" and event["capacity"] > 0:
            going_count = db.execute(
                "SELECT COUNT(*) FROM rsvps WHERE event_id = ? AND status = 'going' AND waitlist = 0",
                (eid,)
            ).fetchone()[0]
            if going_count >= event["capacity"]:
                on_waitlist = 1
                flash("Event is full — you've been added to the waitlist.", "success")
        db.execute("INSERT INTO rsvps (event_id, user_id, status, waitlist, created) VALUES (?, ?, ?, ?, ?)",
                   (eid, session["user_id"], status, on_waitlist, now))

    db.commit()
    return redirect(f"/c/{slug}/events/{eid}")

# ── Community: Resources ─────────────────────────────────────────

SAFE_MIMES = {
    "application/pdf", "application/msword", "application/vnd.ms-excel", "application/zip",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/plain", "text/csv",
    "image/png", "image/jpeg", "image/gif", "image/svg+xml", "image/webp",
    "video/mp4", "video/quicktime", "video/webm",
}

def _allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def _validate_upload(file_storage):
    """Check both extension and MIME type to prevent disguised uploads."""
    if not file_storage or not file_storage.filename:
        return False
    if not _allowed_file(file_storage.filename):
        return False
    # Read first 8KB to sniff content type, then reset
    header = file_storage.read(8192)
    file_storage.seek(0)
    # Block HTML/JS/PHP disguised as other files
    if b"<script" in header.lower() or b"<?php" in header.lower() or b"<html" in header.lower():
        return False
    # Check declared MIME
    if file_storage.content_type and file_storage.content_type not in SAFE_MIMES:
        # Allow if content_type is generic (application/octet-stream) — browsers sometimes misreport
        if file_storage.content_type != "application/octet-stream":
            return False
    return True

def _detect_video_embed(url):
    """Extract embed URL from YouTube, Vimeo, or Loom links."""
    if not url:
        return None
    url = url.strip()
    # YouTube
    if "youtube.com/watch" in url:
        vid = url.split("v=")[1].split("&")[0] if "v=" in url else None
        return f"https://www.youtube.com/embed/{vid}" if vid else None
    if "youtu.be/" in url:
        vid = url.split("youtu.be/")[1].split("?")[0]
        return f"https://www.youtube.com/embed/{vid}"
    # Vimeo
    if "vimeo.com/" in url:
        parts = url.rstrip("/").split("/")
        vid = parts[-1] if parts[-1].isdigit() else None
        return f"https://player.vimeo.com/video/{vid}" if vid else None
    # Loom
    if "loom.com/share/" in url:
        vid = url.split("loom.com/share/")[1].split("?")[0]
        return f"https://www.loom.com/embed/{vid}"
    return None

def _human_file_size(size_bytes):
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.0f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"

@app.route("/c/<slug>/resources")
@community_member_required
def community_resources(slug):
    db = get_db()
    community = g.community
    q = request.args.get("q", "").strip()
    rtype = request.args.get("type", "")
    tag = request.args.get("tag", "").strip()

    sql = """
        SELECT r.*, u.name AS author_name
        FROM resources r JOIN users u ON r.user_id = u.id
        WHERE r.community_id = ?
    """
    params = [community["id"]]

    if q:
        sql += " AND (r.title LIKE ? OR r.description LIKE ? OR r.tags LIKE ?)"
        params += [f"%{q}%", f"%{q}%", f"%{q}%"]
    if rtype:
        sql += " AND r.resource_type = ?"
        params.append(rtype)
    if tag:
        sql += " AND (r.tags LIKE ? OR r.tags LIKE ? OR r.tags LIKE ? OR r.tags = ?)"
        params += [f"{tag},%", f"%, {tag},%", f"%, {tag}", tag]

    sql += " ORDER BY r.created DESC"
    resources = db.execute(sql, params).fetchall()

    # Collect all unique tags for the filter sidebar
    all_tags_raw = db.execute(
        "SELECT DISTINCT tags FROM resources WHERE community_id = ? AND tags != ''",
        (community["id"],)
    ).fetchall()
    all_tags = set()
    for row in all_tags_raw:
        for t in row["tags"].split(","):
            t = t.strip()
            if t:
                all_tags.add(t)
    all_tags = sorted(all_tags)

    return render_template("community/resources.html", community=community,
                           membership=g.membership, resources=resources,
                           all_tags=all_tags, q=q, rtype=rtype, tag=tag,
                           human_file_size=_human_file_size)

@app.route("/c/<slug>/resources/new", methods=["GET", "POST"])
@community_member_required
def community_new_resource(slug):
    community = g.community
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        tags = request.form.get("tags", "").strip()
        url = request.form.get("url", "").strip()
        file = request.files.get("file")

        if not title:
            flash("Title is required.", "error")
            return redirect(f"/c/{slug}/resources/new")

        db = get_db()
        now = datetime.utcnow().isoformat()
        resource_type = "link"
        file_path = ""
        file_name = ""
        file_size = 0

        # Handle file upload
        if file and file.filename:
            if not _validate_upload(file):
                flash("File type not allowed or file contains unsafe content.", "error")
                return redirect(f"/c/{slug}/resources/new")
            file_name = secure_filename(file.filename)
            ext = file_name.rsplit(".", 1)[1].lower()
            # Store in community-specific subfolder
            community_dir = UPLOAD_DIR / str(community["id"])
            community_dir.mkdir(exist_ok=True)
            # Unique filename
            unique_name = f"{int(datetime.utcnow().timestamp())}_{file_name}"
            dest = community_dir / unique_name
            file.save(str(dest))
            file_size = dest.stat().st_size
            file_path = f"{community['id']}/{unique_name}"

            if ext in ("mp4", "mov", "webm"):
                resource_type = "video"
            elif ext == "pdf":
                resource_type = "pdf"
            elif ext in ("png", "jpg", "jpeg", "gif", "svg"):
                resource_type = "image"
            else:
                resource_type = "file"
        elif url:
            embed = _detect_video_embed(url)
            if embed:
                resource_type = "video"
            else:
                resource_type = "link"
        else:
            flash("Please upload a file or provide a URL.", "error")
            return redirect(f"/c/{slug}/resources/new")

        db.execute("""
            INSERT INTO resources (community_id, user_id, title, description,
                                   resource_type, file_path, file_name, file_size,
                                   url, tags, created)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (community["id"], session["user_id"], title, description,
              resource_type, file_path, file_name, file_size, url, tags, now))
        db.commit()
        flash("Resource added!", "success")
        return redirect(f"/c/{slug}/resources")

    return render_template("community/new_resource.html", community=community,
                           membership=g.membership, resource=None)

@app.route("/c/<slug>/resources/<int:rid>")
@community_member_required
def community_view_resource(slug, rid):
    db = get_db()
    community = g.community
    resource = db.execute("""
        SELECT r.*, u.name AS author_name
        FROM resources r JOIN users u ON r.user_id = u.id
        WHERE r.id = ? AND r.community_id = ?
    """, (rid, community["id"])).fetchone()
    if not resource:
        flash("Resource not found.", "error")
        return redirect(f"/c/{slug}/resources")

    embed_url = _detect_video_embed(resource["url"]) if resource["url"] else None

    return render_template("community/resource_detail.html", community=community,
                           membership=g.membership, resource=resource,
                           embed_url=embed_url, human_file_size=_human_file_size)

@app.route("/c/<slug>/resources/<int:rid>/delete", methods=["POST"])
@community_member_required
def community_delete_resource(slug, rid):
    db = get_db()
    community = g.community
    resource = db.execute("SELECT * FROM resources WHERE id = ? AND community_id = ?",
                          (rid, community["id"])).fetchone()
    is_admin = g.membership["role"] in ("owner", "admin")
    if not resource or (resource["user_id"] != session["user_id"] and not is_admin):
        flash("Not allowed.", "error")
        return redirect(f"/c/{slug}/resources")
    # Delete file if exists
    if resource["file_path"]:
        fpath = UPLOAD_DIR / resource["file_path"]
        if fpath.exists():
            fpath.unlink()
    db.execute("DELETE FROM resources WHERE id = ?", (rid,))
    db.commit()
    flash("Resource deleted.", "success")
    return redirect(f"/c/{slug}/resources")

@app.route("/uploads/<path:filepath>")
@login_required
def serve_upload(filepath):
    return send_from_directory(str(UPLOAD_DIR), filepath)

# ── Community: Members ───────────────────────────────────────────

@app.route("/c/<slug>/members")
@community_member_required
def community_members(slug):
    db = get_db()
    community = g.community
    q = request.args.get("q", "").strip()
    if q:
        members = db.execute("""
            SELECT u.*, m.role AS community_role, m.joined
            FROM memberships m JOIN users u ON m.user_id = u.id
            WHERE m.community_id = ? AND (u.name LIKE ? OR u.bio LIKE ?)
            ORDER BY m.role = 'owner' DESC, m.role = 'admin' DESC, u.name
        """, (community["id"], f"%{q}%", f"%{q}%")).fetchall()
    else:
        members = db.execute("""
            SELECT u.*, m.role AS community_role, m.joined
            FROM memberships m JOIN users u ON m.user_id = u.id
            WHERE m.community_id = ?
            ORDER BY m.role = 'owner' DESC, m.role = 'admin' DESC, u.name
        """, (community["id"],)).fetchall()
    return render_template("community/members.html", community=community,
                           membership=g.membership, members=members, q=q)

@app.route("/c/<slug>/members/<int:uid>")
@community_member_required
def community_profile(slug, uid):
    db = get_db()
    community = g.community
    user = get_user_by_id(uid)
    if not user:
        flash("User not found.", "error")
        return redirect(f"/c/{slug}/members")
    member = get_membership(uid, community["id"])
    if not member:
        flash("User is not a member of this community.", "error")
        return redirect(f"/c/{slug}/members")
    posts = db.execute("""
        SELECT p.*, u.name AS author_name,
               (SELECT COUNT(*) FROM comments c WHERE c.post_id = p.id) AS comment_count
        FROM posts p JOIN users u ON p.user_id = u.id
        WHERE p.user_id = ? AND p.community_id = ?
        ORDER BY p.created DESC
    """, (uid, community["id"])).fetchall()
    return render_template("community/profile.html", community=community,
                           membership=g.membership, user=user,
                           member=member, posts=posts)

# ── Community: Admin / Settings ──────────────────────────────────

@app.route("/c/<slug>/settings")
@community_admin_required
def community_settings(slug):
    db = get_db()
    community = g.community
    tab = request.args.get("tab", "overview")
    q = request.args.get("q", "").strip()

    stats = {
        "total_members": db.execute(
            "SELECT COUNT(*) FROM memberships WHERE community_id = ?", (community["id"],)
        ).fetchone()[0],
        "total_posts": db.execute(
            "SELECT COUNT(*) FROM posts WHERE community_id = ?", (community["id"],)
        ).fetchone()[0],
        "total_comments": db.execute("""
            SELECT COUNT(*) FROM comments c JOIN posts p ON c.post_id = p.id
            WHERE p.community_id = ?
        """, (community["id"],)).fetchone()[0],
        "total_events": db.execute(
            "SELECT COUNT(*) FROM events WHERE community_id = ?", (community["id"],)
        ).fetchone()[0],
        "new_members_7d": db.execute("""
            SELECT COUNT(*) FROM memberships
            WHERE community_id = ? AND joined >= datetime('now', '-7 days')
        """, (community["id"],)).fetchone()[0],
    }

    # Members with counts
    if q:
        members = db.execute("""
            SELECT u.*, m.role AS community_role, m.joined,
                   (SELECT COUNT(*) FROM posts p WHERE p.user_id = u.id AND p.community_id = ?) AS post_count,
                   (SELECT COUNT(*) FROM comments c JOIN posts p ON c.post_id = p.id
                    WHERE c.user_id = u.id AND p.community_id = ?) AS comment_count
            FROM memberships m JOIN users u ON m.user_id = u.id
            WHERE m.community_id = ? AND (u.name LIKE ? OR u.email LIKE ?)
            ORDER BY m.role = 'owner' DESC, m.role = 'admin' DESC, m.joined DESC
        """, (community["id"], community["id"], community["id"], f"%{q}%", f"%{q}%")).fetchall()
    else:
        members = db.execute("""
            SELECT u.*, m.role AS community_role, m.joined,
                   (SELECT COUNT(*) FROM posts p WHERE p.user_id = u.id AND p.community_id = ?) AS post_count,
                   (SELECT COUNT(*) FROM comments c JOIN posts p ON c.post_id = p.id
                    WHERE c.user_id = u.id AND p.community_id = ?) AS comment_count
            FROM memberships m JOIN users u ON m.user_id = u.id
            WHERE m.community_id = ?
            ORDER BY m.role = 'owner' DESC, m.role = 'admin' DESC, m.joined DESC
        """, (community["id"], community["id"], community["id"])).fetchall()

    # Activity
    activity = []
    joins = db.execute("""
        SELECT m.joined AS created, m.user_id, u.name AS user_name
        FROM memberships m JOIN users u ON m.user_id = u.id
        WHERE m.community_id = ? ORDER BY m.joined DESC LIMIT 20
    """, (community["id"],)).fetchall()
    for j in joins:
        activity.append({"type": "join", "user_id": j["user_id"],
                         "user_name": j["user_name"], "target_id": None,
                         "title": None, "created": j["created"]})

    post_events = db.execute("""
        SELECT p.id AS target_id, p.title, p.created, p.user_id, u.name AS user_name
        FROM posts p JOIN users u ON p.user_id = u.id
        WHERE p.community_id = ? ORDER BY p.created DESC LIMIT 20
    """, (community["id"],)).fetchall()
    for p in post_events:
        activity.append({"type": "post", "user_id": p["user_id"],
                         "user_name": p["user_name"], "target_id": p["target_id"],
                         "title": p["title"], "created": p["created"]})

    comment_events = db.execute("""
        SELECT c.created, c.user_id, u.name AS user_name, p.id AS target_id, p.title
        FROM comments c JOIN users u ON c.user_id = u.id
        JOIN posts p ON c.post_id = p.id
        WHERE p.community_id = ? ORDER BY c.created DESC LIMIT 20
    """, (community["id"],)).fetchall()
    for c in comment_events:
        activity.append({"type": "comment", "user_id": c["user_id"],
                         "user_name": c["user_name"], "target_id": c["target_id"],
                         "title": c["title"], "created": c["created"]})

    activity.sort(key=lambda x: x["created"], reverse=True)
    activity = activity[:50]

    return render_template("community/settings.html", community=community,
                           membership=g.membership, stats=stats, members=members,
                           activity=activity, tab=tab, q=q)

@app.route("/c/<slug>/settings/update", methods=["POST"])
@community_admin_required
def community_update_settings(slug):
    db = get_db()
    community = g.community
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    if not name:
        flash("Community name is required.", "error")
        return redirect(f"/c/{slug}/settings?tab=general")

    # Handle banner upload
    banner = request.files.get("banner")
    if banner and banner.filename:
        ext = banner.filename.rsplit(".", 1)[-1].lower()
        if ext in ("jpg", "jpeg", "png", "webp", "gif"):
            banner_dir = UPLOAD_DIR / "banners"
            banner_dir.mkdir(exist_ok=True)
            banner_name = f"banner_{community['id']}.{ext}"
            banner.save(str(banner_dir / banner_name))
            db.execute("UPDATE communities SET banner_path=? WHERE id=?",
                       (f"banners/{banner_name}", community["id"]))

    welcome_message = request.form.get("welcome_message", "").strip()
    invite_only = 1 if request.form.get("invite_only") else 0
    invite_code = request.form.get("invite_code", "").strip()
    db.execute("UPDATE communities SET name=?, description=?, welcome_message=?, invite_only=?, invite_code=? WHERE id=?",
               (name, description, welcome_message, invite_only, invite_code, community["id"]))
    db.commit()
    flash("Settings updated.", "success")
    return redirect(f"/c/{slug}/settings?tab=general")

@app.route("/c/<slug>/settings/role/<int:uid>", methods=["POST"])
@community_admin_required
def community_change_role(slug, uid):
    community = g.community
    if uid == session["user_id"]:
        flash("Cannot change your own role.", "error")
        return redirect(f"/c/{slug}/settings")
    target = get_membership(uid, community["id"])
    if not target:
        flash("User is not a member.", "error")
        return redirect(f"/c/{slug}/settings")
    if target["role"] == "owner":
        flash("Cannot change the owner's role.", "error")
        return redirect(f"/c/{slug}/settings")
    role = request.form.get("role", "member")
    if role not in ("member", "admin"):
        flash("Invalid role.", "error")
        return redirect(f"/c/{slug}/settings")
    db = get_db()
    db.execute("UPDATE memberships SET role=? WHERE user_id=? AND community_id=?",
               (role, uid, community["id"]))
    db.commit()
    flash("Role updated.", "success")
    return redirect(f"/c/{slug}/settings")

@app.route("/c/<slug>/settings/remove/<int:uid>", methods=["POST"])
@community_admin_required
def community_remove_member(slug, uid):
    community = g.community
    if uid == session["user_id"]:
        flash("Cannot remove yourself.", "error")
        return redirect(f"/c/{slug}/settings")
    target = get_membership(uid, community["id"])
    if target and target["role"] == "owner":
        flash("Cannot remove the owner.", "error")
        return redirect(f"/c/{slug}/settings")
    db = get_db()
    db.execute("DELETE FROM memberships WHERE user_id=? AND community_id=?",
               (uid, community["id"]))
    db.commit()
    flash("Member removed.", "success")
    return redirect(f"/c/{slug}/settings")

@app.route("/c/<slug>/settings/invite", methods=["POST"])
@community_admin_required
def community_invite_member(slug):
    community = g.community
    email = request.form.get("email", "").strip().lower()
    role = request.form.get("role", "member")
    if role not in ("member", "admin"):
        role = "member"
    user = get_user_by_email(email)
    if not user:
        flash("No account found with that email.", "error")
        return redirect(f"/c/{slug}/settings?tab=members")
    existing = get_membership(user["id"], community["id"])
    if existing:
        flash("Already a member.", "error")
        return redirect(f"/c/{slug}/settings?tab=members")
    db = get_db()
    db.execute(
        "INSERT INTO memberships (user_id, community_id, role, joined) VALUES (?, ?, ?, ?)",
        (user["id"], community["id"], role, datetime.utcnow().isoformat())
    )
    db.commit()
    flash(f"{user['name']} added to the community.", "success")
    return redirect(f"/c/{slug}/settings?tab=members")

# ── Community: Job Board ─────────────────────────────────────────

JOB_TYPES = ["full-time", "part-time", "contract", "freelance", "internship"]
JOB_REMOTE = ["on-site", "remote", "hybrid"]
JOB_CATEGORIES = ["Marketing", "Engineering", "Design", "Sales", "Product", "Operations", "Content", "Data", "Finance", "HR", "Other"]

@app.route("/c/<slug>/jobs")
@community_member_required
def community_jobs(slug):
    db = get_db()
    community = g.community
    if not community["channel_jobs"]:
        flash("Job board is not enabled.", "error")
        return redirect(f"/c/{slug}/")

    # Filters
    q = request.args.get("q", "").strip()
    location = request.args.get("location", "").strip()
    job_type = request.args.get("type", "")
    remote = request.args.get("remote", "")
    category = request.args.get("category", "")
    sort = request.args.get("sort", "newest")
    salary_min = request.args.get("salary_min", "")

    where = "j.community_id = ? AND j.status = 'active'"
    params = [community["id"]]

    if q:
        where += " AND (j.title LIKE ? OR j.company LIKE ? OR j.description LIKE ?)"
        params += [f"%{q}%", f"%{q}%", f"%{q}%"]
    if location:
        where += " AND j.location LIKE ?"
        params.append(f"%{location}%")
    if job_type:
        where += " AND j.job_type = ?"
        params.append(job_type)
    if remote:
        where += " AND j.remote_type = ?"
        params.append(remote)
    if category:
        where += " AND j.category = ?"
        params.append(category)
    if salary_min:
        where += " AND j.salary_max >= ?"
        params.append(int(salary_min))

    order = "j.featured DESC, j.created DESC"
    if sort == "salary":
        order = "j.featured DESC, j.salary_max DESC, j.created DESC"

    jobs = db.execute(f"""
        SELECT j.*, u.name AS poster_name
        FROM jobs j JOIN users u ON j.user_id = u.id
        WHERE {where} ORDER BY {order}
    """, params).fetchall()

    # Get unique locations and categories for filters
    locations = db.execute(
        "SELECT DISTINCT location FROM jobs WHERE community_id = ? AND status = 'active' AND location != '' ORDER BY location",
        (community["id"],)
    ).fetchall()
    categories_used = db.execute(
        "SELECT DISTINCT category FROM jobs WHERE community_id = ? AND status = 'active' AND category != '' ORDER BY category",
        (community["id"],)
    ).fetchall()

    return render_template("community/jobs.html", community=community,
                           membership=g.membership, jobs=jobs,
                           q=q, location=location, job_type=job_type,
                           remote=remote, category=category, sort=sort,
                           salary_min=salary_min,
                           locations=[r["location"] for r in locations],
                           categories_used=[r["category"] for r in categories_used],
                           job_types=JOB_TYPES, job_remotes=JOB_REMOTE,
                           job_categories=JOB_CATEGORIES)

@app.route("/c/<slug>/jobs/<int:jid>")
@community_member_required
def community_job_detail(slug, jid):
    db = get_db()
    community = g.community
    job = db.execute("""
        SELECT j.*, u.name AS poster_name
        FROM jobs j JOIN users u ON j.user_id = u.id
        WHERE j.id = ? AND j.community_id = ?
    """, (jid, community["id"])).fetchone()
    if not job:
        flash("Job not found.", "error")
        return redirect(f"/c/{slug}/jobs")
    other_jobs = db.execute("""
        SELECT id, title, company, location FROM jobs
        WHERE community_id = ? AND status = 'active' AND id != ?
        ORDER BY created DESC LIMIT 5
    """, (community["id"], jid)).fetchall()
    return render_template("community/job_detail.html", community=community,
                           membership=g.membership, job=job, other_jobs=other_jobs)

@app.route("/c/<slug>/jobs/post", methods=["GET", "POST"])
@community_admin_required
def community_post_job(slug):
    community = g.community
    if request.method == "POST":
        db = get_db()
        title = request.form.get("title", "").strip()
        company = request.form.get("company", "").strip()
        company_logo = request.form.get("company_logo", "").strip()
        company_url = request.form.get("company_url", "").strip()
        location = request.form.get("location", "").strip()
        remote_type = request.form.get("remote_type", "on-site")
        job_type = request.form.get("job_type", "full-time")
        category = request.form.get("category", "")
        salary_min = int(request.form.get("salary_min", 0) or 0)
        salary_max = int(request.form.get("salary_max", 0) or 0)
        salary_currency = request.form.get("salary_currency", "USD")
        description = request.form.get("description", "").strip()
        apply_url = request.form.get("apply_url", "").strip()
        featured = 1 if request.form.get("featured") else 0

        if not title or not company:
            flash("Title and company are required.", "error")
            return redirect(f"/c/{slug}/jobs/post")

        db.execute("""
            INSERT INTO jobs (community_id, user_id, title, company, company_logo, company_url,
                              location, remote_type, job_type, category, salary_min, salary_max,
                              salary_currency, description, apply_url, featured, created)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (community["id"], session["user_id"], title, company, company_logo, company_url,
              location, remote_type, job_type, category, salary_min, salary_max,
              salary_currency, description, apply_url, featured,
              datetime.utcnow().isoformat()))
        db.commit()
        flash("Job posted!", "success")
        return redirect(f"/c/{slug}/jobs")

    return render_template("community/job_post.html", community=community,
                           membership=g.membership, job_types=JOB_TYPES,
                           job_remotes=JOB_REMOTE, job_categories=JOB_CATEGORIES)

@app.route("/c/<slug>/jobs/<int:jid>/delete", methods=["POST"])
@community_admin_required
def community_delete_job(slug, jid):
    db = get_db()
    db.execute("DELETE FROM jobs WHERE id = ? AND community_id = ?", (jid, g.community["id"]))
    db.commit()
    flash("Job removed.", "success")
    return redirect(f"/c/{slug}/jobs")

# ── Community: Content Channels ──────────────────────────────────

CHANNELS = {
    "podcasts":   {"label": "Podcasts", "icon": "🎙️", "flag": "channel_podcasts", "desc": "Listen to episodes from the community"},
    "news":       {"label": "News", "icon": "📰", "flag": "channel_news", "desc": "Latest updates and announcements"},
    "blog":       {"label": "Blog", "icon": "✍️", "flag": "channel_blog", "desc": "Articles and long-form content"},
    "newsletter": {"label": "Newsletter", "icon": "📧", "flag": "channel_newsletter", "desc": "Regular updates delivered to you"},
    "jobs":       {"label": "Job Board", "icon": "💼", "flag": "channel_jobs", "desc": "Career opportunities from the community"},
}

@app.route("/c/<slug>/channel/<channel>")
@community_member_required
def community_channel(slug, channel):
    if channel not in CHANNELS:
        abort(404)
    community = g.community
    info = CHANNELS[channel]
    if not community[info["flag"]]:
        flash(f"{info['label']} is not enabled for this community.", "error")
        return redirect(f"/c/{slug}/")
    db = get_db()
    entries = db.execute("""
        SELECT ce.*, u.name AS author_name
        FROM content_entries ce JOIN users u ON ce.user_id = u.id
        WHERE ce.community_id = ? AND ce.channel = ? AND ce.published = 1
        ORDER BY ce.created DESC
    """, (community["id"], channel)).fetchall()
    member_count = db.execute(
        "SELECT COUNT(*) FROM memberships WHERE community_id = ?", (community["id"],)
    ).fetchone()[0]
    recent_posts = db.execute("""
        SELECT id, title, created FROM posts WHERE community_id = ?
        ORDER BY created DESC LIMIT 5
    """, (community["id"],)).fetchall()
    return render_template("community/channel.html", community=community,
                           membership=g.membership, entries=entries, channel=channel,
                           info=info, member_count=member_count, recent_posts=recent_posts)

@app.route("/c/<slug>/channel/<channel>/<int:eid>")
@community_member_required
def community_channel_entry(slug, channel, eid):
    if channel not in CHANNELS:
        abort(404)
    community = g.community
    info = CHANNELS[channel]
    db = get_db()
    entry = db.execute("""
        SELECT ce.*, u.name AS author_name
        FROM content_entries ce JOIN users u ON ce.user_id = u.id
        WHERE ce.id = ? AND ce.community_id = ? AND ce.channel = ?
    """, (eid, community["id"], channel)).fetchone()
    if not entry:
        flash("Entry not found.", "error")
        return redirect(f"/c/{slug}/channel/{channel}")
    # Detect video/audio embed
    embed_url = _detect_video_embed(entry["media_url"]) if entry["media_url"] else None
    member_count = db.execute(
        "SELECT COUNT(*) FROM memberships WHERE community_id = ?", (community["id"],)
    ).fetchone()[0]
    other_entries = db.execute("""
        SELECT id, title, created FROM content_entries
        WHERE community_id = ? AND channel = ? AND id != ? AND published = 1
        ORDER BY created DESC LIMIT 5
    """, (community["id"], channel, eid)).fetchall()
    return render_template("community/channel_entry.html", community=community,
                           membership=g.membership, entry=entry, channel=channel,
                           info=info, embed_url=embed_url, member_count=member_count,
                           other_entries=other_entries)

@app.route("/c/<slug>/channel/<channel>/new", methods=["GET", "POST"])
@community_admin_required
def community_channel_new(slug, channel):
    if channel not in CHANNELS:
        abort(404)
    community = g.community
    info = CHANNELS[channel]
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        body = request.form.get("body", "").strip()
        excerpt = request.form.get("excerpt", "").strip()
        cover_url = request.form.get("cover_url", "").strip()
        media_url = request.form.get("media_url", "").strip()
        if not title:
            flash("Title is required.", "error")
            return redirect(f"/c/{slug}/channel/{channel}/new")
        db = get_db()
        db.execute("""
            INSERT INTO content_entries (community_id, user_id, channel, title, body, excerpt,
                                         cover_url, media_url, created)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (community["id"], session["user_id"], channel, title, body, excerpt,
              cover_url, media_url, datetime.utcnow().isoformat()))
        db.commit()
        flash(f"{info['label']} entry published!", "success")
        return redirect(f"/c/{slug}/channel/{channel}")
    return render_template("community/channel_new.html", community=community,
                           membership=g.membership, channel=channel, info=info)

@app.route("/c/<slug>/channel/<channel>/<int:eid>/delete", methods=["POST"])
@community_admin_required
def community_channel_delete(slug, channel, eid):
    db = get_db()
    db.execute("DELETE FROM content_entries WHERE id = ? AND community_id = ?",
               (eid, g.community["id"]))
    db.commit()
    flash("Entry deleted.", "success")
    return redirect(f"/c/{slug}/channel/{channel}")

@app.route("/c/<slug>/settings/announcement", methods=["POST"])
@community_admin_required
def community_update_announcement(slug):
    db = get_db()
    community = g.community
    announcement = request.form.get("announcement", "").strip()
    db.execute("UPDATE communities SET announcement = ? WHERE id = ?",
               (announcement, community["id"]))
    db.commit()
    flash("Announcement updated." if announcement else "Announcement cleared.", "success")
    return redirect(f"/c/{slug}/settings?tab=general")

@app.route("/c/<slug>/settings/channels", methods=["POST"])
@community_admin_required
def community_toggle_channel(slug):
    channel = request.form.get("channel", "")
    if channel not in CHANNELS:
        flash("Invalid channel.", "error")
        return redirect(f"/c/{slug}/settings?tab=general")
    db = get_db()
    community = g.community
    flag = CHANNELS[channel]["flag"]
    new_val = 0 if community[flag] else 1
    db.execute(f"UPDATE communities SET {flag} = ? WHERE id = ?", (new_val, community["id"]))
    db.commit()
    label = CHANNELS[channel]["label"]
    flash(f"{label} {'enabled' if new_val else 'disabled'}.", "success")
    return redirect(f"/c/{slug}/settings?tab=general")

# ── Community: Insight Loop ──────────────────────────────────────

def get_user_points(user_id, community_id):
    return get_db().execute(
        "SELECT COALESCE(SUM(amount), 0) FROM points_ledger WHERE user_id = ? AND community_id = ?",
        (user_id, community_id)
    ).fetchone()[0]

@app.route("/c/<slug>/insights")
@community_member_required
def community_insights(slug):
    db = get_db()
    community = g.community
    if not community["insights_enabled"]:
        flash("Insight Loop is not enabled for this community.", "error")
        return redirect(f"/c/{slug}/")

    polls = db.execute("""
        SELECT p.*, u.name AS creator_name,
               (SELECT COUNT(DISTINCT pr.user_id) FROM poll_responses pr WHERE pr.poll_id = p.id) AS response_count,
               (SELECT COUNT(*) FROM poll_questions pq WHERE pq.poll_id = p.id) AS question_count
        FROM polls p JOIN users u ON p.user_id = u.id
        WHERE p.community_id = ? AND p.status = 'active'
        ORDER BY p.created DESC
    """, (community["id"],)).fetchall()

    # Check which polls the user has already answered
    answered_polls = set()
    for p in polls:
        answered = db.execute(
            "SELECT 1 FROM poll_responses WHERE poll_id = ? AND user_id = ? LIMIT 1",
            (p["id"], session["user_id"])
        ).fetchone()
        if answered:
            answered_polls.add(p["id"])

    my_points = get_user_points(session["user_id"], community["id"])

    rewards = db.execute(
        "SELECT * FROM rewards WHERE community_id = ? AND active = 1 ORDER BY cost ASC",
        (community["id"],)
    ).fetchall()

    # Points leaderboard top 10
    leaderboard = db.execute("""
        SELECT u.id, u.name, SUM(pl.amount) AS total_points
        FROM points_ledger pl JOIN users u ON pl.user_id = u.id
        WHERE pl.community_id = ?
        GROUP BY pl.user_id ORDER BY total_points DESC LIMIT 10
    """, (community["id"],)).fetchall()

    return render_template("community/insights.html", community=community,
                           membership=g.membership, polls=polls,
                           answered_polls=answered_polls, my_points=my_points,
                           rewards=rewards, leaderboard=leaderboard)

@app.route("/c/<slug>/insights/poll/<int:pid>")
@community_member_required
def community_poll_view(slug, pid):
    db = get_db()
    community = g.community
    poll = db.execute("SELECT p.*, u.name AS creator_name FROM polls p JOIN users u ON p.user_id = u.id WHERE p.id = ? AND p.community_id = ?",
                      (pid, community["id"])).fetchone()
    if not poll:
        flash("Poll not found.", "error")
        return redirect(f"/c/{slug}/insights")
    questions = db.execute("SELECT * FROM poll_questions WHERE poll_id = ? ORDER BY sort_order", (pid,)).fetchall()
    already_answered = db.execute("SELECT 1 FROM poll_responses WHERE poll_id = ? AND user_id = ? LIMIT 1",
                                  (pid, session["user_id"])).fetchone() is not None
    return render_template("community/poll.html", community=community,
                           membership=g.membership, poll=poll, questions=questions,
                           already_answered=already_answered)

@app.route("/c/<slug>/insights/poll/<int:pid>/submit", methods=["POST"])
@community_member_required
def community_poll_submit(slug, pid):
    db = get_db()
    community = g.community
    poll = db.execute("SELECT * FROM polls WHERE id = ? AND community_id = ? AND status = 'active'",
                      (pid, community["id"])).fetchone()
    if not poll:
        flash("Poll not found.", "error")
        return redirect(f"/c/{slug}/insights")
    # Check not already answered
    already = db.execute("SELECT 1 FROM poll_responses WHERE poll_id = ? AND user_id = ? LIMIT 1",
                         (pid, session["user_id"])).fetchone()
    if already:
        flash("You've already answered this poll.", "error")
        return redirect(f"/c/{slug}/insights")

    questions = db.execute("SELECT * FROM poll_questions WHERE poll_id = ?", (pid,)).fetchall()
    now = datetime.utcnow().isoformat()
    for q in questions:
        answer = request.form.get(f"q_{q['id']}", "").strip()
        if answer:
            db.execute("INSERT INTO poll_responses (poll_id, question_id, user_id, answer, created) VALUES (?,?,?,?,?)",
                       (pid, q["id"], session["user_id"], answer, now))

    # Award points
    db.execute("INSERT INTO points_ledger (user_id, community_id, amount, reason, ref_type, ref_id, created) VALUES (?,?,?,?,?,?,?)",
               (session["user_id"], community["id"], poll["points_reward"],
                f'Completed poll: {poll["title"]}', 'poll', pid, now))
    db.commit()
    flash(f"Thanks! You earned {poll['points_reward']} points.", "success")
    return redirect(f"/c/{slug}/insights")

@app.route("/c/<slug>/insights/poll/<int:pid>/results")
@community_member_required
def community_poll_results(slug, pid):
    db = get_db()
    community = g.community
    poll = db.execute("SELECT * FROM polls WHERE id = ? AND community_id = ?",
                      (pid, community["id"])).fetchone()
    if not poll:
        flash("Poll not found.", "error")
        return redirect(f"/c/{slug}/insights")
    questions = db.execute("SELECT * FROM poll_questions WHERE poll_id = ? ORDER BY sort_order", (pid,)).fetchall()
    results = {}
    for q in questions:
        responses = db.execute("SELECT answer, COUNT(*) AS cnt FROM poll_responses WHERE question_id = ? GROUP BY answer ORDER BY cnt DESC",
                               (q["id"],)).fetchall()
        total = sum(r["cnt"] for r in responses)
        results[q["id"]] = {"responses": responses, "total": total}
    response_count = db.execute("SELECT COUNT(DISTINCT user_id) FROM poll_responses WHERE poll_id = ?", (pid,)).fetchone()[0]
    is_admin = g.membership["role"] in ("owner", "admin")
    return render_template("community/poll_results.html", community=community,
                           membership=g.membership, poll=poll, questions=questions,
                           results=results, response_count=response_count, is_admin=is_admin)

@app.route("/c/<slug>/insights/reward/<int:rid>/claim", methods=["POST"])
@community_member_required
def community_claim_reward(slug, rid):
    db = get_db()
    community = g.community
    reward = db.execute("SELECT * FROM rewards WHERE id = ? AND community_id = ? AND active = 1",
                        (rid, community["id"])).fetchone()
    if not reward:
        flash("Reward not found.", "error")
        return redirect(f"/c/{slug}/insights")
    my_points = get_user_points(session["user_id"], community["id"])
    if my_points < reward["cost"]:
        flash("Not enough points.", "error")
        return redirect(f"/c/{slug}/insights")
    if reward["quantity"] != -1:
        claimed = db.execute("SELECT COUNT(*) FROM reward_claims WHERE reward_id = ?", (rid,)).fetchone()[0]
        if claimed >= reward["quantity"]:
            flash("Reward is sold out.", "error")
            return redirect(f"/c/{slug}/insights")
    now = datetime.utcnow().isoformat()
    db.execute("INSERT INTO reward_claims (reward_id, user_id, community_id, created) VALUES (?,?,?,?)",
               (rid, session["user_id"], community["id"], now))
    db.execute("INSERT INTO points_ledger (user_id, community_id, amount, reason, ref_type, ref_id, created) VALUES (?,?,?,?,?,?,?)",
               (session["user_id"], community["id"], -reward["cost"],
                f'Claimed reward: {reward["title"]}', 'reward', rid, now))
    db.commit()
    flash(f"Reward claimed! '{reward['title']}'", "success")
    return redirect(f"/c/{slug}/insights")

# Admin: Create poll
@app.route("/c/<slug>/insights/create", methods=["GET", "POST"])
@community_admin_required
def community_create_poll(slug):
    community = g.community
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        points = int(request.form.get("points_reward", 10))
        if not title:
            flash("Title is required.", "error")
            return redirect(f"/c/{slug}/insights/create")
        db = get_db()
        now = datetime.utcnow().isoformat()
        cursor = db.execute("INSERT INTO polls (community_id, user_id, title, description, points_reward, created) VALUES (?,?,?,?,?,?)",
                            (community["id"], session["user_id"], title, description, points, now))
        poll_id = cursor.lastrowid
        # Parse questions from form
        i = 0
        while True:
            q = request.form.get(f"question_{i}", "").strip()
            if not q:
                break
            qtype = request.form.get(f"type_{i}", "text")
            options = request.form.get(f"options_{i}", "").strip()
            db.execute("INSERT INTO poll_questions (poll_id, question, question_type, options, sort_order) VALUES (?,?,?,?,?)",
                       (poll_id, q, qtype, options, i))
            i += 1
        db.commit()
        flash("Poll created!", "success")
        return redirect(f"/c/{slug}/insights")
    return render_template("community/create_poll.html", community=community, membership=g.membership)

# Admin: Create reward
@app.route("/c/<slug>/insights/rewards/create", methods=["POST"])
@community_admin_required
def community_create_reward(slug):
    community = g.community
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    cost = int(request.form.get("cost", 50))
    quantity = int(request.form.get("quantity", -1))
    if not title:
        flash("Reward title is required.", "error")
        return redirect(f"/c/{slug}/insights")
    db = get_db()
    db.execute("INSERT INTO rewards (community_id, title, description, cost, quantity, created) VALUES (?,?,?,?,?,?)",
               (community["id"], title, description, cost, quantity, datetime.utcnow().isoformat()))
    db.commit()
    flash("Reward added!", "success")
    return redirect(f"/c/{slug}/insights")

# Admin: Toggle insights
@app.route("/c/<slug>/insights/toggle", methods=["POST"])
@community_admin_required
def community_toggle_insights(slug):
    db = get_db()
    community = g.community
    new_val = 0 if community["insights_enabled"] else 1
    db.execute("UPDATE communities SET insights_enabled = ? WHERE id = ?", (new_val, community["id"]))
    db.commit()
    flash(f"Insight Loop {'enabled' if new_val else 'disabled'}.", "success")
    return redirect(f"/c/{slug}/settings?tab=general")

# Admin: Close poll
@app.route("/c/<slug>/insights/poll/<int:pid>/close", methods=["POST"])
@community_admin_required
def community_close_poll(slug, pid):
    db = get_db()
    db.execute("UPDATE polls SET status = 'closed' WHERE id = ? AND community_id = ?", (pid, g.community["id"]))
    db.commit()
    flash("Poll closed.", "success")
    return redirect(f"/c/{slug}/insights")

# ── Community: Lander (Page Builder) ─────────────────────────────

@app.route("/c/<slug>/lander")
@community_admin_required
def community_lander(slug):
    community = g.community
    config = community["lander_config"] or "[]"
    return render_template("community/lander.html", community=community,
                           membership=g.membership, config=config)

@app.route("/c/<slug>/lander/save", methods=["POST"])
@csrf.exempt
@community_admin_required
def community_lander_save(slug):
    community = g.community
    config = request.get_json()
    if config is None:
        return {"error": "Invalid JSON"}, 400
    db = get_db()
    db.execute("UPDATE communities SET lander_config = ? WHERE id = ?",
               (json.dumps(config), community["id"]))
    db.commit()
    return {"ok": True}

@app.route("/c/<slug>/site")
def community_site(slug):
    """Public landing page — no auth required."""
    db = get_db()
    community = db.execute("SELECT * FROM communities WHERE slug = ?", (slug,)).fetchone()
    if not community:
        abort(404)
    config = json.loads(community["lander_config"] or "[]")
    member_count = db.execute(
        "SELECT COUNT(*) FROM memberships WHERE community_id = ?", (community["id"],)
    ).fetchone()[0]
    return render_template("community/site.html", community=community,
                           rows=config, member_count=member_count)

# ══════════════════════════════════════════════════════════════════
#  PLATFORM ADMIN — /admin/...
# ══════════════════════════════════════════════════════════════════

@app.route("/admin")
@platform_admin_required
def admin_dashboard():
    db = get_db()
    tab = request.args.get("tab", "communities")

    stats = {
        "total_users": db.execute("SELECT COUNT(*) FROM users").fetchone()[0],
        "total_communities": db.execute("SELECT COUNT(*) FROM communities").fetchone()[0],
        "total_posts": db.execute("SELECT COUNT(*) FROM posts").fetchone()[0],
        "total_comments": db.execute("SELECT COUNT(*) FROM comments").fetchone()[0],
        "new_users_7d": db.execute(
            "SELECT COUNT(*) FROM users WHERE created >= datetime('now', '-7 days')"
        ).fetchone()[0],
        "new_communities_7d": db.execute(
            "SELECT COUNT(*) FROM communities WHERE created >= datetime('now', '-7 days')"
        ).fetchone()[0],
    }

    communities = db.execute("""
        SELECT c.*, u.name AS owner_name,
               (SELECT COUNT(*) FROM memberships m WHERE m.community_id = c.id) AS member_count,
               (SELECT COUNT(*) FROM posts p WHERE p.community_id = c.id) AS post_count,
               (SELECT COUNT(*) FROM comments cm JOIN posts p2 ON cm.post_id = p2.id WHERE p2.community_id = c.id) AS comment_count
        FROM communities c JOIN users u ON c.owner_id = u.id
        ORDER BY c.created DESC
    """).fetchall()

    users = db.execute("""
        SELECT u.*,
               (SELECT COUNT(*) FROM communities c WHERE c.owner_id = u.id) AS communities_owned,
               (SELECT COUNT(*) FROM memberships m WHERE m.user_id = u.id) AS communities_joined
        FROM users u ORDER BY u.created DESC
    """).fetchall()

    # Platform-wide activity
    activity = []
    for u in db.execute("SELECT id AS user_id, name AS user_name, created FROM users ORDER BY created DESC LIMIT 20").fetchall():
        activity.append({"type": "signup", "user_name": u["user_name"], "user_id": u["user_id"],
                         "detail": None, "slug": None, "created": u["created"]})
    for c in db.execute("""
        SELECT c.name, c.slug, c.created, u.name AS user_name, u.id AS user_id
        FROM communities c JOIN users u ON c.owner_id = u.id ORDER BY c.created DESC LIMIT 20
    """).fetchall():
        activity.append({"type": "community", "user_name": c["user_name"], "user_id": c["user_id"],
                         "detail": c["name"], "slug": c["slug"], "created": c["created"]})
    for p in db.execute("""
        SELECT p.title, p.created, p.id, u.name AS user_name, u.id AS user_id, c.slug, c.name AS community_name
        FROM posts p JOIN users u ON p.user_id = u.id JOIN communities c ON p.community_id = c.id
        ORDER BY p.created DESC LIMIT 20
    """).fetchall():
        activity.append({"type": "post", "user_name": p["user_name"], "user_id": p["user_id"],
                         "detail": p["title"], "slug": p["slug"], "post_id": p["id"],
                         "community_name": p["community_name"], "created": p["created"]})

    activity.sort(key=lambda x: x["created"], reverse=True)
    activity = activity[:50]

    return render_template("admin.html", stats=stats, communities=communities,
                           users=users, activity=activity, tab=tab)

@app.route("/admin/community/<int:cid>")
@platform_admin_required
def admin_community_detail(cid):
    db = get_db()
    community = db.execute("SELECT * FROM communities WHERE id = ?", (cid,)).fetchone()
    if not community:
        flash("Community not found.", "error")
        return redirect("/admin")

    stats = {
        "total_members": db.execute(
            "SELECT COUNT(*) FROM memberships WHERE community_id = ?", (cid,)
        ).fetchone()[0],
        "total_posts": db.execute(
            "SELECT COUNT(*) FROM posts WHERE community_id = ?", (cid,)
        ).fetchone()[0],
        "total_comments": db.execute("""
            SELECT COUNT(*) FROM comments c JOIN posts p ON c.post_id = p.id WHERE p.community_id = ?
        """, (cid,)).fetchone()[0],
    }

    members = db.execute("""
        SELECT u.*, m.role AS community_role, m.joined,
               (SELECT COUNT(*) FROM posts p WHERE p.user_id = u.id AND p.community_id = ?) AS post_count,
               (SELECT COUNT(*) FROM comments c JOIN posts p ON c.post_id = p.id
                WHERE c.user_id = u.id AND p.community_id = ?) AS comment_count
        FROM memberships m JOIN users u ON m.user_id = u.id
        WHERE m.community_id = ?
        ORDER BY m.role = 'owner' DESC, m.role = 'admin' DESC, m.joined DESC
    """, (cid, cid, cid)).fetchall()

    recent_posts = db.execute("""
        SELECT p.*, u.name AS author_name,
               (SELECT COUNT(*) FROM comments c WHERE c.post_id = p.id) AS comment_count
        FROM posts p JOIN users u ON p.user_id = u.id
        WHERE p.community_id = ?
        ORDER BY p.created DESC LIMIT 20
    """, (cid,)).fetchall()

    return render_template("admin_community.html", community=community,
                           stats=stats, members=members, recent_posts=recent_posts)

@app.route("/admin/community/<int:cid>/delete", methods=["POST"])
@platform_admin_required
def admin_delete_community(cid):
    db = get_db()
    db.execute("DELETE FROM communities WHERE id = ?", (cid,))
    db.commit()
    flash("Community deleted.", "success")
    return redirect("/admin")

@app.route("/admin/community/<int:cid>/remove/<int:uid>", methods=["POST"])
@platform_admin_required
def admin_remove_community_member(cid, uid):
    db = get_db()
    db.execute("DELETE FROM memberships WHERE user_id=? AND community_id=?", (uid, cid))
    db.commit()
    flash("Member removed.", "success")
    return redirect(f"/admin/community/{cid}")

@app.route("/admin/community/<int:cid>/role/<int:uid>", methods=["POST"])
@platform_admin_required
def admin_change_community_role(cid, uid):
    role = request.form.get("role", "member")
    if role not in ("member", "admin"):
        role = "member"
    db = get_db()
    db.execute("UPDATE memberships SET role=? WHERE user_id=? AND community_id=?", (role, uid, cid))
    db.commit()
    flash("Role updated.", "success")
    return redirect(f"/admin/community/{cid}")

@app.route("/admin/users/<int:uid>/toggle-admin", methods=["POST"])
@platform_admin_required
def admin_toggle_platform_admin(uid):
    if uid == session["user_id"]:
        flash("Cannot change your own admin status.", "error")
        return redirect("/admin?tab=users")
    db = get_db()
    user = get_user_by_id(uid)
    if not user:
        flash("User not found.", "error")
        return redirect("/admin?tab=users")
    db.execute("UPDATE users SET is_admin = ? WHERE id = ?", (0 if user["is_admin"] else 1, uid))
    db.commit()
    flash(f"{'Removed' if user['is_admin'] else 'Granted'} platform admin for {user['name']}.", "success")
    return redirect("/admin?tab=users")

@app.route("/admin/users/<int:uid>/delete", methods=["POST"])
@platform_admin_required
def admin_delete_user(uid):
    if uid == session["user_id"]:
        flash("Cannot delete yourself.", "error")
        return redirect("/admin?tab=users")
    db = get_db()
    db.execute("DELETE FROM users WHERE id = ?", (uid,))
    db.commit()
    flash("User deleted.", "success")
    return redirect("/admin?tab=users")

@app.route("/admin/users/<int:uid>/impersonate", methods=["POST"])
@platform_admin_required
def admin_impersonate(uid):
    user = get_user_by_id(uid)
    if not user:
        flash("User not found.", "error")
        return redirect("/admin?tab=users")
    # Store real admin id so we can switch back
    session["impersonator_id"] = session["user_id"]
    session["user_id"] = user["id"]
    session["user_name"] = user["name"]
    flash(f"Now viewing as {user['name']}. Use the banner to switch back.", "success")
    return redirect("/dashboard")

@app.route("/admin/stop-impersonating", methods=["POST"])
@login_required
def admin_stop_impersonating():
    real_id = session.pop("impersonator_id", None)
    if real_id:
        user = get_user_by_id(real_id)
        if user:
            session["user_id"] = user["id"]
            session["user_name"] = user["name"]
            flash("Switched back to your admin account.", "success")
            return redirect("/admin")
    return redirect("/dashboard")

# ── Bookmarks & Notifications ────────────────────────────────────

@app.route("/c/<slug>/drafts")
@community_member_required
def community_drafts(slug):
    db = get_db()
    community = g.community
    drafts = db.execute("""
        SELECT p.*, (SELECT COUNT(*) FROM comments c WHERE c.post_id = p.id) AS comment_count
        FROM posts p WHERE p.community_id = ? AND p.user_id = ? AND p.is_draft = 1
        ORDER BY p.updated DESC
    """, (community["id"], session["user_id"])).fetchall()
    return render_template("community/drafts.html", community=community,
                           membership=g.membership, drafts=drafts)

@app.route("/c/<slug>/posts/<int:pid>/publish", methods=["POST"])
@community_member_required
def community_publish_draft(slug, pid):
    db = get_db()
    community = g.community
    post = db.execute("SELECT * FROM posts WHERE id = ? AND community_id = ? AND user_id = ?",
                      (pid, community["id"], session["user_id"])).fetchone()
    if not post or not post["is_draft"]:
        flash("Not found.", "error")
        return redirect(f"/c/{slug}/drafts")
    now = datetime.utcnow().isoformat()
    db.execute("UPDATE posts SET is_draft = 0, created = ?, updated = ? WHERE id = ?", (now, now, pid))
    db.commit()
    flash("Post published!", "success")
    return redirect(f"/c/{slug}/posts/{pid}")

@app.route("/c/<slug>/bookmarks")
@community_member_required
def community_bookmarks(slug):
    db = get_db()
    community = g.community
    posts = db.execute("""
        SELECT p.*, u.name AS author_name,
               (SELECT COUNT(*) FROM comments c WHERE c.post_id = p.id) AS comment_count,
               (SELECT COALESCE(SUM(v.value), 0) FROM votes v WHERE v.post_id = p.id) AS vote_score,
               b.created AS bookmarked_at
        FROM bookmarks b
        JOIN posts p ON b.post_id = p.id
        JOIN users u ON p.user_id = u.id
        WHERE b.user_id = ? AND p.community_id = ?
        ORDER BY b.created DESC
    """, (session["user_id"], community["id"])).fetchall()
    return render_template("community/bookmarks.html", community=community,
                           membership=g.membership, posts=posts)

@app.route("/c/<slug>/notifications")
@community_member_required
def community_notifications(slug):
    db = get_db()
    community = g.community
    notifications = db.execute("""
        SELECT n.*, p.title AS post_title
        FROM notifications n
        LEFT JOIN posts p ON n.post_id = p.id
        WHERE n.user_id = ? AND n.community_id = ?
        ORDER BY n.created DESC LIMIT 50
    """, (session["user_id"], community["id"])).fetchall()
    # Mark all as read
    db.execute(
        "UPDATE notifications SET is_read = 1 WHERE user_id = ? AND community_id = ?",
        (session["user_id"], community["id"])
    )
    db.commit()
    return render_template("community/notifications.html", community=community,
                           membership=g.membership, notifications=notifications)

# ── Screenshots ──────────────────────────────────────────────────

SCREENSHOTS_DIR = APP_DIR / "static" / "screenshots"

@app.route("/screenshots")
@login_required
def screenshots_page():
    manifest_path = SCREENSHOTS_DIR / "manifest.json"
    manifest = {}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
    return render_template("screenshots.html", manifest=manifest)

@app.route("/screenshots/capture", methods=["POST"])
@login_required
def screenshots_capture():
    script = APP_DIR / "screenshots.py"
    try:
        subprocess.Popen(
            [sys.executable, str(script)],
            cwd=str(APP_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        flash("Screenshot capture started. Refresh in ~30 seconds to see results.", "success")
    except Exception as e:
        flash(f"Failed to start capture: {e}", "error")
    return redirect("/screenshots")

# ── Legal Pages ──────────────────────────────────────────────────

@app.route("/terms")
def terms_page():
    return render_template("legal.html", title="Terms of Service",
                           content="These terms of service govern your use of the Community platform. By using this platform, you agree to these terms. This is a placeholder — full terms will be published before public launch.")

@app.route("/privacy")
def privacy_page():
    return render_template("legal.html", title="Privacy Policy",
                           content="We collect your name, email, and content you post. We do not sell your data to third parties. Cookies are used for session management only. This is a placeholder — a full privacy policy will be published before public launch.")

@app.route("/account/export")
@login_required
def account_export():
    """GDPR data export — download all your data as JSON."""
    db = get_db()
    uid = session["user_id"]
    user = get_user_by_id(uid)

    data = {
        "account": {
            "id": user["id"], "name": user["name"], "email": user["email"],
            "bio": user["bio"], "location": user["location"], "website": user["website"],
            "created": user["created"]
        },
        "posts": [dict(r) for r in db.execute(
            "SELECT id, community_id, title, body, category, created FROM posts WHERE user_id = ?", (uid,)).fetchall()],
        "comments": [dict(r) for r in db.execute(
            "SELECT id, post_id, body, parent_id, created FROM comments WHERE user_id = ?", (uid,)).fetchall()],
        "votes": [dict(r) for r in db.execute(
            "SELECT post_id, value, created FROM votes WHERE user_id = ?", (uid,)).fetchall()],
        "bookmarks": [dict(r) for r in db.execute(
            "SELECT post_id, created FROM bookmarks WHERE user_id = ?", (uid,)).fetchall()],
        "follows": [dict(r) for r in db.execute(
            "SELECT post_id, created FROM follows WHERE user_id = ?", (uid,)).fetchall()],
        "rsvps": [dict(r) for r in db.execute(
            "SELECT event_id, status, created FROM rsvps WHERE user_id = ?", (uid,)).fetchall()],
        "memberships": [dict(r) for r in db.execute(
            "SELECT community_id, role, joined FROM memberships WHERE user_id = ?", (uid,)).fetchall()],
        "poll_responses": [dict(r) for r in db.execute(
            "SELECT poll_id, question_id, answer, created FROM poll_responses WHERE user_id = ?", (uid,)).fetchall()],
        "notifications": [dict(r) for r in db.execute(
            "SELECT community_id, type, message, created FROM notifications WHERE user_id = ? ORDER BY created DESC LIMIT 100", (uid,)).fetchall()],
        "exported_at": datetime.utcnow().isoformat()
    }

    return json.dumps(data, indent=2), 200, {
        "Content-Type": "application/json",
        "Content-Disposition": f"attachment; filename=my-data-{uid}.json"
    }

@app.route("/account/delete", methods=["POST"])
@login_required
def account_delete():
    """GDPR right to deletion — delete account and all associated data."""
    uid = session["user_id"]
    db = get_db()
    db.execute("DELETE FROM users WHERE id = ?", (uid,))
    db.commit()
    session.clear()
    flash("Your account and all data have been permanently deleted.", "success")
    return redirect("/")

# ── Error Handlers ────────────────────────────────────────────────

@app.errorhandler(404)
def page_not_found(e):
    return render_template("errors/404.html"), 404

@app.errorhandler(500)
def internal_error(e):
    return render_template("errors/500.html"), 500

@app.errorhandler(429)
def rate_limited(e):
    return render_template("errors/429.html"), 429

# ── Boot ─────────────────────────────────────────────────────────

init_db()

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG", "0") == "1", host="0.0.0.0", port=DEFAULT_PORT)
