"""
Flask Foundation
────────────────
A clean starting point for Flask apps with:
  - User auth (login / logout / account settings)
  - Role-based access (admin / user)
  - Admin panel (add/delete users, manage roles)
  - JSON-file persistence — no database needed for small apps
  - Auto-creates a default admin account on first boot

To add your app: build beneath the '── YOUR APP ──' section.
"""

import json
import uuid
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import (
    Flask, render_template_string, request,
    redirect, url_for, session, jsonify
)
from werkzeug.security import generate_password_hash, check_password_hash

# ── Config ────────────────────────────────────────────────────────
APP_DIR   = Path(__file__).parent
DATA_DIR  = APP_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

USERS_FILE   = DATA_DIR / "users.json"
SECRET_KEY   = "change-me-in-production"   # override with env var in prod
DEFAULT_PORT = 5001                         # change per-app to avoid clashes

app = Flask(__name__)
app.secret_key = SECRET_KEY

# ── User persistence ───────────────────────────────────────────────

def _load_users():
    if USERS_FILE.exists():
        return json.loads(USERS_FILE.read_text())
    return []

def _save_users(users):
    USERS_FILE.write_text(json.dumps(users, indent=2))

def _get_user_by_email(email):
    for u in _load_users():
        if u["email"].lower() == email.lower():
            return u
    return None

def _get_user_by_id(uid):
    for u in _load_users():
        if u["id"] == uid:
            return u
    return None

def _init_default_admin():
    """Create a default admin account if no users exist yet."""
    if not _load_users():
        _save_users([{
            "id": "1",
            "name": "Admin",
            "email": "admin@example.com",
            "password_hash": generate_password_hash("changeme"),
            "role": "admin",
            "created": datetime.utcnow().isoformat(),
        }])
        print("Default admin created — email: admin@example.com  password: changeme")

# ── Auth decorators ────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = _get_user_by_id(session.get("user_id"))
        if not user or user.get("role") != "admin":
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated

# ── Templates ──────────────────────────────────────────────────────

