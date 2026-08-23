# Internal Credits Portal

Admin-only Telegram bot + read-only web portal sharing one PostgreSQL database.
Credits are internal points — no real money, no deposits/withdrawals.

## Local setup

1. **Python env**

   ```
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Environment variables**

   ```
   cp .env.example .env
   ```

   Fill in `.env`. `DJANGO_SECRET_KEY` should be unique per environment — generate one with:

   ```
   python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
   ```

3. **Postgres (Docker)**

   ```
   docker compose up -d
   ```

   This starts Postgres on `localhost:5432` with the credentials already in `.env.example`
   (`credits`/`credits`, db `credits`).

   > If you have another local Postgres running (Homebrew, an installer package, etc.) it
   > may already be listening on port 5432 and will silently race the container for
   > connections — symptoms look like intermittent "role does not exist" errors rather
   > than a clean port-in-use failure. Check with `lsof -iTCP:5432 -sTCP:LISTEN` and stop
   > anything that isn't the Docker proxy (`brew services stop postgresql@14`, etc.), or
   > remap the container's host port in `docker-compose.yml` and update `DATABASE_URL` to
   > match.

4. **Migrate**

   ```
   python manage.py migrate
   ```

   The `accounts.0002_seed_owner_admin` migration reads `OWNER_TELEGRAM_ID` from the
   environment and seeds that Telegram ID as the first `Admin` with role `owner`. If the
   variable isn't set when `migrate` runs, seeding is skipped (safe for CI/test runs) —
   set it and re-run `migrate` (or `python manage.py migrate accounts 0002`) to seed later.

5. **Run the dev server**

   ```
   python manage.py runserver
   ```

## Project structure

```
config/     Django settings, urls, wsgi
accounts/   models (User, Admin, LedgerEntry, InviteToken), portal views, templates
bot/        Telegram bot handlers, management command to run the bot
templates/  shared templates
static/     static assets
```

## Data model

- **Balance is never stored.** It is always computed as `SUM(LedgerEntry.amount)` for a
  user. Every balance change is a new, immutable `LedgerEntry` row — there is no update
  path for balances anywhere in the system.
- `LedgerEntry.amount` cannot be `0` (enforced by a DB check constraint) — every entry is
  a genuine credit or debit.
- `User.username` must match `^[a-z0-9_]{3,32}$`; both `username` and `email` are
  lowercased on save, so uniqueness is effectively case-insensitive.
- Portal login is by **email**, not username (`USERNAME_FIELD = "email"` on the custom
  user model). Django admin login (owner only, see below) uses the same field.
- `User.account_id` gates access to `/login/` (see Portal pages below); `User.account_number`
  is a separate, purely cosmetic bank-statement-style identifier shown on `/accounts/` —
  the two are unrelated and neither is derived from the other.
- `User.first_name`/`last_name` are AbstractUser's built-in fields, set at creation time
  via the bot's `/create <username> <email> <first_name> <last_name>` (single-token names
  only, no spaces — same constraint as `username`).

## Running the bot

```
python manage.py runbot
```

Long-polling, no HTTP port opened. Updates are processed one at a time (no
`concurrent_updates`), which serializes ledger writes without extra locking.

## Bot safety mechanisms

- **Authorization**: every command and button press first checks the sender's Telegram
  ID against the `Admin` table (`bot/handlers.py`: `command_auth_gate` /
  `callback_auth_gate`, registered in handler group `-1` so they run before anything
  else). Unknown senders get "Not authorized." and nothing further runs.
- **Idempotency**: every write command is wrapped in `bot.idempotency.process_once`,
  which inserts a `ProcessedUpdate(update_id=...)` row in the same transaction as the
  effect it guards. A redelivered update either finds that row already committed (skips
  silently, no double-write) or finds nothing because the prior attempt failed and rolled
  back too (safe to retry).
- **Large-amount confirmation**: any `/credit` or `/debit` ≥ 10,000, or any `/debit` that
  would take a balance negative, requires an inline Confirm/Cancel step
  (`bot/confirmations.py`). Pending confirmations live in an in-memory dict (fine for a
  single worker process), expire after 60 seconds, and can only be confirmed by the same
  admin who issued the original command.
- **Logging**: every authorized command is logged on dispatch, plus a second line for
  mutating commands' outcome (success detail or rejection reason). Unauthorized attempts,
  owner-only violations, and wrong-admin confirmation attempts log at `WARNING`.

## Portal pages

- `/access/` — Account ID gate. Must be passed (once per session) before `/login/` is
  reachable. Every `User` gets a unique `account_id` (format `XXXXX-XXXXX`, generated in
  `accounts/models.py::generate_account_id`) at creation time; the bot surfaces it in the
  `/create` and `/resendinvite` replies, and it's included in the invite email, since the
  user needs it every time they log in, not just once. Session flag is cleared on logout.
- `/invite/<token>/` — set password, single-use, 72h expiry, auto-login on success
  (bypasses the Account ID gate — it's already a secure single-use token)
- `/login/` — email + password, rate-limited via django-axes (5 failures/hour lockout)
- `/` — dashboard (login required): balance, paginated ledger history (20/page)
- `/profile/` — first/last name, username, email, member-since, status. Read-only.
- `/accounts/` — account number (cosmetic, bank-statement-style, distinct from the
  Account ID gate code), Account ID, balance, opened date. Read-only.
- `/transactions/` — full paginated ledger history, same view as the dashboard's activity
  feed, its own page.
- `/messages/` — static empty state ("You have no messages"). No messaging feature
  exists; this is here so the nav item goes somewhere rather than being disabled.
- `/transfers/`, `/deposits/` — banking-style request forms backed by a real review
  queue (`ServiceRequest` model), NOT by any ledger write path. The transfer form takes a
  routing number with live bank detection: ABA checksum validation plus a small
  server-side directory of well-known bank routing numbers
  (`accounts/routing_banks.py`), queried as-you-type via `/api/routing-lookup/`; the
  detected bank is re-derived server-side on submit and stored on the request. Submitting
  validates the input (transfer amounts are checked against the available balance,
  routing numbers against the ABA checksum), stores a request
  row with a reference code (`TR-XXXXXX` / `DP-XXXXXX`), Telegram-notifies the admin who
  created the user (owner(s) as fallback), and redirects to `/requests/<ref>/` — a status
  page showing "pending admin approval" with a submitted → review → decision timeline.
  Admins act on the queue from the bot: `/requests` lists, `/approve <ref>` /
  `/decline <ref>` record a decision (idempotency-guarded). **Approving a transfer
  writes the debit automatically**: `bot/handlers.py::_decide_request_core` creates the
  negative `LedgerEntry` in the same transaction as the status change, so a request can
  never read "approved" without its matching ledger row. The sender row is taken with
  `select_for_update()` and the balance re-checked at approval time — if it no longer
  covers the amount the approval is refused outright (status stays pending, nothing is
  written) and the admin is told to credit the account or decline. Deposit approvals stay
  a status change only; adding funds is still a deliberate `/credit`. Declines never
  touch the ledger. Both form pages list the user's recent requests with live status
  chips.
- `/requests/<ref>/` — status page for one request (owner-scoped: other users' references
  404).
- `/logout/`
- `/password-reset/`, `/password-reset/done/`, `/reset/<uidb64>/<token>/`,
  `/reset/done/` — Django's built-in password reset flow, emails routed through Resend
  via a custom `EMAIL_BACKEND` (`accounts/email_backend.py`) so both this and the bot's
  invite emails share one send path
- Sidebar nav (shown only when logged in): every item is a real, reachable page now (see
  above). Icons + a blue accent color + active-state highlighting throughout, styled to
  read as a modern banking app for demo purposes. Per the owner's direction (Aug 2026),
  the portal now displays amounts as USD ("$1,234.00", via the `usd` template filter in
  `accounts/templatetags/money.py`) and the user-facing brand is "Member Portal" — this
  is presentation only, for investor demos with sample data: the ledger remains plain
  integer units and no real money exists anywhere in the system.
- Tailwind via the CDN script (`https://cdn.tailwindcss.com`) in `templates/base.html` —
  no Node/build step. Fine for an internal tool; revisit if this ever needs to scale up.

