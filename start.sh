#!/bin/bash

# Release-phase / setup script.
# Runs migrations, static files, webhook sync, and superuser creation.
# All services (gunicorn, celery, bot) are launched by the Procfile.

set -e

echo "🚀 Starting WalletBot setup..."

# Collect static files
echo "⚙️ Collecting static files..."
python manage.py collectstatic --noinput

# Run database migrations
echo "⚙️ Running database migrations..."
python manage.py migrate

# Sync Helius webhooks on startup to fix mismatched watch lists
echo "📡 Syncing Helius webhooks with database..."
python manage.py sync_helius_webhooks

# Create superuser if it doesn't exist
echo "👤 Creating superuser..."
echo "from django.contrib.auth import get_user_model; User = get_user_model(); User.objects.filter(email='talktoshamsuddeen@gmail.com').exists() or User.objects.create_superuser('shamsuddeen', 'talktoshamsuddeen@gmail.com', 'Allahu_akbar01')" | python manage.py shell

# Configure Redis to ignore snapshotting errors (fixes Celery connection issues on Railway)
echo "🔧 Configuring Redis settings..."
python -c '
import os, redis
url = os.environ.get("REDIS_URL")
if url:
    try:
        r = redis.from_url(url)
        r.config_set("stop-writes-on-bgsave-error", "no")
        print("✅ Configured Redis to ignore bgsave errors")
    except Exception as e:
        print(f"⚠️ Could not configure Redis: {e}")
'

echo "✅ Setup complete. Services are started by the Procfile."
