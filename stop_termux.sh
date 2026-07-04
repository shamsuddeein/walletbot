#!/usr/bin/env bash

echo "🛑 Stopping WalletBot services..."

# Kill Python processes running Django or bot
pkill -f "manage.py runserver"
pkill -f "manage.py run_bot"

# Kill Celery worker
pkill -f "celery -A walletbot"

# Kill Ngrok
pkill -f "ngrok http"

echo "✅ WalletBot services stopped."
echo "Note: Redis and Postgres servers are left running. Stop them manually if needed."