## Django admin (`/staff-admin/`)

Three permission tiers, all enforced at the permission layer (a crafted direct POST to a
blocked view 403s or gets redirected before it ever reaches the object — not just hidden
buttons):

- **`LedgerEntry`** — read-only for everyone, including the owner/superuser. No carve-out.
  Balance changes only ever happen through the bot (confirmation step for large amounts,
  `authorized_by` tied to a real Telegram admin, full logging, idempotency-safe). This is
  the one guarantee that doesn't bend regardless of who's asking.
- **`Admin`** (the bot's own authorization table) — superuser only. Regular admins never
  see or manage other admins here, mirroring `/addadmin`/`/removeadmin` already being
  owner-only bot commands. The owner row's `role` field is locked read-only and it can't
  be deleted via admin, mirroring the bot's own "can't remove the owner" protection.
- **`User`** — superusers see and edit every user. A staff account linked to a bot `Admin`
  record (via the new `Admin.portal_login` field) is scoped to only the users *that
  admin* created via `/create` (tracked by the new `User.created_by` field) —
  `accounts/admin.py::PortalUserAdmin.get_queryset` filters by it, and
  `has_view_permission`/`has_change_permission` re-check it per-object so a crafted URL
  to someone else's user just isn't found. Nobody can add or delete `User` rows through
  admin — creation stays the bot's job (that's what wires up the invite token and email),
  and deletion stays unavailable everywhere to preserve ledger history (deactivate
  instead, same as the bot's `/deactivate`).

**Setting up a paying admin's login**: they need to already exist as a bot `Admin` (via
`/addadmin`), then:

