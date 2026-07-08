web:      gunicorn walletbot.wsgi:application --bind 0.0.0.0:$PORT --workers 1 --timeout 30
worker:   celery -A walletbot worker -Q live_alerts,default --concurrency=1 --loglevel=info --max-tasks-per-child=20
backfill: celery -A walletbot worker -Q backfills --concurrency=1 --loglevel=info --max-tasks-per-child=5
beat:     celery -A walletbot beat --loglevel=info
bot:      python manage.py run_bot
