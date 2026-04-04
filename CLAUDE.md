# Project: Community Platform

A multi-tenant community platform where anyone can create, manage, and grow their own community space. Built for community owners who want Q&A discussions, events, resources, and member engagement tools — all under one roof.

**Live:** https://community.revolv.uk
**Repo:** https://github.com/revolv-build/community

---

## Core Architecture

| Layer | Technology |
|---|---|
| **Framework** | Flask 3.x (Python 3.12) |
| **Database** | SQLite with WAL mode, foreign keys, cascading deletes |
| **Server** | Gunicorn (4 workers) behind Nginx reverse proxy |
| **Hosting** | DigitalOcean droplet at 165.22.123.55 (1 vCPU, 2GB RAM, Ubuntu) |
| **SSL** | Let's Encrypt via Certbot (auto-renewing) |
| **Email** | Resend API (transactional emails, not yet activated — needs API key) |
| **Process manager** | systemd (`community.service`) |
| **Static files** | Served directly by Nginx with gzip + 7-day cache |
| **Uploads** | Stored on disk at `/root/community/uploads/`, served by Nginx |

### Single-file architecture
The entire backend is `app.py` (~2,500 lines). No blueprints, no separate models file. This is intentional for now but will need splitting as the codebase grows (see Tech Debt section).

### Multi-tenant model
- **Platform level:** User accounts, authentication, platform admin dashboard
- **Community level:** Each community at `/c/<slug>/` with isolated posts, events, resources, members
- **Membership roles:** owner > admin > member (per community)
- **Platform admin:** `is_admin` flag on users table — separate from community roles

---

## Database Schema (20 tables)

```
users           — Platform accounts (name, email, password_hash, is_admin, email_verified, bio, location, website)
communities     — Tenant spaces (name, slug, description, owner_id, banner_path, lander_config, insights_enabled)
memberships     — User ↔ Community join table (role: owner/admin/member)
posts           — Discussion posts scoped to community (title, body, category)
comments        — Threaded comments on posts (parent_id for nesting)
votes           — Post upvotes/downvotes (+1/-1)
comment_votes   — Comment upvotes/downvotes
comment_awards  — Emoji reactions on comments (fire/love/brain/clap/star/hundred)
bookmarks       — User's saved posts
follows         — Post follow for notifications
notifications   — In-app notification queue
events          — Community events (date, time, location, type, link)
rsvps           — Event RSVP (going/maybe)
resources       — File uploads and video embeds (PDF, video, link)
polls           — Multi-question insight polls
poll_questions  — Questions within a poll (text/choice/rating)
poll_responses  — Member answers
points_ledger   — Points economy transaction log
rewards         — Claimable rewards set by admin
reward_claims   — Reward redemption records
```

---

## Deployment

- **Repo:** `revolv-build/community` on GitHub, `main` branch only
- **Server:** SSH as root to 165.22.123.55
- **CI/CD:** GitHub Actions (`.github/workflows/deploy.yml`) — pushes to main trigger SSH deploy: `git pull`, `pip install -r requirements.txt`, `systemctl restart community`
- **systemd:** `/etc/systemd/system/community.service` — auto-restarts on crash
- **Nginx:** `/etc/nginx/sites-available/community` — SSL, gzip, static file serving, upload serving
- **DNS:** `community.revolv.uk` → A record → 165.22.123.55 (Cloudflare or DNS provider)
- **Logs:** `journalctl -u community -f` or `/var/log/community.log` (Gunicorn access log)

### Manual deploy
```bash
ssh root@165.22.123.55
cd /root/community
git pull origin main
pip install -r requirements.txt
systemctl restart community
```

---

## Key Decisions Log

**2026-04-03** — Chose SQLite over PostgreSQL for initial build. Simple, zero-config, good enough for early users. Will need migration to PostgreSQL when concurrent writes become an issue (~50+ active users).

**2026-04-03** — Single `app.py` monolith. Faster to build and iterate. Will refactor into blueprints when the file exceeds ~3,000 lines or when multiple developers are contributing.

**2026-04-03** — Path-based multi-tenancy (`/c/<slug>/`) over subdomain-based. Single deployment, single SSL cert, simpler DNS. Subdomain routing can be added later as a premium feature.

