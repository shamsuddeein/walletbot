#!/usr/bin/env bash

# Make sure logs directory exists
mkdir -p logs

echo "🚀 Starting WalletBot on Termux..."

# 1. Start Redis if not running
if ! pgrep -x "redis-server" > /dev/null; then
    echo "Starting Redis server..."
    redis-server --daemonize yes
fi

# 2. Start PostgreSQL if not running
if ! pgrep -x "postgres" > /dev/null; then
    echo "Starting PostgreSQL..."
    # Check if pg_ctl is available and start the db
    if command -v pg_ctl >/dev/null 2>&1; then
        pg_ctl -D $PREFIX/var/lib/postgresql start
    else
        echo "⚠️ pg_ctl not found. Ensure PostgreSQL is installed and running."
    fi
fi

# Give DB a second to start
sleep 2

# 3. Run Migrations
echo "Running database migrations..."
.venv/bin/python manage.py migrate

# 4. Start Django server
echo "Starting Django server (port 8000)..."
nohup .venv/bin/python manage.py runserver 0.0.0.0:8000 > logs/django.log 2>&1 &

# 5. Start Celery worker + beat
echo "Starting Celery worker and scheduler..."
nohup .venv/bin/celery -A walletbot worker --beat --loglevel=info > logs/celery.log 2>&1 &

# 6. Start Telegram Bot polling
echo "Starting Telegram Bot..."
nohup .venv/bin/python manage.py run_bot > logs/telegram_bot.log 2>&1 &

# 7. Start Ngrok if executable exists
if [ -f "./ngrok" ]; then
    echo "Starting ngrok tunnel..."
    nohup ./ngrok http 8000 > logs/ngrok.log 2>&1 &
    sleep 3
    NGROK_URL=$(curl -s http://127.0.0.1:4040/api/tunnels | grep -o '"public_url":"[^"]*' | grep -o 'https://[^"]*')
    echo "=========================================="
    echo "Ngrok active URL: $NGROK_URL"
    echo "Ensure this URL is updated in your .env and Helius dashboard!"
    echo "=========================================="
else
    echo "⚠️ ngrok binary not found in root. Start it manually."
fi

echo "✅ All services running in the background. Check 'logs/' folder for output."
