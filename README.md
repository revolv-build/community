# Flask Foundation

A minimal Flask starter with user auth, role-based access, and an admin panel. No database — users are stored in `data/users.json`. Designed to be cloned as the base for any new internal tool.

---

## What's included

| Feature | Detail |
|---|---|
| Login / logout | Session-based, password hashed with bcrypt |
| Account settings | Name, email, password change |
| Role-based access | `admin` and `user` roles, decorator-protected routes |
| Admin panel | Add / delete users, assign roles |
| Auto-setup | Creates a default admin account on first boot |
| Deploy | GitHub Actions → SSH → systemd |

---

## Local development

```bash
cd flask-foundation
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

Visit `http://localhost:5001`

Default credentials (change immediately):
- Email: `admin@example.com`
- Password: `changeme`

---

## Deploying a new app on the Revolv droplet

The droplet is at **165.22.123.55**, running Ubuntu, managed as root. Brandkit already runs there on port 5000 as a systemd service. Each new app runs on its own port, and Nginx routes traffic by subdomain.

### Step 1 — SSH into the droplet

```bash
ssh root@165.22.123.55
```

If using a key file:

```bash
ssh -i ~/.ssh/your_key root@165.22.123.55
```

---

### Step 2 — Clone your new app onto the server

```bash
cd /root
git clone https://github.com/revolv-build/YOUR-REPO-NAME myapp
cd myapp
```

---

### Step 3 — Install dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

### Step 4 — Create a systemd service

This keeps the app running and restarts it automatically on crash or reboot.

```bash
nano /etc/systemd/system/myapp.service
```

Paste this (adjust `myapp`, port, and paths as needed):

```ini
[Unit]
Description=MyApp Flask service
After=network.target

[Service]
User=root
WorkingDirectory=/root/myapp
ExecStart=/root/myapp/venv/bin/gunicorn -w 2 -b 0.0.0.0:5001 app:app
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

Enable and start it:

```bash
systemctl daemon-reload
systemctl enable myapp
systemctl start myapp
systemctl status myapp   # should show "active (running)"
```

> **Port convention:** Brandkit = 5000. Add new apps incrementally: 5001, 5002, etc.

---

### Step 5 — Point a subdomain at the droplet

In your DNS provider (e.g. Cloudflare), add an **A record**:

```
myapp.revolv.uk  →  165.22.123.55
```

---

### Step 6 — Configure Nginx as a reverse proxy

If Nginx isn't installed yet:

```bash
apt install nginx -y
```

Create a config file for your app:

```bash
nano /etc/nginx/sites-available/myapp
```

Paste:

```nginx
server {
    listen 80;
    server_name myapp.revolv.uk;

    location / {
        proxy_pass http://127.0.0.1:5001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 120s;
    }
}
```

Enable it:

```bash
ln -s /etc/nginx/sites-available/myapp /etc/nginx/sites-enabled/
nginx -t           # test config — should say "ok"
systemctl reload nginx
```

> **For brandkit too:** Once Nginx is in place, create an equivalent config for `brandkit.revolv.uk → 127.0.0.1:5000` so everything routes consistently through Nginx.

---

### Step 7 — Add HTTPS (free, automatic)

```bash
apt install certbot python3-certbot-nginx -y
certbot --nginx -d myapp.revolv.uk
```

Certbot will handle renewal automatically.

---

### Step 8 — Set up GitHub Actions for auto-deploy

In your GitHub repo, go to **Settings → Secrets and variables → Actions** and add:

| Secret name | Value |
|---|---|
| `SSH_HOST` | `165.22.123.55` |
| `SSH_PRIVATE_KEY` | Contents of your private SSH key (`~/.ssh/id_rsa` or equivalent) |

The `.github/workflows/deploy.yml` file in this repo will then auto-deploy on every push to `main`.

Update the service name in `deploy.yml` to match what you used in Step 4:

```yaml
systemctl restart myapp   # ← change "myapp" to your service name
```

---

### Step 9 — First-time app setup

Once deployed, visit your subdomain and log in with:

- Email: `admin@example.com`
- Password: `changeme`

**Immediately** go to Account → Change Password.

---

## Checklist for each new app

- [ ] Clone repo to `/root/your-app-name` on the droplet
- [ ] Create virtualenv and install requirements
- [ ] Pick a unique port (5001, 5002, …)
- [ ] Create and start a systemd service
- [ ] Add DNS A record for subdomain
- [ ] Add Nginx site config and reload
- [ ] Run Certbot for HTTPS
- [ ] Add `SSH_HOST` and `SSH_PRIVATE_KEY` secrets to GitHub repo
- [ ] Update `deploy.yml` with correct service name
- [ ] Change default admin password after first login

---

## Building your app

Open `app.py` and find the section marked:

```python
# ══════════════════════════════════════════════════════════════════
#  ── YOUR APP ──
```

Add your routes and logic below that line. The authenticated user is always available with:

```python
user = _get_user_by_id(session["user_id"])
```

Protect routes with:

```python
@app.route("/my-route")
@login_required       # any logged-in user
def my_route():
    ...

@app.route("/admin-only")
@admin_required       # admin role only
def admin_only():
    ...
```

---

## Useful commands on the droplet

```bash
# View live logs for your app
journalctl -u myapp -f

# Restart after a manual code change
systemctl restart myapp

# Check all running services
systemctl list-units --type=service --state=running

# Check which ports are in use
ss -tlnp | grep LISTEN

# Reload Nginx after config changes
systemctl reload nginx
```