BASE_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0f0f0f; color: #ccc; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 14px; min-height: 100vh; }
a { color: inherit; text-decoration: none; }
input, select, textarea { background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 6px; color: #eee; font-size: 14px; padding: 10px 12px; width: 100%; outline: none; transition: border-color .15s; }
input:focus, select:focus, textarea:focus { border-color: #555; }
.btn { display: inline-flex; align-items: center; gap: 6px; padding: 9px 18px; border-radius: 6px; border: none; font-size: 13px; font-weight: 500; cursor: pointer; transition: opacity .15s; }
.btn:hover { opacity: .85; }
.btn-primary { background: #fff; color: #000; }
.btn-ghost { background: transparent; border: 1px solid #2a2a2a; color: #aaa; }
.btn-danger { background: #3a1010; border: 1px solid #5a2020; color: #e05050; }
.card { background: #141414; border: 1px solid #1e1e1e; border-radius: 10px; padding: 24px; }
.label { font-size: 11px; font-weight: 600; letter-spacing: 1.5px; text-transform: uppercase; color: #555; margin-bottom: 6px; }
.field { margin-bottom: 16px; }
.msg { padding: 10px 14px; border-radius: 6px; font-size: 13px; margin-bottom: 16px; }
.msg-ok  { background: #0d2a1a; border: 1px solid #1a5a30; color: #5adb8a; }
.msg-err { background: #2a0d0d; border: 1px solid #5a2020; color: #e05050; }
nav { background: #0a0a0a; border-bottom: 1px solid #1a1a1a; padding: 0 32px; display: flex; align-items: center; justify-content: space-between; height: 52px; }
nav .logo { font-size: 15px; font-weight: 600; color: #fff; letter-spacing: -.02em; }
nav .nav-links { display: flex; align-items: center; gap: 24px; }
nav .nav-links a { font-size: 13px; color: #666; transition: color .15s; }
nav .nav-links a:hover { color: #ccc; }
.page { max-width: 960px; margin: 0 auto; padding: 48px 24px; }
.page-narrow { max-width: 440px; margin: 0 auto; padding: 80px 24px; }
h1 { font-size: 22px; font-weight: 600; color: #fff; margin-bottom: 6px; }
h2 { font-size: 16px; font-weight: 600; color: #fff; margin-bottom: 16px; }
.sub { font-size: 13px; color: #555; margin-bottom: 32px; }
table { width: 100%; border-collapse: collapse; }
th { font-size: 10px; font-weight: 600; letter-spacing: 1.5px; text-transform: uppercase; color: #444; text-align: left; padding: 10px 12px; border-bottom: 1px solid #1a1a1a; }
td { padding: 12px; border-bottom: 1px solid #141414; font-size: 13px; color: #aaa; vertical-align: middle; }
tr:last-child td { border-bottom: none; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 20px; font-size: 10px; font-weight: 600; letter-spacing: .5px; text-transform: uppercase; }
.badge-admin { background: #1a1a2a; color: #7070e0; border: 1px solid #2a2a4a; }
.badge-user  { background: #1a1a1a; color: #555; border: 1px solid #2a2a2a; }
"""

def _nav(user):
    is_admin = user and user.get("role") == "admin"
    name = user["name"] if user else ""
    return f"""
    <nav>
      <span class="logo">◈ MyApp</span>
      <div class="nav-links">
        <a href="/">Home</a>
        {'<a href="/admin">Admin</a>' if is_admin else ''}
        <a href="/account">{ name }</a>
        <a href="/logout">Log out</a>
      </div>
    </nav>"""

# ── Auth routes ────────────────────────────────────────────────────

LOGIN_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Login</title>
<style>""" + BASE_CSS + """</style></head><body>
<div class="page-narrow">
  <div style="text-align:center;margin-bottom:32px;">
    <div style="font-size:28px;margin-bottom:8px;">◈</div>
    <h1>MyApp</h1>
    <div class="sub">Sign in to continue</div>
  </div>
  {% if err %}<div class="msg msg-err">{{ err }}</div>{% endif %}
  <div class="card">
    <form method="POST">
      <div class="field"><div class="label">Email</div><input name="email" type="email" required autofocus /></div>
      <div class="field"><div class="label">Password</div><input name="password" type="password" required /></div>
      <button class="btn btn-primary" style="width:100%;justify-content:center;">Sign in</button>
    </form>
  </div>
</div></body></html>"""

@app.route("/login", methods=["GET", "POST"])
def login_page():
    if session.get("user_id"):
        return redirect("/")
    err = None
    if request.method == "POST":
        user = _get_user_by_email(request.form.get("email", ""))
        if user and check_password_hash(user["password_hash"], request.form.get("password", "")):
            session["user_id"]   = user["id"]
            session["user_name"] = user["name"]
            session["user_role"] = user.get("role", "user")
            return redirect("/")
        err = "Invalid email or password."
    return render_template_string(LOGIN_HTML, err=err)

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

# ── Account settings ───────────────────────────────────────────────

ACCOUNT_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Account</title>
<style>""" + BASE_CSS + """</style></head><body>
{{ nav | safe }}
<div class="page" style="max-width:560px;">
  <h1>Account</h1><div class="sub">Update your details</div>
  {% if msg %}<div class="msg msg-ok">{{ msg }}</div>{% endif %}
  {% if err %}<div class="msg msg-err">{{ err }}</div>{% endif %}
  <div class="card" style="margin-bottom:16px;">
    <h2>Profile</h2>
    <form method="POST" action="/account/profile">
      <div class="field"><div class="label">Name</div><input name="name" value="{{ user.name }}" required /></div>
      <div class="field"><div class="label">Email</div><input name="email" type="email" value="{{ user.email }}" required /></div>
      <button class="btn btn-primary">Save</button>
    </form>
  </div>
  <div class="card">
    <h2>Change Password</h2>
    <form method="POST" action="/account/password">
      <div class="field"><div class="label">Current password</div><input name="current" type="password" required /></div>
      <div class="field"><div class="label">New password</div><input name="new" type="password" required /></div>
      <button class="btn btn-primary">Update password</button>
    </form>
  </div>
</div></body></html>"""

@app.route("/account")
@login_required
def account_page():
    user = _get_user_by_id(session["user_id"])
    return render_template_string(ACCOUNT_HTML, user=user, nav=_nav(user),
                                  msg=request.args.get("msg"), err=request.args.get("err"))

@app.route("/account/profile", methods=["POST"])
@login_required
def account_profile():
    users = _load_users()
    name  = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    for u in users:
        if u["id"] == session["user_id"]:
            existing = _get_user_by_email(email)
            if existing and existing["id"] != session["user_id"]:
                return redirect("/account?err=Email+already+in+use")
            u["name"] = name
            u["email"] = email
            _save_users(users)
            session["user_name"] = name
            return redirect("/account?msg=Profile+updated")
    return redirect("/account?err=User+not+found")

@app.route("/account/password", methods=["POST"])
@login_required
def account_password():
    users = _load_users()
    for u in users:
        if u["id"] == session["user_id"]:
            if not check_password_hash(u["password_hash"], request.form.get("current", "")):
                return redirect("/account?err=Current+password+incorrect")
            new_pw = request.form.get("new", "")
            if len(new_pw) < 8:
                return redirect("/account?err=Password+must+be+at+least+8+characters")
            u["password_hash"] = generate_password_hash(new_pw)
            _save_users(users)
            return redirect("/account?msg=Password+updated")
    return redirect("/account?err=User+not+found")

# ── Admin panel ────────────────────────────────────────────────────

ADMIN_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Admin</title>
<style>""" + BASE_CSS + """</style></head><body>
{{ nav | safe }}
<div class="page">
  <h1>Admin Panel</h1><div class="sub">Manage users</div>
  {% if msg %}<div class="msg msg-ok">{{ msg }}</div>{% endif %}
  {% if err %}<div class="msg msg-err">{{ err }}</div>{% endif %}

  <div class="card" style="margin-bottom:24px;">
    <h2>Add User</h2>
    <form method="POST" action="/admin/users/add" style="display:grid;grid-template-columns:1fr 1fr 1fr auto auto;gap:10px;align-items:end;">
      <div><div class="label">Name</div><input name="name" placeholder="Full name" required /></div>
      <div><div class="label">Email</div><input name="email" type="email" placeholder="email@example.com" required /></div>
      <div><div class="label">Password</div><input name="password" type="password" placeholder="Min 8 chars" required /></div>
      <div><div class="label">Role</div>
        <select name="role">
          <option value="user">User</option>
          <option value="admin">Admin</option>
        </select>
      </div>
      <div style="padding-top:18px;"><button class="btn btn-primary">Add</button></div>
    </form>
  </div>

  <div class="card">
    <h2>Users ({{ users | length }})</h2>
    <table>
      <tr><th>Name</th><th>Email</th><th>Role</th><th>Created</th><th></th></tr>
      {% for u in users %}
      <tr>
        <td style="color:#eee;">{{ u.name }}</td>
        <td>{{ u.email }}</td>
        <td><span class="badge badge-{{ u.role }}">{{ u.role }}</span></td>
        <td>{{ u.get('created','—')[:10] }}</td>
        <td style="text-align:right;">
          {% if u.id != current_user.id %}
          <form method="POST" action="/admin/users/delete/{{ u.id }}" style="display:inline;" onsubmit="return confirm('Delete {{ u.name }}?')">
            <button class="btn btn-danger" style="padding:6px 12px;font-size:12px;">Delete</button>
          </form>
          {% endif %}
        </td>
      </tr>
      {% endfor %}
    </table>
  </div>
</div></body></html>"""

@app.route("/admin")
@admin_required
def admin_panel():
    users = _load_users()
    current_user = _get_user_by_id(session["user_id"])
    return render_template_string(ADMIN_HTML, users=users, current_user=current_user,
                                  nav=_nav(current_user),
                                  msg=request.args.get("msg"), err=request.args.get("err"))

@app.route("/admin/users/add", methods=["POST"])
@admin_required
def admin_add_user():
    name     = request.form.get("name", "").strip()
    email    = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    role     = request.form.get("role", "user")
    if not name or not email or not password:
        return redirect("/admin?err=All+fields+required")
    if len(password) < 8:
        return redirect("/admin?err=Password+must+be+at+least+8+characters")
    if _get_user_by_email(email):
        return redirect("/admin?err=Email+already+exists")
    users = _load_users()
    new_id = str(max((int(u["id"]) for u in users if u["id"].isdigit()), default=0) + 1)
    users.append({
        "id": new_id,
        "name": name,
        "email": email,
        "password_hash": generate_password_hash(password),
        "role": role,
        "created": datetime.utcnow().isoformat(),
    })
    _save_users(users)
    return redirect(f"/admin?msg=User+{name}+added")

@app.route("/admin/users/delete/<uid>", methods=["POST"])
@admin_required
def admin_delete_user(uid):
    if uid == session["user_id"]:
        return redirect("/admin?err=Cannot+delete+yourself")
    users = [u for u in _load_users() if u["id"] != uid]
    _save_users(users)
    return redirect("/admin?msg=User+deleted")

# ══════════════════════════════════════════════════════════════════
#  ── YOUR APP ──
#  Build your application routes and logic below this line.
#  The user is available via: _get_user_by_id(session["user_id"])
#  Protect routes with @login_required or @admin_required.
# ══════════════════════════════════════════════════════════════════

HOME_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Home</title>
<style>""" + BASE_CSS + """</style></head><body>
{{ nav | safe }}
<div class="page">
  <h1>Welcome, {{ user.name }}</h1>
  <div class="sub">Your app starts here.</div>
  <div class="card">
    <div style="color:#444;font-size:13px;">Build your app beneath the <code style="color:#888;">── YOUR APP ──</code> section in app.py.</div>
  </div>
</div></body></html>"""

@app.route("/")
@login_required
def home():
    user = _get_user_by_id(session["user_id"])
    return render_template_string(HOME_HTML, user=user, nav=_nav(user))

# ── Boot ───────────────────────────────────────────────────────────

_init_default_admin()

if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=DEFAULT_PORT)
