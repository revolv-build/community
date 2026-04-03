# Community

A community platform built with Flask. Discussion posts, member profiles, comments, and admin tools — all backed by SQLite.

---

## What's included

| Feature | Detail |
|---|---|
| Login / logout | Session-based, password hashed with bcrypt |
| Account settings | Name, email, password, bio, location, website |
| Role-based access | `admin` and `user` roles, decorator-protected routes |
| Admin panel | Add / delete users, assign roles |
| Discussion posts | Create, edit, delete posts with full text |
| Comments | Threaded comments on posts, delete by author or admin |
| Member directory | Searchable list of all community members |
| User profiles | Public profile pages with bio, location, website, post history |
| SQLite database | WAL mode, foreign keys, cascading deletes |
| Auto-setup | Creates a default admin account on first boot |
| Deploy | GitHub Actions → SSH → systemd |

---

## Local development

```bash
cd community
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

## Configuration

Copy `.env.example` to `.env` and update values:

```bash
cp .env.example .env
```

| Variable | Default | Description |
|---|---|---|
| `SECRET_KEY` | `change-me-in-production` | Flask session signing key |
| `PORT` | `5001` | Port to run on |
| `FLASK_DEBUG` | `0` | Set to `1` for debug mode |

---

## Project structure

```
community/
├── app.py                  # Main application
├── data/
│   └── community.db        # SQLite database (auto-created)
├── templates/
│   ├── base.html           # Base layout with nav
│   ├── login.html          # Login page
│   ├── home.html           # Feed / home page
│   ├── account.html        # Account settings
│   ├── admin.html          # Admin panel
│   ├── members.html        # Member directory
│   ├── profile.html        # User profile page
│   ├── new_post.html       # Create / edit post
│   └── post.html           # Post detail with comments
├── static/
│   └── style.css           # All styles
├── .env.example            # Environment variable template
├── requirements.txt        # Python dependencies
└── .github/workflows/
    └── deploy.yml          # GitHub Actions CI/CD
```

---

## Deploying on the Revolv droplet

The droplet is at **165.22.123.55**, running Ubuntu, managed as root.

### Step 1 — SSH into the droplet

```bash
ssh root@165.22.123.55
```

### Step 2 — Clone and install

```bash
cd /root
git clone https://github.com/revolv-build/community
cd community
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Step 3 — Configure environment

```bash
cp .env.example .env
nano .env   # Set a real SECRET_KEY
```

### Step 4 — Create a systemd service

```bash
nano /etc/systemd/system/community.service
```

```ini
[Unit]
Description=Community Flask service
After=network.target

[Service]
User=root
WorkingDirectory=/root/community
ExecStart=/root/community/venv/bin/gunicorn -w 2 -b 0.0.0.0:5001 app:app
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable community
systemctl start community
systemctl status community
```

### Step 5 — DNS and Nginx

Add an A record: `community.revolv.uk → 165.22.123.55`

```bash
nano /etc/nginx/sites-available/community
```

```nginx
server {
    listen 80;
    server_name community.revolv.uk;

    location / {
        proxy_pass http://127.0.0.1:5001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 120s;
    }
}
```

```bash
ln -s /etc/nginx/sites-available/community /etc/nginx/sites-enabled/
nginx -t
systemctl reload nginx
```

### Step 6 — HTTPS

```bash
certbot --nginx -d community.revolv.uk
```

### Step 7 — GitHub Actions

Add repo secrets: `SSH_HOST` = `165.22.123.55`, `SSH_PRIVATE_KEY` = your private key.

Update `.github/workflows/deploy.yml` service name to `community`.

---

## Useful commands

```bash
journalctl -u community -f          # Live logs
systemctl restart community          # Restart app
sqlite3 data/community.db ".tables"  # Check database
```