```
python manage.py create_staff_admin <telegram_id> <email> [--username <name>]
```

This creates their Django staff account (`is_staff=True`, `is_superuser=False`), links it
to their `Admin` record, and prints a one-time password for you to hand them directly —
it isn't stored or emailed anywhere. From then on, whatever users they create by running
`/create` through the bot are the only ones they'll see in `/staff-admin/`.

There's one superuser, created at setup time with username `owner` (see step 6 of the
Railway deployment section below). This account is a separate identity from bot-side
`Admin(telegram_id=...)` authorization by design — Django admin login and bot
authorization don't cross-reference each other except through the explicit `portal_login`
link described above.

## Deploying to Railway

Three services, one Postgres, one repo. Uses the `Procfile` (`web` / `worker`) with
Railway's Nixpacks builder — no Dockerfile needed. Static files (mainly Django admin's
own CSS/JS) are served by WhiteNoise directly from gunicorn; there's no separate static
host.

1. **Push this repo to GitHub** (Railway deploys from a GitHub repo or via `railway up`
   from the CLI).

2. **Create a Railway project**, then add a **Postgres** database from the Railway
   dashboard (New → Database → PostgreSQL). Railway manages its `DATABASE_URL`
   automatically.

3. **Add two services from this same repo** — one named `web`, one named `worker`. For
   each, go to Settings → Deploy and set a **Custom Start Command** matching the
   corresponding `Procfile` line exactly (don't rely on Railway auto-detecting which
   Procfile process to run — setting it explicitly avoids any ambiguity):
   - `web`: `python manage.py migrate --noinput && python manage.py collectstatic --noinput && gunicorn config.wsgi:application --bind 0.0.0.0:$PORT --workers 2`
   - `worker`: `python manage.py migrate --noinput && python manage.py runbot`

   Both run `migrate` on startup — it's idempotent, so this is just belt-and-suspenders
   in case the two services start close enough together that one might otherwise race
   ahead of a not-yet-migrated schema.

