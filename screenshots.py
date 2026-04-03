"""
Screenshot Generator
────────────────────
Uses Playwright to capture screenshots of every page at four device sizes.
Run: python screenshots.py

Starts the Flask app on a temp port, seeds demo data, logs in,
and captures each page. Saves PNGs to static/screenshots/.
"""

import json
import os
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright
from werkzeug.security import generate_password_hash

APP_DIR = Path(__file__).parent
SCREENSHOTS_DIR = APP_DIR / "static" / "screenshots"
DB_PATH = APP_DIR / "data" / "community.db"
PORT = 5099  # temp port for screenshots

DEVICES = {
    "mobile":  {"width": 375,  "height": 812},
    "tablet":  {"width": 768,  "height": 1024},
    "laptop":  {"width": 1366, "height": 768},
    "desktop": {"width": 1920, "height": 1080},
}

# Pages to capture — (name, path, needs_auth)
PAGES = [
    ("login",          "/login",            False),
    ("register",       "/register",         False),
    ("feed",           "/",                 True),
    ("new-post",       "/posts/new",        True),
    ("post-detail",    "/posts/1",          True),
    ("members",        "/members",          True),
    ("profile",        "/members/1",        True),
    ("account",        "/account",          True),
    ("admin-members",  "/admin",            True),
    ("admin-activity", "/admin?tab=activity", True),
    ("admin-add-user", "/admin?tab=add",    True),
]


def seed_demo_data():
    """Populate the database with demo content for realistic screenshots."""
    DB_PATH.parent.mkdir(exist_ok=True)
    db = sqlite3.connect(str(DB_PATH))
    db.execute("PRAGMA foreign_keys=ON")

    # Check if demo data already seeded
    count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if count > 1:
        db.close()
        return

    now = datetime.utcnow()
    users = [
        ("Sarah Chen",     "sarah@example.com",   "user",  "Product designer based in London. Love building community tools.", "London, UK",    "https://sarah.design"),
        ("Marcus Johnson", "marcus@example.com",   "user",  "Full-stack developer, coffee enthusiast.",                         "New York, USA", "https://marcus.dev"),
        ("Priya Patel",    "priya@example.com",    "user",  "Community manager and writer. Always learning.",                   "Mumbai, India", ""),
        ("Alex Rivera",    "alex@example.com",     "admin", "DevOps engineer. Automating all the things.",                      "Berlin, DE",    "https://alexr.io"),
        ("Emma Wilson",    "emma@example.com",     "user",  "UX researcher. Fascinated by how people interact online.",         "Toronto, CA",   ""),
    ]

    for i, (name, email, role, bio, location, website) in enumerate(users):
        created = (now - timedelta(days=30 - i * 5)).isoformat()
        db.execute(
            "INSERT INTO users (name, email, password_hash, role, bio, location, website, created) VALUES (?,?,?,?,?,?,?,?)",
            (name, email, generate_password_hash("password123"), role, bio, location, website, created)
        )

    posts = [
        (2, "Welcome to our community!",
         "Hey everyone! Excited to have this space up and running. Feel free to introduce yourselves and share what you're working on.\n\nLooking forward to hearing from all of you.",
         now - timedelta(days=12)),
        (3, "Tips for effective remote collaboration",
         "I've been working remotely for 3 years now. Here are some things that have helped me stay productive:\n\n- Set clear working hours and communicate them\n- Use async communication by default\n- Schedule regular 1:1s with your team\n- Take breaks and go outside\n\nWhat are your tips?",
         now - timedelta(days=8)),
        (4, "Thoughts on the new design system?",
         "Been exploring the idea of building a shared design system across our projects. Would love to hear thoughts on:\n\n1. Which components do you reuse most?\n2. Should we go with Figma or code-first?\n3. How do we handle theming?\n\nLet's discuss!",
         now - timedelta(days=5)),
        (5, "Automating our deployment pipeline",
         "Just set up GitHub Actions for auto-deployment. Push to main and it deploys in about 90 seconds. Happy to walk anyone through the setup.",
         now - timedelta(days=3)),
        (6, "Book recommendation: Team Topologies",
         "Just finished reading Team Topologies by Matthew Skelton. Highly recommend it for anyone thinking about how to structure engineering teams. The concepts of stream-aligned teams and platform teams are really practical.",
         now - timedelta(days=1)),
    ]

    for user_id, title, body, created in posts:
        db.execute(
            "INSERT INTO posts (user_id, title, body, created, updated) VALUES (?,?,?,?,?)",
            (user_id, title, body, created.isoformat(), created.isoformat())
        )

    comments = [
        (1, 3, "Great tips! I'd add: invest in a good mic for calls.",                      now - timedelta(days=7)),
        (1, 2, "Thanks for setting this up Marcus!",                                         now - timedelta(days=11)),
        (1, 4, "Code-first for sure. Figma drifts from reality too fast.",                   now - timedelta(days=4)),
        (2, 5, "This is awesome Alex. Can you share the workflow YAML?",                     now - timedelta(days=2)),
        (3, 1, "Welcome! Happy to be here.",                                                 now - timedelta(days=10)),
        (4, 3, "Async by default is a game changer. +1 to that.",                            now - timedelta(days=6)),
        (5, 5, "90 seconds is impressive. We're still at 5 min deploys.",                    now - timedelta(days=2)),
        (5, 6, "Added to my reading list. Thanks Emma!",                                     now - timedelta(hours=12)),
        (2, 2, "One of my favorites too. The interaction modes chapter is gold.",             now - timedelta(hours=6)),
    ]

    for post_id, user_id, body, created in comments:
        db.execute(
            "INSERT INTO comments (post_id, user_id, body, created) VALUES (?,?,?,?)",
            (post_id, user_id, body, created.isoformat())
        )

    db.commit()
    db.close()
    print("Demo data seeded.")


