# VPS Deployment Guide

## Prerequisites
- Python 3.12+, git, nginx, postgresql, redis-server installed
- A Helius API key (free tier)
- An OpenRouter API key (free tier)
- A Telegram bot token from @BotFather
- Your Telegram user ID

---

## 1. Clone the repo
```bash
git clone <your-repo-url> /home/deen/walletbot
cd /home/deen/walletbot
```

## 2. Set up environment
```bash
cp .env.example .env
nano .env        # fill in all values including OPENROUTER_API_KEY
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install gunicorn   # add gunicorn for prod
```

## 3. Create Helius webhook (one-time)
```bash
.venv/bin/python - <<'EOF'
import django, os
os.environ["DJANGO_SETTINGS_MODULE"] = "walletbot.settings"
django.setup()
from tracker.helius import create_webhook
from django.conf import settings
wid = create_webhook(f"{settings.WEBHOOK_BASE_URL}/webhook/helius/")
print(f"HELIUS_WEBHOOK_ID={wid}")
EOF
```
Copy the printed webhook ID into your `.env` as `HELIUS_WEBHOOK_ID`.

## 4. Database setup
```bash
# Create the database
sudo -u postgres psql -c "CREATE USER walletbot WITH PASSWORD 'walletbot';"
sudo -u postgres psql -c "CREATE DATABASE walletbot OWNER walletbot;"

# Run migrations
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createsuperuser   # optional, for /admin
```

## 5. Install systemd services
```bash
sudo cp deploy/walletbot.service /etc/systemd/system/
sudo cp deploy/celery.service    /etc/systemd/system/
sudo cp deploy/bot.service       /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable walletbot celery bot
sudo systemctl start walletbot celery bot
```

## 6. nginx reverse proxy & SSL setup (expose Django securely to Helius)
To protect transaction data over the open internet, you must use HTTPS. 

First, install Nginx and Certbot:
```bash
sudo apt update
sudo apt install nginx certbot python3-certbot-nginx -y
```

Create a new Nginx server configuration at `/etc/nginx/sites-available/walletbot`:
```nginx
server {
    listen 80;
    server_name your-domain.com;

    location /webhook/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /static/ {
        alias /home/deen/walletbot/staticfiles/;
    }
}
```

Enable the configuration and reload Nginx:
```bash
sudo ln -s /etc/nginx/sites-available/walletbot /etc/nginx/sites-enabled/
sudo rm /etc/nginx/sites-enabled/default  # remove default if active
sudo nginx -t && sudo systemctl reload nginx
```

Obtain a free Let's Encrypt SSL certificate and let Certbot configure HTTPS redirection automatically:
```bash
sudo certbot --nginx -d your-domain.com
```

Once Certbot completes:
1. Update `WEBHOOK_BASE_URL` in `.env` to `https://your-domain.com`.
2. Sync the updated secure URL to Helius.
3. Certbot will handle auto-renewal of the SSL certificates automatically.

## 7. Static files
```bash
.venv/bin/python manage.py collectstatic
```

## 8. Verify everything is running
```bash
sudo systemctl status walletbot celery bot
# Send yourself a test Telegram message:
.venv/bin/python -c "
import django, os
os.environ['DJANGO_SETTINGS_MODULE'] = 'walletbot.settings'
django.setup()
from tracker.telegram_bot import _send_message
from django.conf import settings
_send_message(settings.TELEGRAM_ALLOWED_USER_ID, '✅ WalletBot deployed!')
"
```

---

## Local Development

```bash
# Start postgres + redis
docker compose up -d

# Create .env from example
cp .env.example .env   # edit values

# Run migrations
.venv/bin/python manage.py migrate

# In terminal 1 — Django dev server
.venv/bin/python manage.py runserver

# In terminal 2 — Celery worker + beat scheduler
.venv/bin/celery -A walletbot worker --beat --loglevel=info

# In terminal 3 — Telegram bot
.venv/bin/python manage.py run_bot

# In terminal 4 — ngrok (to receive Helius webhook locally)
ngrok http 8000
# Copy the ngrok URL and set it as WEBHOOK_BASE_URL in .env
```

## Run Tests
```bash
.venv/bin/python manage.py test tracker.tests
```

---

## AI Features & Commands

The bot is augmented with local AI capabilities powered by OpenRouter:

*   **Conversational Chat:** Type naturally to query and command the bot. Examples:
    *   *"add wallet [address] call it shamo"*
    *   *"what wallets am I tracking?"*
    *   *"what is shamo's trading profile?"*
*   **Token Risk Assessment:** Live market check via DexScreener to rate the risk level (`HIGH`/`MEDIUM`/`LOW`) on incoming buys.
*   **Wallet Strategy Analysis:** Summarizes strategy trends based on the tracked wallet's history.
*   **AI Profiles (`/profile <nickname>`):** Generates a trading strategy/personality profile.
*   **Daily Digest:** Automatically compiles a morning text report summarizing the last 24 hours of buys/alerts (sent daily at 9:00 AM UTC).
*   **Behavior Anomaly Detection:** Fired automatically if a wallet does 3+ transactions in under 2 hours.
