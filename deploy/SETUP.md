# VPS deployment

Ubuntu 24.04 LTS, single server running Postgres, gunicorn (web), the Telegram
bot (worker), and nginx with a Let's Encrypt certificate.

Everything below runs as root over SSH unless noted. Replace `YOUR_DOMAIN`
throughout.

## 1. Server and DNS

Create an Ubuntu 24.04 server (1 GB RAM is plenty). Point an **A record** for
your domain at its IP address before starting step 6 — certbot verifies over
HTTP and needs DNS to already resolve.

## 2. Base packages

```
apt update && apt upgrade -y
apt install -y python3-venv python3-dev build-essential \
    postgresql postgresql-contrib nginx git ufw
```

## 3. Firewall

```
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw --force enable
```

Postgres is never exposed — it listens on localhost only, which is the default.

## 4. Database and app user

```
sudo -u postgres psql -c "CREATE USER portal WITH PASSWORD 'CHANGE_ME_STRONG';"
sudo -u postgres psql -c "CREATE DATABASE portal OWNER portal;"

adduser --system --group --home /srv/portal portal
```

## 5. Code and Python environment

```
git clone https://github.com/suttontrayvon-goof/personal.git /srv/portal
cd /srv/portal
python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt
```

Create `/srv/portal/.env` (systemd reads it; note **no quotes** around values):

```
DJANGO_SECRET_KEY=<generate one, see below>
DATABASE_URL=postgres://portal:CHANGE_ME_STRONG@localhost:5432/portal
DEBUG=False
ALLOWED_HOSTS=YOUR_DOMAIN
PORTAL_BASE_URL=https://YOUR_DOMAIN
TELEGRAM_BOT_TOKEN=<from BotFather>
OWNER_TELEGRAM_ID=<your numeric Telegram ID>
TIME_ZONE=America/Chicago
DEFAULT_FROM_EMAIL=no-reply@example.com
RESEND_API_KEY=
```

Generate the secret key with:

```
venv/bin/python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

Then migrate and collect static files:

```
chown -R portal:portal /srv/portal
sudo -u portal venv/bin/python manage.py migrate
sudo -u portal venv/bin/python manage.py collectstatic --noinput
```

`OWNER_TELEGRAM_ID` must be correct **before** this first migrate — that is when
the owner admin row is seeded.

## 6. Services

```
cp deploy/portal-web.service deploy/portal-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now portal-web portal-bot
systemctl status portal-web portal-bot --no-pager
```

## 7. nginx and TLS

```
cp deploy/nginx.conf /etc/nginx/sites-available/portal
sed -i "s/YOUR_DOMAIN/your-actual-domain.com/" /etc/nginx/sites-available/portal
ln -sf /etc/nginx/sites-available/portal /etc/nginx/sites-enabled/portal
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

apt install -y certbot python3-certbot-nginx
certbot --nginx -d your-actual-domain.com
```

Certbot installs a renewal timer automatically.

## 8. Superuser

```
cd /srv/portal
sudo -u portal venv/bin/python manage.py createsuperuser
```

## 9. Verify

- `https://your-domain/access/` loads the Case ID gate
- `https://your-domain/staff-admin/` accepts the superuser login
- `/menu` in Telegram responds (proves the bot worker is running)

## Operating it

```
# logs
journalctl -u portal-web -f
journalctl -u portal-bot -f

# deploy a new version
cd /srv/portal
sudo -u portal git pull
sudo -u portal venv/bin/pip install -r requirements.txt
sudo -u portal venv/bin/python manage.py migrate
sudo -u portal venv/bin/python manage.py collectstatic --noinput
systemctl restart portal-web portal-bot

# database backup
sudo -u postgres pg_dump portal | gzip > ~/portal-$(date +%F).sql.gz
```

Only one `portal-bot` may run at a time — two pollers on one Telegram token
conflict and updates go to whichever grabs them first.