**2026-04-03** — Chose Resend over SendGrid/Mailgun for transactional email. Better DX, generous free tier (3,000/month), simple REST API with no SDK dependency.

**2026-04-03** — No JavaScript framework. Vanilla JS only (lightbox.js, csrf.js, inline scripts). Keeps the stack simple and the page weight low. Will reassess if we need real-time features (WebSocket).

**2026-04-03** — Native HTML5 drag-and-drop for the Lander page builder instead of a library like SortableJS. Works well enough, zero dependencies.

**2026-04-03** — Points economy in Insight Loop uses a ledger pattern (append-only `points_ledger` table) rather than a balance field on users. Provides full audit trail and prevents race conditions.

**2026-04-03** — Bot protection uses honeypot + time trap instead of CAPTCHA. No external dependencies, no UX friction, blocks ~90% of automated signups. Cloudflare Turnstile can be added later if needed.

---

## Current State

### Built and working
- Multi-tenant community platform (create, join, manage communities)
- Q&A discussion board with upvotes/downvotes, categories, search, sort (new/top/discussed)
- Lightbox post viewer with AJAX comment submission
- Nested comment threads with votes and emoji awards
- User flair system (Top 1%/5%/10%/25% Contributor)
- Events system with RSVP and calendar view
- Resource hub with file uploads and video embeds (YouTube/Vimeo/Loom)
- Member directory and public profiles
- Bookmarks, follows, notification system
- Customisable community banner
- Drag-and-drop landing page builder (Lander) with 9 row types
- Public community site at `/c/<slug>/site` (no auth required)
- Insight Loop: polls, points economy, rewards store, leaderboard
- Platform admin dashboard with community management and user impersonation
- Yellow admin bar for platform admins
- Left sidebar navigation with role-based sections
- Starter question CTA for new members
- Registration and login with bot protection (CSRF, honeypot, time trap, rate limiting)
- Security headers, session fixation prevention, file upload MIME validation
- Mobile responsive with hamburger sidebar drawer
- Custom error pages (404, 500, 429)
- Password reset flow (via Resend — needs API key to activate)
- Email verification on registration (via Resend — needs API key to activate)
- Playwright screenshot tool for multi-device previews
- Gunicorn + Nginx production setup with gzip and caching

### Not yet activated (needs config)
- **Resend email:** Set `RESEND_API_KEY` in `.env` to activate password reset and verification emails
- **Production secret key:** Set `SECRET_KEY` in `.env` (app refuses to boot with default in production mode)

### Known issues / tech debt
- `app.py` is 2,500 lines — should split into blueprints (auth, community, admin, insights)
- Some templates from the original single-tenant build are still in `templates/` (home.html, members.html, post.html, profile.html, new_post.html) — orphaned, can be deleted
- Screenshots are from the old single-tenant version — need recapturing
- SQLite will bottleneck under concurrent write load — plan PostgreSQL migration
- No automated tests — need unit + integration test suite
- No rich text editor — posts and comments are plain text only
- No avatar/profile photo uploads — using initial letter badges
- No OAuth (Google/GitHub login)
- No real-time updates (WebSocket/SSE)
- No background job queue — emails sent synchronously in request cycle
- `datetime.utcnow()` deprecation warnings — should migrate to `datetime.now(UTC)`
- The `import time as _time` inside route functions is messy — should be a top-level import

---

## Patterns & Conventions

### Route structure
- **Platform routes:** `/login`, `/register`, `/dashboard`, `/account`, `/admin`
- **Community routes:** `/c/<slug>/` prefix for all community-scoped pages
- **API/JSON routes:** `/c/<slug>/posts/<id>/json` for lightbox data
- **Admin routes:** `/admin`, `/admin/community/<id>`, `/admin/users/<id>/...`

### Auth decorators
- `@login_required` — any authenticated user
- `@platform_admin_required` — `is_admin` flag on users table
- `@community_member_required` — sets `g.community` and `g.membership`
- `@community_admin_required` — community owner or admin role

### Database access
- `get_db()` — returns SQLite connection from Flask `g` object, auto-closed on teardown
- `sqlite3.Row` row factory — access columns by name
- All queries use parameterised `?` placeholders (no SQL injection)