4. **Environment variables** — set these on **both** `web` and `worker` (Railway lets you
   reference the Postgres service's connection string as `${{Postgres.DATABASE_URL}}`
   instead of copy-pasting it):

   ```
   DJANGO_SECRET_KEY=<generate one, see Local setup step 2>
   DATABASE_URL=${{Postgres.DATABASE_URL}}
   DEBUG=False
   ALLOWED_HOSTS=<your-app>.up.railway.app
   TELEGRAM_BOT_TOKEN=<from BotFather>
   OWNER_TELEGRAM_ID=<your numeric Telegram ID>
   RESEND_API_KEY=<from Resend>
   DEFAULT_FROM_EMAIL=<a verified Resend sender address>
   PORTAL_BASE_URL=https://<your-app>.up.railway.app
   ```

   `OWNER_TELEGRAM_ID` must be set correctly *before* the first deploy — that's what
   `accounts.0002_seed_owner_admin` reads when `migrate` runs to seed the owner admin.

5. **Only generate a public domain for `web`** (Settings → Networking → Generate
   Domain). Leave `worker` with no domain — it never binds to an HTTP port
   (`run_polling`, long-polling out to Telegram), so there's nothing to expose. Once you
   have the `web` domain, go back and fix `ALLOWED_HOSTS` / `PORTAL_BASE_URL` above to
   match it exactly, then redeploy.

6. **Create your Django admin superuser** against the deployed database. With the
   [Railway CLI](https://docs.railway.app/guides/cli) linked to this project:

   ```
   railway run python manage.py shell -c "
   from accounts.models import User
   User.objects.create_superuser(email='you@example.com', username='owner', password='change-this')
   "
   ```

   (`railway run` executes locally but with the linked service's environment variables
   injected, so this reaches the real production database — do it deliberately, once.)

7. **Confirm**: message your bot's `/help` on Telegram, and visit
   `https://<your-app>.up.railway.app/staff-admin/`.

## Status

Phase 1 complete: project scaffold, settings, models, migrations, owner-admin seeding.
Phase 2 complete: all bot commands (`/create`, `/credit`, `/debit`, `/balance`,
`/history`, `/rename`, `/deactivate`, `/reactivate`, `/resendinvite`, `/addadmin`,
`/removeadmin`, `/audit`, `/help`), auth gate, idempotency, and the large-amount
confirmation flow — all manually verified against a live Telegram bot.
Phase 3 complete: invite acceptance, login/logout, dashboard with pagination, password
reset, an Account ID gate in front of login, and a sidebar nav — all manually verified
end-to-end (Django test client + a real browser/Telegram session). Deactivated-user
login rejection and axes lockout both confirmed working.
Phase 4 complete: Django admin at `/staff-admin/` with tiered permissions (superuser sees
everything but `LedgerEntry`, which stays read-only for everyone; scoped admins see only
users they created) — verified that permission boundaries hold against crafted requests,
not just hidden UI.
Phase 5 complete: Railway deployment config (`Procfile`, WhiteNoise static serving,
CSRF_TRUSTED_ORIGINS for the reverse-proxy setup) — verified locally with `DEBUG=False`
and a real gunicorn process (migrate → collectstatic → serve, admin static assets
confirmed loading through WhiteNoise). Not yet deployed to an actual Railway project.

Post-Phase-5, for investor-demo purposes: full modern-fintech visual pass (icons, blue
accent, avatar badge) across every page, plus first/last name and a cosmetic account
number on `User`, and all six sidebar items are now real pages (Profile, Accounts,
Transactions, Messages, Transfers, External deposits) instead of disabled placeholders.
Transfers/deposits render real forms but their `POST` handlers are intentionally inert —
verified via test client that submitting either never creates a `LedgerEntry` or mutates
any `User` row, no matter what's submitted.

Not yet built: automated test suite. See the project brief for the full build order.

Note: neither invite emails nor password-reset emails are actually deliverable yet —
`RESEND_API_KEY` isn't configured. The user/token/reset-link side of both flows works
regardless (confirmed above); only the outbound send is blocked on that key.
