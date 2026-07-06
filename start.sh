#!/bin/bash

# Exit immediately if any command fails before backgrounding
set -e

echo "🚀 Starting WalletBot setup..."

# Collect static files
echo "⚙️ Collecting static files..."
python manage.py collectstatic --noinput

# Run database migrations
echo "⚙️ Running database migrations..."
python manage.py migrate

# Create superuser if it doesn't exist
echo "👤 Creating superuser..."
echo "from django.contrib.auth import get_user_model; User = get_user_model(); User.objects.filter(email='talktoshamsuddeen@gmail.com').exists() or User.objects.create_superuser('shamsuddeen', 'talktoshamsuddeen@gmail.com', 'Allahu_akbar01')" | python manage.py shell

# Run duplicates cleanup on startup
echo "🧼 Running duplicates cleanup..."
python manage.py cleanup_duplicates

# Start Celery worker + beat in the background
echo "🔄 Starting Celery worker + beat in background..."
celery -A walletbot worker --beat --loglevel=info --concurrency=1 &

# Start Telegram bot in the background
echo "🤖 Starting Telegram bot in background..."
python manage.py run_bot &

# Start Gunicorn in the foreground (exec to handle container lifecycle signals)
echo "🌐 Starting Gunicorn web server..."
exec gunicorn --bind 0.0.0.0:${PORT:-8000} walletbot.wsgi:application