### Template hierarchy
- `templates/base.html` — platform-level layout (top nav)
- `templates/community/base.html` — community-level layout (sidebar + main content)
- Standalone templates for auth pages (login, register, forgot password)

### CSS
- `static/style.css` — all platform and community styles (~900 rules)
- `static/site.css` — public landing page styles
- Dark theme throughout (#0f0f0f background, #ccc text)
- No CSS framework — all custom

### JavaScript
- `static/csrf.js` — auto-injects CSRF tokens into forms and fetch calls
- `static/lightbox.js` — post lightbox with AJAX loading and form interception
- Inline JS in lander.html for drag-and-drop builder
- No build step, no bundling, no framework

### Security layers
CSRF (Flask-WTF) → Rate limiting (Flask-Limiter) → Honeypot → Time trap → Security headers → Session regeneration → Upload MIME validation

---

## Do Not Touch

- **`/etc/nginx/sites-available/community`** — SSL cert paths are managed by Certbot. Don't modify the `listen 443 ssl` block or cert paths.
- **`/etc/systemd/system/community.service`** — changing the port here requires matching change in Nginx config.
- **`data/community.db`** — live database with user data. Never delete in production. Always migrate schema with `ALTER TABLE` + `try/except` pattern (see any `ALTER TABLE` in the codebase for examples).
- **Brandkit on port 5000** — another app runs on this server at port 5000. Don't use that port.
- **The `_serializer` secret** — token signing uses `app.secret_key`. Changing the secret key invalidates all active password reset and email verification tokens.

---

## Session Protocol

At the end of every session, before finishing:
1. Update **Current State** to reflect what changed
2. Add a dated entry to **Session Log**
3. Add any new architectural decisions to **Key Decisions Log**
4. Commit the updated CLAUDE.md to GitHub with the message: `docs: session log YYYY-MM-DD`

---

## Session Log

### 2026-04-03 / 2026-04-04
**First build session — everything from zero to production.**

Built:
- Complete multi-tenant community platform from a single-file Flask starter
- Q&A board with voting, categories, search, nested comments, emoji awards
- Events, resources, member directory, profiles
- Lightbox post viewer with AJAX
- Bookmarks, follows, notifications
- Community banner and landing page builder (Lander)
- Insight Loop (polls, points, rewards, leaderboard)
- Platform admin dashboard with impersonation
- Security hardening (CSRF, rate limiting, honeypot, time trap, headers, session fixation, upload validation)
- Mobile responsive with sidebar drawer
- Error pages (404, 500, 429)
- Email system via Resend (password reset + email verification)
- Production deployment on DigitalOcean with Gunicorn + Nginx + systemd

Decisions made:
- SQLite for now, PostgreSQL later
- Single app.py monolith for speed
- Path-based multi-tenancy
- Resend for email
- No JS framework
- Ledger pattern for points economy

Seeded "The Content Cooking Club" community with 5 members, 13 posts, realistic comments, an event, polls, and rewards.

Where we left off:
- Email system built but not activated (needs Resend API key + domain verification)
- Next priorities from the audit: avatar uploads, moderation tools, Stripe billing
- Orphaned templates from single-tenant era can be cleaned up

### 2026-04-04
**QOL improvements for community owners.**

Built:
- Rich text editor (EasyMDE + Python-Markdown + bleach) with dark theme
- Pinned/sticky posts with 📌 toggle for admins, always sort to top
- Welcome message automation — configurable message sent as notification on join

Also built:
- Toggleable content channels: Blog, Podcasts, News, Newsletter with listing + detail + sidebar
- Notification bell with unread count badge in sidebar
- Relative timestamps everywhere (|timeago filter + JS timeago)
- Announcement banner system (configurable, dismissible, purple gradient)
- @mentions in comments with notifications
- Favicon (SVG) and OG meta tags on all pages
- Seeded 8 content entries across all channels

Where we left off:
- Resend API key still needs configuring for live emails
- Next QOL features to consider: email digest, custom categories management, member roles/badges, invite codes
- From the audit: avatar uploads, moderation tools, Stripe billing, PostgreSQL migration