def start_app():
    """Start the Flask app in a background thread."""
    os.environ["PORT"] = str(PORT)
    from app import app
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)


def capture_screenshots():
    """Capture all screenshots using Playwright."""
    # Clean and recreate screenshots dir
    if SCREENSHOTS_DIR.exists():
        shutil.rmtree(SCREENSHOTS_DIR)
    SCREENSHOTS_DIR.mkdir(parents=True)

    base_url = f"http://127.0.0.1:{PORT}"

    with sync_playwright() as p:
        browser = p.chromium.launch()

        for device_name, viewport in DEVICES.items():
            print(f"\n  [{device_name}] {viewport['width']}x{viewport['height']}")
            device_dir = SCREENSHOTS_DIR / device_name
            device_dir.mkdir()

            context = browser.new_context(viewport=viewport)
            page = context.new_page()

            # Capture pages that don't need auth first
            for page_name, path, needs_auth in PAGES:
                if not needs_auth:
                    url = f"{base_url}{path}"
                    page.goto(url, wait_until="networkidle")
                    page.wait_for_timeout(300)
                    filepath = device_dir / f"{page_name}.png"
                    page.screenshot(path=str(filepath), full_page=True)
                    print(f"    {page_name}")

            # Log in as admin
            page.goto(f"{base_url}/login", wait_until="networkidle")
            page.fill('input[name="email"]', "admin@example.com")
            page.fill('input[name="password"]', "changeme")
            page.click('.btn-primary')
            page.wait_for_load_state("networkidle")

            # Capture authenticated pages
            for page_name, path, needs_auth in PAGES:
                if needs_auth:
                    url = f"{base_url}{path}"
                    page.goto(url, wait_until="networkidle")
                    page.wait_for_timeout(300)
                    filepath = device_dir / f"{page_name}.png"
                    page.screenshot(path=str(filepath), full_page=True)
                    print(f"    {page_name}")

            context.close()

        browser.close()

    # Write manifest for the viewer
    manifest = {}
    for page_name, path, _ in PAGES:
        manifest[page_name] = {
            "path": path,
            "devices": {}
        }
        for device_name in DEVICES:
            img_path = f"screenshots/{device_name}/{page_name}.png"
            if (SCREENSHOTS_DIR / device_name / f"{page_name}.png").exists():
                manifest[page_name]["devices"][device_name] = img_path

    (SCREENSHOTS_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"\nManifest written. {len(manifest)} pages x {len(DEVICES)} devices.")


if __name__ == "__main__":
    print("Initializing database...")
    from app import init_db
    init_db()

    print("Seeding demo data...")
    seed_demo_data()

    print("Starting app...")
    thread = threading.Thread(target=start_app, daemon=True)
    thread.start()

    # Wait for server to be ready
    import urllib.request
    for _ in range(30):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/login")
            break
        except Exception:
            time.sleep(0.5)
    else:
        print("Server didn't start in time!")
        exit(1)

    print("Capturing screenshots...")
    capture_screenshots()
    print("\nDone!")
